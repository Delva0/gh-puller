"""Start a persistent CBM frontend and expose its query tools to Python.

The client resolves the same immutable CBM executable used by archive builds and
owns one stdio MCP session.  It deliberately keeps tool arguments open-ended so
new CBM schema fields do not require an SDK release, while the common graph-query
methods request machine-readable responses.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from .binary import CBMBinary, resolve_cbm_binary
from .cbm_transport import CBMTransportError, PersistentMCPTransport

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType


class _ClientMonitor:
    child_pid: int | None = None
    exceeded = False

    def sample(self) -> None:
        return


def default_cbm_cache(environ: Mapping[str, str] | None = None) -> Path:
    """Return the cache directory shared with CBM.

    Args:
        environ: Environment used to resolve ``CBM_CACHE_DIR``. ``None`` uses
            the process environment.
    """
    values = os.environ if environ is None else environ
    configured = values.get("CBM_CACHE_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".cache" / "codebase-memory-mcp"


def _json_object(name: str, result: dict[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    if structured is not None:
        raise CBMTransportError(f"CBM tool {name} returned non-object structured content")

    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                continue
            try:
                decoded = json.loads(block["text"])
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
    raise CBMTransportError(f"CBM tool {name} did not return a JSON object")


class CBMClient:
    """A synchronous, thread-safe client backed by one persistent MCP frontend."""

    def __init__(
        self,
        binary: CBMBinary | str | Path | None = None,
        *,
        manifest: str | Path | None = None,
        registry: str | Path | None = None,
        cache_root: str | Path | None = None,
        timeout: float = 120,
        environment: Mapping[str, str] | None = None,
    ):
        """Resolve CBM and complete MCP initialization before returning.

        Args:
            binary: Pinned binary identity, explicit executable, or command name.
                ``None`` uses the normal accepted-binary resolution order.
            manifest: Accepted binary manifest used when ``binary`` is absent.
            registry: Registry root used for default manifest resolution.
            cache_root: CBM database directory. ``None`` honors ``CBM_CACHE_DIR``
                and then uses CBM's per-user default.
            timeout: Maximum seconds for each MCP request.
            environment: Environment overrides passed to CBM and used during
                executable and cache resolution.
        """
        if timeout <= 0:
            raise ValueError("CBM timeout must be positive")
        overrides = dict(environment or {})
        values = {**os.environ, **overrides}
        identity = (
            binary
            if isinstance(binary, CBMBinary)
            else resolve_cbm_binary(binary, manifest=manifest, registry=registry, environ=values)
        )
        identity.verify_unchanged()
        resolved_cache = (
            Path(cache_root).expanduser() if cache_root is not None else default_cbm_cache(values)
        ).resolve()
        monitor = _ClientMonitor()
        self.binary = identity
        self.cache_root = resolved_cache
        self._monitor = monitor
        self._transport = PersistentMCPTransport(
            identity.path,
            resolved_cache,
            timeout,
            monitor,
            overrides,
        )

    @property
    def pid(self) -> int:
        """Return the persistent MCP frontend process ID."""
        return self._transport.process.pid

    @property
    def instructions(self) -> str:
        """Return optional query guidance advertised by the CBM server."""
        return self._transport.instructions

    def list_tools(self) -> list[dict[str, Any]]:
        """Return all tool definitions advertised by the live CBM server."""
        return self._transport.list_tools()

    def call_tool(self, name: str, arguments: Mapping[str, object] | None = None) -> dict[str, Any]:
        """Call any CBM tool and return its raw MCP result envelope.

        Args:
            name: Advertised MCP tool name.
            arguments: Tool-specific arguments. ``None`` sends an empty object.
        """
        return self._transport.call_tool(name, dict(arguments or {}))

    def call_json_tool(self, name: str, arguments: Mapping[str, object] | None = None) -> dict[str, Any]:
        """Call a tool whose logical response is a JSON object.

        Args:
            name: Advertised MCP tool name.
            arguments: Tool-specific arguments. ``None`` sends an empty object.

        Returns:
            Parsed ``structuredContent``, with a text-content fallback for older
            CBM binaries.

        Raises:
            CBMTransportError: The call fails or its response is not a JSON object.
        """
        return _json_object(name, self.call_tool(name, arguments))

    def search_graph(self, *, project: str, **filters: object) -> dict[str, Any]:
        """Run CBM structured, BM25, or semantic graph search.

        Args:
            project: CBM project name to query.
            **filters: Native ``search_graph`` fields such as ``query``, ``label``,
                ``name_pattern``, ``limit``, and ``offset``.
        """
        return self.call_json_tool("search_graph", {"project": project, **filters, "format": "json"})

    def query_graph(self, *, project: str, query: str, **options: object) -> dict[str, Any]:
        """Run a read-only Cypher-like query against a CBM graph.

        Args:
            project: CBM project name to query.
            query: Native CBM graph query.
            **options: Additional ``query_graph`` fields such as ``graph`` and
                ``max_rows``.
        """
        return self.call_json_tool(
            "query_graph",
            {"project": project, "query": query, **options, "format": "json"},
        )

    def trace_path(self, *, project: str, function_name: str, **options: object) -> dict[str, Any]:
        """Trace calls, data flow, or cross-service paths from one symbol.

        Args:
            project: CBM project name to query.
            function_name: Qualified or discoverable function name used as the
                traversal origin.
            **options: Native ``trace_path`` fields such as ``direction``, ``depth``,
                ``mode``, ``limit``, and ``cursor``.
        """
        return self.call_json_tool(
            "trace_path",
            {"project": project, "function_name": function_name, **options, "format": "json"},
        )

    def close(self) -> None:
        """Finish or terminate the frontend and release its pipes."""
        self._transport.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
