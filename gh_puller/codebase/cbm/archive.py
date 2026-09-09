"""Materialize KGA snapshots as immutable CBM stores.

This module owns archive inspection, immutable-store cache identity, and the
KGA-specific helper request. Opening and querying the resulting database belong
to the pure CBM client, which never imports this adapter or the archive format.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..archive import (
    COVERAGE_FIDELITY_VERSION,
    GRAPH_FIDELITY_VERSION,
    Archive,
    ArchiveError,
)
from ..utils import (
    NativeExecutable,
    NativeExecutableError,
    NullResourceMonitor,
    ResourceMonitorLike,
    resolve_native_executable,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_CACHE_SCHEMA = 5
_MATERIALIZER_PROTOCOL = 1
_MATERIALIZER_ENV = "GH_PULLER_CODEBASE_KGA_MATERIALIZER"
_MATERIALIZER_NAME = "gh-puller-kga-materializer"


def _materializer_contract(executable: NativeExecutable) -> int:
    fields: dict[str, int] = {}
    parts = executable.version.split()
    try:
        protocol = int(parts[1])
        for part in parts[2:]:
            name, value = part.split("=", 1)
            fields[name] = int(value)
    except (IndexError, ValueError):
        protocol = -1
    if (
        protocol != _MATERIALIZER_PROTOCOL
        or fields.get("kga") != 5
        or fields.get("graph") != GRAPH_FIDELITY_VERSION
        or fields.get("coverage") != COVERAGE_FIDELITY_VERSION
        or fields.get("store", 0) < 1
        or fields.get("sdk", 0) < 3
    ):
        raise ArchiveError("KGA materializer has an incompatible contract")
    return fields["store"]


@dataclass(frozen=True, slots=True)
class CBMArchiveStore:
    """Describe one KGA snapshot materialized as an immutable CBM store."""

    database_path: Path
    project: str
    nodes: int
    edges: int
    graph_digest: str
    materialization_digest: str
    graph_fidelity: int
    coverage_rows: int
    coverage_fidelity: int
    materialized: bool


class CBMArchiveAdapter:
    """Materialize KGA snapshots without owning or opening CBM graphs."""

    def __init__(
        self,
        materializer: NativeExecutable | str | Path | None,
        cache_root: str | Path,
        timeout: float,
        environment: Mapping[str, str] | None = None,
        monitor: ResourceMonitorLike | None = None,
    ):
        """Configure one KGA-to-CBM adapter.

        Args:
            materializer: One-shot KGA-to-CBM materializer executable.
            cache_root: Parent of engine-versioned immutable stores.
            timeout: Maximum seconds for a helper request or cache lock.
            environment: Child-process environment overrides.
            monitor: Optional process and memory observer.
        """
        if timeout <= 0:
            raise ValueError("archive materialization timeout must be positive")
        self.cache_root = Path(cache_root).expanduser().resolve()
        self.timeout = timeout
        self.environment = dict(environment or {})
        self.monitor = monitor or NullResourceMonitor()
        values = {**os.environ, **self.environment}
        try:
            self.materializer = resolve_native_executable(
                materializer,
                environ=values,
                environment_key=_MATERIALIZER_ENV,
                local_name=_MATERIALIZER_NAME,
                version_prefix=f"{_MATERIALIZER_NAME} ",
            )
        except NativeExecutableError as exc:
            raise ArchiveError(str(exc).replace("native executable", "KGA materializer")) from exc
        self.store_format = _materializer_contract(self.materializer)

    def _run(self, parameters: Mapping[str, object]) -> dict:
        try:
            self.materializer.verify_unchanged()
        except NativeExecutableError as exc:
            raise ArchiveError(str(exc).replace("native executable", "KGA materializer")) from exc
        environment = {**os.environ, **self.environment}
        environment.setdefault("CBM_LOG_LEVEL", "error")
        try:
            process = subprocess.Popen(
                [str(self.materializer.path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                env=environment,
            )
        except OSError as exc:
            raise ArchiveError(f"cannot start KGA materializer: {exc}") from exc
        self.monitor.add_child(process.pid)
        try:
            try:
                stdout, stderr = process.communicate(
                    json.dumps(parameters, ensure_ascii=False, separators=(",", ":")) + "\n",
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise ArchiveError(f"KGA materialization timed out after {self.timeout:g}s") from None
        finally:
            self.monitor.remove_child(process.pid)
        if process.returncode:
            detail = (stderr or stdout).strip()[-4000:]
            raise ArchiveError(f"KGA materialization failed: {detail}")
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ArchiveError("KGA materializer returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ArchiveError("KGA materializer returned a non-object result")
        self.monitor.sample()
        if self.monitor.exceeded:
            raise ArchiveError("memory limit exceeded while materializing KGA")
        return result

    @staticmethod
    def _cache_paths(cache_root: Path, materializer_digest: str, graph_digest: str) -> tuple[Path, Path, Path]:
        directory = cache_root / "archive-cbm-v1" / materializer_digest
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return (
            directory / f"{graph_digest}.db",
            directory / f"{graph_digest}.ready.json",
            directory / f"{graph_digest}.lock",
        )

    def materialize(
        self,
        archive: str | Path | Archive,
        commit: str | None = None,
        *,
        allow_incomplete: bool = True,
    ) -> CBMArchiveStore:
        """Materialize one archived commit without moving bulk rows through Python.

        Args:
            archive: KGA path or an already captured reader.
            commit: Exact archived commit. ``None`` selects the captured latest.
            allow_incomplete: For path inputs, accept the writer's final durable
                checkpoint while the archive is still being built.

        Returns:
            The immutable CBM store identity and KGA materialization metadata.
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
            database, marker, lock = self._cache_paths(
                self.cache_root,
                self.materializer.sha256,
                materialization,
            )
            expected_marker = {
                "schema": _CACHE_SCHEMA,
                "materializer_sha256": self.materializer.sha256,
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
                result = self._run(parameters)
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
                    raise ArchiveError("KGA materializer returned the wrong graph identity")
                _write_marker(marker, expected_marker)
            return CBMArchiveStore(
                database_path=database,
                project=project,
                nodes=result["nodes"],
                edges=result["edges"],
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
                    raise ArchiveError(f"timed out waiting for KGA cache lock: {path}") from None
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
