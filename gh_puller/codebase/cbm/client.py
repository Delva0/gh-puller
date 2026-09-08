"""Implement the synchronous client exported by the CBM package facade.

Build and daemon tools use a selectable MCP or CLI backend. Archive tools use
the compact native runtime, while open-ended arguments keep new server fields
independent of SDK releases. Backend process and protocol mechanics remain in
private sibling modules.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from ._daemon import (
    CBMTransportError,
    CLITransport,
    PersistentMCPTransport,
    ResourceMonitorLike,
    make_transport,
)
from ._native import NativeArchiveTransport, NativeHelper
from .binary import CBMBinary, resolve_cbm_binary

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import TracebackType

    from ..archive import Archive


class _ClientMonitor:
    child_pid: int | None = None
    exceeded = False

    def sample(self) -> None:
        return


@dataclass(frozen=True, slots=True)
class GraphTarget:
    """Identify a mutable graph project served by the configured daemon backend."""

    project: str


@dataclass(frozen=True, slots=True)
class ArchiveGraph(GraphTarget):
    """Identify one immutable KGA generation loaded by the native backend.

    A handle becomes stale when its client loads a different archive generation.
    """

    graph_digest: str
    materialization_digest: str
    database_path: Path
    nodes: int
    edges: int
    graph_fidelity: int
    coverage_rows: int
    coverage_fidelity: int
    materialized: bool


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
    """Route CBM operations to native, persistent MCP, or one-shot CLI backends."""

    def __init__(
        self,
        binary: CBMBinary | str | Path | None = None,
        *,
        manifest: str | Path | None = None,
        registry: str | Path | None = None,
        cache_root: str | Path | None = None,
        native_helper: NativeHelper | str | Path | None = None,
        daemon_transport: str = "persistent-mcp",
        timeout: float = 120,
        environment: Mapping[str, str] | None = None,
        resource_monitor: ResourceMonitorLike | None = None,
    ):
        """Configure lazily initialized backends and their shared runtime state.

        Args:
            binary: Pinned binary identity, explicit executable, or command name.
                ``None`` uses the normal accepted-binary resolution order.
            manifest: Accepted binary manifest used when ``binary`` is absent.
            registry: Registry root used for default manifest resolution.
            cache_root: CBM database directory. ``None`` honors ``CBM_CACHE_DIR``
                and then uses CBM's per-user default.
            native_helper: Helper for archive-backed tools. ``None`` uses the
                normal environment, local-build, and ``PATH`` resolution order.
            daemon_transport: Persistent MCP or one-process-per-call CLI backend
                used by daemon operations.
            timeout: Maximum seconds for each backend request or cache lock.
            environment: Environment overrides passed to CBM and used during
                executable and cache resolution.
            resource_monitor: Optional build-owned process and memory monitor.
                Omission uses a no-op monitor suitable for direct SDK calls.
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
        self.daemon_transport = daemon_transport
        self._monitor = resource_monitor or _ClientMonitor()
        self._backend_lock = threading.Lock()
        self._daemon_backend: CLITransport | PersistentMCPTransport | None = None
        self._native_transport: NativeArchiveTransport | None = None

    @property
    def binary(self) -> CBMBinary:
        """Return the authenticated executable used by daemon operations."""
        if self._binary_identity is None:
            self._binary_identity = resolve_cbm_binary(
                self._binary_input,
                manifest=self._manifest,
                registry=self._registry,
                environ=self._values,
            )
        self._binary_identity.verify_unchanged()
        return self._binary_identity

    def _daemon(self) -> CLITransport | PersistentMCPTransport:
        with self._backend_lock:
            binary = self.binary
            if self._daemon_backend is None:
                self._daemon_backend = make_transport(
                    self.daemon_transport,
                    binary.path,
                    self.cache_root,
                    self.timeout,
                    self._monitor,
                    self._overrides,
                )
        return self._daemon_backend

    def _mcp(self) -> PersistentMCPTransport:
        backend = self._daemon()
        if not isinstance(backend, PersistentMCPTransport):
            raise CBMTransportError("server metadata requires the persistent-mcp backend")
        return backend

    def _native(self) -> NativeArchiveTransport:
        with self._backend_lock:
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

    def daemon_graph(self, project: str) -> GraphTarget:
        """Bind a project to the configured daemon backend.

        Args:
            project: Exact mutable CBM project name.
        """
        return GraphTarget(project)

    def capabilities(self) -> frozenset[str]:
        """Return build capabilities advertised through the daemon backend."""
        return self._daemon().capabilities()

    def index_repository(
        self,
        tree: str | Path,
        project: str,
        mode: str,
        *,
        force_full: bool = False,
        incremental_controls: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any]:
        """Publish one repository graph through the selected daemon backend.

        Args:
            tree: Materialized repository tree to analyze.
            project: Stable CBM project receiving the generation.
            mode: CBM analysis coverage mode.
            force_full: Require CBM to confirm a full-build route.
            incremental_controls: Delta-policy overrides whose acknowledgement
                must be confirmed by CBM.

        Returns:
            Machine-readable route evidence reported by CBM.
        """
        return self._daemon().index(
            Path(tree),
            project,
            mode,
            force_full=force_full,
            incremental_controls=incremental_controls,
        )

    def delete_project(self, project: str) -> tuple[bool, str]:
        """Delete one daemon-owned CBM project.

        Args:
            project: Exact CBM project name to delete.
        """
        return self._daemon().delete_project(project)

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        target: GraphTarget | None = None,
    ) -> dict[str, Any]:
        """Call any CBM tool and return a backend-neutral result envelope.

        Args:
            name: Advertised MCP tool name.
            arguments: Tool-specific arguments. ``None`` sends an empty object.
            target: Explicit graph binding. Archive handles select native;
                daemon handles and omission select the configured daemon backend.
        """
        values = dict(arguments or {})
        if target is not None:
            supplied_project = values.get("project")
            if supplied_project is not None and supplied_project != target.project:
                raise CBMTransportError("CBM graph target disagrees with the tool project")
            values["project"] = target.project
        if isinstance(target, ArchiveGraph):
            native = self._native_transport
            if (
                native is None
                or native.loaded_project != target.project
                or native.loaded_materialization_digest != target.materialization_digest
            ):
                raise CBMTransportError("archive graph is no longer loaded by this CBM client")
            if name not in native.tools:
                raise CBMTransportError(f"native CBM tool is not supported for this archive graph: {name}")
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
        return self._daemon().call_tool(name, values)

    def call_json_tool(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        target: GraphTarget | None = None,
    ) -> dict[str, Any]:
        """Call a tool whose logical response is a JSON object.

        Args:
            name: Advertised MCP tool name.
            arguments: Tool-specific arguments. ``None`` sends an empty object.
            target: Optional explicit graph/backend binding.

        Returns:
            Parsed ``structuredContent``, with a text-content fallback for older
            CBM binaries.

        Raises:
            CBMTransportError: The call fails or its response is not a JSON object.
        """
        return _json_object(name, self.call_tool(name, arguments, target=target))

    def search_graph(self, target: GraphTarget, **filters: object) -> dict[str, Any]:
        """Run CBM structured, BM25, or semantic graph search.

        Args:
            target: Graph and backend selected by :meth:`daemon_graph` or
                :meth:`load_archive`.
            **filters: Native ``search_graph`` fields such as ``query``, ``label``,
                ``name_pattern``, ``limit``, and ``offset``.
        """
        return self.call_json_tool(
            "search_graph",
            {**filters, "format": "json"},
            target=target,
        )

    def query_graph(self, target: GraphTarget, *, query: str, **options: object) -> dict[str, Any]:
        """Run a read-only Cypher-like query against a CBM graph.

        Args:
            target: Graph and backend selected by :meth:`daemon_graph` or
                :meth:`load_archive`.
            query: Native CBM graph query.
            **options: Additional ``query_graph`` fields such as ``graph`` and
                ``max_rows``.
        """
        return self.call_json_tool(
            "query_graph",
            {"query": query, **options, "format": "json"},
            target=target,
        )

    def get_graph_schema(self, target: GraphTarget) -> dict[str, Any]:
        """Return labels, relationship types, and their available properties.

        Args:
            target: Graph and backend selected by :meth:`daemon_graph` or
                :meth:`load_archive`.
        """
        return self.call_json_tool("get_graph_schema", target=target)

    def load_archive(
        self,
        archive: str | Path | Archive,
        commit: str | None = None,
        *,
        allow_incomplete: bool = True,
    ) -> ArchiveGraph:
        """Load one KGA snapshot into the native query engine.

        Args:
            archive: KGA path or an already captured :class:`Archive` reader.
            commit: Exact archived commit. ``None`` selects the captured latest.
            allow_incomplete: For path inputs, accept the writer's final durable
                checkpoint while the archive is still being built.

        Returns:
            Immutable native graph handle containing its identity, row counts,
            cache path, and materialization status.
        """
        result = self._native().load_archive(archive, commit, allow_incomplete=allow_incomplete)
        return ArchiveGraph(
            project=result["project"],
            graph_digest=result["graph_digest"],
            materialization_digest=result["materialization_digest"],
            database_path=Path(result["database_path"]),
            nodes=result["nodes"],
            edges=result["edges"],
            graph_fidelity=result["graph_fidelity"],
            coverage_rows=result["coverage_rows"],
            coverage_fidelity=result["coverage_fidelity"],
            materialized=result["materialized"],
        )

    def trace_path(
        self,
        target: GraphTarget,
        *,
        function_name: str,
        **options: object,
    ) -> dict[str, Any]:
        """Trace calls, data flow, or cross-service paths from one symbol.

        Args:
            target: Graph and backend selected by :meth:`daemon_graph` or
                :meth:`load_archive`.
            function_name: Qualified or discoverable function name used as the
                traversal origin.
            **options: Native ``trace_path`` fields such as ``direction``, ``depth``,
                ``mode``, ``limit``, and ``cursor``.
        """
        return self.call_json_tool(
            "trace_path",
            {"function_name": function_name, **options, "format": "json"},
            target=target,
        )

    def get_architecture(
        self,
        target: GraphTarget,
        *,
        path: str | None = None,
        aspects: Sequence[str] | None = None,
        **options: object,
    ) -> dict[str, Any]:
        """Summarize graph structure, dependencies, and architectural views.

        Args:
            target: Graph and backend selected by :meth:`daemon_graph` or
                :meth:`load_archive`.
            path: Optional repository-relative directory scope.
            aspects: Optional CBM architecture sections. ``None`` selects the
                compact default view.
            **options: Additional ``get_architecture`` fields supported by CBM.
        """
        arguments = dict(options)
        if path is not None:
            arguments["path"] = path
        if aspects is not None:
            arguments["aspects"] = list(aspects)
        arguments["format"] = "json"
        return self.call_json_tool("get_architecture", arguments, target=target)

    def check_index_coverage(
        self,
        target: GraphTarget,
        *,
        paths: Sequence[str] = (),
        scopes: Sequence[str] = (),
        scope_limit: int = 200,
        scope_offset: int = 0,
    ) -> dict[str, Any]:
        """Inspect CBM's best-effort coverage record for paths or scopes.

        Args:
            target: Graph and backend selected by :meth:`daemon_graph` or
                :meth:`load_archive`.
            paths: Repository-relative files to check exactly.
            scopes: Repository-relative path prefixes to enumerate.
            scope_limit: Maximum coverage rows returned for each scope.
            scope_offset: Starting row offset for each scope.
        """
        return self.call_json_tool(
            "check_index_coverage",
            {
                "paths": list(paths),
                "scopes": list(scopes),
                "scope_limit": scope_limit,
                "scope_offset": scope_offset,
            },
            target=target,
        )

    def index_status(
        self,
        target: GraphTarget,
        *,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """Return graph counts, root identity, and persisted coverage status.

        Args:
            target: Graph and backend selected by :meth:`daemon_graph` or
                :meth:`load_archive`.
            verbose: Include live Git/worktree context for daemon graphs.
                Archive graphs contain no live worktree context.

        Raises:
            CBMTransportError: ``verbose`` is requested for an archive graph.
        """
        if verbose and isinstance(target, ArchiveGraph):
            raise CBMTransportError("verbose index status requires a daemon-backed graph")
        return self.call_json_tool(
            "index_status",
            {"verbose": verbose},
            target=target,
        )

    def compare_graphs(
        self,
        base: GraphTarget,
        target: GraphTarget,
        *,
        limit: int = 200,
        scan_limit: int = 2_000_000,
    ) -> dict[str, Any]:
        """Compare stable node and edge identities across two graph generations.

        Args:
            base: Older daemon project or materialized archive generation.
            target: Newer graph from the same backend kind.
            limit: Maximum returned entries per change set.
            scan_limit: Maximum combined rows scanned per node or edge phase.

        Raises:
            CBMTransportError: Archive and daemon graph kinds are mixed.
        """
        if isinstance(base, ArchiveGraph) and isinstance(target, ArchiveGraph):
            return self._native().compare_graphs(
                base_database=base.database_path,
                base_project=base.project,
                target_database=target.database_path,
                target_project=target.project,
                limit=limit,
                scan_limit=scan_limit,
            )
        if isinstance(base, ArchiveGraph) or isinstance(target, ArchiveGraph):
            raise CBMTransportError("cannot compare archive and daemon graphs")
        return self.call_json_tool(
            "compare_graphs",
            {
                "base_project": base.project,
                "target_project": target.project,
                "limit": limit,
                "scan_limit": scan_limit,
            },
        )

    def close(self) -> None:
        """Finish active native and daemon processes and release their pipes."""
        if self._native_transport is not None:
            self._native_transport.close()
        if self._daemon_backend is not None:
            self._daemon_backend.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
