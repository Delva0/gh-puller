"""Run code graph archive commands."""

from __future__ import annotations

import argparse

from .build import add_build_arguments, run_from_namespace


def main() -> int:
    """Dispatch the code graph archive CLI."""
    parser = argparse.ArgumentParser(prog="uv run -m gh_puller.codebase")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="build or resume a code graph archive")
    add_build_arguments(build)
    args = parser.parse_args()
    if args.command == "build":
        return run_from_namespace(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
