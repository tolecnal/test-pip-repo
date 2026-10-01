"""pipcheck: an end-to-end test harness for an internal devpi pip repository.

Deliberately stdlib-only, so the harness itself never depends on the repository it
is testing. Third-party tooling (build, twine, optionally devpi-client) lives in a
dedicated venv under .venvs/tooling.
"""

import sys

# Ruff sees this as dead code because the project targets 3.11; it exists precisely to
# greet someone whose interpreter is older with a sentence instead of a traceback.
if sys.version_info < (3, 11):  # ruff: ignore[outdated-version-block]  -- tomllib, used to read pipcheck.toml
    raise SystemExit(
        "pipcheck needs Python 3.11 or newer; this is "
        f"{sys.version.split()[0]} ({sys.executable}).\n"
        "Point it at a newer interpreter: PIPCHECK_PYTHON=python3.11 ./pipcheck ...\n"
        "(The Python used for the install tests is separate -- set `python` in "
        "pipcheck.toml to test against an older one.)"
    )

__all__ = ["__version__"]

__version__ = "1.0.0"
