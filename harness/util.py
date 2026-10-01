"""Small shared helpers: process running, HTTP, hashing, terminal colour."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def yellow(t: str) -> str:
    return _c("33", t)


def cyan(t: str) -> str:
    return _c("36", t)


# --------------------------------------------------------------------------- procs


@dataclass
class Proc:
    cmd: list[str]
    returncode: int
    output: str
    duration: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def tail(self, lines: int = 25) -> str:
        return "\n".join(self.output.strip().splitlines()[-lines:])

    def pretty(self) -> str:
        return f"$ {shlex_join(self.cmd)}\n{self.tail()}"


def shlex_join(cmd: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(str(c)) for c in cmd)


def run(
    cmd: list[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 1800,
    verbose: bool = False,
) -> Proc:
    """Run a command, capturing stdout+stderr together. Never raises on failure."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    # Keep child pip/python output deterministic and unbuffered.
    full_env.setdefault("PYTHONUNBUFFERED", "1")
    full_env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")

    cmd = [str(c) for c in cmd]
    if verbose:
        print(dim(f"  $ {shlex_join(cmd)}"))
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        out, rc = proc.stdout, proc.returncode
    except FileNotFoundError as exc:
        out, rc = f"command not found: {exc}", 127
    except subprocess.TimeoutExpired as exc:
        partial = exc.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        out, rc = f"{partial}\n<timed out after {timeout}s>", 124
    duration = time.monotonic() - started
    if verbose and out.strip():
        print(dim("  | " + out.strip().replace("\n", "\n  | ")))
    return Proc(cmd, rc, out, duration)


# ---------------------------------------------------------------------------- http


@dataclass
class Response:
    url: str
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        import json

        return json.loads(self.body or b"null")


def http(
    url: str,
    *,
    method: str = "GET",
    auth: tuple[str, str] | None = None,
    accept: str | None = None,
    timeout: int = 30,
    verify_tls: bool = True,
) -> Response:
    """Fetch a URL. HTTP error statuses are returned, not raised."""
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", "pipcheck/1.0")
    if accept:
        req.add_header("Accept", accept)
    if auth and auth[0]:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")

    ctx = None
    if url.startswith("https") and not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return Response(
                url, resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
            )
    except urllib.error.HTTPError as exc:
        return Response(
            url, exc.code, exc.read(), {k.lower(): v for k, v in (exc.headers or {}).items()}
        )
    except urllib.error.URLError as exc:
        raise ConnectionError(f"{url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ConnectionError(f"{url}: timed out after {timeout}s") from exc


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc


# --------------------------------------------------------------------------- misc


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """Remove terminal escape sequences from captured output (twine is colourful)."""
    return _ANSI.sub("", text)


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def human(seconds: float) -> str:
    return f"{seconds:.1f}s" if seconds < 60 else f"{seconds // 60:.0f}m{seconds % 60:02.0f}s"
