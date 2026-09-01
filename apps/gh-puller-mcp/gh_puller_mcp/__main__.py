"""Run the gh_puller_mcp MCP server on stdio (default) or Streamable HTTP.

    python -m gh_puller_mcp [--tool-profile analysis|scout] [--binary PATH]
                         [--debug] [--timeout SEC]
    python -m gh_puller_mcp --http [--host HOST] [--port PORT] [--path PATH] [...]

HTTP mode: stateless single endpoint (JSON-RPC tools/list & tools/call),
plain-JSON responses, no initialize handshake required.

Exit codes: 0 on clean EOF (SDK stdio loop), 2 on bad flags. HTTP mode shuts
down gracefully on SIGINT/SIGTERM but exits with that signal's status (uvicorn
re-raises it after shutdown).
"""

from __future__ import annotations

import argparse
import sys

from gh_puller_mcp import __version__
from gh_puller_mcp.backend import Backend, BackendConfig
from gh_puller_mcp.server import ServerConfig, run_server, run_server_http


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
    parser.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address (0.0.0.0 for cross-machine)")
    parser.add_argument("--port", type=int, default=8787, help="HTTP port (default: 8787)")
    parser.add_argument("--path", default="/mcp", help="HTTP endpoint path (default: /mcp)")
    parser.add_argument("--version", action="version", version=f"gh_puller_mcp {__version__}")
    args = parser.parse_args(argv)

    config = ServerConfig(
        profile=args.tool_profile or "all",
        backend=Backend(BackendConfig(binary=args.binary, timeout=args.timeout, debug=args.debug)),
    )
    if args.http:
        run_server_http(config, host=args.host, port=args.port, path=args.path)
    else:
        run_server(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
