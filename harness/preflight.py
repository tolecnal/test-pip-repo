"""Checks that must pass before `cycle` touches the version or publishes anything.

The suite proper reports what the repository does; this answers the narrower question
"can this run possibly publish?" -- right URL, right port, a TLS setup we can talk to,
an index that exists, credentials the server accepts. Each of those failing used to
surface only as a twine traceback after the version had already been bumped.
"""

from __future__ import annotations

import json
import urllib.parse
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from .util import Response, http

if TYPE_CHECKING:
    from .config import Config

# Principals in an index's acl_upload that are not plain user names.
_ANYONE = {":ANONYMOUS:", ":AUTHENTICATED:"}


class PreflightError(RuntimeError):
    """The configured repository cannot be published to, for a reason the operator can fix."""


def _get(cfg: Config, url: str, *, auth: bool = True) -> Response:
    return http(url, auth=cfg.auth if auth else None, accept="application/json",
                timeout=cfg.http_timeout, verify_tls=cfg.tls)


def _result(resp: Response, what: str) -> dict[str, Any]:
    """Parse a devpi JSON answer.

    Returns:
        The `result` object of the answer.

    Raises:
        PreflightError: if the body is not devpi's JSON -- a different service on that
            port, or a proxy's error page.
    """
    try:
        body = resp.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("result"), dict):
        raise PreflightError(
            f"{what} ({resp.url}) did not answer with devpi's JSON -- is url pointing at "
            "devpi-server, on the right port?"
        )
    return body["result"]


def _reach(cfg: Config, *, upload: bool) -> list[str]:
    """Prove the server answers as devpi and the index exists.

    Returns:
        Warnings: advertised URLs that were not adopted because they point elsewhere.

    Raises:
        PreflightError: if the server cannot be reached or is not devpi, or the index is
            missing or cannot take uploads.
    """
    try:
        root = _get(cfg, f"{cfg.base}/+api", auth=False)
    except ConnectionError as exc:
        raise PreflightError(f"cannot reach the devpi server:\n  {exc}") from exc
    if root.status != HTTPStatus.OK:
        raise PreflightError(
            f"GET {root.url} returned HTTP {root.status} -- is url the devpi-server root "
            "(no index path, right port)?"
        )
    _result(root, "the server root")

    index_api = _get(cfg, f"{cfg.index_url}/+api")
    if index_api.status == HTTPStatus.NOT_FOUND:
        path = urllib.parse.urlsplit(cfg.base).path
        if path.strip("/"):
            raise PreflightError(
                f"index {cfg.index!r} not found under {cfg.base} -- url has the path "
                f"{path!r}; it should be the server root, with the index set separately"
            )
        raise PreflightError(
            f"index {cfg.index!r} does not exist on {cfg.base} "
            "(create it with: devpi index -c <index> bases=root/pypi)"
        )
    if index_api.status != HTTPStatus.OK:
        raise PreflightError(f"GET {index_api.url} returned HTTP {index_api.status}")
    api = _result(index_api, "the index API")
    ignored = cfg.adopt_api(api)
    warnings = [(
        f"ignoring URLs the server advertises on another host ({', '.join(ignored)}); "
        f"using {cfg.base} -- check devpi-server's --outside-url"
    )] if ignored else []
    if upload and not api.get("pypisubmit"):
        raise PreflightError(
            f"index {cfg.index!r} does not advertise an upload URL -- it is probably a "
            "mirror index, which cannot be published to"
        )
    return warnings


def _may_upload(cfg: Config) -> list[str]:
    """Prove the credentials work, and look for an acl that would refuse them.

    Returns:
        Warnings that do not block the run.

    Raises:
        PreflightError: if there are no credentials, or devpi rejects them.
    """
    if not cfg.user:
        raise PreflightError(
            "publishing needs credentials: set user/password in pipcheck.toml or "
            "DEVPI_USER/DEVPI_PASSWORD in the environment (or pass --no-upload)"
        )
    # Always the configured server, never a login URL the server advertises: that could
    # name another host, and this request carries the password.
    credentials = {"user": cfg.user, "password": cfg.password}
    login = http(f"{cfg.base}/+login", method="POST", json_body=credentials,
                 accept="application/json", timeout=cfg.http_timeout, verify_tls=cfg.tls)
    if login.status == HTTPStatus.UNAUTHORIZED:
        raise PreflightError(f"devpi rejected the password for user {cfg.user!r}")
    if login.status != HTTPStatus.OK:
        raise PreflightError(f"login as {cfg.user!r} returned HTTP {login.status}")

    index = _get(cfg, cfg.index_url)
    if index.status != HTTPStatus.OK:
        return []
    acl = _result(index, "the index").get("acl_upload")
    if not isinstance(acl, list) or cfg.user == "root" or cfg.user in acl or _ANYONE & set(acl):
        return []
    # Groups from an auth plugin can grant access too, so this only warns.
    return [(
        f"user {cfg.user!r} is not in acl_upload {acl} of {cfg.index!r}; "
        "the upload will be refused unless a group grants it"
    )]


def run(cfg: Config, *, upload: bool) -> list[str]:
    """Prove the server is reachable and, when `upload` is set, that we may publish to it.

    Also adopts the server's advertised URLs into `cfg`, so twine posts where devpi
    says to rather than where we guessed.

    Returns:
        Warnings that do not block the run but are worth printing.
    """
    warnings = _reach(cfg, upload=upload)
    return warnings + (_may_upload(cfg) if upload else [])
