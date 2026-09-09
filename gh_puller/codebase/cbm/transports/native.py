"""Implement the native SDK transport for the CBM client.

This module owns helper authentication, framed requests, graph handles, and
native index execution. It accepts only CBM stores and SDK-level operations;
formats that may produce a store belong to callers outside this package.
"""

from __future__ import annotations

import json
import os
import queue
import struct
import subprocess
import threading
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO, Self

from ...utils import NativeExecutable, NativeExecutableError, resolve_native_executable
from .utils import (
    CBMTransportError,
    OpenedGraph,
    checked_index_execution,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from ...utils import ResourceMonitorLike

_QUERY_PROTOCOL_VERSION = 9
_INDEX_PROTOCOL_VERSION = 10
_RESPONSE_MAX_BYTES = 256 << 20
_QUERY_HELPER_ENV = "GH_PULLER_CODEBASE_CBM_HELPER"
_INDEX_HELPER_ENV = "GH_PULLER_CODEBASE_CBM_INDEX_HELPER"
_STREAM_CLOSED = object()
_CAPABILITY_BY_OPERATION = {
    "project_graph": "project-open",
    "open_store": "project-open",
    "list_projects": "project-list",
    "delete_project": "project-delete",
    "compare_graphs": "graph-compare",
}


NativeHelper = NativeExecutable


def resolve_native_helper(
    helper: str | Path | NativeHelper | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    environment_key: str = _QUERY_HELPER_ENV,
    local_name: str = "gh-puller-cbm-helper",
) -> NativeHelper:
    """Resolve and authenticate one native helper.

    Args:
        helper: Explicit executable path, command name, or pinned identity.
        environ: Environment used for the helper override and executable lookup.
        environment_key: Variable naming this helper when no path is explicit.
        local_name: Executable name used for local-build and ``PATH`` lookup.

    Returns:
        Executable identity pinned to its current inode and bytes.

    Raises:
        CBMTransportError: No valid helper can be resolved.
    """
    values = os.environ if environ is None else environ
    try:
        return resolve_native_executable(
            helper,
            environ=values,
            environment_key=environment_key,
            local_name=local_name,
            version_prefix="gh-puller-cbm-helper ",
        )
    except NativeExecutableError as exc:
        raise CBMTransportError(str(exc).replace("native executable", "native CBM helper")) from exc


def _read_exact(stream: BinaryIO, size: int, *, clean_eof: bool = False) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            if clean_eof and not chunks:
                return None
            raise EOFError("native CBM helper closed a partial frame")
        chunks.extend(chunk)
    return bytes(chunks)


class _NativeSession:
    """Own one thread-safe native CBM helper process and its active graph binding."""

    def __init__(
        self,
        helper: str | Path | NativeHelper | None,
        cache_root: Path,
        timeout: float,
        environment: Mapping[str, str] | None = None,
        monitor: ResourceMonitorLike | None = None,
        *,
        helper_environment_key: str = _QUERY_HELPER_ENV,
        helper_filename: str = "gh-puller-cbm-helper",
        protocol: int = _QUERY_PROTOCOL_VERSION,
        required_capabilities: frozenset[str] = frozenset(),
    ):
        """Start the helper and negotiate the fixed native protocol.

        Args:
            helper: Explicit helper or its normal environment/PATH resolution.
            cache_root: Parent directory for engine-versioned immutable stores.
            timeout: Maximum seconds for one lock wait or helper request.
            environment: Environment passed to helper resolution and execution.
            monitor: Optional build-owned resource monitor.
            helper_environment_key: Environment override for this helper kind.
            helper_filename: Local and ``PATH`` executable name.
            protocol: Exact framed protocol version required from the helper.
            required_capabilities: Features required during negotiation.
        """
        if timeout <= 0:
            raise ValueError("native CBM timeout must be positive")
        overrides = dict(environment or {})
        values = {**os.environ, **overrides}
        self.helper = resolve_native_helper(
            helper,
            environ=values,
            environment_key=helper_environment_key,
            local_name=helper_filename,
        )
        self.helper.verify_unchanged()
        self.cache_root = cache_root
        self.timeout = timeout
        self.monitor = monitor
        self._responses: queue.Queue[object] = queue.Queue()
        self._stderr = deque(maxlen=80)
        self._request_lock = threading.Lock()
        self._next_id = 1
        self._closed = False
        self.loaded_project: str | None = None
        self.loaded_database_path: Path | None = None
        self.loaded_source_root: Path | None = None
        self._binding = 0
        helper_environment = dict(values)
        helper_environment.setdefault("CBM_LOG_LEVEL", "error")
        self.process = subprocess.Popen(
            [str(self.helper.path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=helper_environment,
        )
        if self.monitor is not None:
            self.monitor.add_child(self.process.pid)
        self._stdout_thread = threading.Thread(target=self._stdout_loop, daemon=True)
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        try:
            hello = self._request("hello", {"protocol": protocol})
            capabilities = hello.get("capabilities")
            tools = hello.get("tools")
            store_format = hello.get("store_format")
            sdk_abi = hello.get("sdk_abi")
            if (
                hello.get("protocol") != protocol
                or not isinstance(capabilities, list)
                or not all(isinstance(item, str) for item in capabilities)
                or not required_capabilities <= set(capabilities)
                or not isinstance(tools, list)
                or not all(isinstance(item, str) and item for item in tools)
                or type(store_format) is not int
                or store_format < 1
                or type(sdk_abi) is not int
                or sdk_abi < 2
            ):
                raise CBMTransportError("native CBM helper advertised an incompatible protocol")
            self.capabilities = frozenset(capabilities)
            self.tools = frozenset(tools)
            self.store_format = store_format
            self.sdk_abi = sdk_abi
            self.hello = hello
        except BaseException:
            self.close()
            raise

    @property
    def pid(self) -> int:
        """Return the persistent helper process ID."""
        return self.process.pid

    @property
    def binding(self) -> int:
        """Return the generation of the graph currently active in the helper."""
        return self._binding

    def _activate_graph(
        self,
        database_path: Path,
        project: str,
        source_root: Path | None,
    ) -> None:
        self._binding += 1
        self.loaded_project = project
        self.loaded_database_path = database_path
        self.loaded_source_root = source_root

    def is_active_graph(
        self,
        binding: int,
        database_path: Path,
        project: str,
        source_root: Path | None,
    ) -> bool:
        """Return whether a native graph capability still names the active binding.

        Args:
            binding: Generation captured when the graph capability was created.
            database_path: Exact CBM store path bound by the capability.
            project: Project identity inside the store.
            source_root: Optional source snapshot available to source-aware tools.
        """
        return (
            binding == self._binding
            and self.loaded_database_path == database_path
            and self.loaded_project == project
            and self.loaded_source_root == source_root
        )

    def _stdout_loop(self) -> None:
        try:
            while True:
                header = _read_exact(self.process.stdout, 4, clean_eof=True)
                if header is None:
                    break
                length = struct.unpack(">I", header)[0]
                if length == 0 or length > _RESPONSE_MAX_BYTES:
                    raise CBMTransportError("native CBM helper returned an invalid frame size")
                payload = _read_exact(self.process.stdout, length)
                response = json.loads(payload)
                if not isinstance(response, dict):
                    raise CBMTransportError("native CBM helper returned a non-object response")
                self._responses.put(response)
        except BaseException as exc:
            self._responses.put(exc)
        finally:
            self._responses.put(_STREAM_CLOSED)

    def _stderr_loop(self) -> None:
        for line in self.process.stderr:
            self._stderr.append(line.decode(errors="replace").rstrip())

    def _detail(self) -> str:
        return "\n".join(self._stderr)[-4000:]

    def _terminate(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            with suppress(OSError):
                stream.close()
        self._stdout_thread.join(timeout=2)
        self._stderr_thread.join(timeout=2)
        if self.monitor is not None:
            self.monitor.remove_child(self.process.pid)

    def _request(self, method: str, parameters: Mapping[str, object], *, timeout: float | None = None) -> dict:
        if self._closed:
            raise CBMTransportError("native CBM transport is closed")
        with self._request_lock:
            if self.process.poll() is not None:
                raise CBMTransportError(
                    f"native CBM helper exited {self.process.returncode}: {self._detail()}",
                )
            request_id = self._next_id
            self._next_id += 1
            payload = json.dumps(
                {"id": request_id, "method": method, "params": parameters},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            try:
                self.process.stdin.write(struct.pack(">I", len(payload)))
                self.process.stdin.write(payload)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._terminate()
                raise CBMTransportError(f"native CBM helper write failed: {self._detail()}") from exc
            try:
                response = self._responses.get(timeout=self.timeout if timeout is None else timeout)
            except queue.Empty:
                self._terminate()
                raise CBMTransportError(
                    f"native CBM request timed out after {self.timeout if timeout is None else timeout:g}s",
                ) from None
            if isinstance(response, BaseException):
                self._terminate()
                raise CBMTransportError(f"native CBM response failed: {response}; {self._detail()}") from response
            if response is _STREAM_CLOSED:
                self._terminate()
                raise CBMTransportError(f"native CBM helper closed its response stream: {self._detail()}")
            if response.get("id") != request_id or not isinstance(response.get("ok"), bool):
                self._terminate()
                raise CBMTransportError("native CBM helper returned an unmatched response")
            if not response["ok"]:
                error = response.get("error")
                code = error.get("code") if isinstance(error, dict) else "unknown"
                message = error.get("message") if isinstance(error, dict) else str(error)
                raise CBMTransportError(f"native CBM {code}: {message}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise CBMTransportError("native CBM helper returned a non-object result")
            if self.monitor is not None:
                self.monitor.sample()
                if self.monitor.exceeded:
                    self._terminate()
                    raise CBMTransportError("memory limit exceeded while running CBM")
            return result

    def open_project(
        self,
        database_path: Path,
        project: str,
        source_root: Path | None = None,
    ) -> dict[str, Any]:
        """Bind an indexed project's current generation to native graph tools.

        Args:
            database_path: Exact project database opened read-only.
            project: Project expected inside that database.
            source_root: Matching checkout used by tools that read source text.
        """
        result = self._request(
            "open",
            {
                "database_path": str(database_path),
                "project": project,
                "source_root": str(source_root) if source_root is not None else None,
            },
        )
        if (
            result.get("project") != project
            or type(result.get("nodes")) is not int
            or type(result.get("edges")) is not int
        ):
            raise CBMTransportError("native CBM helper opened the wrong project")
        self._activate_graph(database_path, project, source_root)
        return result

    def list_projects(self, arguments: Mapping[str, object] | None = None) -> dict[str, Any]:
        """List projects from this transport's explicit cache directory.

        Args:
            arguments: Native pagination and detail options.
        """
        return self._request(
            "list",
            {"cache_directory": str(self.cache_root), "arguments": dict(arguments or {})},
        )

    def delete_project(self, database_path: Path, project: str) -> dict[str, Any]:
        """Delete a mutable project and its SQLite sidecars.

        Args:
            database_path: Exact database published by the indexing helper.
            project: Project expected inside that database.
        """
        if self.loaded_project == project and self.loaded_database_path == database_path:
            self._clear_loaded()
        return self._request(
            "delete",
            {"database_path": str(database_path), "project": project},
        )

    def _clear_loaded(self) -> None:
        self._binding += 1
        self.loaded_project = None
        self.loaded_database_path = None
        self.loaded_source_root = None

    def query_graph(self, *, project: str, query: str, graph: str = "code", max_rows: int = 0) -> dict[str, Any]:
        """Query the loaded immutable CBM store directly.

        Args:
            project: Project identity bound in the active graph.
            query: Read-only CBM Cypher-like query.
            graph: ``code`` or the derived ``missed`` graph.
            max_rows: Result ceiling; zero selects CBM's native default.
        """
        return self.call_tool(
            "query_graph",
            {"project": project, "query": query, "graph": graph, "max_rows": max_rows},
        )

    def compare_graphs(
        self,
        *,
        base_database: Path,
        base_project: str,
        target_database: Path,
        target_project: str,
        limit: int,
        scan_limit: int,
    ) -> dict[str, Any]:
        """Compare two CBM graph stores with request-scoped handles.

        Args:
            base_database: Materialized database for the older generation.
            base_project: Project bound inside the base database.
            target_database: Materialized database for the newer generation.
            target_project: Project bound inside the target database.
            limit: Maximum returned entries per change set.
            scan_limit: Maximum combined rows scanned per node or edge phase.
        """
        return self._request(
            "compare",
            {
                "base": {"database_path": str(base_database), "project": base_project},
                "target": {"database_path": str(target_database), "project": target_project},
                "limit": limit,
                "scan_limit": scan_limit,
            },
        )

    def call_tool(self, name: str, arguments: Mapping[str, object] | None = None) -> dict[str, Any]:
        """Call one tool advertised by the native graph runtime.

        Args:
            name: Tool name from the negotiated native registry.
            arguments: Tool-specific JSON arguments.

        Returns:
            The tool's logical JSON object without a transport envelope.
        """
        values = dict(arguments or {})
        if name not in self.tools:
            raise CBMTransportError(f"native CBM tool is not supported: {name}")
        if name == "index_status" and values.get("verbose") is True and self.loaded_source_root is None:
            raise CBMTransportError("verbose index status requires a live source snapshot")
        return self._request("call", {"name": name, "arguments": values})

    def close(self) -> None:
        """Request clean shutdown and release process resources."""
        if self._closed:
            return
        if self.process.poll() is None:
            with suppress(CBMTransportError):
                self._request("shutdown", {}, timeout=min(self.timeout, 2))
        self._closed = True
        self._terminate()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args) -> None:
        self.close()


class _NativeIndexSession(_NativeSession):
    """Run repository indexing in a persistent crash-isolated native helper."""

    def __init__(
        self,
        helper: str | Path | NativeHelper | None,
        cache_root: Path,
        timeout: float,
        environment: Mapping[str, str] | None = None,
        monitor: ResourceMonitorLike | None = None,
    ):
        """Start the full SDK helper without changing the compact query helper.

        Args:
            helper: Explicit full helper or its normal resolution order.
            cache_root: CBM database directory owned by this client.
            timeout: Maximum seconds for one native operation.
            environment: Environment overrides passed to the helper.
            monitor: Optional build-owned resource monitor.
        """
        super().__init__(
            helper,
            cache_root,
            timeout,
            environment,
            monitor,
            helper_environment_key=_INDEX_HELPER_ENV,
            helper_filename="gh-puller-cbm-index-helper",
            protocol=_INDEX_PROTOCOL_VERSION,
        )

    def index_repository(
        self,
        tree: Path,
        database_path: Path,
        project: str,
        mode: str,
        *,
        force_full: bool = False,
        incremental_controls: Mapping[str, str | int] | None = None,
        target_projects: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Run one typed SDK indexing request and return its logical JSON result.

        Args:
            tree: Materialized repository tree analyzed by CBM.
            database_path: Explicit normal-mode publication path.
            project: Stable CBM project receiving the graph.
            mode: CBM analysis mode, including cross-repository matching.
            force_full: Bypass CBM's incremental routing.
            incremental_controls: Complete delta policy, or ``None`` for CBM defaults.
            target_projects: Cross-repository targets; required only by that mode.
        """
        self._clear_loaded()
        return self._request(
            "index",
            {
                "repo_path": str(tree),
                "database_path": str(database_path),
                "cache_directory": str(self.cache_root),
                "project": project,
                "mode": mode,
                "persistence": False,
                "force_full": force_full,
                "incremental_controls": dict(incremental_controls) if incremental_controls else None,
                "target_projects": list(target_projects or ()),
            },
        )


@dataclass(frozen=True, slots=True)
class _NativeBinding:
    transport: NativeTransport
    session: _NativeSession
    generation: int
    database_path: Path
    project: str
    source_root: Path | None


class NativeTransport:
    """Expose compact-query and full-index helpers as one native transport."""

    name = "native"

    def __init__(
        self,
        helper: str | Path | NativeHelper | None,
        index_helper: str | Path | NativeHelper | None,
        cache_root: Path,
        timeout: float,
        environment: Mapping[str, str] | None,
        monitor: ResourceMonitorLike | None,
    ):
        self._helper = helper
        self._index_helper = index_helper
        self.cache_root = cache_root
        self.timeout = timeout
        self.environment = dict(environment or {})
        self.monitor = monitor
        self._lock = threading.RLock()
        self._query: _NativeSession | None = None
        self._index: _NativeIndexSession | None = None

    def _query_session(self) -> _NativeSession:
        with self._lock:
            if self._query is None:
                self._query = _NativeSession(
                    self._helper,
                    self.cache_root,
                    self.timeout,
                    self.environment,
                    self.monitor,
                )
            return self._query

    def _index_session(self) -> _NativeIndexSession:
        with self._lock:
            if self._index is None:
                self._index = _NativeIndexSession(
                    self._index_helper,
                    self.cache_root,
                    self.timeout,
                    self.environment,
                    self.monitor,
                )
            return self._index

    @property
    def engine(self) -> NativeHelper:
        return self._index_session().helper

    def capabilities(self) -> frozenset[str]:
        return self._index_session().capabilities

    @staticmethod
    def _session_supports(session: _NativeSession, operation: str) -> bool:
        capability = _CAPABILITY_BY_OPERATION.get(operation)
        return capability in session.capabilities if capability is not None else operation in session.tools

    def supports(self, operation: str) -> bool:
        """Return whether the loaded SDK adapter advertises an operation."""
        if operation == "index_repository":
            return "repository-index" in self._index_session().capabilities
        with self._lock:
            index = self._index
        if index is not None and self._session_supports(index, operation):
            return True
        return self._session_supports(self._query_session(), operation)

    @staticmethod
    def _database_path(cache_root: Path, project: str) -> Path:
        if (
            not project
            or project.startswith(".")
            or ".." in project
            or any(not (character.isascii() and (character.isalnum() or character in "-_.")) for character in project)
        ):
            raise ValueError(f"invalid CBM project name: {project!r}")
        return cache_root / f"{project}.db"

    def _session_for(self, database_path: Path) -> _NativeSession:
        with self._lock:
            for session in (self._index, self._query):
                if session is not None and session.loaded_database_path == database_path:
                    return session
            if self._index is not None and "project-open" in self._index.capabilities:
                return self._index
            return self._query_session()

    def open_project(self, project: str, source_root: Path | None) -> OpenedGraph:
        database_path = self._database_path(self.cache_root, project)
        return self.open_store(database_path, project, source_root)

    def open_store(
        self,
        database_path: Path,
        project: str,
        source_root: Path | None,
    ) -> OpenedGraph:
        """Open an explicit CBM database through the native SDK.

        Args:
            database_path: Exact CBM store passed to ``cbm_sdk_graph_open``.
            project: Project expected inside the store.
            source_root: Matching checkout for source-aware graph tools.

        Returns:
            Opaque binding and graph counts reported by the SDK.
        """
        session = self._session_for(database_path)
        result = session.open_project(database_path, project, source_root)
        return self._opened_graph(
            session,
            database_path,
            project,
            source_root,
            result["nodes"],
            result["edges"],
        )

    def _opened_graph(
        self,
        session: _NativeSession,
        database_path: Path,
        project: str,
        source_root: Path | None,
        nodes: int,
        edges: int,
    ) -> OpenedGraph:
        binding = _NativeBinding(
            self,
            session,
            session.binding,
            database_path,
            project,
            source_root,
        )
        return OpenedGraph(
            project,
            binding,
            source_root,
            nodes,
            edges,
        )

    def call_tool(
        self,
        binding: object | None,
        name: str,
        arguments: Mapping[str, object],
    ) -> dict[str, Any]:
        if not isinstance(binding, _NativeBinding) or binding.transport is not self:
            raise CBMTransportError("native CBM tools require a native graph handle")
        if not binding.session.is_active_graph(
            binding.generation,
            binding.database_path,
            binding.project,
            binding.source_root,
        ):
            raise CBMTransportError("native graph is no longer active")
        values = dict(arguments)
        supplied = values.get("project")
        if supplied is not None and supplied != binding.project:
            raise CBMTransportError("CBM graph handle disagrees with the tool project")
        values["project"] = binding.project
        return binding.session.call_tool(name, values)

    def index_repository(
        self,
        tree: Path,
        database_path: Path,
        project: str,
        mode: str,
        *,
        force_full: bool = False,
        incremental_controls: Mapping[str, str | int] | None = None,
        target_projects: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        result = self._index_session().index_repository(
            tree,
            database_path,
            project,
            mode,
            force_full=force_full,
            incremental_controls=incremental_controls,
            target_projects=target_projects,
        )
        return checked_index_execution(result, force_full, incremental_controls)

    def list_projects(self, options: Mapping[str, object]) -> dict[str, Any]:
        session = (
            self._index
            if self._index is not None and "project-list" in self._index.capabilities
            else self._query_session()
        )
        return session.list_projects(options)

    def delete_project(self, database_path: Path, project: str) -> tuple[bool, str]:
        session = self._session_for(database_path)
        try:
            result = session.delete_project(database_path, project)
        except CBMTransportError as exc:
            return False, str(exc)[-1000:]
        return True, json.dumps(result, ensure_ascii=False)[-1000:]

    def compare_graphs(
        self,
        base: object,
        target: object,
        *,
        limit: int,
        scan_limit: int,
    ) -> dict[str, Any]:
        if (
            not isinstance(base, _NativeBinding)
            or not isinstance(target, _NativeBinding)
            or base.transport is not self
            or target.transport is not self
        ):
            raise CBMTransportError("graph handles belong to different CBM transports")
        return target.session.compare_graphs(
            base_database=base.database_path,
            base_project=base.project,
            target_database=target.database_path,
            target_project=target.project,
            limit=limit,
            scan_limit=scan_limit,
        )

    def close(self) -> None:
        with self._lock:
            sessions = [session for session in (self._query, self._index) if session is not None]
            self._query = None
            self._index = None
        for session in sessions:
            session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
