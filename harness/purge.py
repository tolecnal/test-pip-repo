"""Remove everything pipcheck has published to the configured index.

Only pipcheck's own projects are touched: the test package, and the throwaway
`pipcheck-authprobe-*` projects `upload_requires_auth` leaves behind if an index ever
accepts its bogus upload. Anything else on the index is never listed, so a purge
pointed at a shared index by mistake cannot take other people's packages with it.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING

from . import client, preflight, simple
from .preflight import PreflightError

if TYPE_CHECKING:
    from .config import Config

# Prefix of the projects make_probe_sdist() creates.
PROBE_PREFIX = "pipcheck-authprobe-"


@dataclass
class Target:
    """One thing to delete: a release, or (version None) a whole project."""

    project: str
    version: str | None = None

    @property
    def spec(self) -> str:
        return f"{self.project}=={self.version}" if self.version else self.project


@dataclass
class Outcome:
    """What happened when deleting one target."""

    target: Target
    deleted: bool
    detail: str


def _ours(cfg_package: str, project: str) -> bool:
    name = simple.normalize(project)
    return name == simple.normalize(cfg_package) or name.startswith(PROBE_PREFIX)


def plan(cfg: Config) -> list[Target]:
    """List pipcheck's releases on the configured index, without changing anything.

    Returns:
        A target per release, oldest version first within a project, followed by one
        for the project itself, so the project is removed once its releases are.

    Raises:
        PreflightError: if the index cannot be read, is a mirror, or is not volatile --
            devpi refuses to delete from a non-volatile index, so there is no point
            asking for confirmation first.
    """
    preflight.run(cfg, upload=False)
    index = preflight.fetch_json(cfg, cfg.index_url, "the index")
    if index.get("type") == "mirror":
        raise PreflightError(f"{cfg.index} is a mirror index; there is nothing of ours to purge")
    if not index.get("volatile", True):
        raise PreflightError(
            f"{cfg.index} is not volatile, and devpi refuses to delete releases from it "
            f"(make it volatile first: devpi index {cfg.index} volatile=True)"
        )

    targets: list[Target] = []
    for project in sorted(p for p in index.get("projects") or [] if _ours(cfg.package, p)):
        # devpi's JSON view of a project maps each of its versions on this index to
        # that release's metadata.
        releases = preflight.fetch_json(cfg, f"{cfg.index_url}/{project}", project)
        targets += [Target(project, v) for v in sorted(releases, key=simple.version_key)]
        targets.append(Target(project))
    return targets


def execute(cfg: Config, targets: list[Target]) -> list[Outcome]:
    """Delete each target, carrying on past failures so the report is complete.

    Returns:
        One outcome per target. A release that was already gone counts as deleted.
    """
    outcomes = []
    for target in targets:
        try:
            resp = client.remove(cfg, target.spec)
        except ConnectionError as exc:
            outcomes.append(Outcome(target, deleted=False, detail=str(exc)))
            continue
        gone = resp.status in {HTTPStatus.OK, HTTPStatus.NOT_FOUND}
        outcomes.append(Outcome(target, deleted=gone, detail=client.message(resp)))
    return outcomes


def remaining(cfg: Config) -> list[str] | None:
    """Re-read the index after a purge.

    Returns:
        The specs of pipcheck's releases still on it -- what the report trusts, rather
        than the HTTP statuses of the deletes -- or None if the index could not be read
        back, which the report must say rather than claim success.
    """
    try:
        return [target.spec for target in plan(cfg) if target.version]
    except (PreflightError, ConnectionError):
        return None
