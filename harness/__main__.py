"""pipcheck command line: control the version, publish, and test the repository."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__, builder, checks, client, config, envs, runner, state, versioning
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
                        help="do not verify TLS certificates (self-signed internal certs)")

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
                             help="delete a release from the index (needs devpi-client)")
    remove.add_argument("spec", nargs="?", help="name==version (default: the last build)")

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
        print(dim(f"  devpi-client   {'yes' if tool.has('devpi') else 'no (admin commands off)'}"))
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
    proc = client.remove(cfg, spec)
    print(proc.tail(15))
    if not proc.ok:
        print(red("removal failed"))
        return EXIT_FAILED
    record = st.find(spec.split("==")[-1])
    if record:
        record.uploaded = False
        st.save()
    print(green(f"removed {spec}"))
    return EXIT_OK


def cmd_cycle(args: argparse.Namespace, cfg: config.Config, st: state.State) -> int:
    # Validate the check selection before building anything, so a typo in --only
    # cannot leave a half-finished publish behind.
    selected = runner.select(**selection(args))
    # Fail before touching the version if the publish step cannot possibly work.
    if not args.no_upload and not cfg.user:
        raise RuntimeError(
            "publishing needs credentials: set user/password in pipcheck.toml or "
            "DEVPI_USER/DEVPI_PASSWORD in the environment (or pass --no-upload)"
        )

    # 1. decide the version
    if args.set_version:
        version = versioning.write(args.set_version)
        print(f"version set to {bold(version)}")
    elif args.no_bump:
        version = versioning.read()
        print(f"keeping version {bold(version)}")
    else:
        version = versioning.write(versioning.bump(args.bump))
        print(f"{args.bump} bump -> {bold(version)}")

    # 2. build
    record = builder.build(cfg, st)
    names = sorted(Path(f).name for f in record.files)
    print(f"built {green(record.version)} build {record.build_id}: {', '.join(names)}")

    # 3. upload
    if args.no_upload:
        print(yellow("skipping upload (--no-upload)"))
    else:
        proc = builder.upload(cfg, st, record)
        if not proc.ok:
            print(red("upload failed:"))
            print(proc.tail(25))
            return EXIT_FAILED
        print(green(f"uploaded {record.version} to {cfg.index_url}"))

    # 4. verify
    report = runner.run(cfg, st, selected, record=record, json_out=args.json_out)
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
    proc = builder.upload(cfg, st, record)
    print(proc.tail(20))
    if not proc.ok:
        return EXIT_FAILED
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
