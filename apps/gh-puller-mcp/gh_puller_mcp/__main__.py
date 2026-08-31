"""Run the gh_puller_mcp MCP server on stdio.

    python -m gh_puller_mcp [--tool-profile analysis|scout] [--binary PATH]
                         [--debug] [--timeout SEC]

Exit codes: 0 on clean EOF (SDK stdio loop), 2 on bad flags.
"""

from __future__ import annotations

import argparse
import sys

from gh_puller_mcp import __version__
from gh_puller_mcp.backend import Backend, BackendConfig
from gh_puller_mcp.server import ServerConfig, run_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gh_puller_mcp", description="codebase-memory-mcp MCP server (Python re-implementation)",
    )
    parser.add_argument(
        "--tool-profile",
        choices=("analysis", "scout"),
        default=None,
        help="restrict the exposed tool surface (default: all 15 tools)",
    )
    parser.add_argument(
        "--binary", metavar="PATH", default=None, help="codebase-memory-mcp binary (default: resolved)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="forward backend stderr to stderr",
    )
    parser.add_argument(
        "--timeout", type=float, default=None, metavar="SEC", help="per-tool-call timeout (default: none)",
    )
    parser.add_argument("--version", action="version", version=f"gh_puller_mcp {__version__}")
    args = parser.parse_args(argv)

    config = ServerConfig(
        profile=args.tool_profile or "all",
        backend=Backend(BackendConfig(binary=args.binary, timeout=args.timeout, debug=args.debug)),
    )
    run_server(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
