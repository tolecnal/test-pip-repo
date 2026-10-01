"""Persistent record of what this checkout has built and uploaded.

Lets `verify` run against the last build without rebuilding, and lets the suite
check that *older* versions are still resolvable from the index.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .config import STATE_FILE


@dataclass
class Build:
    """One build of the test package, and whether it reached an index."""

    version: str
    build_id: str
    built_at: str
    files: list[str] = field(default_factory=list)  # local paths in dist/
    sha256: dict[str, str] = field(default_factory=dict)  # filename -> digest
    uploaded: bool = False
    index: str = ""


@dataclass
class State:
    """What this checkout has built so far, newest last."""

    builds: list[Build] = field(default_factory=list)

    # ------------------------------------------------------------------- lookups
    @property
    def last(self) -> Build | None:
        return self.builds[-1] if self.builds else None

    @property
    def uploaded(self) -> list[Build]:
        return [b for b in self.builds if b.uploaded]

    def uploaded_to(self, index: str) -> list[Build]:
        return [b for b in self.builds if b.uploaded and b.index == index]

    def find(self, version: str) -> Build | None:
        for build in reversed(self.builds):
            if build.version == version:
                return build
        return None

    # --------------------------------------------------------------------- io
    def record(self, build: Build) -> Build:
        existing = self.find(build.version)
        if existing:
            self.builds.remove(existing)
        self.builds.append(build)
        self.save()
        return build

    def save(self) -> None:
        STATE_FILE.write_text(
            json.dumps({"builds": [asdict(b) for b in self.builds]}, indent=2) + "\n"
        )


def load() -> State:
    if not STATE_FILE.is_file():
        return State()
    try:
        raw = json.loads(STATE_FILE.read_text() or "{}")
    except json.JSONDecodeError:
        return State()
    return State(builds=[Build(**b) for b in raw.get("builds", [])])
