"""Implement the transport-neutral synchronous CBM client.

Public operations follow CBM SDK concepts. MCP, CLI, and native transports are
peer routes selected globally or per API, while opaque graph handles
retain their route without exposing its process protocol.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from ._transport import (
    CBMTransportError,
    OpenedGraph,
    ResourceMonitorLike,
    Transport,
    TransportName,
    create_transport,
    parse_transport_name,
)
from .binary import CBMBinary, resolve_cbm_binary

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import TracebackType

    from ._native import NativeHelper


class _ClientMonitor:
    """Discard process observations for clients without resource limits."""

    exceeded = False

    def add_child(self, pid: int) -> None:
        return

    def remove_child(self, pid: int) -> None:
        return

    def sample(self) -> None:
        return


@dataclass(frozen=True, slots=True)
class GraphHandle:
    """Bind one CBM graph to the transport that opened it."""

    project: str
    transport: TransportName
    source_root: Path | None
    nodes: int | None
    edges: int | None
    _transport: Transport = field(repr=False, compare=False)
    _binding: object = field(repr=False, compare=False)


def default_cbm_cache(environ: Mapping[str, str] | None = None) -> Path:
    """Return the cache directory shared with CBM.

    Args:
        environ: Environment used to resolve ``CBM_CACHE_DIR``. ``None`` uses
            the process environment.
    """
    values = os.environ if environ is None else environ
    configured = values.get("CBM_CACHE_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".cache" / "codebase-memory-mcp"


def _project_database(cache_root: Path, project: str) -> Path:
    if (
        not project
        or project.startswith(".")
        or ".." in project
        or any(not (character.isascii() and (character.isalnum() or character in "-_.")) for character in project)
    ):
        raise ValueError(f"invalid CBM project name: {project!r}")
    return cache_root / f"{project}.db"


class CBMClient:
    """Route CBM SDK operations through interchangeable peer transports."""

    def __init__(
        self,
        binary: CBMBinary | str | Path | None = None,
        *,
        manifest: str | Path | None = None,
        registry: str | Path | None = None,
        cache_root: str | Path | None = None,
        native_helper: NativeHelper | str | Path | None = None,
        native_index_helper: NativeHelper | str | Path | None = None,
        transport: str = "native",
        routes: Mapping[str, str] | None = None,
        timeout: float = 120,
        environment: Mapping[str, str] | None = None,
        resource_monitor: ResourceMonitorLike | None = None,
    ):
        """Configure lazily initialized transports and their shared runtime state.

        Args:
            binary: Pinned binary identity, explicit executable, or command name.
                ``None`` uses the normal accepted-binary resolution order.
            manifest: Accepted binary manifest used when ``binary`` is absent.
            registry: Registry root used for default manifest resolution.
            cache_root: CBM database directory. ``None`` honors ``CBM_CACHE_DIR``
                and then uses CBM's per-user default.
            native_helper: Compact helper for native graph tools. ``None`` uses
                the normal environment, local-build, and ``PATH`` resolution order.
            native_index_helper: Full SDK helper for indexing. ``None`` uses its
                independent environment, local-build, and ``PATH`` resolution order.
            transport: Default native, MCP, or CLI route for every operation.
            routes: Per-API route overrides keyed by public operation or tool name.
            timeout: Maximum seconds for each transport request or cache lock.
            environment: Environment overrides passed to CBM and used during
                executable and cache resolution.
            resource_monitor: Optional build-owned process and memory monitor.
                Omission uses a no-op monitor suitable for direct SDK calls.
        """
        if timeout <= 0:
            raise ValueError("CBM timeout must be positive")
        selected_transport = parse_transport_name(transport)
        selected_routes = {
            operation: parse_transport_name(route)
            for operation, route in dict(routes or {}).items()
        }
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
        self._native_index_helper = native_index_helper
        self.transport = selected_transport
        self.routes = selected_routes
        self._monitor = resource_monitor or _ClientMonitor()
        self._transport_lock = threading.Lock()
        self._transports: dict[TransportName, Transport] = {}

    @property
    def binary(self) -> CBMBinary:
        """Return the authenticated executable used by MCP and CLI."""
        if self._binary_identity is None:
            self._binary_identity = resolve_cbm_binary(
                self._binary_input,
                manifest=self._manifest,
                registry=self._registry,
                environ=self._values,
            )
        self._binary_identity.verify_unchanged()
        return self._binary_identity

    def _get_transport(self, name: TransportName) -> Transport:
        with self._transport_lock:
            if name not in self._transports:
                self._transports[name] = create_transport(
                    name,
                    resolve_binary=lambda: self.binary,
                    cache_root=self.cache_root,
                    timeout=self.timeout,
                    environment=self._overrides,
                    monitor=self._monitor,
                    native_helper=self._native_helper,
                    native_index_helper=self._native_index_helper,
                )
        return self._transports[name]

    def _select_transport(self, operation: str, transport: str | None = None) -> Transport:
        selected = (
            parse_transport_name(transport)
            if transport is not None
            else self.routes.get(operation, self.transport)
        )
        return self._get_transport(selected)

    @staticmethod
    def _graph(transport: Transport, opened: OpenedGraph) -> GraphHandle:
        return GraphHandle(
            opened.project,
            transport.name,
            opened.source_root,
            opened.nodes,
            opened.edges,
            transport,
            opened.binding,
        )

    def project_graph(
        self,
        project: str,
        *,
        source_root: str | Path | None = None,
        transport: str | None = None,
    ) -> GraphHandle:
        """Open or bind an indexed project's current generation.

        Args:
            project: Exact mutable CBM project name.
            source_root: Matching checkout for source-aware native tools.
            transport: Explicit route overriding this API's configured selection.

        Returns:
            A transport-neutral graph handle.
        """
        resolved_source = Path(source_root).resolve() if source_root is not None else None
        route = self._select_transport("project_graph", transport)
        return self._graph(route, route.open_project(project, resolved_source))

    def open_store(
        self,
        database_path: str | Path,
        project: str,
        *,
        source_root: str | Path | None = None,
        transport: str | None = None,
    ) -> GraphHandle:
        """Open an explicit CBM store through a selected SDK route.

        Args:
            database_path: Exact CBM database containing the graph.
            project: Project expected inside the store.
            source_root: Matching checkout for source-aware graph tools.
            transport: Explicit route overriding this API's configured selection.

        Returns:
            A transport-neutral graph handle.
        """
        database = Path(database_path).expanduser().resolve()
        resolved_source = Path(source_root).resolve() if source_root is not None else None
        route = self._select_transport("open_store", transport)
        return self._graph(
            route,
            route.open_store(database, project, resolved_source),
        )

    def capabilities(self, *, transport: str | None = None) -> frozenset[str]:
        """Return capabilities advertised by an indexing route.

        Args:
            transport: Explicit route overriding ``index_repository`` selection.
        """
        return self._select_transport("index_repository", transport).capabilities()

    @property
    def index_engine(self) -> CBMBinary | NativeHelper:
        """Return the pinned executable that implements repository indexing."""
        return self._select_transport("index_repository").engine

    def index_repository(
        self,
        tree: str | Path,
        project: str,
        mode: str,
        *,
        force_full: bool = False,
        incremental_controls: Mapping[str, str | int] | None = None,
        target_projects: Sequence[str] | None = None,
        transport: str | None = None,
    ) -> dict[str, Any]:
        """Publish one repository graph through the selected transport.

        Args:
            tree: Materialized repository tree to analyze.
            project: Stable CBM project receiving the generation.
            mode: CBM analysis coverage mode.
            force_full: Require CBM to confirm a full-build route.
            incremental_controls: Delta-policy overrides whose acknowledgement
                must be confirmed by CBM.
            target_projects: Projects matched by cross-repository intelligence.
            transport: Explicit route overriding this API's configured selection.

        Returns:
            Machine-readable route evidence reported by CBM.
        """
        tree_path = Path(tree)
        cross_repo = mode == "cross-repo-intelligence"
        if cross_repo and not target_projects:
            raise ValueError("cross-repo-intelligence requires target projects")
        if cross_repo and force_full:
            raise ValueError("cross-repo-intelligence has no full-build route")
        if not cross_repo and target_projects:
            raise ValueError("target projects require cross-repo-intelligence mode")
        effective_controls = None if cross_repo else incremental_controls
        route = self._select_transport("index_repository", transport)
        return route.index_repository(
            tree_path,
            _project_database(self.cache_root, project),
            project,
            mode,
            force_full=force_full,
            incremental_controls=effective_controls,
            target_projects=target_projects,
        )

    def delete_project(self, project: str, *, transport: str | None = None) -> tuple[bool, str]:
        """Delete one project through a selected transport.

        Args:
            project: Exact CBM project name to delete.
            transport: Explicit route overriding this API's configured selection.
        """
        route = self._select_transport("delete_project", transport)
        return route.delete_project(_project_database(self.cache_root, project), project)

    def list_projects(self, *, transport: str | None = None, **options: object) -> dict[str, Any]:
        """List projects through a selected transport.

        Args:
            transport: Explicit route overriding this API's configured selection.
            **options: Native CBM pagination and detail fields.
        """
        return self._select_transport("list_projects", transport).list_projects(options)

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        target: GraphHandle | None = None,
        transport: str | None = None,
    ) -> dict[str, Any]:
        """Call any CBM tool and return its logical JSON object.

        Args:
            name: CBM SDK tool name.
            arguments: Tool-specific arguments. ``None`` sends an empty object.
            target: Explicit graph binding. Its captured route takes precedence.
            transport: Route used only when ``target`` is absent.
        """
        if target is not None:
            if transport is not None and parse_transport_name(transport) != target.transport:
                raise CBMTransportError("explicit transport disagrees with the graph handle")
            return target._transport.call_tool(target._binding, name, dict(arguments or {}))
        return self._select_transport(name, transport).call_tool(None, name, dict(arguments or {}))

    def call_json_tool(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        target: GraphHandle | None = None,
        transport: str | None = None,
    ) -> dict[str, Any]:
        """Call a tool whose logical response is a JSON object.

        Args:
            name: CBM SDK tool name.
            arguments: Tool-specific arguments. ``None`` sends an empty object.
            target: Optional explicit graph/transport binding.
            transport: Route used only when ``target`` is absent.

        Returns:
            The logical JSON object returned by CBM.

        Raises:
            CBMTransportError: The call fails or returns no logical JSON object.
        """
        return self.call_tool(name, arguments, target=target, transport=transport)

    def search_graph(self, target: GraphHandle, **filters: object) -> dict[str, Any]:
        """Run CBM structured, BM25, or semantic graph search.

        Args:
            target: Graph returned by :meth:`project_graph`.
            **filters: Native ``search_graph`` fields such as ``query``, ``label``,
                ``name_pattern``, ``limit``, and ``offset``.
        """
        return self.call_json_tool(
            "search_graph",
            {**filters, "format": "json"},
            target=target,
        )

    def search_code(
        self,
        target: GraphHandle,
        *,
        pattern: str,
        **options: object,
    ) -> dict[str, Any]:
        """Search source text and enrich matches with graph structure.

        Args:
            target: Graph returned by :meth:`project_graph`.
            pattern: Literal or regular-expression source pattern.
            **options: Additional ``search_code`` fields such as ``mode``,
                ``file_pattern``, ``path_filter``, and ``limit``.
        """
        return self.call_json_tool(
            "search_code",
            {"pattern": pattern, **options},
            target=target,
        )

    def query_graph(self, target: GraphHandle, *, query: str, **options: object) -> dict[str, Any]:
        """Run a read-only Cypher-like query against a CBM graph.

        Args:
            target: Graph returned by :meth:`project_graph`.
            query: Native CBM graph query.
            **options: Additional ``query_graph`` fields such as ``graph`` and
                ``max_rows``.
        """
        return self.call_json_tool(
            "query_graph",
            {"query": query, **options, "format": "json"},
            target=target,
        )

    def get_graph_schema(self, target: GraphHandle) -> dict[str, Any]:
        """Return labels, relationship types, and their available properties.

        Args:
            target: Graph returned by :meth:`project_graph`.
        """
        return self.call_json_tool("get_graph_schema", target=target)

    def trace_path(
        self,
        target: GraphHandle,
        *,
        function_name: str,
        **options: object,
    ) -> dict[str, Any]:
        """Trace calls, data flow, or cross-service paths from one symbol.

        Args:
            target: Graph returned by :meth:`project_graph`.
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

    def get_code_snippet(
        self,
        target: GraphHandle,
        *,
        qualified_name: str,
        **options: object,
    ) -> dict[str, Any]:
        """Read the source belonging to one graph symbol.

        Args:
            target: Graph returned by :meth:`project_graph`.
            qualified_name: Exact symbol identity or a short name accepted by CBM.
            **options: Additional ``get_code_snippet`` fields such as
                ``include_neighbors``.
        """
        return self.call_json_tool(
            "get_code_snippet",
            {"qualified_name": qualified_name, **options},
            target=target,
        )

    def get_architecture(
        self,
        target: GraphHandle,
        *,
        path: str | None = None,
        aspects: Sequence[str] | None = None,
        **options: object,
    ) -> dict[str, Any]:
        """Summarize graph structure, dependencies, and architectural views.

        Args:
            target: Graph returned by :meth:`project_graph`.
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
        target: GraphHandle,
        *,
        paths: Sequence[str] = (),
        scopes: Sequence[str] = (),
        scope_limit: int = 200,
        scope_offset: int = 0,
    ) -> dict[str, Any]:
        """Inspect CBM's best-effort coverage record for paths or scopes.

        Args:
            target: Graph returned by :meth:`project_graph`.
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

    def detect_changes(self, target: GraphHandle, **options: object) -> dict[str, Any]:
        """Map source changes to graph impact.

        Args:
            target: Graph returned by :meth:`project_graph`.
            **options: ``detect_changes`` fields such as ``base_branch``,
                ``since``, ``scope``, ``direction``, ``depth``, and ``limit``.
        """
        return self.call_json_tool(
            "detect_changes",
            {**options, "format": "json"},
            target=target,
        )

    def manage_adr(
        self,
        target: GraphHandle,
        *,
        mode: str = "get",
        content: str | None = None,
        section_updates: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Read or explicitly update a project's architecture record.

        Args:
            target: Graph returned by :meth:`project_graph`.
            mode: ADR operation; ``get`` is the read-only default. Writes require
                a writable graph handle.
            content: Complete replacement document used by ``update``.
            section_updates: Named replacement sections used by ``set_sections``.
        """
        arguments: dict[str, object] = {"mode": mode}
        if content is not None:
            arguments["content"] = content
        if section_updates is not None:
            arguments["section_updates"] = dict(section_updates)
        return self.call_json_tool("manage_adr", arguments, target=target)

    def ingest_traces(
        self,
        target: GraphHandle,
        traces: Sequence[Mapping[str, object]],
    ) -> dict[str, Any]:
        """Submit runtime call observations to CBM.

        Args:
            target: Graph returned by :meth:`project_graph`.
            traces: Caller, callee, and count objects accepted by CBM.
        """
        return self.call_json_tool(
            "ingest_traces",
            {"traces": [dict(trace) for trace in traces]},
            target=target,
        )

    def index_status(
        self,
        target: GraphHandle,
        *,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """Return graph counts, root identity, and persisted coverage status.

        Args:
            target: Graph returned by :meth:`project_graph`.
            verbose: Include live Git/worktree context when supported.
        """
        return self.call_json_tool(
            "index_status",
            {"verbose": verbose},
            target=target,
        )

    def compare_graphs(
        self,
        base: GraphHandle,
        target: GraphHandle,
        *,
        limit: int = 200,
        scan_limit: int = 2_000_000,
    ) -> dict[str, Any]:
        """Compare stable node and edge identities across two graph generations.

        Args:
            base: Older graph generation.
            target: Newer graph from the same transport instance.
            limit: Maximum returned entries per change set.
            scan_limit: Maximum combined rows scanned per node or edge phase.

        Raises:
            CBMTransportError: Handles belong to different transports.
        """
        if base._transport is not target._transport:
            raise CBMTransportError("graph handles belong to different CBM transports")
        return target._transport.compare_graphs(
            base._binding,
            target._binding,
            limit=limit,
            scan_limit=scan_limit,
        )

    def close(self) -> None:
        """Close only transports and child processes owned by this client."""
        with self._transport_lock:
            transports = tuple(self._transports.values())
            self._transports.clear()
        for transport in transports:
            transport.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
