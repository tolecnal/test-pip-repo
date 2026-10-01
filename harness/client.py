"""Thin wrapper around the optional `devpi` client, used for index administration.

Kept optional on purpose: everything in the check suite works with plain pip and
twine, so a missing devpi-client only disables the admin conveniences.
"""

from __future__ import annotations

from .config import VENV_DIR, Config
from .util import Proc, run

CLIENT_DIR = VENV_DIR / "devpi-clientdir"  # never touch the user's ~/.devpi


def available(cfg: Config) -> bool:
    from . import envs

    return envs.tooling(cfg).has("devpi")


def _devpi(cfg: Config, *args: str) -> Proc:
    from . import envs

    tool = envs.tooling(cfg)
    if not tool.has("devpi"):
        raise RuntimeError(
            "devpi-client is not installed in the tooling venv; "
            "run `pipcheck bootstrap` with network access to install it"
        )
    CLIENT_DIR.mkdir(parents=True, exist_ok=True)
    return run(
        [str(tool.bin("devpi")), "--clientdir", str(CLIENT_DIR), *args],
        timeout=cfg.timeout,
        verbose=cfg.verbose,
    )


def login(cfg: Config) -> Proc:
    use = _devpi(cfg, "use", cfg.index_url)
    if not use.ok:
        return use
    return _devpi(cfg, "login", cfg.user, "--password", cfg.password)


def remove(cfg: Config, spec: str) -> Proc:
    """Delete a release (`name==version`) or a whole project from the index."""
    logged_in = login(cfg)
    if not logged_in.ok:
        return logged_in
    return _devpi(cfg, "remove", "-y", spec)


def list_releases(cfg: Config, project: str) -> Proc:
    use = _devpi(cfg, "use", cfg.index_url)
    if not use.ok:
        return use
    return _devpi(cfg, "list", project)
