"""Own build-scoped resources around one reusable CBM client.

The runner accepts per-commit build plans and exposes only the published
database path and execution metadata. Persistence and consumer coordination
remain outside this package.
"""

from __future__ import annotations

import resource
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self

from .client import CBMClient, CBMTransportError

if TYPE_CHECKING:
    from .binary import CBMBinary
    from .plan import BuildPlan
    from .transports.native import NativeHelper


def _total_memory() -> int:
    candidates = []
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            candidates.append(int(line.split()[1]) * 1024)
            break
    cgroup = Path("/sys/fs/cgroup/memory.max")
    if cgroup.exists() and (value := cgroup.read_text().strip()) != "max":
        candidates.append(int(value))
    return min(candidates)


def _size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return 0


def _rss(pid: int | None) -> int:
    if pid is None:
        return 0
    total, pending, seen = 0, [pid], set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            for line in Path(f"/proc/{current}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1]) * 1024
                    break
            children = Path(f"/proc/{current}/task/{current}/children").read_text().split()
            pending.extend(int(child) for child in children)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    return total


class ResourceMonitor:
    """Sample scratch bytes and aggregate process RSS during CBM execution."""

    def __init__(self, paths: list[Path], memory_limit: int):
        self.paths = paths
        self.memory_limit = memory_limit
        self.peak_scratch = 0
        self.peak_rss = 0
        self._children: set[int] = set()
        self._children_lock = threading.Lock()
        self.exceeded = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        """Start background sampling."""
        self._thread.start()

    def sample(self) -> None:
        """Capture one resource high-water sample."""
        with self._children_lock:
            children = tuple(self._children)
        self.peak_scratch = max(self.peak_scratch, sum(_size(path) for path in self.paths))
        own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        self.peak_rss = max(self.peak_rss, own + sum(_rss(pid) for pid in children))
        self.exceeded |= self.peak_rss > self.memory_limit

    def add_child(self, pid: int) -> None:
        """Include a transport child in aggregate RSS observations."""
        with self._children_lock:
            self._children.add(pid)

    def remove_child(self, pid: int) -> None:
        """Stop observing a transport child after its process exits."""
        with self._children_lock:
            self._children.discard(pid)

    def _loop(self) -> None:
        while not self._stop.wait(0.1):
            self.sample()

    def stop(self) -> None:
        """Stop sampling and retain the final high-water values."""
        self._stop.set()
        self._thread.join(timeout=2)
        self.sample()


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    """Peak build resources observed by a runner."""

    scratch_bytes: int
    rss_bytes: int


class CBMRunner:
    """Reuse one CBM client across independently planned commit builds."""

    def __init__(
        self,
        binary: CBMBinary | None,
        project: str,
        cache_root: str | Path,
        work_dir: str | Path,
        *,
        timeout: int = 3600,
        memory_limit: int | None = None,
        transport: str = "native",
        native_index_helper: NativeHelper | str | Path | None = None,
    ):
        """Start a build-scoped CBM runner.

        Args:
            binary: Authenticated CBM executable; native indexing does not use it.
            project: Stable CBM project receiving every planned commit.
            cache_root: Directory containing CBM's atomically published database.
            work_dir: Runner-owned materialized Git tree and commit state.
            timeout: Maximum seconds for one CBM request.
            memory_limit: Aggregate runner and child RSS ceiling; omission reserves
                one GiB for the host.
            transport: Native SDK or one-process-per-call MCP/CLI index path.
            native_index_helper: Explicit full SDK helper for the native index path.
        """
        self.binary = binary
        self.project = project
        self.cache_root = Path(cache_root)
        self.work_dir = Path(work_dir)
        self.tree = self.work_dir / "tree"
        self.db_path = self.cache_root / f"{project}.db"
        limit = memory_limit or max(1 << 30, _total_memory() - (1 << 30))
        self.monitor = ResourceMonitor(
            [self.work_dir, self.db_path, self.db_path.with_suffix(".db-wal")],
            limit,
        )
        self.monitor.start()
        self._client: CBMClient | None = None
        self._closed = False
        try:
            started = time.monotonic()
            self._client = CBMClient(
                binary,
                cache_root=self.cache_root,
                transport=transport,
                native_index_helper=native_index_helper,
                timeout=timeout,
                resource_monitor=self.monitor,
            )
            self.capabilities = self._client.capabilities()
            self.engine = self._client.index_engine
            self.transport_name = transport
            self.startup_seconds = round(time.monotonic() - started, 3)
        except BaseException:
            if self._client is not None:
                self._client.close()
                self._client = None
            self.monitor.stop()
            raise

    @property
    def resources(self) -> ResourceUsage:
        """Return current resource high-water values."""
        self.monitor.sample()
        return ResourceUsage(self.monitor.peak_scratch, self.monitor.peak_rss)

    def index(self, plan: BuildPlan) -> dict:
        """Publish the graph produced by one per-commit CBM plan.

        Args:
            plan: Analysis coverage, route request, and delta controls for this commit.

        Returns:
            CBM's machine-readable execution evidence when available.

        Raises:
            CBMTransportError: Required CBM capabilities are absent or indexing fails.
        """
        required = set(plan.required_capabilities())
        if missing := required - self.capabilities:
            raise CBMTransportError(f"CBM binary lacks required capabilities: {sorted(missing)}")
        if self._client is None:
            raise CBMTransportError("CBM runner is closed")
        return self._client.index_repository(
            self.tree,
            self.project,
            plan.analysis_mode,
            force_full=plan.force_full,
            incremental_controls=plan.incremental.to_dict(),
            target_projects=plan.target_projects or None,
        )

    def delete_project(self) -> tuple[bool, str]:
        """Delete the runner's CBM project through its client."""
        if self._client is None:
            return False, "CBM runner is closed"
        return self._client.delete_project(self.project)

    def cleanup_work_dir(self) -> None:
        """Remove only this runner's explicitly configured work directory."""
        if self.work_dir.exists():
            shutil.rmtree(self.work_dir)

    def close(self) -> None:
        """Stop the client and resource sampler without deleting build state."""
        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            self._client.close()
            self._client = None
        self.monitor.stop()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
