"""Coordinate asynchronous construction of versioned indexes for online requests.

The snapshot registry remains the authority for repository paths. Missing indexes
move through queued, running, and verifying states in one process-local queue;
CBM's published project binding is the authority for readiness. Callers poll by
repeating their original business-tool request, so no public index tool or task
identifier is required.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from vllm_kb_adapter.prebuild import indexed_projects
from vllm_kb_adapter.upstream import MCPUpstream, tool_error_text

if TYPE_CHECKING:
    from vllm_kb_adapter.snapshots import Snapshot

IndexState = Literal["queued", "running", "verifying", "failed"]


class _IndexingError(RuntimeError):
    """An online index could not be built or verified."""


@dataclass(frozen=True, slots=True)
class IndexNotice:
    """Stable public projection of an unfinished or failed index task."""

    state: IndexState
    snapshot: Snapshot
    error: str | None = None

    def payload(self) -> dict[str, str]:
        """Return the structured result forwarded to vllm-kb."""
        data = {
            "kind": "index_repository",
            "state": self.state,
            "project": self.snapshot.logical_project,
            "version": self.snapshot.version,
        }
        if self.error is not None:
            data["error"] = self.error
        return data


@dataclass(slots=True)
class _Job:
    snapshot: Snapshot
    state: IndexState = "queued"
    error: str | None = None
    task: asyncio.Task[None] | None = None


class IndexCoordinator:
    """Run one online index at a time without tying it to an HTTP request."""

    def __init__(self, upstream: MCPUpstream, ready: tuple[Snapshot, ...]) -> None:
        """Create an index queue from the startup binding audit.

        Args:
            upstream: Long-lived gh-puller-mcp client without a request timeout.
            ready: Snapshots whose index names already bind to their exact paths.
        """
        self._upstream = upstream
        self._ready = {snapshot.index_name for snapshot in ready}
        self._jobs: dict[str, _Job] = {}
        self._lock = asyncio.Lock()
        self._worker = asyncio.Semaphore(1)
        self._closed = False

    async def prepare(self, snapshot: Snapshot) -> IndexNotice | None:
        """Return ``None`` for a ready index or its current asynchronous state.

        Args:
            snapshot: Trusted versioned source selected by the snapshot registry.
        """
        async with self._lock:
            if snapshot.index_name in self._ready:
                return None
            if self._closed:
                return IndexNotice("failed", snapshot, "online index coordinator is closed")
            job = self._jobs.get(snapshot.index_name)
            if job is None:
                job = self._queue(snapshot)
                return IndexNotice(job.state, snapshot)
            notice = IndexNotice(job.state, snapshot, job.error)
            if job.state == "failed":
                self._jobs[snapshot.index_name] = self._new_job(snapshot)
            return notice

    async def aclose(self) -> None:
        """Cancel adapter-owned waiters and close the indexing transport."""
        async with self._lock:
            self._closed = True
            tasks = [job.task for job in self._jobs.values() if job.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._upstream.aclose()

    def _queue(self, snapshot: Snapshot) -> _Job:
        job = self._new_job(snapshot)
        self._jobs[snapshot.index_name] = job
        return job

    def _new_job(self, snapshot: Snapshot) -> _Job:
        job = _Job(snapshot)
        job.task = asyncio.create_task(
            self._run(job),
            name=f"index:{snapshot.index_name}",
        )
        return job

    async def _run(self, job: _Job) -> None:
        async with self._worker:
            await self._set_state(job, "running")
            try:
                projects = await indexed_projects(self._upstream)
                bound = projects.get(job.snapshot.index_name)
                if bound is not None and bound != job.snapshot.path:
                    raise _IndexingError(
                        f"index {job.snapshot.index_name} points to {bound}, expected {job.snapshot.path}",
                    )
                if bound is None:
                    result = await self._upstream.call_tool(
                        "index_repository",
                        {
                            "repo_path": str(job.snapshot.path),
                            "name": job.snapshot.index_name,
                            "mode": "full",
                        },
                    )
                    if result.get("isError"):
                        raise _IndexingError(tool_error_text(result))
                await self._set_state(job, "verifying")
                projects = await indexed_projects(self._upstream)
                if projects.get(job.snapshot.index_name) != job.snapshot.path:
                    raise _IndexingError(f"index {job.snapshot.index_name} was not published at the expected path")
            except Exception as exc:  # Background jobs must always publish a terminal state to later requests.
                await self._fail(job, str(exc))
                return
            async with self._lock:
                if self._jobs.get(job.snapshot.index_name) is job:
                    self._jobs.pop(job.snapshot.index_name)
                    self._ready.add(job.snapshot.index_name)

    async def _set_state(self, job: _Job, state: IndexState) -> None:
        async with self._lock:
            if self._jobs.get(job.snapshot.index_name) is job:
                job.state = state

    async def _fail(self, job: _Job, error: str) -> None:
        async with self._lock:
            if self._jobs.get(job.snapshot.index_name) is job:
                job.state = "failed"
                job.error = error
