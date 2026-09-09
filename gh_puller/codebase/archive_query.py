"""Adapt KGA snapshots to graph handles accepted by the pure CBM client.

This module owns archive inspection, immutable-store cache identity, and the
KGA-specific helper request. It hands the materialized store to a pure CBM
client; the CBM package never imports this adapter or the archive format.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self

from .archive import COVERAGE_FIDELITY_VERSION, GRAPH_FIDELITY_VERSION, Archive
from .cbm._native import NativeHelper, _NativeSession
from .cbm._transport import CBMTransportError, ResourceMonitorLike
from .cbm.client import CBMClient, GraphHandle

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_CACHE_SCHEMA = 4


class _ArchiveMonitor:
    """Discard process observations for a standalone archive loader."""

    exceeded = False

    def add_child(self, pid: int) -> None:
        return

    def remove_child(self, pid: int) -> None:
        return

    def sample(self) -> None:
        return


@dataclass(frozen=True, slots=True)
class ArchiveGraph(GraphHandle):
    """Describe one KGA snapshot loaded as a CBM graph handle."""

    database_path: Path
    graph_digest: str
    materialization_digest: str
    graph_fidelity: int
    coverage_rows: int
    coverage_fidelity: int
    materialized: bool


class ArchiveLoader:
    """Materialize KGA snapshots into stores opened by a CBM client."""

    def __init__(
        self,
        helper: NativeHelper | str | Path | None,
        cache_root: str | Path,
        timeout: float,
        environment: Mapping[str, str] | None = None,
        monitor: ResourceMonitorLike | None = None,
    ):
        """Configure one KGA-to-CBM adapter.

        Args:
            helper: Compact helper capable of KGA materialization and CBM queries.
            cache_root: Parent of engine-versioned immutable stores.
            timeout: Maximum seconds for a helper request or cache lock.
            environment: Child-process environment overrides.
            monitor: Optional process and memory observer.
        """
        if timeout <= 0:
            raise ValueError("archive load timeout must be positive")
        self._helper = helper
        self.cache_root = Path(cache_root).expanduser().resolve()
        self.timeout = timeout
        self.environment = dict(environment or {})
        self.monitor = monitor or _ArchiveMonitor()
        self._lock = threading.Lock()
        self._session: _NativeSession | None = None

    def _materializer(self) -> _NativeSession:
        with self._lock:
            if self._session is None:
                self._session = _NativeSession(
                    self._helper,
                    self.cache_root,
                    self.timeout,
                    self.environment,
                    self.monitor,
                )
            return self._session

    @staticmethod
    def _cache_paths(cache_root: Path, helper_digest: str, graph_digest: str) -> tuple[Path, Path, Path]:
        directory = cache_root / "archive-query-v1" / helper_digest
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return (
            directory / f"{graph_digest}.db",
            directory / f"{graph_digest}.ready.json",
            directory / f"{graph_digest}.lock",
        )

    def load(
        self,
        client: CBMClient,
        archive: str | Path | Archive,
        commit: str | None = None,
        *,
        allow_incomplete: bool = True,
    ) -> ArchiveGraph:
        """Load one archived commit without moving bulk graph rows through Python.

        Args:
            client: Pure CBM client that owns the returned graph handle.
            archive: KGA path or an already captured reader.
            commit: Exact archived commit. ``None`` selects the captured latest.
            allow_incomplete: For path inputs, accept the writer's final durable
                checkpoint while the archive is still being built.

        Returns:
            A CBM graph handle plus immutable KGA identity and cache metadata.
        """
        session = self._materializer()
        if (
            "archive-load" not in session.capabilities
            or session.hello.get("kga_format") != 5
            or session.hello.get("graph_fidelity") != GRAPH_FIDELITY_VERSION
            or session.hello.get("coverage_fidelity") != COVERAGE_FIDELITY_VERSION
        ):
            raise CBMTransportError("native helper does not support this KGA contract")
        owned = not isinstance(archive, Archive)
        reader = Archive(archive, allow_incomplete=allow_incomplete) if owned else archive
        try:
            manifest, project = reader._restorable_manifest(commit)
            status = os.fstat(reader._reader.fd)
            archive_path = reader.path.resolve(strict=True)
            materialization = manifest.get("materialization_digest", manifest["graph_digest"])
            coverage_fidelity = manifest.get("coverage_fidelity_version", 0)
            coverage_count = manifest.get("coverage_rows", 0)
            database, marker, lock = self._cache_paths(
                self.cache_root,
                session.helper.sha256,
                materialization,
            )
            expected_marker = {
                "schema": _CACHE_SCHEMA,
                "helper_sha256": session.helper.sha256,
                "store_format": session.store_format,
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
                result = session._request("load", parameters)
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
                    raise CBMTransportError("native helper returned the wrong KGA graph identity")
                _write_marker(marker, expected_marker)
            opened = client.open_store(database, project, transport="native")
            if opened.nodes != result["nodes"] or opened.edges != result["edges"]:
                raise CBMTransportError("opened CBM store has the wrong graph counts")
            return ArchiveGraph(
                project=opened.project,
                transport=opened.transport,
                source_root=opened.source_root,
                nodes=opened.nodes,
                edges=opened.edges,
                _transport=opened._transport,
                _binding=opened._binding,
                database_path=database,
                graph_digest=result["graph_digest"],
                materialization_digest=result["materialization_digest"],
                graph_fidelity=result["graph_fidelity"],
                coverage_rows=result["coverage_rows"],
                coverage_fidelity=result["coverage_fidelity"],
                materialized=result["materialized"],
            )
        finally:
            if owned:
                reader.close()
    def close(self) -> None:
        """Close the materialization helper without affecting graph handles."""
        with self._lock:
            session = self._session
            self._session = None
        if session is not None:
            session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


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
                    raise CBMTransportError(f"timed out waiting for KGA cache lock: {path}") from None
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
