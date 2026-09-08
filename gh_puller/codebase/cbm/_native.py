"""Implement private native engine backends and the immutable store cache.

This module owns helper authentication, framed control messages, native index
requests, cross-process materialization locks, and cache identity. KGA parsing
and CBM algorithms remain in the helpers; public routing remains in
:mod:`.client`.
"""

from __future__ import annotations

import fcntl
import json
import os
import queue
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Self

from ..archive import COVERAGE_FIDELITY_VERSION, GRAPH_FIDELITY_VERSION, Archive
from ._daemon import CBMTransportError, ResourceMonitorLike

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

_ARCHIVE_PROTOCOL_VERSION = 8
_INDEX_PROTOCOL_VERSION = 9
_RESPONSE_MAX_BYTES = 256 << 20
_ARCHIVE_HELPER_ENV = "GH_PULLER_CODEBASE_CBM_HELPER"
_INDEX_HELPER_ENV = "GH_PULLER_CODEBASE_CBM_INDEX_HELPER"
_CACHE_SCHEMA = 4
_STREAM_CLOSED = object()


@dataclass(frozen=True, slots=True)
class NativeHelper:
    """An executable helper pinned to the bytes inspected before startup."""

    path: Path
    sha256: str
    size: int
    version: str
    source: str
    _device: int
    _inode: int
    _mtime_ns: int

    def verify_unchanged(self) -> None:
        """Reject replacement of the helper before it is executed."""
        try:
            status = self.path.stat()
        except OSError as exc:
            raise CBMTransportError(f"native CBM helper disappeared: {self.path}") from exc
        identity = (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)
        expected = (self._device, self._inode, self.size, self._mtime_ns)
        if identity != expected:
            raise CBMTransportError(f"native CBM helper changed after resolution: {self.path}")

    def provenance(self) -> dict[str, object]:
        """Return stable helper identity for archive and summary metadata."""
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "bytes": self.size,
            "version": self.version,
            "source": self.source,
        }


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def resolve_native_helper(
    helper: str | Path | NativeHelper | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    environment_key: str = _ARCHIVE_HELPER_ENV,
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
    if isinstance(helper, NativeHelper):
        helper.verify_unchanged()
        return helper
    values = os.environ if environ is None else environ
    configured = helper if helper is not None else values.get(environment_key)
    source = "explicit" if helper is not None else f"environment:{environment_key}"
    candidate: Path | None = None
    if configured is not None:
        raw = os.fspath(configured)
        located = shutil.which(raw, path=values.get("PATH")) if os.sep not in raw else None
        candidate = Path(located or raw).expanduser().resolve()
    else:
        local = Path(__file__).resolve().parents[3] / "build" / "native" / local_name
        located = shutil.which(local_name, path=values.get("PATH"))
        candidate = local if local.exists() else Path(located).resolve() if located else None
        source = "local-build" if local.exists() else "PATH"
    if candidate is None:
        raise CBMTransportError(
            f"no native CBM helper: pass its path, set {environment_key}, or build Makefile.native",
        )
    try:
        status = candidate.stat()
    except OSError as exc:
        raise CBMTransportError(f"native CBM helper does not exist: {candidate}") from exc
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise CBMTransportError(f"native CBM helper is not executable: {candidate}")
    try:
        result = subprocess.run(
            [str(candidate), "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
            env=dict(values),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CBMTransportError(f"cannot execute native CBM helper {candidate}: {exc}") from exc
    version = result.stdout.strip().splitlines()
    if result.returncode or not version or not version[0].startswith("gh-puller-cbm-helper "):
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise CBMTransportError(f"invalid native CBM helper {candidate}: {detail}")
    return NativeHelper(
        candidate,
        _hash_file(candidate),
        status.st_size,
        version[0],
        source,
        status.st_dev,
        status.st_ino,
        status.st_mtime_ns,
    )


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


class NativeArchiveTransport:
    """One thread-safe helper process with a content-addressed store cache."""

    def __init__(
        self,
        helper: str | Path | NativeHelper | None,
        cache_root: Path,
        timeout: float,
        environment: Mapping[str, str] | None = None,
        monitor: ResourceMonitorLike | None = None,
        *,
        helper_environment_key: str = _ARCHIVE_HELPER_ENV,
        helper_filename: str = "gh-puller-cbm-helper",
        protocol: int = _ARCHIVE_PROTOCOL_VERSION,
        required_capabilities: frozenset[str] = frozenset(
            {
                "archive-load",
                "tool-call",
                "graph-compare",
                "project-open",
                "project-open-read-only",
                "project-list",
                "project-delete",
            },
        ),
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
        self.loaded_digest: str | None = None
        self.loaded_materialization_digest: str | None = None
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
            self.monitor.child_pid = self.process.pid
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
                or hello.get("kga_format") != 5
                or hello.get("graph_fidelity") != GRAPH_FIDELITY_VERSION
                or hello.get("coverage_fidelity") != COVERAGE_FIDELITY_VERSION
                or not isinstance(capabilities, list)
                or not all(isinstance(item, str) for item in capabilities)
                or not required_capabilities <= set(capabilities)
                or not isinstance(tools, list)
                or not tools
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
        except BaseException:
            self.close()
            raise

    @property
    def pid(self) -> int:
        """Return the persistent helper process ID."""
        return self.process.pid

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
        if self.monitor is not None and self.monitor.child_pid == self.process.pid:
            self.monitor.child_pid = None

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

    def _cache_paths(self, digest: str) -> tuple[Path, Path, Path]:
        directory = self.cache_root / "archive-query-v1" / self.helper.sha256
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return (
            directory / f"{digest}.db",
            directory / f"{digest}.ready.json",
            directory / f"{digest}.lock",
        )

    def load_archive(
        self,
        archive: str | Path | Archive,
        commit: str | None = None,
        *,
        allow_incomplete: bool = True,
    ) -> dict[str, Any]:
        """Materialize and retain one archived commit without Python bulk rows.

        Args:
            archive: KGA path or an already captured reader.
            commit: Exact archived commit. ``None`` selects the captured latest.
            allow_incomplete: When opening a path, accept its final durable writer
                checkpoint instead of requiring a final footer.

        Returns:
            Loaded identity, row counts, materialization status, and cache path.
        """
        owned = not isinstance(archive, Archive)
        reader = Archive(archive, allow_incomplete=allow_incomplete) if owned else archive
        try:
            manifest, project = reader._restorable_manifest(commit)
            status = os.fstat(reader._reader.fd)
            archive_path = reader.path.resolve(strict=True)
            materialization = manifest.get("materialization_digest", manifest["graph_digest"])
            coverage_fidelity = manifest.get("coverage_fidelity_version", 0)
            coverage_count = manifest.get("coverage_rows", 0)
            database, marker, lock = self._cache_paths(materialization)
            expected_marker = {
                "schema": _CACHE_SCHEMA,
                "helper_sha256": self.helper.sha256,
                "store_format": self.store_format,
                "graph_digest": manifest["graph_digest"],
                "materialization_digest": materialization,
                "project": project,
                "nodes": manifest["nodes"],
                "edges": manifest["edges"],
                "graph_fidelity": manifest["graph_fidelity_version"],
                "coverage_digest": manifest.get("coverage_digest"),
                "coverage_rows": coverage_count,
                "coverage_fidelity": coverage_fidelity,
            }
            parameters = {
                "archive_path": str(archive_path),
                "archive_device": status.st_dev,
                "archive_inode": status.st_ino,
                "captured_size": reader._reader.size,
                "project": project,
                "graph_digest": manifest["graph_digest"],
                "materialization_digest": materialization,
                "database_path": str(database),
                "node_count": manifest["nodes"],
                "edge_count": manifest["edges"],
                "graph_fidelity": manifest["graph_fidelity_version"],
                "coverage_fidelity": coverage_fidelity,
                "coverage_count": coverage_count,
                "node_root": manifest["node_root"],
                "edge_root": manifest["edge_root"],
                "coverage_root": manifest.get("coverage_root"),
                "coverage_metadata": manifest.get("coverage_metadata"),
            }
            with _exclusive_lock(lock, self.timeout):
                parameters["reuse"] = database.is_file() and _read_marker(marker) == expected_marker
                result = self._request("load", parameters)
                expected_result = {
                    "project": project,
                    "graph_digest": manifest["graph_digest"],
                    "materialization_digest": materialization,
                    "nodes": manifest["nodes"],
                    "edges": manifest["edges"],
                    "graph_fidelity": manifest["graph_fidelity_version"],
                    "coverage_rows": coverage_count,
                    "coverage_fidelity": coverage_fidelity,
                }
                if (
                    any(result.get(key) != value for key, value in expected_result.items())
                    or type(result.get("materialized")) is not bool
                ):
                    raise CBMTransportError("native CBM helper returned the wrong loaded graph identity")
                _write_marker(marker, expected_marker)
            self.loaded_project = project
            self.loaded_database_path = database
            self.loaded_source_root = None
            self.loaded_digest = manifest["graph_digest"]
            self.loaded_materialization_digest = materialization
            return {**result, "database_path": str(database)}
        finally:
            if owned:
                reader.close()

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
        self.loaded_project = project
        self.loaded_database_path = database_path
        self.loaded_source_root = source_root
        self.loaded_digest = None
        self.loaded_materialization_digest = None
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
        self.loaded_project = None
        self.loaded_database_path = None
        self.loaded_source_root = None
        self.loaded_digest = None
        self.loaded_materialization_digest = None

    def query_graph(self, *, project: str, query: str, graph: str = "code", max_rows: int = 0) -> dict[str, Any]:
        """Query the loaded immutable CBM store directly.

        Args:
            project: Project identity returned by :meth:`load_archive`.
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
        """Compare two materialized archive generations with request-scoped handles.

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
        if name not in self.tools:
            raise CBMTransportError(f"native CBM tool is not supported: {name}")
        return self._request("call", {"name": name, "arguments": dict(arguments or {})})

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


class NativeIndexTransport(NativeArchiveTransport):
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
            required_capabilities=frozenset(
                {
                    "repository-index",
                    "project-open",
                    "project-open-read-only",
                    "project-list",
                    "project-delete",
                    "granular-delta-controls",
                    "force-full-route",
                },
            ),
        )

    def index_repository(
        self,
        tree: Path,
        database_path: Path,
        project: str,
        mode: str,
        *,
        force_full: bool,
        incremental_controls: Mapping[str, str | int] | None,
        target_projects: Sequence[str] | None,
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


@contextmanager
def _exclusive_lock(path: Path, timeout: float) -> Iterator[None]:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CBMTransportError(f"timed out waiting for native CBM cache lock: {path}") from None
                time.sleep(0.05)
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_marker(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_marker(path: Path, value: Mapping[str, object]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
