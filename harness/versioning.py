"""Read and bump the test package's version in pkg/pyproject.toml.

pyproject.toml is the single source of truth; _build_info.py is regenerated from it
at build time.
"""

from __future__ import annotations

import re
import tomllib

from .config import PYPROJECT

PART_NAMES = ("major", "minor", "patch", "dev", "post")
_VERSION_LINE = re.compile(
    r'^(?P<prefix>version\s*=\s*")(?P<version>[^"]+)(?P<suffix>")', re.MULTILINE
)
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\.(?:dev|post)(\d+))?$")


def read() -> str:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def write(version: str) -> str:
    """Rewrite the version line in place, leaving the rest of the file untouched."""
    validate(version)
    text = PYPROJECT.read_text(encoding="utf-8")
    new_text, count = _VERSION_LINE.subn(
        lambda m: f"{m['prefix']}{version}{m['suffix']}", text, count=1
    )
    if count != 1:
        raise SystemExit(f"could not find a single version line in {PYPROJECT}")
    PYPROJECT.write_text(new_text, encoding="utf-8")
    return version


def validate(version: str) -> str:
    # Permissive: any PEP 440-ish release string, since users may set one by hand.
    if not re.fullmatch(r"[0-9][0-9a-zA-Z.\-_+!]*", version):
        raise SystemExit(f"{version!r} does not look like a PEP 440 version")
    return version


def bump(part: str, current: str | None = None) -> str:
    """Return `current` incremented by `part`. Does not write anything."""
    if part not in PART_NAMES:
        raise SystemExit(f"cannot bump {part!r}; choose one of {', '.join(PART_NAMES)}")
    current = current or read()
    match = _SEMVER.match(current)
    if not match:
        raise SystemExit(
            f"version {current!r} is not major.minor.patch[.devN|.postN]; "
            "set an explicit version instead (pipcheck set-version X.Y.Z)"
        )
    major, minor, patch = (int(match.group(i)) for i in (1, 2, 3))
    serial = int(match.group(4) or 0)

    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        # A pre/post-release bumping to "patch" settles onto its own release number.
        if match.group(4) is not None:
            return f"{major}.{minor}.{patch}"
        return f"{major}.{minor}.{patch + 1}"
    if part == "dev":
        return f"{major}.{minor}.{patch}.dev{serial + 1}"
    return f"{major}.{minor}.{patch}.post{serial + 1}"
