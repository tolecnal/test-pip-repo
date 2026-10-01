"""Reading devpi's PEP 503 simple index and JSON views."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING

from .util import Response, http

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .config import Config

_HREF = re.compile(r"""<a[^>]+href=["']([^"']+)["'][^>]*>([^<]*)</a>""", re.IGNORECASE)


def normalize(name: str) -> str:
    """Normalise a project name the way PEP 503 requires.

    Returns:
        The lower-cased name with runs of `-`, `_` and `.` collapsed to a single `-`,
        which is the only spelling a simple index is obliged to answer to.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class Link:
    """One distribution file as the simple index advertises it."""

    filename: str
    url: str
    sha256: str | None = None

    @property
    def is_wheel(self) -> bool:
        return self.filename.endswith(".whl")

    @property
    def is_sdist(self) -> bool:
        return self.filename.endswith((".tar.gz", ".zip"))

    @property
    def version(self) -> str | None:
        return version_of(self.filename)


def project_url(cfg: Config, project: str) -> str:
    return f"{cfg.simple_url.rstrip('/')}/{normalize(project)}/"


def fetch(cfg: Config, project: str, *, auth: bool = True) -> tuple[Response, list[Link]]:
    """Fetch a project's simple page and parse its distribution links.

    Returns:
        The raw response (so callers can judge a 404 for themselves) and the links it
        advertises, each with the sha256 from its URL fragment where one is given. The
        link list is empty for any status other than 200.
    """
    url = project_url(cfg, project)
    resp = http(
        url,
        auth=cfg.auth if auth else None,
        timeout=cfg.http_timeout,
        verify_tls=cfg.verify_tls,
    )
    links: list[Link] = []
    if resp.status == HTTPStatus.OK:
        for href, text in _HREF.findall(resp.text):
            absolute = urllib.parse.urljoin(resp.url, href)
            fragment = urllib.parse.urlsplit(absolute).fragment
            digest = None
            if fragment.startswith("sha256="):
                digest = fragment.split("=", 1)[1]
            filename = (text or "").strip() or absolute.rsplit("/", 1)[-1].split("#")[0]
            links.append(Link(filename, absolute.split("#")[0], digest))
    return resp, links


def version_of(filename: str) -> str | None:
    """Extract the version from a wheel or sdist filename.

    Returns:
        The version, or None if the filename is neither a wheel nor a recognised source
        archive -- devpi also lists docs and toxresult files on some views.
    """
    if filename.endswith(".whl"):
        parts = filename[: -len(".whl")].split("-")
        return parts[1] if parts[1:] else None
    for suffix in (".tar.gz", ".zip", ".tar.bz2"):
        if filename.endswith(suffix):
            stem = filename[: -len(suffix)]
            _, _, version = stem.rpartition("-")
            return version or None
    return None


def versions(links: list[Link]) -> dict[str, list[Link]]:
    found: dict[str, list[Link]] = {}
    for link in links:
        version = link.version
        if version:
            found.setdefault(version, []).append(link)
    return found


def version_key(version: str) -> tuple[tuple[int, ...], int, int]:
    """Build a sort key approximating PEP 440 ordering.

    Returns:
        A key ordering releases correctly for the version shapes this harness produces:
        the numeric release, then a stage rank putting dev/alpha/beta/rc before the
        release and post after it, then that stage's serial. Not a full PEP 440
        implementation -- it has no dependencies, which matters more here.
    """
    match = re.match(r"\d+(?:\.\d+)*", version)
    release = tuple(int(p) for p in match.group(0).split(".")) if match else (0,)
    release = (*release, 0, 0, 0, 0)[:4]
    rest = version[match.end() :].lower().lstrip(".-_") if match else version.lower()

    serial_match = re.search(r"\d+", rest)
    serial = int(serial_match.group(0)) if serial_match else 0
    if rest.startswith("dev"):
        stage = -4
    elif rest.startswith(("a", "alpha")):
        stage = -3
    elif rest.startswith(("b", "beta")):
        stage = -2
    elif rest.startswith(("rc", "c", "pre")):
        stage = -1
    elif rest.startswith(("post", "r")):
        stage = 1
    else:
        stage, serial = 0, 0
    return release, stage, serial


def latest(version_list: Iterable[str]) -> str | None:
    versions_ = list(version_list)
    return max(versions_, key=version_key) if versions_ else None


def project_json(cfg: Config, project: str) -> Response:
    """Ask devpi for its JSON view of a project.

    Returns:
        The response. Its `result` maps each version to that release's metadata and
        `+links`, which is where requires_dist is checked.
    """
    return http(
        f"{cfg.index_url}/{normalize(project)}",
        accept="application/json",
        auth=cfg.auth,
        timeout=cfg.http_timeout,
        verify_tls=cfg.verify_tls,
    )
