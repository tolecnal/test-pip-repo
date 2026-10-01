"""Console entry point: `devpi-smoke`."""

from __future__ import annotations

import argparse
import json
import sys

from . import build_info, hello


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="devpi-smoke", description="Print a greeting, or this build's provenance."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable build provenance (used by the pipcheck harness)",
    )
    parser.add_argument("--who", default="world", help="who to greet")
    args = parser.parse_args(argv)

    if args.json:
        json.dump(build_info(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(hello(args.who))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
