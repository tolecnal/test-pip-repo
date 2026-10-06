"""pipcheck command line: control the version, publish, and test the repository."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

from . import (
    __version__,
    builder,
    checks,
    client,
    config,
    envs,
    preflight,
    purge,
    runner,
    state,
    util,
    versioning,
)
from .config import DIST_DIR, REPORT_DIR, STATE_FILE, VENV_DIR
from .util import bold, dim, green, red, yellow

if TYPE_CHECKING:
    from collections.abc import Callable

EXIT_OK, EXIT_FAILED, EXIT_SETUP = 0, 1, 2

# `compare` needs a run on each side of the change.
_RUNS_TO_COMPARE = 2


# ------------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipcheck",
        description="End-to-end test harness for an internal devpi pip repository.",
        epilog="The usual call is `pipcheck cycle`: bump the version, build, upload, "
               "then verify the repository served it back correctly.",
    )
    parser.add_argument("--version", action="version", version=f"pipcheck {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="path to a config file (default: pipcheck.toml)")
    common.add_argument("--url", help="devpi base URL, e.g. http://devpi.internal:3141")
    common.add_argument("--index", help="target index as <user>/<index>, e.g. testing/dev")
    common.add_argument("--user", help="devpi username for uploads")
    common.add_argument("-v", "--verbose", action="store_true", help="echo every command it runs")
    common.add_argument("--keep-venvs", action="store_true",
                        help="leave test venvs on disk for inspection")
    common.add_argument("--insecure", action="store_true",
                        help="do not verify TLS certificates; prefer ca_bundle for a private CA")

    selection = argparse.ArgumentParser(add_help=False)
    selection.add_argument("--only", metavar="NAMES",
                           help="comma-separated check names to run exclusively")
    selection.add_argument("--skip", metavar="NAMES", help="comma-separated check names to skip")
    selection.add_argument("--phase", metavar="PHASES",
                           help=f"comma-separated phases ({', '.join(checks.PHASES)})")
    selection.add_argument("--json", metavar="FILE", dest="json_out",
                           help="also write a JSON report (bare names land in reports/)")

    subs = parser.add_subparsers(dest="command", required=True)

    cycle = subs.add_parser("cycle", parents=[common, selection],
                            help="bump, build, upload, then run the full suite (the usual call)")
    group = cycle.add_mutually_exclusive_group()
    group.add_argument("--bump", choices=versioning.PART_NAMES, default="patch",
                       help="which part of the version to increment (default: patch)")
    group.add_argument("--set-version", metavar="X.Y.Z", help="use this exact version instead")
    group.add_argument("--no-bump", action="store_true",
                       help="rebuild and re-upload the current version as-is")
    cycle.add_argument("--no-upload", action="store_true",
                       help="build but do not publish; checks that need an upload are skipped")

    subs.add_parser("verify", parents=[common, selection],
                    help="run checks against the last build without rebuilding")
    subs.add_parser("doctor", parents=[common],
                    help="server-side checks only: reachability, index config, web UI")

    subs.add_parser("show", parents=[common], help="show current version, config and build history")
    bump = subs.add_parser("bump", parents=[common], help="increment the version and stop")
    bump.add_argument("part", choices=versioning.PART_NAMES, nargs="?", default="patch")
    setver = subs.add_parser("set-version", parents=[common], help="set an exact version and stop")
    setver.add_argument("value", metavar="X.Y.Z")

    subs.add_parser("build", parents=[common], help="stamp and build sdist + wheel")
    subs.add_parser("upload", parents=[common], help="upload the last build with twine")

    boot = subs.add_parser("bootstrap", parents=[common],
                           help="create the tooling venv (build, twine, devpi-client)")
    boot.add_argument("--via-index", action="store_true",
                      help="install tooling through the internal index instead of PyPI")
    boot.add_argument("--rebuild", action="store_true", help="recreate it from scratch")

    remove = subs.add_parser("remove", parents=[common],
                             help="delete a release from the index")
    remove.add_argument("spec", nargs="?", help="name==version (default: the last build)")

    purge_ = subs.add_parser(
        "purge", parents=[common],
        help="delete every release pipcheck published to the index, then reset the version",
        description="Delete every release of the test package (and any leftover "
                    "pipcheck-authprobe-* project) from the configured index, after you "
                    "confirm by typing Yes. Nothing else on the index is touched. Then "
                    "reset the version in pkg/pyproject.toml.",
    )
    purge_.add_argument(
        "--reset-version", metavar="X.Y.Z", default=versioning.INITIAL_VERSION,
        help=f"version to reset to afterwards (default: {versioning.INITIAL_VERSION})",
    )

    cmp_ = subs.add_parser(
        "compare", parents=[common],
        help="diff two saved runs (default: the two most recent) -- the upgrade check",
    )
    cmp_.add_argument("before", nargs="?", help="earlier report (default: second-newest)")
    cmp_.add_argument("after", nargs="?", help="later report (default: newest)")
    cmp_.add_argument("--list", action="store_true", dest="list_reports",
                      help="just list the saved reports")

    subs.add_parser("list", help="list the available checks")
    clean = subs.add_parser("clean", parents=[common], help="remove venvs, builds and state")
    clean.add_argument("--all", action="store_true",
                       help="also remove the tooling venv and reports")
    return parser


def load_config(args: argparse.Namespace) -> config.Config:
    overrides = {
        "url": getattr(args, "url", None),
        "index": getattr(args, "index", None),
        "user": getattr(args, "user", None),
        "verbose": True if getattr(args, "verbose", False) else None,
        "keep_venvs": True if getattr(args, "keep_venvs", False) else None,
        "verify_tls": False if getattr(args, "insecure", False) else None,
    }
    return config.load(getattr(args, "config", None), overrides)


def selection(args: argparse.Namespace) -> dict[str, list[str] | None]:
    def split(value: str) -> list[str]:
        return [part.strip() for part in value.split(",") if part.strip()]

    return {
        "only": split(args.only) if getattr(args, "only", None) else None,
        "skip": split(args.skip) if getattr(args, "skip", None) else None,
        "phases": split(args.phase) if getattr(args, "phase", None) else None,
    }


# ---------------------------------------------------------------------- commands


def cmd_show(cfg: config.Config, st: state.State) -> int:
    print()
    print(bold("config"))
    if cfg.sources:
        for source in cfg.sources:
            print(f"  file           {Path(source).name}")
    else:
        print(f"  file           {dim('none -- using built-in defaults and environment')}")
        if config.EXAMPLE_CONFIG_FILE.is_file():
            print(dim(f"                 cp {config.EXAMPLE_CONFIG_FILE.name} "
                      f"{config.CONFIG_FILE.name}  (gitignored) to set your own"))
    print(f"  index          {cfg.index_url}")
    print(f"  simple         {cfg.simple_url}")
    print(f"  upload         {cfg.upload_url}")
    print(f"  user           {cfg.user or dim('<none: read-only checks only>')}")
    print(f"  password       {'set' if cfg.password else dim('not set')}")
    print(f"  package        {cfg.package}")
    print(f"  mirror probe   {cfg.mirror_probe}")
    print(f"  interpreter    {cfg.interpreter}")
    print()
    print(bold("version"))
    print(f"  pyproject      {green(versioning.read())}")
    nxt = ", ".join(f"{p}->{versioning.bump(p)}" for p in ("patch", "minor", "major"))
    print(dim(f"  next           {nxt}"))
    print()
    print(bold("build history") + dim(f"  ({STATE_FILE.name})"))
    if not st.builds:
        print(dim("  nothing built yet -- run: pipcheck cycle"))
    for record in st.builds[-10:]:
        flag = green("uploaded") if record.uploaded else yellow("local only")
        print(f"  {record.version:<14} build {record.build_id}  {record.built_at}  "
              f"{flag} {dim(record.index)}")
    print()
    tool = envs.Venv(VENV_DIR / "tooling")
    ready = tool.has("twine")
    print(bold("tooling") + f"  {green('ready') if ready else yellow('not bootstrapped')}"
          f" {dim(str(tool.path))}")
    if ready:
        print(dim(f"  devpi-client   {'yes' if tool.has('devpi') else 'no'}"))
    print()
    return EXIT_OK


def cmd_list() -> int:
    print()
    for phase in checks.PHASES:
        print(bold(f"[{phase}]"))
        for definition in checks.REGISTRY:
            if definition.phase != phase:
                continue
            needs = []
            if definition.needs_upload:
                needs.append("needs an upload")
            if definition.needs_auth:
                needs.append("needs credentials")
            suffix = dim(f"  ({'; '.join(needs)})") if needs else ""
            print(f"  {definition.name:<24} {definition.description}{suffix}")
        print()
    return EXIT_OK


def cmd_clean(args: argparse.Namespace) -> int:
    targets = [DIST_DIR, STATE_FILE, config.PKG_DIR / "build"]
    targets += list(VENV_DIR.glob("test-*"))
    targets += list(config.PKG_DIR.glob("src/*.egg-info"))
    targets += [p for p in config.ROOT.rglob("__pycache__") if ".venvs" not in p.parts]
    targets.append(config.ROOT / ".ruff_cache")
    targets.append(client.CLIENT_DIR)
    if args.all:
        targets += [VENV_DIR, REPORT_DIR]
    removed = []
    for target in targets:
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(target.name + "/")
        elif target.exists():
            target.unlink()
            removed.append(target.name)
    # Reset the build stamp so a source checkout stays reproducible.
    builder.reset_stamp()
    print(f"removed: {', '.join(removed) or 'nothing'}")
    return EXIT_OK


def cmd_bootstrap(args: argparse.Namespace, cfg: config.Config) -> int:
    venv = envs.tooling(cfg, rebuild=args.rebuild, via_index=args.via_index)
    source = venv.source or "an existing venv"
    extras = "with devpi-client" if venv.has("devpi") else "without devpi-client"
    print(green(f"tooling venv ready at {venv.path} (from {source}, {extras})"))
    return EXIT_OK


def _resolve_report(value: str) -> Path:
    """Resolve a report named on the command line.

    Returns:
        The path to the report, accepting a full path, a bare filename inside reports/,
        or that filename without its .json suffix.

    Raises:
        SystemExit: if no report matches, rather than silently comparing the wrong pair.
    """
    candidates = [Path(value), REPORT_DIR / value]
    if not value.endswith(".json"):
        candidates.append(REPORT_DIR / f"{value}.json")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit(f"no such report: {value}")


def _check_names(path: Path) -> frozenset[str]:
    """Read which checks a saved report covers.

    Returns:
        The set of check names in that report, used to pair runs of the same scope.
    """
    return frozenset(
        r.get("name") for r in json.loads(path.read_text(encoding="utf-8")).get("results", [])
    )


def cmd_compare(args: argparse.Namespace) -> int:
    saved = runner.saved_reports()
    if args.list_reports or (not args.before and len(saved) < _RUNS_TO_COMPARE):
        if not saved:
            print("no saved runs yet -- every `pipcheck cycle`/`verify` saves one into reports/")
            return EXIT_OK if args.list_reports else EXIT_SETUP
        print()
        print(bold(f"saved runs in {REPORT_DIR.name}/") + dim("  (oldest first)"))
        width = max(len(p.name) for p in saved) + 2
        for path in saved:
            report = json.loads(path.read_text(encoding="utf-8"))
            counts = report.get("counts", {})
            versions = (report.get("server") or {}).get("versions") or {}
            stack = ", ".join(f"{k} {v}" for k, v in sorted(versions.items()))
            verdict = green("ok") if report.get("ok") else red("failed")
            print(f"  {path.name:<{width}}{report.get('index', '?')}  {verdict}  "
                  f"{counts.get('pass', 0)}/{sum(counts.values()) or 0} passed  {dim(stack)}")
        print()
        if args.list_reports:
            return EXIT_OK
        print("need two runs to compare: one before the change, one after")
        return EXIT_SETUP

    if args.before and args.after:
        before, after = _resolve_report(args.before), _resolve_report(args.after)
    elif args.before:  # one argument: compare it against the newest run
        before, after = _resolve_report(args.before), saved[-1]
    else:
        # Default to the newest run and the most recent earlier run that covered the
        # same checks -- so a quick `doctor` in between does not become the baseline.
        after = saved[-1]
        scope = _check_names(after)
        before = next((p for p in reversed(saved[:-1]) if _check_names(p) == scope), None)
        if before is None:
            before = saved[-2]
            print(yellow(
                "no earlier run covered the same checks; comparing against the previous "
                "run, whose scope differs"
            ))
    if before == after:
        raise SystemExit("those are the same report")
    return runner.compare(before, after)


def cmd_remove(args: argparse.Namespace, cfg: config.Config, st: state.State) -> int:
    spec = args.spec
    if not spec:
        if not st.last:
            raise SystemExit("nothing built yet; pass an explicit name==version")
        spec = f"{cfg.package}=={st.last.version}"
    if not cfg.user:
        raise SystemExit("removing a release needs credentials (DEVPI_USER/DEVPI_PASSWORD)")
    print(dim(f"removing {spec} from {cfg.index_url}"))
    try:
        resp = client.remove(cfg, spec)
    except ConnectionError as exc:
        raise RuntimeError(str(exc)) from exc
    if resp.status != HTTPStatus.OK:
        print(red(f"removal failed: {client.message(resp)}"))
        return EXIT_FAILED
    record = st.find(spec.split("==")[-1])
    if record:
        record.uploaded = False
        st.save()
    print(green(f"removed {spec}"))
    return EXIT_OK


def _confirmed(prompt: str) -> bool:
    """Ask for the literal word Yes.

    Returns:
        True only if the answer is exactly "Yes" -- not "y", not "yes", not an empty
        line, and not end of input, so neither a stray Enter nor a closed pipe deletes
        anything.
    """
    try:
        return input(prompt).strip() == "Yes"
    except EOFError:
        print()
        return False


def _forget_index(st: state.State, index: str) -> int:
    """Drop the build records of releases that were on `index`.

    Returns:
        How many were dropped.
    """
    gone = [b for b in st.builds if b.uploaded and b.index == index]
    if st.last in gone:
        shutil.rmtree(DIST_DIR, ignore_errors=True)  # those files are the purged release
        builder.reset_stamp()
    st.builds = [b for b in st.builds if b not in gone]
    st.save()
    return len(gone)


def _show_plan(cfg: config.Config, targets: list[purge.Target], reset_to: str) -> bool:
    """Print what a purge would delete.

    Returns:
        Whether there is anything to delete.
    """
    releases = [t for t in targets if t.version]
    projects = [t.project for t in targets if not t.version]
    print()
    print(bold(f"pipcheck's projects on {cfg.index_url}"))
    if not projects:
        print(dim("  none -- nothing to delete"))
        print()
        return False
    for project in projects:
        versions = ", ".join(t.spec.split("==")[1] for t in releases if t.project == project)
        print(f"  {project:<32} {versions or dim('(no releases)')}")
    print()
    print(f"This deletes {len(releases)} release(s) in {len(projects)} project(s) from "
          f"{bold(cfg.index)}. Nothing else on the index is touched.")
    print(f"Afterwards the version in pkg/pyproject.toml goes from {versioning.read()} "
          f"to {reset_to}.")
    return True


def _report_purge(outcomes: list[purge.Outcome], left: list[str] | None) -> int:
    """Print what was deleted, judged by re-reading the index where that worked.

    Returns:
        How many releases are not confirmed gone.
    """
    releases = [o for o in outcomes if o.target.version]
    if left is None:  # could not re-read: fall back on what the deletes answered
        gone = [o for o in releases if o.deleted]
    else:
        gone = [o for o in releases if o.target.spec not in left]
    print()
    print(bold(f"deleted ({len(gone)})"))
    for outcome in gone:
        print(f"  {green(outcome.target.spec)}")
    failed = [o for o in releases if o not in gone]
    if failed:
        print()
        print(bold(f"not deleted ({len(failed)})"))
        for outcome in failed:
            print(f"  {red(outcome.target.spec)}: {outcome.detail}")
    return len(failed)


def cmd_purge(args: argparse.Namespace, cfg: config.Config, st: state.State) -> int:
    reset_to = versioning.validate(args.reset_version)
    if not cfg.user:
        raise RuntimeError("purging needs credentials (DEVPI_USER/DEVPI_PASSWORD)")
    try:
        targets = purge.plan(cfg)
    except ConnectionError as exc:
        raise RuntimeError(str(exc)) from exc
    if not _show_plan(cfg, targets, reset_to):
        return EXIT_OK
    if not _confirmed("Type Yes to delete them: "):
        print(yellow("aborted -- nothing was deleted"))
        return EXIT_FAILED

    current = versioning.read()
    outcomes = purge.execute(cfg, targets)
    left = purge.remaining(cfg)
    failed = _report_purge(outcomes, left)
    if left is None:
        print(yellow("\ncould not read the index back to confirm the deletions; "
                     f"version left at {current}"))
        return EXIT_FAILED
    if failed or left:
        print(yellow(f"\n{len(left)} release(s) remain; version left at {current}"))
        return EXIT_FAILED

    versioning.write(reset_to)
    forgotten = _forget_index(st, cfg.index)
    print()
    print(green(f"{cfg.index} holds nothing of pipcheck's any more (checked by re-reading it)"))
    print(f"version reset {current} -> {bold(reset_to)}; "
          f"forgot {forgotten} build record(s) for {cfg.index}")
    return EXIT_OK


def _print_upload_failure(proc: util.Proc) -> None:
    print(red("upload failed:"))
    print(util.strip_ansi(proc.tail(25)))
    hint = builder.explain_upload_failure(proc)
    if hint:
        print(yellow(f"  -> {hint}"))


def _build_and_upload(
    args: argparse.Namespace, cfg: config.Config, st: state.State,
) -> tuple[state.Build, util.Proc | None]:
    """Set the version, build, and (unless --no-upload) upload.

    Returns:
        The build, and the finished twine process -- None when --no-upload skipped it.
    """
    if args.set_version:
        version = versioning.write(args.set_version)
        print(f"version set to {bold(version)}")
    elif args.no_bump:
        version = versioning.read()
        print(f"keeping version {bold(version)}")
    else:
        version = versioning.write(versioning.bump(args.bump))
        print(f"{args.bump} bump -> {bold(version)}")

    record = builder.build(cfg, st)
    names = sorted(Path(f).name for f in record.files)
    print(f"built {green(record.version)} build {record.build_id}: {', '.join(names)}")

    if args.no_upload:
        print(yellow("skipping upload (--no-upload)"))
        return record, None
    return record, builder.upload(cfg, st, record)


def _publish(args: argparse.Namespace, cfg: config.Config, st: state.State) -> state.Build | None:
    """Version, build and upload -- all or nothing.

    If the build or upload fails, pyproject's version, the build stamp and the recorded
    build history go back to what they were, so a failed publish neither burns a
    version number nor leaves an unpublished build looking like the current one.

    Returns:
        The published (or, with --no-upload, built) record, or None if the upload failed.
    """
    previous_version = versioning.read()
    previous_builds = list(st.builds)

    def rollback() -> None:
        if versioning.read() != previous_version:
            versioning.write(previous_version)
        st.builds = previous_builds
        st.save()
        builder.reset_stamp()
        shutil.rmtree(DIST_DIR, ignore_errors=True)  # holds only the failed build now
        print(yellow(f"rolled back: version is {previous_version} again, nothing recorded"))

    try:
        record, proc = _build_and_upload(args, cfg, st)
    except BaseException:
        rollback()
        raise
    if proc is None:
        return record
    if not proc.ok:
        _print_upload_failure(proc)
        rollback()
        return None
    print(green(f"uploaded {record.version} to {cfg.index_url}"))
    return record


def cmd_cycle(args: argparse.Namespace, cfg: config.Config, st: state.State) -> int:
    # Validate the check selection before building anything, so a typo in --only
    # cannot leave a half-finished publish behind.
    selected = runner.select(**selection(args))
    # Prove the publish can work -- reachable, TLS, index, credentials -- before the
    # version is touched.
    for warning in preflight.run(cfg, upload=not args.no_upload):
        print(yellow(f"warning: {warning}"))

    record = _publish(args, cfg, st)
    if record is None:
        return EXIT_FAILED

    # 4. verify
    report = runner.run(cfg, st, selected, record=record, json_out=args.json_out,
                        unpublished_ok=args.no_upload)
    return EXIT_OK if report.ok else EXIT_FAILED


def cmd_bump(args: argparse.Namespace) -> int:
    print(versioning.write(versioning.bump(args.part)))
    return EXIT_OK


def cmd_set_version(args: argparse.Namespace) -> int:
    print(versioning.write(args.value))
    return EXIT_OK


def cmd_build(cfg: config.Config, st: state.State) -> int:
    record = builder.build(cfg, st)
    print(green(f"built {record.version} (build {record.build_id}) into {DIST_DIR}"))
    return EXIT_OK


def cmd_upload(cfg: config.Config, st: state.State) -> int:
    record = st.last
    if record is None:
        raise RuntimeError("nothing has been built yet -- run `pipcheck build` first")
    for warning in preflight.run(cfg, upload=True):
        print(yellow(f"warning: {warning}"))
    proc = builder.upload(cfg, st, record)
    if not proc.ok:
        _print_upload_failure(proc)
        return EXIT_FAILED
    print(proc.tail(20))
    print(green(f"uploaded {record.version} to {cfg.index_url}"))
    return EXIT_OK


def cmd_doctor(cfg: config.Config, st: state.State) -> int:
    report = runner.run(cfg, st, runner.select(phases=["server"]))
    return EXIT_OK if report.ok else EXIT_FAILED


def cmd_verify(args: argparse.Namespace, cfg: config.Config, st: state.State) -> int:
    report = runner.run(cfg, st, runner.select(**selection(args)), json_out=args.json_out)
    return EXIT_OK if report.ok else EXIT_FAILED


def _dispatch(args: argparse.Namespace, cfg: config.Config, st: state.State) -> int:
    """Run the requested command.

    Returns:
        The command's process exit code.

    Raises:
        SystemExit: if the parser accepted a command this table does not handle, which
            would be a bug here rather than operator error.
    """
    handlers: dict[str, Callable[[], int]] = {
        "show": lambda: cmd_show(cfg, st),
        "clean": lambda: cmd_clean(args),
        "bootstrap": lambda: cmd_bootstrap(args, cfg),
        "remove": lambda: cmd_remove(args, cfg, st),
        "purge": lambda: cmd_purge(args, cfg, st),
        "bump": lambda: cmd_bump(args),
        "set-version": lambda: cmd_set_version(args),
        "build": lambda: cmd_build(cfg, st),
        "upload": lambda: cmd_upload(cfg, st),
        "doctor": lambda: cmd_doctor(cfg, st),
        "verify": lambda: cmd_verify(args, cfg, st),
        "cycle": lambda: cmd_cycle(args, cfg, st),
    }
    handler = handlers.get(args.command)
    if handler is None:
        raise SystemExit(f"unhandled command {args.command!r}")
    return handler()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # These two need neither a server nor a config file.
    if args.command == "list":
        return cmd_list()
    if args.command == "compare":
        return cmd_compare(args)

    cfg = load_config(args)
    if cfg.cleartext_credentials:
        print(yellow(f"warning: {cfg.base} is plain http -- the password for {cfg.user!r} "
                     "crosses the network unencrypted; use https"), file=sys.stderr)
    if cfg.base.startswith("https://") and not cfg.verify_tls:
        print(yellow("warning: TLS verification is off -- anyone on the network path can "
                     "read the password; set ca_bundle instead"), file=sys.stderr)
    st = state.load()
    try:
        return _dispatch(args, cfg, st)
    except RuntimeError as exc:
        print(red(f"error: {exc}"), file=sys.stderr)
        return EXIT_SETUP
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
