"""Expose persistent CBM query backends through one synchronous Python facade.

Current-project tools use the authenticated MCP executable. Tools supported by
the compact native runtime operate on an explicitly loaded archive generation,
while open-ended arguments keep new server fields independent of SDK releases.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from .binary import CBMBinary, resolve_cbm_binary
from .cbm_native import NativeArchiveTransport, NativeHelper
from .cbm_transport import CBMTransportError, PersistentMCPTransport

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

    from .archive import Archive


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
    """A synchronous facade over native archive and current-project CBM tools."""

    def __init__(
        self,
        binary: CBMBinary | str | Path | None = None,
        *,
        manifest: str | Path | None = None,
        registry: str | Path | None = None,
        cache_root: str | Path | None = None,
        native_helper: NativeHelper | str | Path | None = None,
        timeout: float = 120,
        environment: Mapping[str, str] | None = None,
    ):
        """Resolve the selected backend and initialize its persistent process.

        Args:
            binary: Pinned binary identity, explicit executable, or command name.
                ``None`` uses the normal accepted-binary resolution order.
            manifest: Accepted binary manifest used when ``binary`` is absent.
            registry: Registry root used for default manifest resolution.
            cache_root: CBM database directory. ``None`` honors ``CBM_CACHE_DIR``
                and then uses CBM's per-user default.
            native_helper: Helper for archive-backed tools. An explicit value
                selects native-only startup and defers MCP until an MCP tool is
                requested. ``None`` preserves eager MCP startup.
            timeout: Maximum seconds for each backend request or cache lock.
            environment: Environment overrides passed to CBM and used during
                executable and cache resolution.
        """
        if timeout <= 0:
            raise ValueError("CBM timeout must be positive")
        overrides = dict(environment or {})
        values = {**os.environ, **overrides}
        resolved_cache = (
            Path(cache_root).expanduser() if cache_root is not None else default_cbm_cache(values)
        ).resolve()
        self.cache_root = resolved_cache
        self.timeout = timeout
        self._overrides = overrides
        self._values = values
        self._binary_input = binary
        self._manifest = manifest
        self._registry = registry
        self._binary_identity = binary if isinstance(binary, CBMBinary) else None
        self._native_helper = native_helper
        self._monitor = _ClientMonitor()
        self._transport: PersistentMCPTransport | None = None
        self._native_transport: NativeArchiveTransport | None = None
        if native_helper is None:
            self._mcp()

    @property
    def binary(self) -> CBMBinary:
        """Return the resolved CBM executable used by the MCP fallback."""
        if self._binary_identity is None:
            self._binary_identity = resolve_cbm_binary(
                self._binary_input,
                manifest=self._manifest,
                registry=self._registry,
                environ=self._values,
            )
        self._binary_identity.verify_unchanged()
        return self._binary_identity

    def _mcp(self) -> PersistentMCPTransport:
        if self._transport is None:
            self._transport = PersistentMCPTransport(
                self.binary.path,
                self.cache_root,
                self.timeout,
                self._monitor,
                self._overrides,
            )
        return self._transport

    def _native(self) -> NativeArchiveTransport:
        if self._native_transport is None:
            self._native_transport = NativeArchiveTransport(
                self._native_helper,
                self.cache_root,
                self.timeout,
                self._overrides,
            )
        return self._native_transport

    @property
    def pid(self) -> int:
        """Return the persistent MCP frontend process ID."""
        return self._mcp().process.pid

    @property
    def native_pid(self) -> int:
        """Return the persistent native archive helper process ID."""
        return self._native().pid

    @property
    def instructions(self) -> str:
        """Return optional query guidance advertised by the CBM server."""
        return self._mcp().instructions

    def list_tools(self) -> list[dict[str, Any]]:
        """Return all tool definitions advertised by the live CBM server."""
        return self._mcp().list_tools()

    def call_tool(self, name: str, arguments: Mapping[str, object] | None = None) -> dict[str, Any]:
        """Call any CBM tool and return a backend-neutral result envelope.

        Args:
            name: Advertised MCP tool name.
            arguments: Tool-specific arguments. ``None`` sends an empty object.
        """
        values = dict(arguments or {})
        native = self._native_transport
        if native is not None and values.get("project") == native.loaded_project:
            if name not in native.tools:
                raise CBMTransportError(f"native CBM tool is not supported for the loaded archive: {name}")
            logical = native.call_tool(name, values)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(logical, ensure_ascii=False, separators=(",", ":")),
                    },
                ],
                "structuredContent": logical,
                "isError": False,
            }
        return self._mcp().call_tool(name, values)

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
        return self.call_json_tool("query_graph", {"project": project, "query": query, **options, "format": "json"})

    def load_archive(
        self,
        archive: str | Path | Archive,
        commit: str | None = None,
        *,
        allow_incomplete: bool = True,
    ) -> dict[str, Any]:
        """Load one KGA snapshot into the native query engine.

        Args:
            archive: KGA path or an already captured :class:`Archive` reader.
            commit: Exact archived commit. ``None`` selects the captured latest.
            allow_incomplete: For path inputs, accept the writer's final durable
                checkpoint while the archive is still being built.

        Returns:
            Loaded graph identity, row counts, cache path, and whether the store
            was materialized during this call.
        """
        return self._native().load_archive(archive, commit, allow_incomplete=allow_incomplete)

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
        """Finish active native and MCP processes and release their pipes."""
        if self._native_transport is not None:
            self._native_transport.close()
        if self._transport is not None:
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
