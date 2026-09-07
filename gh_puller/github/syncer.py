"""Orchestrate immediate, recoverable incremental observations of issues and PRs.

Each run freezes a cycle boundary. Cold starts enumerate every parent item; later runs
scan overlapping committed checkpoints for parent and comment signals in immutable
creation order. Directory pages and tasks reach SQLite before independent bounded API
and Git workers consume them. Complete fact sets publish immediately, while the
discovery checkpoint advances only after the directory and every task close.

GitHub signals do not cover every mutable child resource. Silent deletions and
unsignaled old changes are not proactively sought, but selecting a parent observes all
promised fact collections again.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from .collector import GitHubFactCollector, _validate_issue
from .collector import IncompleteGitHubDataError as _IncompleteGitHubDataError
from .locking import archive_lock
from .observations import (
    DiscoveryItemDraft,
    ObservationArchive,
    SyncCycle,
    SyncTask,
    TaskDraft,
)
from .progress import ProgressObserver, _SyncProgressTracker
from .runtime import GitHubAPIReader, GitHubRuntime, GitObjectWriter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_NUMBER_AT_END = re.compile(r"/(\d+)$")
_CATALOG_ACCEPT = "application/vnd.github.raw+json"
_API_TASK_KINDS = frozenset({"closing-issues", "parent"})
_COMMIT_BATCH_SIZE = 512
IncompleteGitHubDataError = _IncompleteGitHubDataError


@dataclass(frozen=True, slots=True)
class GitHubSyncConfig:
    """Configure one repository-bound observation writer."""

    repository: str
    destination: Path
    token: str | None = None
    api_url: str = "https://api.github.com"
    graphql_url: str | None = None
    api_version: str = "2022-11-28"
    concurrency: int = 8
    git_batch_size: int = 8
    request_timeout: float = 30.0
    overlap_seconds: int = 2
    git_url: str | None = None
    git_destination: Path | None = None

    def __post_init__(self) -> None:
        owner, separator, repo = self.repository.partition("/")
        if not separator or not owner or not repo or "/" in repo:
            raise ValueError("repository must be 'owner/repo'")
        if self.concurrency < 1 or self.git_batch_size < 1:
            raise ValueError("concurrency and git_batch_size must be positive")
        if self.overlap_seconds < 1:
            raise ValueError("overlap_seconds must be positive")
        if self.git_url == "":
            raise ValueError("git_url cannot be empty")
        object.__setattr__(self, "destination", Path(self.destination))
        if self.git_destination is not None:
            object.__setattr__(self, "git_destination", Path(self.git_destination))


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Summarize one completed operational sync cycle."""

    cycle_id: int
    started_at: datetime
    completed_at: datetime
    checkpoint_from: datetime | None
    discovered_items: int
    requests: int


async def sync(
    config: GitHubSyncConfig,
    *,
    observer: ProgressObserver | None = None,
) -> SyncResult:
    """Run or resume one repository synchronization.

    Args:
        config: Repository, archive destinations, and request policy.
        observer: Disposable out-of-band progress receiver.

    Returns:
        Completed operational cycle metadata. Facts become visible individually before
        the cycle finishes.
    """
    return await GitHubSyncer(config, observer=observer).sync()


