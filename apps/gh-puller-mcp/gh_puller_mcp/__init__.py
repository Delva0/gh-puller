"""gh_puller_mcp: a Python 1:1 re-implementation of the codebase-memory-mcp MCP server.

The MCP surface (15 tools, prompts, tool profiles) is reproduced verbatim from
src/mcp/mcp.c of codebase-memory-mcp v0.10.8; every tool call is delegated to the
binary's only CLI interface: ``codebase-memory-mcp cli --json <tool>``. Wire and
protocol machinery (stdio framing, JSON-RPC) come from the official `mcp` SDK.
"""

from __future__ import annotations

from gh_puller_mcp import manifest
from gh_puller_mcp.backend import Backend, BackendConfig, BackendError
from gh_puller_mcp.server import ServerConfig, build_server, run_server

__version__ = "0.2.0"

__all__ = [
    "Backend",
    "BackendConfig",
    "BackendError",
    "ServerConfig",
    "__version__",
    "build_server",
    "manifest",
    "run_server",
]
