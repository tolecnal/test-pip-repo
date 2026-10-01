"""Stamping, building and uploading the test package."""

from __future__ import annotations

import contextlib
import datetime as _dt
import io
import secrets
import shutil
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

from . import envs, state, versioning
from .config import BUILD_INFO, DIST_DIR, PKG_DIR, Config
from .util import Proc, run, sha256_file

if TYPE_CHECKING:
    from collections.abc import Iterator

STAMP_TEMPLATE = '''"""Build stamp.

Rewritten by `pipcheck build` immediately before each build. The checked-in values
below are placeholders so the package remains importable straight from a source
checkout.
"""

VERSION = "{version}"
BUILD_ID = "{build_id}"
BUILT_AT = "{built_at}"
'''


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def reset_stamp() -> None:
    """Restore the placeholder stamp that belongs in version control."""
    BUILD_INFO.write_text(
        STAMP_TEMPLATE.format(
            version="0.0.0", build_id="unstamped", built_at="1970-01-01T00:00:00Z"
        )
    )


def stamp(version: str, build_id: str | None = None) -> tuple[str, str]:
    """Write _build_info.py for this build. Returns (build_id, built_at).

    The build id is fresh random hex on every build, which is what makes a stale
    artifact detectable: the version alone cannot distinguish "the release I just
    uploaded" from "a cached release that happens to share its number".
    """
    build_id = build_id or secrets.token_hex(6)
    built_at = _now()
    BUILD_INFO.write_text(
        STAMP_TEMPLATE.format(version=version, build_id=build_id, built_at=built_at)
    )
    return build_id, built_at


def _compile(cfg: Config, outdir: Path, version: str, build_id: str, built_at: str) -> state.Build:
    """Run `python -m build` into outdir and describe the result."""
    outdir.mkdir(parents=True, exist_ok=True)
    # setuptools reuses stale build/ trees; drop it so the stamp cannot be cached.
    shutil.rmtree(PKG_DIR / "build", ignore_errors=True)

    tool = envs.tooling(cfg)
    proc = run(
        [str(tool.python), "-m", "build", "--sdist", "--wheel", "--outdir", str(outdir), "."],
        cwd=PKG_DIR,
        timeout=cfg.timeout,
        verbose=cfg.verbose,
    )
    if not proc.ok:
        raise RuntimeError(f"build failed:\n{proc.tail(40)}")

    files = sorted(p for p in outdir.iterdir() if p.suffix == ".whl" or p.name.endswith(".tar.gz"))
    if not any(f.suffix == ".whl" for f in files) or not any(
        f.name.endswith(".tar.gz") for f in files
    ):
        raise RuntimeError(f"expected both a wheel and an sdist in {outdir}, got {files}")

    return state.Build(
        version=version,
        build_id=build_id,
        built_at=built_at,
        files=[str(f) for f in files],
        sha256={f.name: sha256_file(f) for f in files},
        index=cfg.index,
    )


def build(cfg: Config, st: state.State, *, clean: bool = True) -> state.Build:
    """Stamp and build an sdist + wheel for the current pyproject version."""
    version = versioning.read()
    build_id, built_at = stamp(version)
    if clean and DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    return st.record(_compile(cfg, DIST_DIR, version, build_id, built_at))


@contextlib.contextmanager
def preserved_source() -> Iterator[None]:
    """Leave pyproject's version and the build stamp exactly as they were.

    Lets a check build something without disturbing the working tree.
    """
    version = versioning.read()
    snapshot = BUILD_INFO.read_text() if BUILD_INFO.exists() else None
    try:
        yield
    finally:
        if versioning.read() != version:
            versioning.write(version)
        if snapshot is None:
            reset_stamp()
        elif BUILD_INFO.read_text() != snapshot:
            BUILD_INFO.write_text(snapshot)


def build_variant(cfg: Config, outdir: Path, version: str | None = None) -> state.Build:
    """Rebuild a version with a fresh build id, into a scratch directory.

    Produces artifacts with the same filenames but different bytes -- exactly what an
    accidental (or malicious) re-release looks like. `version` must be the version
    actually under test, which is not necessarily what pyproject.toml currently says
    (a later `cycle` may have moved it on). Not recorded in state; wrap the call in
    `preserved_source()` to keep the working tree unchanged.
    """
    if version and version != versioning.read():
        versioning.write(version)
    version = versioning.read()
    build_id, built_at = stamp(version)
    return _compile(cfg, outdir, version, build_id, built_at)


def adopt_variant(st: state.State, variant: state.Build) -> state.Build:
    """Promote a variant build to be the recorded one, copying its files into dist/.

    Only called once the variant has been uploaded successfully, so it inherits the
    uploaded flag -- otherwise later checks would think nothing is published.
    """
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    moved = []
    for path in (Path(f) for f in variant.files):
        target = DIST_DIR / path.name
        shutil.copy2(path, target)
        moved.append(str(target))
    variant.files = sorted(moved)
    variant.uploaded = True
    return st.record(variant)


def upload(cfg: Config, st: state.State, build_record: state.Build | None = None) -> Proc:
    """Upload the recorded build's artifacts with twine."""
    record = build_record or st.last
    if record is None:
        raise RuntimeError("nothing has been built yet -- run `pipcheck build` first")
    if not cfg.user:
        raise RuntimeError(
            "upload needs credentials: set user/password in pipcheck.toml or "
            "DEVPI_USER/DEVPI_PASSWORD in the environment"
        )
    missing = [f for f in record.files if not Path(f).exists()]
    if missing:
        raise RuntimeError(f"recorded artifacts are gone, rebuild: {missing}")

    proc = _twine(cfg, record.files, cfg.user, cfg.password)
    if proc.ok:
        record.uploaded = True
        record.index = cfg.index
        st.save()
    return proc


def _twine(cfg: Config, files: list[str], user: str, password: str) -> Proc:
    tool = envs.tooling(cfg)
    # --verbose is what makes twine report the HTTP status; it does not echo the
    # password. Credentials go through the environment, never argv.
    cmd = [
        str(tool.bin("twine")), "upload", "--non-interactive",
        "--disable-progress-bar", "--verbose",
        "--repository-url", cfg.upload_url, *files,
    ]
    env = {"TWINE_USERNAME": user, "TWINE_PASSWORD": password}
    return run(cmd, env=env, timeout=cfg.timeout, verbose=cfg.verbose)


def upload_as(cfg: Config, files: list[str], user: str, password: str) -> Proc:
    """Upload with explicit (possibly bogus) credentials -- used by the auth check."""
    return _twine(cfg, files, user, password)


def make_probe_sdist(dest_dir: Path) -> tuple[str, Path]:
    """Build a minimal, never-before-seen sdist in memory.

    Used to test that unauthenticated upload is refused. A fresh project name means
    a rejection cannot be confused with "this version already exists", and a
    success cannot clobber a real release.
    """
    name = f"pipcheck-authprobe-{secrets.token_hex(4)}"
    version = "0.0.1"
    base = f"{name}-{version}"
    pkg_info = (
        "Metadata-Version: 2.1\n"
        f"Name: {name}\n"
        f"Version: {version}\n"
        "Summary: pipcheck anonymous-upload probe; safe to delete.\n"
        "Classifier: Private :: Do Not Upload\n"
    )
    setup_py = (
        "from setuptools import setup\n"
        f"setup(name={name!r}, version={version!r}, py_modules=[])\n"
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = dest_dir / f"{base}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for filename, content in (("PKG-INFO", pkg_info), ("setup.py", setup_py)):
            data = content.encode()
            info = tarfile.TarInfo(f"{base}/{filename}")
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return name, archive
