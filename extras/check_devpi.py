#!/usr/bin/env python3
"""Nagios / Icinga 2 check for a devpi-server, from its JSON /+status endpoint.

devpi-web shows a health banner (ok / degraded / fatal) built from the same status
data, but only on its HTML pages. This plugin applies the same tests to the JSON, with
devpi-web's thresholds as the defaults, so an alert means what the banner would say:

- replica: no contact with the primary, falling behind it, replication errors
- event processing: the hooks that feed devpi-web's search index (and other plugins)
  have stalled or fallen behind the committed changes
- search indexer (devpi-web): queue backing up, indexing errors

It also measures response time, can confirm an index exists (--index), and emits the
serials, lags and devpi's own metrics as perfdata for graphing.

Standalone on purpose: standard library only, Python 3.8+, no import from the harness,
so it can be copied onto a monitoring host as a single file.

Exit codes follow the plugin API: 0 OK, 1 WARNING, 2 CRITICAL, 3 UNKNOWN.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import email.utils
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, NoReturn, Optional, Tuple

__version__ = "1.0.0"

OK, WARNING, CRITICAL, UNKNOWN = 0, 1, 2, 3
_LABELS = {OK: "OK", WARNING: "WARNING", CRITICAL: "CRITICAL", UNKNOWN: "UNKNOWN"}
_HTTP_OK = 200
_HTTP_AUTH = (401, 403)
_HTTP_NOT_FOUND = 404
_TWO_MINUTES, _TWO_HOURS = 120, 7200  # below these, seconds and minutes read better
_METRIC_FIELDS = 3  # devpi reports each metric as [name, type, value]


class Problem(NamedTuple):
    """One finding: how bad it is and what it says."""

    state: int
    message: str


class Result:
    """Everything a run found, rendered as one plugin output."""

    def __init__(self) -> None:
        self.problems: List[Problem] = []
        self.details: List[str] = []
        self.perfdata: List[str] = []

    def add(self, state: int, message: str) -> None:
        if state != OK:
            self.problems.append(Problem(state, message))

    @property
    def state(self) -> int:
        return max((p.state for p in self.problems), default=OK)

    def perf(
        self,
        label: str,
        value: float,
        uom: str = "",
        warn: Optional[float] = None,
        crit: Optional[float] = None,
        minimum: Optional[float] = 0,
    ) -> None:
        def fmt(number: Optional[float]) -> str:
            if number is None:
                return ""
            return str(int(number)) if float(number).is_integer() else f"{number:.6g}"

        fields = [f"{fmt(value)}{uom}", fmt(warn), fmt(crit), fmt(minimum)]
        self.perfdata.append(f"'{label}'=" + ";".join(fields).rstrip(";"))


def _exit(state: int, summary: str, result: Optional[Result] = None) -> NoReturn:
    """Print the plugin output and exit with the plugin's state."""
    line = f"DEVPI {_LABELS[state]} - {summary}"
    if result and result.perfdata:
        line += " | " + " ".join(result.perfdata)
    print(line)
    if result:
        for problem in result.problems:
            print(f"[{_LABELS[problem.state]}] {problem.message}")
        for detail in result.details:
            print(detail)
    sys.exit(state)


# ------------------------------------------------------------------------------ http


def _tls_context(args: argparse.Namespace) -> ssl.SSLContext:
    if args.insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context(cafile=args.ca_file)


def fetch(args: argparse.Namespace, url: str) -> Tuple[int, Dict[str, Any], float, float]:
    """GET a devpi JSON endpoint.

    Returns:
        The HTTP status, the decoded JSON body (empty when the body is not JSON), the
        seconds the request took, and the server's clock from its Date header -- so
        timestamps in the status are aged against the server's own time, not this
        host's, and clock skew between the two cannot raise a false alarm.

    Raises:
        ConnectionError: if the server cannot be reached at all.
    """
    request = urllib.request.Request(url, headers={  # ruff: ignore[suspicious-url-open-usage]  -- scheme checked in parse_args
        "Accept": "application/json",
        "User-Agent": f"check_devpi/{__version__}",
    })
    # Unredirected: if a proxy redirects (http -> https, say), the credentials are not
    # carried along to wherever it points.
    if args.user:
        token = base64.b64encode(f"{args.user}:{args.password}".encode()).decode()
        request.add_unredirected_header("Authorization", f"Basic {token}")
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_tls_context(args)))
    started = time.monotonic()
    try:
        with opener.open(request, timeout=args.timeout) as response:
            status, body, headers = response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        status, body, headers = exc.code, exc.read(), exc.headers
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ConnectionError(f"{url}: {reason}") from exc
    elapsed = time.monotonic() - started

    server_now = time.time()
    date = headers.get("Date") if headers else None
    if date:
        with contextlib.suppress(TypeError, ValueError):
            server_now = email.utils.parsedate_to_datetime(date).timestamp()
    try:
        decoded = json.loads(body or b"null")
    except ValueError:
        decoded = None
    return status, decoded if isinstance(decoded, dict) else {}, elapsed, server_now


