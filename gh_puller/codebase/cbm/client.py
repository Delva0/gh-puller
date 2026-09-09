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

from ..utils import NullResourceMonitor
from .binary import CBMBinary, resolve_cbm_binary
from .transports.utils import (
    CBMTransportError,
    OpenedGraph,
    Transport,
    TransportName,
    create_transport,
    parse_transport_name,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import TracebackType

    from ..utils import ResourceMonitorLike
    from .transports.native import NativeHelper


@dataclass(frozen=True, slots=True)
class GraphHandle:
    """Identify one CBM graph independently of its access path."""

    project: str
    source_root: Path | None
    nodes: int | None
    edges: int | None
    _owner: CBMClient = field(repr=False, compare=False)
    _preferred: TransportName = field(repr=False, compare=False)
    _transport: Transport = field(repr=False, compare=False)
    _binding: object = field(repr=False, compare=False)
    _store: Path | None = field(repr=False, compare=False)


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
        selected_routes = {operation: parse_transport_name(route) for operation, route in dict(routes or {}).items()}
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
        self._monitor = resource_monitor or NullResourceMonitor()
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
            parse_transport_name(transport) if transport is not None else self.routes.get(operation, self.transport)
        )
        return self._get_transport(selected)

    @staticmethod
    def _require(transport: Transport, operation: str) -> None:
        if not transport.supports(operation):
            raise CBMTransportError(
                f"CBM {transport.name} transport does not support {operation}",
            )

    def _native_transport(self) -> Transport:
        return self._get_transport("native")

    def _graph(
        self,
        transport: Transport,
        opened: OpenedGraph,
        store: Path | None = None,
    ) -> GraphHandle:
        return GraphHandle(
            opened.project,
            opened.source_root,
            opened.nodes,
            opened.edges,
            self,
            transport.name,
            transport,
            opened.binding,
            store,
        )

    def _target_transport(
        self,
        operation: str,
        target: GraphHandle,
        transport: str | None,
    ) -> Transport:
        if target._owner is not self:
            raise CBMTransportError("graph handle belongs to another CBM client")
        if target._store is not None:
            if transport is not None and parse_transport_name(transport) != "native":
                raise CBMTransportError("explicit CBM stores require the native transport")
            self._require(target._transport, operation)
            return target._transport
        if transport is not None:
            selected = self._get_transport(parse_transport_name(transport))
        else:
            selected = self._get_transport(self.routes.get(operation, target._preferred))
        self._require(selected, operation)
        return selected

    def _target_binding(self, target: GraphHandle, transport: Transport) -> object:
        if target._owner is not self:
            raise CBMTransportError("graph handle belongs to another CBM client")
        if transport is target._transport:
            return target._binding
        if target._store is not None:
            raise CBMTransportError("explicit CBM stores require the native transport")
        self._require(transport, "project_graph")
        opened = transport.open_project(target.project, target.source_root)
        if (
            opened.project != target.project
            or (target.nodes is not None and opened.nodes is not None and opened.nodes != target.nodes)
            or (target.edges is not None and opened.edges is not None and opened.edges != target.edges)
        ):
            raise CBMTransportError("CBM transport opened a different graph")
        return opened.binding

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
            source_root: Matching checkout for source-aware tools.
            transport: Explicit route overriding this API's configured selection.

        Returns:
            A transport-neutral graph handle.
        """
        _project_database(self.cache_root, project)
        resolved_source = Path(source_root).resolve() if source_root is not None else None
        route = self._select_transport("project_graph", transport)
        self._require(route, "project_graph")
        return self._graph(route, route.open_project(project, resolved_source))

    def open_store(
        self,
        database_path: str | Path,
        project: str,
        *,
        source_root: str | Path | None = None,
    ) -> GraphHandle:
        """Open an explicit CBM store through the native SDK.

        Args:
            database_path: Exact CBM database containing the graph.
            project: Project expected inside the store.
            source_root: Matching checkout for source-aware graph tools.

        Returns:
            A transport-neutral graph handle.
        """
        database = Path(database_path).expanduser().resolve()
        resolved_source = Path(source_root).resolve() if source_root is not None else None
        route = self._native_transport()
        self._require(route, "open_store")
        return self._graph(
            route,
            route.open_store(database, project, resolved_source),
            database,
        )

    def supports(self, operation: str, *, transport: str | None = None) -> bool:
        """Return whether one selected route currently exposes an operation.

        Args:
            operation: Public CBM API or tool name.
            transport: Explicit route. ``None`` uses this operation's configured
                route and then the client default.
        """
        return self._select_transport(operation, transport).supports(operation)

    def capabilities(self, *, transport: str | None = None) -> frozenset[str]:
        """Return detailed features advertised by an indexing route.

        Args:
            transport: Explicit route overriding ``index_repository`` selection.
        """
        route = self._select_transport("index_repository", transport)
        self._require(route, "index_repository")
        return route.capabilities()

    @property
    def index_engine(self) -> CBMBinary | NativeHelper:
        """Return the pinned executable that implements repository indexing."""
        route = self._select_transport("index_repository")
        self._require(route, "index_repository")
        return route.engine

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
        tree_path = Path(tree).resolve()
        cross_repo = mode == "cross-repo-intelligence"
        if cross_repo and not target_projects:
            raise ValueError("cross-repo-intelligence requires target projects")
        if cross_repo and force_full:
            raise ValueError("cross-repo-intelligence has no full-build route")
        if not cross_repo and target_projects:
            raise ValueError("target projects require cross-repo-intelligence mode")
        effective_controls = None if cross_repo else incremental_controls
        route = self._select_transport("index_repository", transport)
        self._require(route, "index_repository")
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
        self._require(route, "delete_project")
        return route.delete_project(_project_database(self.cache_root, project), project)

    def list_projects(self, *, transport: str | None = None, **options: object) -> dict[str, Any]:
        """List projects through a selected transport.

        Args:
            transport: Explicit route overriding this API's configured selection.
            **options: Native CBM pagination and detail fields.
        """
        route = self._select_transport("list_projects", transport)
        self._require(route, "list_projects")
        return route.list_projects(options)

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
            target: Optional graph identity and preferred route.
            transport: Explicit route overriding both API configuration and the
                graph's preferred route.
        """
        if target is not None:
            route = self._target_transport(name, target, transport)
            binding = self._target_binding(target, route)
            return route.call_tool(binding, name, dict(arguments or {}))
        route = self._select_transport(name, transport)
        self._require(route, name)
        return route.call_tool(None, name, dict(arguments or {}))

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
            target: Optional graph identity and preferred route.
            transport: Explicit route overriding both API configuration and the
                graph's preferred route.

        Returns:
            The logical JSON object returned by CBM.

        Raises:
            CBMTransportError: The call fails or returns no logical JSON object.
        """
        return self.call_tool(name, arguments, target=target, transport=transport)

    def search_graph(
        self,
        target: GraphHandle,
        *,
        transport: str | None = None,
        **filters: object,
    ) -> dict[str, Any]:
        """Run CBM structured, BM25, or semantic graph search.

        Args:
            target: Graph returned by :meth:`project_graph`.
            transport: Explicit route overriding this API's configured selection.
            **filters: Native ``search_graph`` fields such as ``query``, ``label``,
                ``name_pattern``, ``limit``, and ``offset``.
        """
        return self.call_json_tool(
            "search_graph",
            {**filters, "format": "json"},
            target=target,
            transport=transport,
        )

    def search_code(
        self,
        target: GraphHandle,
        *,
        pattern: str,
        transport: str | None = None,
        **options: object,
    ) -> dict[str, Any]:
        """Search source text and enrich matches with graph structure.

        Args:
            target: Graph returned by :meth:`project_graph`.
            pattern: Literal or regular-expression source pattern.
            transport: Explicit route overriding this API's configured selection.
            **options: Additional ``search_code`` fields such as ``mode``,
                ``file_pattern``, ``path_filter``, and ``limit``.
        """
        return self.call_json_tool(
            "search_code",
            {"pattern": pattern, **options},
            target=target,
            transport=transport,
        )

    def query_graph(
        self,
        target: GraphHandle,
        *,
        query: str,
        transport: str | None = None,
        **options: object,
    ) -> dict[str, Any]:
        """Run a read-only Cypher-like query against a CBM graph.

        Args:
            target: Graph returned by :meth:`project_graph`.
            query: Native CBM graph query.
            transport: Explicit route overriding this API's configured selection.
            **options: Additional ``query_graph`` fields such as ``graph`` and
                ``max_rows``.
        """
        return self.call_json_tool(
            "query_graph",
            {"query": query, **options, "format": "json"},
            target=target,
            transport=transport,
        )

    def get_graph_schema(
        self,
        target: GraphHandle,
        *,
        transport: str | None = None,
    ) -> dict[str, Any]:
        """Return labels, relationship types, and their available properties.

        Args:
            target: Graph returned by :meth:`project_graph`.
            transport: Explicit route overriding this API's configured selection.
        """
        return self.call_json_tool(
            "get_graph_schema",
            target=target,
            transport=transport,
        )

    def trace_path(
        self,
        target: GraphHandle,
        *,
        function_name: str,
        transport: str | None = None,
        **options: object,
    ) -> dict[str, Any]:
        """Trace calls, data flow, or cross-service paths from one symbol.

        Args:
            target: Graph returned by :meth:`project_graph`.
            function_name: Qualified or discoverable function name used as the
                traversal origin.
            transport: Explicit route overriding this API's configured selection.
            **options: Native ``trace_path`` fields such as ``direction``, ``depth``,
                ``mode``, ``limit``, and ``cursor``.
        """
        return self.call_json_tool(
            "trace_path",
            {"function_name": function_name, **options, "format": "json"},
            target=target,
            transport=transport,
        )

    def get_code_snippet(
        self,
        target: GraphHandle,
        *,
        qualified_name: str,
        transport: str | None = None,
        **options: object,
    ) -> dict[str, Any]:
        """Read the source belonging to one graph symbol.

        Args:
            target: Graph returned by :meth:`project_graph`.
            qualified_name: Exact symbol identity or a short name accepted by CBM.
            transport: Explicit route overriding this API's configured selection.
            **options: Additional ``get_code_snippet`` fields such as
                ``include_neighbors``.
        """
        return self.call_json_tool(
            "get_code_snippet",
            {"qualified_name": qualified_name, **options},
            target=target,
            transport=transport,
        )

    def get_architecture(
        self,
        target: GraphHandle,
        *,
        path: str | None = None,
        aspects: Sequence[str] | None = None,
        transport: str | None = None,
        **options: object,
    ) -> dict[str, Any]:
        """Summarize graph structure, dependencies, and architectural views.

        Args:
            target: Graph returned by :meth:`project_graph`.
            path: Optional repository-relative directory scope.
            aspects: Optional CBM architecture sections. ``None`` selects the
                compact default view.
            transport: Explicit route overriding this API's configured selection.
            **options: Additional ``get_architecture`` fields supported by CBM.
        """
        arguments = dict(options)
        if path is not None:
            arguments["path"] = path
        if aspects is not None:
            arguments["aspects"] = list(aspects)
        arguments["format"] = "json"
        return self.call_json_tool(
            "get_architecture",
            arguments,
            target=target,
            transport=transport,
        )

    def check_index_coverage(
        self,
        target: GraphHandle,
        *,
        paths: Sequence[str] = (),
        scopes: Sequence[str] = (),
        scope_limit: int = 200,
        scope_offset: int = 0,
        transport: str | None = None,
    ) -> dict[str, Any]:
        """Inspect CBM's best-effort coverage record for paths or scopes.

        Args:
            target: Graph returned by :meth:`project_graph`.
            paths: Repository-relative files to check exactly.
            scopes: Repository-relative path prefixes to enumerate.
            scope_limit: Maximum coverage rows returned for each scope.
            scope_offset: Starting row offset for each scope.
            transport: Explicit route overriding this API's configured selection.
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
            transport=transport,
        )

    def detect_changes(
        self,
        target: GraphHandle,
        *,
        transport: str | None = None,
        **options: object,
    ) -> dict[str, Any]:
        """Map source changes to graph impact.

        Args:
            target: Graph returned by :meth:`project_graph`.
            transport: Explicit route overriding this API's configured selection.
            **options: ``detect_changes`` fields such as ``base_branch``,
                ``since``, ``scope``, ``direction``, ``depth``, and ``limit``.
        """
        return self.call_json_tool(
            "detect_changes",
            {**options, "format": "json"},
            target=target,
            transport=transport,
        )

    def manage_adr(
        self,
        target: GraphHandle,
        *,
        mode: str = "get",
        content: str | None = None,
        section_updates: Mapping[str, str] | None = None,
        transport: str | None = None,
    ) -> dict[str, Any]:
        """Read or explicitly update a project's architecture record.

        Args:
            target: Graph returned by :meth:`project_graph`.
            mode: ADR operation; ``get`` is the read-only default. Writes require
                a writable graph handle.
            content: Complete replacement document used by ``update``.
            section_updates: Named replacement sections used by ``set_sections``.
            transport: Explicit route overriding this API's configured selection.
        """
        arguments: dict[str, object] = {"mode": mode}
        if content is not None:
            arguments["content"] = content
        if section_updates is not None:
            arguments["section_updates"] = dict(section_updates)
        return self.call_json_tool(
            "manage_adr",
            arguments,
            target=target,
            transport=transport,
        )

    def ingest_traces(
        self,
        target: GraphHandle,
        traces: Sequence[Mapping[str, object]],
        *,
        transport: str | None = None,
    ) -> dict[str, Any]:
        """Submit runtime call observations to CBM.

        Args:
            target: Graph returned by :meth:`project_graph`.
            traces: Caller, callee, and count objects accepted by CBM.
            transport: Explicit route overriding this API's configured selection.
        """
        return self.call_json_tool(
            "ingest_traces",
            {"traces": [dict(trace) for trace in traces]},
            target=target,
            transport=transport,
        )

    def index_status(
        self,
        target: GraphHandle,
        *,
        verbose: bool = False,
        transport: str | None = None,
    ) -> dict[str, Any]:
        """Return graph counts, root identity, and persisted coverage status.

        Args:
            target: Graph returned by :meth:`project_graph`.
            verbose: Include live Git/worktree context when supported.
            transport: Explicit route overriding this API's configured selection.
        """
        return self.call_json_tool(
            "index_status",
            {"verbose": verbose},
            target=target,
            transport=transport,
        )

    def compare_graphs(
        self,
        base: GraphHandle,
        target: GraphHandle,
        *,
        limit: int = 200,
        scan_limit: int = 2_000_000,
        transport: str | None = None,
    ) -> dict[str, Any]:
        """Compare stable node and edge identities across two graph generations.

        Args:
            base: Older graph generation owned by this client.
            target: Newer graph generation owned by this client.
            limit: Maximum returned entries per change set.
            scan_limit: Maximum combined rows scanned per node or edge phase.
            transport: Explicit route overriding this API's configured selection.

        Raises:
            CBMTransportError: Handles belong to another client or cannot be
                opened through the selected route.
        """
        route = self._target_transport("compare_graphs", target, transport)
        base_binding = self._target_binding(base, route)
        target_binding = self._target_binding(target, route)
        return route.compare_graphs(
            base_binding,
            target_binding,
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
