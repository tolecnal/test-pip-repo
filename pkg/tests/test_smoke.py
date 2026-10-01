"""Tests for the installed devpi-smoke package.

The harness runs these inside a venv that installed devpi-smoke *from the internal
index*, so they assert against whatever the repository actually served.
"""

from __future__ import annotations

import json
import subprocess
import sys

import devpi_smoke


def test_hello_mentions_version():
    greeting = devpi_smoke.hello()
    assert greeting.startswith("hello, world!")
    assert devpi_smoke.__version__ in greeting


def test_hello_takes_a_name():
    assert devpi_smoke.hello("devpi").startswith("hello, devpi!")


def test_transitive_dependency_is_importable():
    import six

    assert six.__version__


def test_build_info_is_self_consistent():
    info = devpi_smoke.build_info()
    assert info["build_id"], "package was built without a build stamp"
    assert info["source_version"] == info["dist_version"], (
        "stamped source version %r disagrees with installed metadata version %r"
        % (info["source_version"], info["dist_version"])
    )
    assert any(req.startswith("six") for req in info["dist_requires"])


def test_console_script_json_output():
    out = subprocess.run(
        [sys.executable, "-m", "devpi_smoke.cli", "--json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert json.loads(out)["source_version"] == devpi_smoke.__version__