class GitHubSyncer:
    """Run an incremental synchronization operation to completion.

    Args:
        config: Repository, SQLite, Git store, and request-concurrency policy.
        api: GitHub reader injected by tests or a host.
        git: Git object store injected by tests or a host.
        now: Aware clock used for real read windows and cycle boundaries.
        sleep: Async wait function used by Git transport retries.
        observer: Progress receiver that does not participate in fact publication.
        runtime: Dependency assembly shared with maintenance, normally built internally.
    """

    def __init__(
        self,
        config: GitHubSyncConfig,
        *,
        api: GitHubAPIReader | None = None,
        git: GitObjectWriter | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        observer: ProgressObserver | None = None,
        runtime: GitHubRuntime | None = None,
    ) -> None:
        self.config = config
        self._now = now
        self._observer = observer
        self._progress = _SyncProgressTracker(observer, now)
        self._runtime = runtime or GitHubRuntime(
            config,
            api=api,
            git=git,
            now=now,
            sleep=sleep,
        )
        self._owner, self._repo = config.repository.split("/", 1)
        self._base = f"/repos/{self._owner}/{self._repo}"
        self._store_lock = self._runtime.store_lock
        self._collector = self._make_collector()

    async def sync(self) -> SyncResult:
        """Resume or execute one complete discovery-and-observation cycle.

        Returns:
            The completed internal cycle and request accounting. Every fact was already
            visible when its own source operation closed.

        Raises:
            IncompleteGitHubDataError: A response is detectably truncated or malformed.
            GitHubAPIError: GitHub rejects an operation without a coverage conclusion.
            GitStoreError: Required Git evidence cannot be safely retained.
        """
        self._progress = _SyncProgressTracker(self._observer, self._now)
        self._collector.bind_progress(self._progress)
        self._progress.start()
        try:
            invoked_at = _utc(self._now())
            async with (
                archive_lock(self.config.destination),
                ObservationArchive(
                    self.config.destination,
                    self.config.repository,
                    self._runtime.git_destination,
                ) as archive,
            ):
                cycle = await archive.start_cycle(invoked_at)
                api, owned = self._runtime.make_api(self._progress.api_progress)
                git = self._runtime.make_git(
                    upstream_synced=await archive.task_completed(cycle.id, "git-refs"),
                )
                request_start = api.request_count
                self._progress.bind_cycle(
                    cycle.id,
                    cycle.checkpoint_from,
                    cycle.request_count,
                    request_start,
                )
                accounted = False
                try:
                    await self._sync_cycle(api, git, archive, cycle)
                    await archive.add_requests(cycle.id, api.request_count - request_start)
                    accounted = True
                    completed_at = _utc(self._now())
                    completed = await archive.complete_cycle(cycle.id, completed_at)
                    result = SyncResult(
                        cycle_id=completed.id,
                        started_at=completed.started_at,
                        completed_at=completed_at,
                        checkpoint_from=completed.checkpoint_from,
                        discovered_items=completed.discovered_items,
                        requests=completed.request_count,
                    )
                    self._progress.done(result.requests)
                    return result
                finally:
                    if not accounted:
                        await archive.add_requests(
                            cycle.id,
                            api.request_count - request_start,
                        )
                    if owned:
                        await api.close()
        except Exception as exc:
            self._progress.error(exc)
            raise

    async def _sync_cycle(
        self,
        api: GitHubAPIReader,
        git: GitObjectWriter,
        archive: ObservationArchive,
        cycle: SyncCycle,
    ) -> None:
        self._progress.phase("discovering")
        cursor = await self._prepare_discovery(api, archive, cycle)
        while cursor is not None:
            self._progress.phase("discovering", "catalog")
            observed_from = _utc(self._now())
            page = await api.get_page(cursor, accept=_CATALOG_ACCEPT)
            observed_until = _utc(self._now())
            current = await archive.active_cycle()
            if current is None or current.id != cycle.id:
                raise RuntimeError("active sync cycle disappeared")
            items = tuple(_discovery_item(item, observed_from, observed_until) for item in page.items)
            tasks = tuple(_parent_task(item.number) for item in items)
            pull_numbers = [item.number for item in items if item.kind == "pull"]
            if pull_numbers:
                tasks += (
                    TaskDraft(
                        task_key=f"closing:{current.discovery_pages + 1}",
                        kind="closing-issues",
                        subject_key=f"catalog-page:{current.discovery_pages + 1}",
                        payload={"numbers": pull_numbers},
                    ),
                )
            async with self._store_lock:
                updated = await archive.save_discovery_page(
                    cycle.id,
                    cursor,
                    page.next_url,
                    items,
                    tasks,
                )
            self._progress.phase("fetching")
            await self._drain(api, git, archive, cycle.id)
            cursor = updated.discovery_cursor
        self._progress.phase("fetching")
        await self._drain(api, git, archive, cycle.id)

    async def _prepare_discovery(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        cycle: SyncCycle,
    ) -> str | None:
        if not cycle.discovery_started:
            tasks = [
                TaskDraft("git-refs", "git-refs", "repository", {}),
            ]
            if cycle.checkpoint_from is not None:
                issue_numbers, pull_numbers = await self._comment_signals(
                    api,
                    cycle.checkpoint_from,
                )
                async with self._store_lock:
                    await archive.record_discovery_signals(
                        cycle.id,
                        issue_numbers,
                        pull_numbers,
                    )
                tasks.extend(_parent_task(number) for number in sorted(issue_numbers | pull_numbers))
            async with self._store_lock:
                await archive.enqueue_tasks(cycle.id, tuple(tasks))
        initial = _catalog_url(
            self._base,
            cycle.checkpoint_from,
            self.config.overlap_seconds,
        )
        async with self._store_lock:
            return await archive.begin_discovery(cycle.id, initial)

    async def _comment_signals(
        self,
        api: GitHubAPIReader,
        checkpoint: datetime,
    ) -> tuple[set[int], set[int]]:
        since = _iso_seconds(checkpoint - timedelta(seconds=self.config.overlap_seconds))
        issue_comments = await api.paginate(
            f"{self._base}/issues/comments",
            params={"sort": "created", "direction": "asc", "since": since},
        )
        pull_comments = await api.paginate(
            f"{self._base}/pulls/comments",
            params={"sort": "created", "direction": "asc", "since": since},
        )
        return (
            _signal_numbers(issue_comments, "issue_url"),
            _signal_numbers(pull_comments, "pull_request_url"),
        )

    async def _drain(
        self,
        api: GitHubAPIReader,
        git: GitObjectWriter,
        archive: ObservationArchive,
        cycle_id: int,
    ) -> None:
        stop = asyncio.Event()
        api_done = asyncio.Event()
        git_ready = asyncio.Event()
        git_ready.set()
        api_errors, git_errors = await asyncio.gather(
            self._drain_api(
                api,
                git,
                archive,
                cycle_id,
                stop,
                api_done,
                git_ready,
            ),
            self._drain_git(
                api,
                git,
                archive,
                cycle_id,
                stop,
                api_done,
                git_ready,
            ),
        )
        failures = [*api_errors, *git_errors]
        if failures:
            raise failures[0]

    async def _drain_api(
        self,
        api: GitHubAPIReader,
        git: GitObjectWriter,
        archive: ObservationArchive,
        cycle_id: int,
        stop: asyncio.Event,
        api_done: asyncio.Event,
        git_ready: asyncio.Event,
    ) -> list[Exception]:
        failures: list[Exception] = []
        try:
            while not stop.is_set():
                async with self._store_lock:
                    tasks = await archive.take_tasks(
                        cycle_id,
                        self.config.concurrency,
                        kinds=_API_TASK_KINDS,
                    )
                if not tasks:
                    break
                self._progress.phase("fetching")
                errors = await asyncio.gather(
                    *(
                        self._run_api_task(api, git, archive, task, git_ready)
                        for task in tasks
                    ),
                )
                failures.extend(error for error in errors if error is not None)
                if failures:
                    stop.set()
        except BaseException:
            stop.set()
            raise
        finally:
            api_done.set()
            git_ready.set()
        return failures

    async def _run_api_task(
        self,
        api: GitHubAPIReader,
        git: GitObjectWriter,
        archive: ObservationArchive,
        task: SyncTask,
        git_ready: asyncio.Event,
    ) -> Exception | None:
        try:
            return await self._collector.run_sync_task(api, git, archive, task)
        finally:
            git_ready.set()

    async def _drain_git(
        self,
        api: GitHubAPIReader,
        git: GitObjectWriter,
        archive: ObservationArchive,
        cycle_id: int,
        stop: asyncio.Event,
        api_done: asyncio.Event,
        git_ready: asyncio.Event,
    ) -> list[Exception]:
        failures: list[Exception] = []
        try:
            while not stop.is_set():
                git_ready.clear()
                tasks = await self._take_git_tasks(archive, cycle_id)
                if not tasks:
                    if api_done.is_set():
                        break
                    await git_ready.wait()
                    continue
                errors = await self._run_git_tasks(api, git, archive, tasks)
                failures.extend(error for error in errors if error is not None)
                if failures:
                    stop.set()
        except BaseException:
            stop.set()
            raise
        return failures

    async def _take_git_tasks(
        self,
        archive: ObservationArchive,
        cycle_id: int,
    ) -> tuple[SyncTask, ...]:
        async with self._store_lock:
            refs = await archive.take_tasks(cycle_id, 1, kinds=("git-refs",))
            if refs:
                return refs
            pulls = await archive.take_tasks(
                cycle_id,
                self.config.git_batch_size,
                kinds=("pull-git",),
            )
            commits = await archive.take_tasks(
                cycle_id,
                _COMMIT_BATCH_SIZE,
                kinds=("commit-object",),
            )
        return (*pulls, *commits)

    async def _run_git_tasks(
        self,
        api: GitHubAPIReader,
        git: GitObjectWriter,
        archive: ObservationArchive,
        tasks: tuple[SyncTask, ...],
    ) -> list[Exception | None]:
        git_refs = [task for task in tasks if task.kind == "git-refs"]
        pull_git = [task for task in tasks if task.kind == "pull-git"]
        commit_objects = [task for task in tasks if task.kind == "commit-object"]
        errors = list(
            await asyncio.gather(
                *(
                    self._collector.run_sync_task(api, git, archive, task)
                    for task in git_refs
                ),
            ),
        )
        if pull_git:
            errors.extend(
                await self._collector.run_pull_git_batch(git, archive, pull_git),
            )
        if commit_objects:
            errors.extend(
                await self._collector.run_commit_batch(git, archive, commit_objects),
            )
        return errors

    def _make_collector(self) -> GitHubFactCollector:
        return GitHubFactCollector(
            self.config,
            now=self._now,
            progress=self._progress,
            store_lock=self._store_lock,
        )