# ---------------------------------------------------------------------------- checks


def _age(now: float, timestamp: Optional[float]) -> Optional[float]:
    return None if timestamp is None else max(0.0, now - timestamp)


def _minutes(seconds: float) -> str:
    if seconds < _TWO_MINUTES:
        return f"{seconds:.0f}s"
    if seconds < _TWO_HOURS:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _check_primary_contact(args: argparse.Namespace, status: Dict[str, Any], now: float,
                           result: Result) -> int:
    """Check that a replica is hearing from its primary.

    Returns:
        How bad being behind the primary is: only a WARNING while updates are arriving
        on time (it is catching up), otherwise CRITICAL -- devpi-web's rule.
    """
    started = status.get("replica-started-at")
    last_update = status.get("update-from-primary-at", status.get("update-from-master-at"))
    if started is None:
        return CRITICAL
    if last_update is None:
        since = _age(now, started) or 0.0
        what = f"no contact with the primary since start, {_minutes(since)} ago"
    else:
        since = _age(now, last_update) or 0.0
        what = f"no update from the primary for {_minutes(since)}"
    if since > args.primary_crit:
        result.add(CRITICAL, what)
    elif since > args.primary_warn:
        result.add(WARNING, what)
    elif last_update is not None:
        return WARNING
    return CRITICAL


def check_replica(args: argparse.Namespace, status: Dict[str, Any], now: float,
                  result: Result) -> bool:
    """Apply devpi-web's replica tests.

    Returns:
        Whether event processing should still be checked: devpi-web skips it while a
        replica is catching up within tolerance, since a lag is expected then.
    """
    behind_state = _check_primary_contact(args, status, now, result)
    serial = int(status.get("serial") or 0)
    primary_serial = status.get("primary-serial", status.get("master-serial"))
    if primary_serial is not None:
        result.perf("replica_lag", max(0, int(primary_serial) - serial))
    in_sync_age = _age(now, status.get("replica-in-sync-at"))
    if primary_serial is not None and int(primary_serial) > serial:
        lag = int(primary_serial) - serial
        if in_sync_age is None or in_sync_age > args.replica_crit:
            when = "never in sync" if in_sync_age is None else _minutes(in_sync_age)
            result.add(behind_state, f"replica is {lag} serial(s) behind the primary ({when})")
        elif in_sync_age > args.replica_warn:
            result.add(WARNING, f"replica is {lag} serial(s) behind the primary for "
                                f"{_minutes(in_sync_age)}")
        else:
            return False
    elif status.get("replication-errors"):
        errors = status["replication-errors"]
        result.add(CRITICAL, f"{len(errors)} unhandled replication error(s): "
                             f"{', '.join(list(errors)[:3])}")
    return True


