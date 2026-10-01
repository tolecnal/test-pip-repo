"""Running the suite and reporting results."""

from __future__ import annotations

import datetime as _dt
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__, checks, state
from .checks import Context, Fail, Skip
from .config import REPORT_DIR, Config
from .util import bold, cyan, dim, green, human, red, yellow

if TYPE_CHECKING:
    from collections.abc import Callable

PASS, FAIL, SKIP, ERROR = "pass", "fail", "skip", "error"
_MARK = {PASS: (green, "ok  "), FAIL: (red, "FAIL"), SKIP: (yellow, "skip"), ERROR: (red, "ERR ")}


@dataclass
class Result:
    """The outcome of running one check."""

    name: str
    phase: str
    status: str
    detail: str
    duration: float
    description: str = ""


@dataclass
class Report:
    """One whole run: what it tested, against which server, and how it went."""

    started_at: str
    index: str
    version: str | None
    build_id: str | None
    pipcheck_version: str = __version__
    server: dict[str, Any] = field(default_factory=dict)  # what we were talking to
    results: list[Result] = field(default_factory=list)
    duration: float = 0.0

    @property
    def counts(self) -> dict[str, int]:
        tally = {PASS: 0, FAIL: 0, SKIP: 0, ERROR: 0}
        for result in self.results:
            tally[result.status] += 1
        return tally

    @property
    def ok(self) -> bool:
        tally = self.counts
        return tally[FAIL] == 0 and tally[ERROR] == 0

    def to_json(self) -> str:
        payload = asdict(self)
        payload["counts"] = self.counts
        payload["ok"] = self.ok
        return json.dumps(payload, indent=2) + "\n"


def select(
    *,
    only: list[str] | None = None,
    skip: list[str] | None = None,
    phases: list[str] | None = None,
) -> list[checks.CheckDef]:
    """Pick checks by name and/or phase, preserving phase then registration order."""
    known = {c.name for c in checks.REGISTRY}
    for name in (only or []) + (skip or []):
        if name not in known:
            raise SystemExit(f"unknown check {name!r}\navailable: {', '.join(sorted(known))}")
    for phase in phases or []:
        if phase not in checks.PHASES:
            raise SystemExit(f"unknown phase {phase!r}\navailable: {', '.join(checks.PHASES)}")

    chosen = [
        c
        for c in checks.REGISTRY
        if (not only or c.name in only)
        and (not skip or c.name not in skip)
        and (not phases or c.phase in phases)
    ]
    return sorted(chosen, key=lambda c: checks.PHASES.index(c.phase))


def run(
    cfg: Config,
    st: state.State,
    selected: list[checks.CheckDef],
    *,
    record: state.Build | None = None,
    json_out: str | None = None,
) -> Report:
    """Execute the selected checks against the repository and print as we go."""
    # Without an explicit build, verify the most recent one published to *this* index,
    # so alternating between a dev and a release index does the obvious thing.
    if record is None:
        for_index = st.uploaded_to(cfg.index)
        record = for_index[-1] if for_index else st.last
    ctx = Context(cfg=cfg, st=st, record=record)
    report = Report(
        started_at=_dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        index=cfg.index_url,
        version=ctx.record.version if ctx.record else None,
        build_id=ctx.record.build_id if ctx.record else None,
    )

    print()
    print(bold(f"pipcheck -> {cfg.summary()}"))
    if ctx.record:
        up = "uploaded" if ctx.record.uploaded else bold("NOT uploaded")
        print(dim(f"  package under test: {cfg.package} {ctx.record.version} "
                  f"(build {ctx.record.build_id}, {up})"))
    print()

    started = time.monotonic()
    phase = None
    blocked = None  # set once a fatal check fails; everything after is skipped
    try:
        for definition in selected:
            if definition.phase != phase:
                phase = definition.phase
                print(cyan(f"[{phase}]"))
            result = _run_one(ctx, definition, blocked=blocked)
            if result.status == FAIL and definition.fatal:
                blocked = f"{definition.name} failed"
            report.results.append(result)
    except KeyboardInterrupt:
        print(red("\ninterrupted"))
    finally:
        ctx.cleanup()
    report.duration = time.monotonic() - started
    # Record what we tested against, so before/after upgrade reports are comparable.
    status = ctx.share.get("status") or {}
    report.server = {
        "url": cfg.base,
        "versions": status.get("versioninfo") or {},
        "role": status.get("role"),
        "serial": status.get("serial"),
    }

    _summarise(report, cfg)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    base = f"{stamp}-{cfg.index.replace('/', '-')}"
    saved = REPORT_DIR / f"{base}.json"
    # Two runs can finish inside the same second; never overwrite an existing baseline.
    serial = 2
    while saved.exists():
        saved = REPORT_DIR / f"{base}-{serial}.json"
        serial += 1
    saved.write_text(report.to_json())
    print(dim(f"  saved {saved.relative_to(saved.parent.parent)}"
              f"   (compare runs with: ./pipcheck compare)"))
    if json_out:
        path = Path(json_out)
        if not path.is_absolute() and path.parent == Path():
            path = REPORT_DIR / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report.to_json())
        print(dim(f"  saved {path}"))
    return report


