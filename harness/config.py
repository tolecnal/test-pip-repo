"""Configuration loading: pipcheck.toml, then pipcheck.local.toml, then environment."""

from __future__ import annotations

import os
import sys
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "pkg"
DIST_DIR = ROOT / "dist"
VENV_DIR = ROOT / ".venvs"
REPORT_DIR = ROOT / "reports"
STATE_FILE = ROOT / ".pipcheck-state.json"
CONFIG_FILE = ROOT / "pipcheck.toml"
LOCAL_CONFIG_FILE = ROOT / "pipcheck.local.toml"
EXAMPLE_CONFIG_FILE = ROOT / "pipcheck.toml.example"
BUILD_INFO = PKG_DIR / "src" / "devpi_smoke" / "_build_info.py"
PYPROJECT = PKG_DIR / "pyproject.toml"

# Config keys that may be overridden from the environment.
ENV_MAP = {
    "DEVPI_URL": "url",
    "DEVPI_INDEX": "index",
    "DEVPI_USER": "user",
    "DEVPI_PASSWORD": "password",
}


@dataclass
class Config:
    # --- connection
    url: str = "http://localhost:3141"
    index: str = "testing/dev"  # "<user>/<index>" on the devpi server
    user: str = ""  # upload credentials; read-only checks work without them
    password: str = ""
    verify_tls: bool = True

    # --- what to test with
    package: str = "devpi-smoke"
    mirror_probe: str = "wcwidth"  # public package, proves root/pypi fall-through

    # --- behaviour
    python: str = ""  # base interpreter for test venvs (default: this one)
    timeout: int = 900
    http_timeout: int = 30
    run_pytest: bool = True
    expect_anonymous_read: bool = True
    expect_upload_auth: bool = True
    expect_overwrite_rejected: str | bool = "auto"  # "auto" follows the index's volatile flag
    search_wait: int = 20  # seconds to let devpi-web index a new release
    keep_venvs: bool = False
    verbose: bool = False

    # --- populated at runtime
    _api: dict[str, str] = field(default_factory=dict, repr=False)
    _sources: list[str] = field(default_factory=list, repr=False)

    @property
    def sources(self) -> list[str]:
        """Config files that were actually read, in increasing precedence."""
        return list(self._sources)

    @classmethod
    def build(cls, data: dict[str, Any], sources: list[str]) -> "Config":
        """Construct from already-validated key/values, remembering where they came from."""
        cfg = cls(**data)
        cfg._sources = list(sources)
        return cfg

    def adopt_api(self, result: dict[str, Any]) -> None:
        """Record the URLs the server advertises, in preference to our guesses."""
        for key in ("simpleindex", "pypisubmit", "index", "login"):
            value = result.get(key)
            if value:
                self._api[key] = urllib.parse.urljoin(self.base + "/", str(value))

    # ---------------------------------------------------------------- derived URLs
    @property
    def base(self) -> str:
        return self.url.rstrip("/")

    @property
    def index_url(self) -> str:
        return f"{self.base}/{self.index.strip('/')}"

    @property
    def simple_url(self) -> str:
        """PEP 503 simple index URL, preferring what the server advertises."""
        return self._api.get("simpleindex") or f"{self.index_url}/+simple/"

    @property
    def upload_url(self) -> str:
        """Where twine POSTs, preferring what the server advertises."""
        return self._api.get("pypisubmit") or f"{self.index_url}/"

    @property
    def auth(self) -> tuple[str, str] | None:
        return (self.user, self.password) if self.user else None

    @property
    def interpreter(self) -> str:
        return self.python or sys.executable

    def pip_index_args(self) -> list[str]:
        """Index arguments forcing pip to use *only* the internal repository."""
        args = ["--index-url", self.simple_url]
        host = self.simple_url.split("//", 1)[-1].split("/", 1)[0]
        if self.simple_url.startswith("http://") or not self.verify_tls:
            args += ["--trusted-host", host.split("@")[-1]]
        return args

    def summary(self) -> str:
        who = self.user or "<anonymous>"
        return f"{self.index_url}  (simple: {self.simple_url}, user: {who})"


def load(path: str | None = None, overrides: dict[str, Any] | None = None) -> Config:
    """Build a Config from file(s), then environment, then explicit overrides."""
    data: dict[str, Any] = {}
    sources: list[str] = []
    files = [Path(path)] if path else [CONFIG_FILE, LOCAL_CONFIG_FILE]
    for candidate in files:
        if candidate.is_file():
            with open(candidate, "rb") as handle:
                loaded = tomllib.load(handle)
            data.update(loaded.get("repo", loaded))
            sources.append(str(candidate))
        elif path:
            raise SystemExit(f"config file not found: {candidate}")

    for env_key, field_name in ENV_MAP.items():
        if os.environ.get(env_key):
            data[field_name] = os.environ[env_key]

    for key, value in (overrides or {}).items():
        if value is not None:
            data[key] = value

    known = {f.name for f in Config.__dataclass_fields__.values() if not f.name.startswith("_")}
    unknown = set(data) - known
    if unknown:
        raise SystemExit(
            f"unknown config key(s): {', '.join(sorted(unknown))}\n"
            f"valid keys: {', '.join(sorted(known))}"
        )
    return Config.build(data, sources)