def check_events(args: argparse.Namespace, status: Dict[str, Any], now: float,
                 result: Result) -> None:
    """Apply devpi-web's event-processing tests.

    Every commit is replayed through plugin hooks (devpi-web's search indexing among
    them); event-serial is how far that has got. A gap that does not close means
    uploads stop showing up in search, and plugins stop seeing changes.
    """
    serial = int(status.get("serial") or 0)
    event_serial = int(status.get("event-serial") or 0)
    result.perf("event_lag", max(0, serial - event_serial))
    if serial <= event_serial:
        return
    sync_age = _age(now, status.get("event-serial-in-sync-at"))
    processed_age = _age(now, status.get("event-serial-timestamp"))
    commit_age = _age(now, status.get("last-commit-timestamp")) or 0.0
    lag = serial - event_serial

    if sync_age is None and processed_age is None:
        if commit_age > args.processing_warn:
            result.add(CRITICAL, f"event processing has not started ({lag} serial(s) pending)")
        return
    if sync_age is None or sync_age > args.sync_crit:
        when = "never" if sync_age is None else f"for {_minutes(sync_age)}"
        result.add(CRITICAL, f"event processing out of sync {when} ({lag} serial(s) behind)")
    elif sync_age > args.sync_warn:
        result.add(WARNING, f"event processing out of sync for {_minutes(sync_age)} "
                            f"({lag} serial(s) behind)")
    if sync_age is not None:
        if processed_age is None or processed_age > args.processing_crit:
            when = "ever" if processed_age is None else f"for {_minutes(processed_age)}"
            result.add(CRITICAL, f"no changes processed by plugins {when}")
        elif processed_age > args.processing_warn:
            result.add(WARNING, f"no changes processed by plugins for {_minutes(processed_age)}")


def check_metrics(args: argparse.Namespace, status: Dict[str, Any], result: Result) -> None:
    """Judge devpi-web's indexer queues and pass every metric on as perfdata."""
    metrics = {}
    for entry in status.get("metrics") or []:
        if isinstance(entry, (list, tuple)) and len(entry) == _METRIC_FIELDS:
            name, kind, value = entry
            if isinstance(value, (int, float)):
                metrics[str(name)] = (str(kind), value)

    queue = metrics.get("devpi_web_whoosh_index_queue_size")
    if queue and queue[1] > args.queue_warn:
        result.add(WARNING, f"{queue[1]:.0f} items waiting in the search index queue")
    errors = metrics.get("devpi_web_whoosh_index_error_queue_size")
    if errors and errors[1] > 0:
        result.add(WARNING, f"{errors[1]:.0f} search indexing error(s)")

    if args.no_metrics:
        return
    for name, (kind, value) in sorted(metrics.items()):
        warn = args.queue_warn if name == "devpi_web_whoosh_index_queue_size" else None
        warn = 0 if name == "devpi_web_whoosh_index_error_queue_size" else warn
        result.perf(name, value, "c" if kind == "counter" else "", warn=warn)


def check_index(args: argparse.Namespace, result: Result) -> None:
    url = f"{args.url}/{args.index.strip('/')}"
    try:
        code, body, _, _ = fetch(args, url)
    except ConnectionError as exc:
        result.add(CRITICAL, f"index {args.index}: {exc}")
        return
    if code == _HTTP_NOT_FOUND:
        result.add(CRITICAL, f"index {args.index} does not exist")
    elif code in _HTTP_AUTH:
        result.add(CRITICAL, f"index {args.index}: HTTP {code} (credentials?)")
    elif code != _HTTP_OK or not isinstance(body.get("result"), dict):
        result.add(CRITICAL, f"index {args.index}: HTTP {code}, not devpi's JSON")
    else:
        config = body["result"]
        result.details.append(f"index {args.index}: type={config.get('type')}, "
                              f"bases={','.join(config.get('bases') or []) or '-'}")