# ------------------------------------------------------------------ comparing runs


def saved_reports() -> list[Path]:
    """Every saved report, oldest first."""
    if not REPORT_DIR.is_dir():
        return []
    return sorted(REPORT_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)


def _load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"could not read report {path}: {exc}") from exc


def _clip(text: str, width: int = 96) -> str:
    line = (text or "").splitlines()[0] if text else ""
    return line if len(line) <= width else line[: width - 1] + "\u2026"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _describe(label: str, report: dict[str, Any], path: Path) -> str:
    versions = (report.get("server") or {}).get("versions") or {}
    stack = ", ".join(f"{k} {v}" for k, v in sorted(versions.items())) or "versions unknown"
    counts = report.get("counts") or {}
    tally = ", ".join(f"{n} {name}" for name, n in counts.items() if n)
    return (f"  {label:<7} {report.get('started_at', '?')}  {report.get('index', '?')}\n"
            f"          {stack}\n"
            f"          {tally or 'no results'}   {dim(path.name)}")


@dataclass
class Delta:
    """How each check moved between two runs."""

    regressed: list[tuple[str, dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    fixed: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    still_failing: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    stopped_running: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    started_running: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    shared: int = 0

    @property
    def changed(self) -> int:
        return (len(self.regressed) + len(self.fixed) + len(self.still_failing)
                + len(self.stopped_running))


def _delta(old: dict[str, Any], new: dict[str, Any]) -> Delta:
    """Sort every check in the later run into what happened to it."""
    bad = (FAIL, ERROR)
    delta = Delta(
        missing=[name for name in old if name not in new],
        shared=len(set(old) & set(new)),
    )
    for name, result in new.items():
        was = old.get(name)
        if was is None:
            delta.started_running.append((name, result))
        elif result["status"] in bad and was["status"] not in bad:
            delta.regressed.append((name, was, result))
        elif result["status"] == PASS and was["status"] in bad:
            delta.fixed.append((name, result))
        elif result["status"] in bad and was["status"] in bad:
            delta.still_failing.append((name, result))
        elif result["status"] == SKIP and was["status"] == PASS:
            delta.stopped_running.append((name, result))
    return delta


def compare(before_path: Path, after_path: Path) -> int:
    """Diff two runs. Returns a process exit code: 1 if anything regressed."""
    before, after = _load(before_path), _load(after_path)
    delta = _delta(
        {r["name"]: r for r in before.get("results", [])},
        {r["name"]: r for r in after.get("results", [])},
    )
    regressed = delta.regressed
    fixed = delta.fixed
    still_failing = delta.still_failing
    stopped_running = delta.stopped_running
    started_running = delta.started_running
    missing = delta.missing

    print()
    print(bold("comparing runs"))
    print(_describe("before", before, before_path))
    print(_describe("after", after, after_path))
    print()

    def block(title: str, colour: Callable[[str], str], rows: list[tuple[str, str]]) -> None:
        if not rows:
            return
        print(colour(f"{title} ({len(rows)})"))
        for name, detail in rows:
            print(f"  {name:<24} {detail}")
        print()

    block("regressions", red,
          [(n, f"{w['status']} -> {r['status']}  {_clip(r['detail'])}") for n, w, r in regressed])
    block("fixed", green, [(n, f"now passing: {_clip(r['detail'])}") for n, r in fixed])
    block("still failing", red, [(n, _clip(r["detail"])) for n, r in still_failing])
    block("no longer ran", yellow,
          [(n, f"passed before, now skipped: {_clip(r['detail'], 70)}")
           for n, r in stopped_running])
    block("newly ran", cyan,
          [(n, f"{r['status']}: {_clip(r['detail'], 70)}") for n, r in started_running])
    if missing:
        print(dim(f"not in the later run: {', '.join(sorted(missing))}"))
        print()

    print(f"  {delta.shared - delta.changed} of {_plural(delta.shared, 'shared check')} unchanged")
    if regressed:
        print("  " + red(f"verdict: {_plural(len(regressed), 'regression')} after the change"))
        print()
        return 1
    if still_failing:
        print("  " + yellow(
            f"verdict: no new regressions, but {_plural(len(still_failing), 'check')} "
            "failed before and still fail"
        ))
        print()
        return 0
    print(f"  {green('verdict: nothing regressed')}")
    print()
    return 0


def _run_one(ctx: Context, definition: checks.CheckDef, *, blocked: str | None = None) -> Result:
    label = f"  {definition.name:<24}"
    print(label, end="", flush=True)
    started = time.monotonic()

    status, detail = PASS, ""
    reason = blocked or _prerequisite(ctx, definition)
    if reason:
        status, detail = SKIP, reason
    else:
        try:
            detail = definition.func(ctx) or ""
        except Skip as exc:
            status, detail = SKIP, str(exc)
        except Fail as exc:
            status, detail = FAIL, str(exc)
        except ConnectionError as exc:
            status, detail = FAIL, f"could not reach the server: {exc}"
        except Exception as exc:  # noqa: BLE001 -- a harness bug must not abort the suite
            status = ERROR
            detail = f"{type(exc).__name__}: {exc}"
    duration = time.monotonic() - started

    colour, mark = _MARK[status]
    first, *rest = (detail or "-").splitlines()
    print(f"{colour(mark)} {first} {dim(f'({duration:.1f}s)')}")
    for line in rest:
        print(dim(f"        {line}"))
    return Result(definition.name, definition.phase, status, detail, duration,
                  definition.description)


def _prerequisite(ctx: Context, definition: checks.CheckDef) -> str | None:
    """Why this check cannot run, if it cannot."""
    if definition.needs_auth and not ctx.cfg.user:
        return "no credentials configured"
    if definition.needs_build or definition.needs_upload:
        record = ctx.record
        if record is None:
            return "nothing built yet (run: pipcheck cycle)"
        if definition.needs_upload:
            if not record.uploaded:
                return f"{record.version} has not been uploaded"
            if record.index != ctx.cfg.index:
                return f"{record.version} was uploaded to {record.index}, not {ctx.cfg.index}"
    return None


def _summarise(report: Report, cfg: Config) -> None:
    tally = report.counts
    parts = [
        green(f"{tally[PASS]} passed"),
        red(f"{tally[FAIL]} failed") if tally[FAIL] else f"{tally[FAIL]} failed",
        yellow(f"{tally[SKIP]} skipped") if tally[SKIP] else f"{tally[SKIP]} skipped",
    ]
    if tally[ERROR]:
        parts.append(red(f"{tally[ERROR]} errored"))
    print()
    print(f"  {', '.join(parts)} in {human(report.duration)}")

    failures = [r for r in report.results if r.status in (FAIL, ERROR)]
    if not failures and not tally[PASS]:
        print(f"  {yellow('nothing ran')} -- every selected check was skipped")
        print()
        return
    if failures:
        print()
        print(bold("  failures:"))
        for result in failures:
            print(f"    {red(result.name)}: {result.description}")
            for line in result.detail.splitlines():
                print(f"      {dim(line)}")
    else:
        print(f"  {green('repository behaves as expected')} -- {cfg.index_url}")
    print()
