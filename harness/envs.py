"""Creation of throwaway virtualenvs: one for tooling, one per install test."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import VENV_DIR, Config
from .util import Proc, run, yellow

if TYPE_CHECKING:
    from pathlib import Path

TOOLING = ("build", "twine")
TOOLING_OPTIONAL = ("devpi-client",)


@dataclass
class Venv:
    """A virtualenv on disk, with the handful of operations the harness needs."""

    path: Path
    source: str = ""  # where its packages came from, for the bootstrap message

    @property
    def python(self) -> Path:
        exe = self.path / "bin" / "python"
        return exe if exe.exists() else self.path / "Scripts" / "python.exe"

    def bin(self, name: str) -> Path:
        exe = self.path / "bin" / name
        return exe if exe.exists() else self.path / "Scripts" / f"{name}.exe"

    def has(self, name: str) -> bool:
        return self.bin(name).exists()

    def pip(self, *args: str, cfg: Config | None = None, timeout: int | None = None) -> Proc:
        cmd = [str(self.python), "-m", "pip", "--no-input", "--disable-pip-version-check", *args]
        return run(cmd, timeout=timeout or (cfg.timeout if cfg else 900),
                   verbose=bool(cfg and cfg.verbose))

    def run_python(self, *args: str, cfg: Config | None = None) -> Proc:
        return run([str(self.python), *args], timeout=cfg.timeout if cfg else 900,
                   verbose=bool(cfg and cfg.verbose))


def create(cfg: Config, name: str, *, with_pip: bool = True) -> Venv:
    """Create (or recreate) a venv under .venvs/<name>."""
    path = VENV_DIR / name
    if path.exists():
        shutil.rmtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    args = [cfg.interpreter, "-m", "venv"]
    if not with_pip:
        args.append("--without-pip")
    proc = run([*args, str(path)], timeout=300, verbose=cfg.verbose)
    if not proc.ok:
        raise RuntimeError(
            f"could not create venv {name} with {cfg.interpreter}:\n{proc.tail()}\n"
            "On Debian/Ubuntu this usually means the python3-venv package is missing."
        )
    return Venv(path)


def discard(cfg: Config, venv: Venv) -> None:
    if not cfg.keep_venvs and venv.path.exists():
        shutil.rmtree(venv.path, ignore_errors=True)


def tooling(cfg: Config, *, rebuild: bool = False, via_index: bool = False,
            with_devpi_client: bool = True) -> Venv:
    """Return the tooling venv holding build/twine, creating it on first use.

    Tooling comes from upstream PyPI by default, so a broken internal repo cannot stop
    the harness from running. On a machine with no PyPI access -- common for a host
    that only talks to the internal mirror -- it falls back to installing through the
    internal index, and says so. `via_index=True` goes straight there.
    """
    venv = Venv(VENV_DIR / "tooling")
    if not rebuild and venv.python.exists() and venv.has("twine"):
        return venv

    venv = create(cfg, "tooling")
    sources: list[tuple[str, list[str]]] = [("the internal index", cfg.pip_index_args())]
    if not via_index:
        sources.insert(0, ("PyPI", []))

    failures = []
    for attempt, (source, index_args) in enumerate(sources):
        if attempt:
            print(
                yellow(f"could not install the tooling from {sources[attempt - 1][0]}; "
                       f"retrying through {source}")
            )
        # Best-effort: the venv's bundled pip is usually fine on its own.
        venv.pip("install", "--upgrade", "pip", *index_args, cfg=cfg)
        install = venv.pip("install", *TOOLING, *index_args, cfg=cfg)
        if install.ok:
            if with_devpi_client:
                # Optional: only needed for `pipcheck remove` and index administration.
                venv.pip("install", *TOOLING_OPTIONAL, *index_args, cfg=cfg)
            venv.source = source
            return venv
        failures.append(f"from {source}:\n{install.tail(12)}")

    raise RuntimeError(
        f"could not install {', '.join(TOOLING)} into {venv.path}.\n\n"
        + "\n\n".join(failures)
        + "\n\nIf this machine reaches neither PyPI nor the internal index, copy a "
          "populated .venvs/tooling from a machine that does."
    )
