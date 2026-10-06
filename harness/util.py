"""Small shared helpers: process running, HTTP, hashing, terminal colour."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def yellow(t: str) -> str:
    return _c("33", t)


def cyan(t: str) -> str:
    return _c("36", t)


# --------------------------------------------------------------------------- procs


@dataclass
class Proc:
    """A finished subprocess: what ran, how it exited, and everything it printed."""

    cmd: list[str]
    returncode: int
    output: str
    duration: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def tail(self, lines: int = 25) -> str:
        return "\n".join(self.output.strip().splitlines()[-lines:])

    def pretty(self) -> str:
        return f"$ {shlex_join(self.cmd)}\n{self.tail()}"


def shlex_join(cmd: list[str]) -> str:
    return " ".join(shlex.quote(str(c)) for c in cmd)


def run(
    cmd: list[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 1800,
    verbose: bool = False,
) -> Proc:
    """Run a command, capturing stdout+stderr together.

    Returns:
        The finished process. A failure -- including a missing executable or a timeout --
        comes back as a non-zero `returncode` with the reason in `output`, because the
        caller decides what a failure means here.
    """
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    # Keep child pip/python output deterministic and unbuffered.
    full_env.setdefault("PYTHONUNBUFFERED", "1")
    full_env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")

    cmd = [str(c) for c in cmd]
    if verbose:
        print(dim(f"  $ {shlex_join(cmd)}"))
    started = time.monotonic()
    try:
        proc = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]  -- running pip/twine is this tool's purpose
            cmd,
            check=False,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        out, rc = proc.stdout, proc.returncode
    except FileNotFoundError as exc:
        out, rc = f"command not found: {exc}", 127
    except subprocess.TimeoutExpired as exc:
        partial = exc.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        out, rc = f"{partial}\n<timed out after {timeout}s>", 124
    duration = time.monotonic() - started
    if verbose and out.strip():
        print(dim("  | " + out.strip().replace("\n", "\n  | ")))
    return Proc(cmd, rc, out, duration)


# ---------------------------------------------------------------------------- http


@dataclass
class Response:
    """An HTTP response, including error statuses -- those are data here, not failures."""

    url: str
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:  # ruff: ignore[any-type]  -- JSON is Any by nature
        """Parse the body as JSON.

        Nothing is caught: devpi answering with HTML where JSON was asked for is a real
        failure, and json.loads raising is how the caller finds out.

        Returns:
            Whatever the body decodes to, or None for an empty body.
        """
        return json.loads(self.body or b"null")


_DEFAULT_PORTS = {"http": 80, "https": 443}


def origin(url: str) -> tuple[str, str, int | None]:
    """Reduce a URL to its origin: scheme, host and port.

    Returns:
        The lower-cased scheme and host, and the port with the scheme's default filled
        in, so `https://h/` and `https://H:443/x` compare equal.
    """
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or _DEFAULT_PORTS.get(scheme)


class _SameOriginAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but carry credentials only to the origin they were meant for.

    urllib copies every ordinary header onto the redirected request, Authorization
    included -- to another host, or from https down to http. Credentials are therefore
    attached as an unredirected header, which urllib drops, and re-attached here only
    when the redirect stays on the same scheme, host and port.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,  # ruff: ignore[any-type]  -- urllib's own annotation
        code: int,
        msg: str,
        headers: Any,  # ruff: ignore[any-type]  -- urllib's own annotation
        newurl: str,
    ) -> urllib.request.Request | None:
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        auth = req.unredirected_hdrs.get("Authorization")
        if new is not None and auth and origin(newurl) == origin(req.full_url):
            new.add_unredirected_header("Authorization", auth)
        return new


def tls_context(*, verify_tls: bool | str) -> ssl.SSLContext:
    """Build the TLS context for a `verify_tls` setting.

    Returns:
        A context that verifies against the system store (True), against a CA bundle
        (a path), or not at all (False).
    """
    if verify_tls is False:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if isinstance(verify_tls, str):
        return ssl.create_default_context(cafile=verify_tls)
    return ssl.create_default_context()


def http(
    url: str,
    *,
    method: str = "GET",
    auth: tuple[str, str] | None = None,
    accept: str | None = None,
    timeout: int = 30,
    verify_tls: bool | str = True,  # a str is a CA bundle to verify against
    json_body: Any = None,  # ruff: ignore[any-type]  -- any JSON-serialisable value
) -> Response:
    """Fetch a URL.

    Credentials follow a redirect only to the same scheme, host and port.

    Returns:
        The response, including error statuses: a 404 or 401 is data a check reasons
        about, not an exception.

    Raises:
        ValueError: if the URL is not http(s), so a `file:` scheme in a config file
            cannot turn a repository check into a local file read.
        ConnectionError: if the server could not be reached at all, or timed out. The
            message says why in operator terms -- wrong host, wrong port, wrong scheme,
            untrusted certificate -- because that is what decides the fix.
    """
    # Only ever speak HTTP(S): a `file:` or custom scheme in a config file must not
    # turn a repository check into a local file read.
    if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError(f"refusing to fetch a non-HTTP(S) URL: {url}")
    data = None if json_body is None else json.dumps(json_body).encode()
    req = urllib.request.Request(url, data=data, method=method)  # ruff: ignore[suspicious-url-open-usage]  -- scheme checked above
    req.add_header("User-Agent", "pipcheck/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if accept:
        req.add_header("Accept", accept)
    if auth and auth[0]:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        # Unredirected: see _SameOriginAuthRedirect.
        req.add_unredirected_header("Authorization", f"Basic {token}")

    https = urllib.request.HTTPSHandler(context=tls_context(verify_tls=verify_tls))
    opener = urllib.request.build_opener(https, _SameOriginAuthRedirect())
    try:
        with opener.open(req, timeout=timeout) as resp:
            return Response(
                url, resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
            )
    except urllib.error.HTTPError as exc:
        return Response(
            url, exc.code, exc.read(), {k.lower(): v for k, v in (exc.headers or {}).items()}
        )
    except urllib.error.URLError as exc:
        raise ConnectionError(f"{url}: {explain_unreachable(url, exc.reason)}") from exc
    except (TimeoutError, OSError) as exc:  # raised outside URLError mid-response
        raise ConnectionError(f"{url}: {explain_unreachable(url, exc, timeout)}") from exc


def explain_unreachable(url: str, reason: object, timeout: int | None = None) -> str:
    """Turn a low-level connection failure into what the operator should check.

    Returns:
        The original reason, followed by a hint naming the likely misconfiguration.
    """
    netloc = urllib.parse.urlsplit(url).netloc
    text = str(reason)
    if isinstance(reason, ssl.SSLCertVerificationError):
        hint = ("the server's TLS certificate is not trusted: set ca_bundle to your internal "
                "CA's PEM file (or DEVPI_CA_BUNDLE), fix the certificate, or as a last "
                "resort set verify_tls = false / pass --insecure")
    elif (isinstance(reason, ssl.SSLError) and "WRONG_VERSION_NUMBER" in text) or (
        url.startswith("https://") and "handshake" in text
    ):
        hint = (f"{netloc} does not speak TLS -- the url probably wants http://, not https://, "
                "or the port is wrong")
    elif isinstance(reason, ConnectionRefusedError):
        hint = f"nothing is listening on {netloc} -- check the host and port in url"
    elif isinstance(reason, socket.gaierror):
        hint = f"the host in {netloc} does not resolve -- check the url"
    elif isinstance(reason, TimeoutError | socket.timeout):
        after = f" after {timeout}s" if timeout else ""
        hint = (f"timed out{after} -- a firewall dropping the port, or the wrong host, "
                "is the usual cause")
    elif isinstance(reason, ConnectionResetError | ssl.SSLError):
        hint = (f"{netloc} closed the connection -- check that the scheme (http/https) and "
                "port match what the server listens on")
    else:
        return text
    return f"{text}\n  -> {hint}"


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc


# --------------------------------------------------------------------------- misc


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """Remove terminal escape sequences from captured output (twine is colourful).

    Returns:
        The text with ANSI sequences stripped, safe to match on and to put in a report.
    """
    return _ANSI.sub("", text)


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_SECONDS_PER_MINUTE = 60


def human(seconds: float) -> str:
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(seconds, _SECONDS_PER_MINUTE)
    return f"{minutes:.0f}m{rest:02.0f}s"