# ------------------------------------------------------------------------------ main


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="check_devpi",
        description="Nagios/Icinga 2 check for devpi-server, from its JSON /+status. "
                    "Defaults are the thresholds devpi-web uses for its own health banner.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-u", "--url", required=True,
                        help="devpi-server root URL, e.g. https://devpi.internal:3141")
    parser.add_argument("-i", "--index", help="also check that this index exists, e.g. root/pypi")
    parser.add_argument("-t", "--timeout", type=float, default=10.0,
                        help="seconds per HTTP request (default: 10)")
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument("--ca-file", help="PEM bundle of CAs to trust, for an internal CA")
    tls.add_argument("-k", "--insecure", action="store_true",
                     help="do not verify the TLS certificate")
    parser.add_argument("--user", help="user for HTTP basic auth, if the server requires it")
    parser.add_argument("--password-file", type=Path,
                        help="file holding the password (or set DEVPI_PASSWORD); "
                             "never on the command line, where `ps` shows it")
    parser.add_argument("--no-metrics", action="store_true",
                        help="leave devpi's own metrics out of the perfdata")

    thresholds = parser.add_argument_group("thresholds (seconds, unless stated)")
    thresholds.add_argument("-w", "--time-warn", type=float, default=2.0,
                            help="response time warning (default: 2)")
    thresholds.add_argument("-c", "--time-crit", type=float, default=10.0,
                            help="response time critical (default: 10)")
    thresholds.add_argument("--primary-warn", type=float, default=60,
                            help="replica: no update from the primary (default: 60)")
    thresholds.add_argument("--primary-crit", type=float, default=300,
                            help="replica: no update from the primary (default: 300)")
    thresholds.add_argument("--replica-warn", type=float, default=300,
                            help="replica: behind the primary for (default: 300)")
    thresholds.add_argument("--replica-crit", type=float, default=3600,
                            help="replica: behind the primary for (default: 3600)")
    thresholds.add_argument("--sync-warn", type=float, default=3600,
                            help="event processing out of sync for (default: 3600)")
    thresholds.add_argument("--sync-crit", type=float, default=21600,
                            help="event processing out of sync for (default: 21600)")
    thresholds.add_argument("--processing-warn", type=float, default=300,
                            help="no change processed by plugins for (default: 300)")
    thresholds.add_argument("--processing-crit", type=float, default=1800,
                            help="no change processed by plugins for (default: 1800)")
    thresholds.add_argument("--queue-warn", type=float, default=10,
                            help="search index queue length, in items (default: 10)")

    args = parser.parse_args(argv)
    args.url = args.url.rstrip("/")
    if urllib.parse.urlsplit(args.url).scheme not in {"http", "https"}:
        parser.error("--url must start with http:// or https://")
    args.password = ""
    if args.password_file:
        try:
            args.password = args.password_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            parser.error(f"cannot read --password-file: {exc}")
    elif args.user:
        args.password = os.environ.get("DEVPI_PASSWORD", "")
    return args


def read_status(args: argparse.Namespace) -> Tuple[Dict[str, Any], float, float]:
    """Fetch /+status, exiting with the right state if that alone fails.

    Returns:
        The status, how long the request took, and the server's clock.
    """
    try:
        code, body, elapsed, now = fetch(args, f"{args.url}/+status")
    except ConnectionError as exc:
        _exit(CRITICAL, f"cannot reach devpi-server: {exc}")
    if code in _HTTP_AUTH:
        _exit(UNKNOWN, f"/+status answered HTTP {code}: pass --user and a password")
    if code == _HTTP_NOT_FOUND:
        _exit(CRITICAL, f"{args.url}/+status answered HTTP 404 -- is --url the devpi-server "
                        "root, without an index path?")
    if code != _HTTP_OK:
        _exit(CRITICAL, f"/+status answered HTTP {code}")
    status = body.get("result")
    if not isinstance(status, dict) or "serial" not in status:
        _exit(UNKNOWN, f"{args.url}/+status did not return devpi's status JSON -- is --url "
                       "the devpi-server root?")
    return status, elapsed, now


def main(argv: Optional[List[str]] = None) -> NoReturn:
    try:
        args = parse_args(argv)
    except SystemExit as exc:  # argparse: --help/--version exit 0, bad usage is UNKNOWN
        sys.exit(OK if exc.code in {0, None} else UNKNOWN)

    status, elapsed, now = read_status(args)
    result = Result()
    result.perf("time", round(elapsed, 4), "s", args.time_warn, args.time_crit)
    result.perf("serial", int(status.get("serial") or 0), "c")
    if elapsed > args.time_crit:
        result.add(CRITICAL, f"/+status took {elapsed:.3g}s")
    elif elapsed > args.time_warn:
        result.add(WARNING, f"/+status took {elapsed:.3g}s")

    role = str(status.get("role") or "?").upper()
    events = check_replica(args, status, now, result) if role == "REPLICA" else True
    if events:
        check_events(args, status, now, result)
    check_metrics(args, status, result)
    if args.index:
        check_index(args, result)

    versions = status.get("versioninfo") or {}
    stack = ", ".join(f"{name} {versions[name]}" for name in sorted(versions)) or "devpi"
    summary = f"{stack}, {role.lower()}, serial {status.get('serial')}"
    if result.problems:
        summary = "; ".join(p.message for p in result.problems) + f" ({summary})"
    replicas = status.get("polling_replicas") or {}
    if replicas:
        result.details.append(f"{len(replicas)} replica(s) polling this server")
    _exit(result.state, summary, result)


if __name__ == "__main__":
    main()