def _discovery_item(
    value: dict[str, Any],
    observed_from: datetime,
    observed_until: datetime,
) -> DiscoveryItemDraft:
    number = value.get("number")
    if type(number) is not int or number < 1:
        raise IncompleteGitHubDataError("catalog item has an invalid number")
    _validate_issue(value, number)
    return DiscoveryItemDraft(
        number=number,
        kind="pull" if "pull_request" in value else "issue",
        observed_from=observed_from,
        observed_until=observed_until,
        summary=value,
    )


def _parent_task(number: int) -> TaskDraft:
    return TaskDraft(
        task_key=f"parent:{number}",
        kind="parent",
        subject_key=f"issue:{number}",
        resource_number=number,
        payload={"number": number},
    )


def _catalog_url(
    base: str,
    checkpoint: datetime | None,
    overlap_seconds: int,
) -> str:
    parameters = {
        "state": "all",
        "sort": "created",
        "direction": "asc",
        "per_page": 100,
    }
    if checkpoint is not None:
        parameters["since"] = _iso_seconds(
            checkpoint - timedelta(seconds=overlap_seconds),
        )
    return f"{base}/issues?{urlencode(parameters)}"


def _signal_numbers(items: list[dict[str, Any]], field: str) -> set[int]:
    numbers = set()
    for item in items:
        value = item.get(field)
        match = _NUMBER_AT_END.search(value) if isinstance(value, str) else None
        if match is not None:
            numbers.add(int(match.group(1)))
    return numbers


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _iso_seconds(value: datetime) -> str:
    return _utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")
