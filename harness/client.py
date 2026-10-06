"""Index administration over devpi's HTTP API.

Goes through the same Basic-auth HTTP path as the checks rather than driving the
`devpi` client, whose `login --password` puts the password on a command line where
any local user can read it with `ps`.
"""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING

from . import simple
from .config import VENV_DIR
from .util import Response, http

if TYPE_CHECKING:
    from .config import Config

# Left behind by older versions, which drove devpi-client; `clean` removes it, along
# with the login token it may hold.
CLIENT_DIR = VENV_DIR / "devpi-clientdir"


def remove(cfg: Config, spec: str) -> Response:
    """Delete a release (`name==version`) or a whole project (`name`) from the index.

    Returns:
        devpi's answer: 200 when deleted, 403 when refused (a non-volatile index, or no
        permission), 404 when there is nothing by that name.
    """
    name, _, version = spec.partition("==")
    url = f"{cfg.index_url}/{simple.normalize(name.strip())}"
    if version.strip():
        url += "/" + urllib.parse.quote(version.strip(), safe="")
    return http(url, method="DELETE", auth=cfg.auth, accept="application/json",
                timeout=cfg.http_timeout, verify_tls=cfg.tls)


def message(resp: Response) -> str:
    """Pull devpi's explanation out of an answer.

    Returns:
        The `message` devpi sent, or the HTTP status when there is none.
    """
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("message"):
        return " ".join(str(body["message"]).split())
    return f"HTTP {resp.status}"
