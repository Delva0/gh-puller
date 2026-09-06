"""编排立即发布、可断点恢复的 GitHub Issue/PR 增量观测。

同步入口在调用前冻结 cycle 起点 S。冷启动遍历全部 Issue/PR；后续从已提交
checkpoint 的重叠边界筛选父对象和评论变更信号。两者均按不可变的创建时间升序
遍历。每个目录页及其任务先落 SQLite，再并发消费。一个事实集合完成后立即追加
观测；cycle 仅在目录闭合且所有任务完成后把内部 discovery checkpoint 推进到 S。

发现信号并不覆盖 GitHub 的全部可变子资源。拉取器不主动寻找静默删除或无信号的
旧对象变化，但一旦父对象被选择，就重新观测其全部已承诺事实集合。
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlencode

from .client import GitHubAPI, GitHubPage, GitHubResource
from .commit_references import CommitReference, observation_commit_references
from .errors import GitHubAPIError
from .git_store import (
    GitObjectStore,
    GitStoreError,
    TransientGitStoreError,
    default_git_url,
    git_store_path,
)
from .locking import archive_lock
from .observations import (
    Coverage,
    DiscoveryItem,
    DiscoveryItemDraft,
    FactDraft,
    FactObservation,
    ObservationArchive,
    Origin,
    SyncCycle,
    SyncTask,
    TaskDraft,
)
from .progress import ProgressObserver, _SyncProgressTracker

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

_NUMBER_AT_END = re.compile(r"/(\d+)$")
_CATALOG_ACCEPT = "application/vnd.github.raw+json"
_SHA = re.compile(r"[0-9a-f]{40,64}\Z")


class IncompleteGitHubDataError(RuntimeError):
    """A source response cannot prove the promised collection is complete."""


class _API(Protocol):
    request_count: int

    async def close(self) -> None: ...

    async def get_json_cached(
        self,
        path: str,
        *,
        previous: Any | None,
        cache: dict[str, Any] | None,
        params: dict[str, Any] | None = None,
        accept: str | None = None,
    ) -> tuple[Any, dict[str, Any] | None]: ...

    async def get_page(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str | None = None,
    ) -> GitHubPage: ...

    async def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        page_observer: Callable[[int], None] | None = None,
    ) -> list[dict[str, Any]]: ...

    async def paginate_cached(
        self,
        path: str,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
        params: dict[str, Any] | None = None,
        page_observer: Callable[[int], None] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]: ...

    async def issue_comments(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource: ...

    async def reactions(
        self,
        path: str,
        node_id: str | None,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource: ...

    async def issue_relations(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> GitHubResource: ...

    async def pull_request(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        previous: dict[str, Any] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource: ...

    async def pull_reviews(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource: ...

    async def pull_review_threads(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> GitHubResource: ...

    async def pull_review_comments(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource: ...

    async def pull_commits(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        expected: int,
        base: str,
        head: str,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource: ...

    async def closing_issue_references(
        self,
        owner: str,
        repo: str,
        numbers: list[int],
    ) -> dict[int, list[dict[str, Any]]]: ...


class _GitStore(Protocol):
    async def sync_upstream(
        self,
        *,
        heartbeat: Callable[[], None] | None = None,
        retry: Callable[[float], None] | None = None,
    ) -> dict[str, Any]: ...

    async def prefetch(
        self,
        pulls: Mapping[int, dict[str, Any]],
        *,
        heartbeat: Callable[[], None] | None = None,
        retry: Callable[[float], None] | None = None,
        retry_transient: bool = True,
    ) -> None: ...

    async def capture(
        self,
        number: int,
        pull: dict[str, Any],
        *,
        heartbeat: Callable[[], None] | None = None,
        retry: Callable[[float], None] | None = None,
    ) -> dict[str, Any]: ...

    async def retain_commits(
        self,
        shas: Sequence[str],
        *,
        heartbeat: Callable[[], None] | None = None,
        retry: Callable[[float], None] | None = None,
    ) -> dict[str, dict[str, Any]]: ...


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
    """执行一个可随时调用并阻塞到完成的增量同步操作。

    Args:
        config: 仓库、SQLite、Git 对象库和请求并发策略。
        api: 测试或宿主注入的 GitHub 读取对象。
        git: 测试或宿主注入的 Git 对象库。
        now: 记录实际读取窗口和 cycle 边界的时区时钟。
        sleep: Git 网络重试使用的异步等待函数。
        observer: 不参与事实发布的同步进度接收器。
    """

    def __init__(
        self,
        config: GitHubSyncConfig,
        *,
        api: _API | None = None,
        git: _GitStore | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        observer: ProgressObserver | None = None,
    ) -> None:
        self.config = config
        self._api = api
        self._git = git
        self._now = now
        self._sleep = sleep
        self._observer = observer
        self._progress = _SyncProgressTracker(observer, now)
        self._owner, self._repo = config.repository.split("/", 1)
        self._base = f"/repos/{self._owner}/{self._repo}"
        self._store_lock = asyncio.Lock()

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
        self._progress.start()
        try:
            invoked_at = _utc(self._now())
            async with (
                archive_lock(self.config.destination),
                ObservationArchive(
                    self.config.destination,
                    self.config.repository,
                    self._git_destination(),
                ) as archive,
            ):
                cycle = await archive.start_cycle(invoked_at)
                api, owned = self._make_api()
                git = self._make_git()
                request_start = api.request_count
                self._progress.bind(
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

    def _make_api(self) -> tuple[_API, bool]:
        if self._api is not None:
            return self._api, False
        return (
            GitHubAPI(
                token=_token(self.config.token),
                base_url=self.config.api_url,
                graphql_url=self.config.graphql_url,
                api_version=self.config.api_version,
                timeout=self.config.request_timeout,
                sleep=self._sleep,
                now=self._now,
                progress=self._progress.api_progress,
            ),
            True,
        )

    def _make_git(self) -> _GitStore:
        if self._git is not None:
            return self._git
        return GitObjectStore(
            self._git_destination(),
            self.config.repository,
            self.config.git_url or default_git_url(self.config.repository),
            token=_token(self.config.token),
            sleep=self._sleep,
        )

    def _git_destination(self) -> Path:
        return self.config.git_destination or git_store_path(self.config.destination)

    async def _sync_cycle(
        self,
        api: _API,
        git: _GitStore,
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
            items = tuple(
                _discovery_item(item, observed_from, observed_until)
                for item in page.items
            )
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
        api: _API,
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
                tasks.extend(
                    _parent_task(number)
                    for number in sorted(issue_numbers | pull_numbers)
                )
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
        api: _API,
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
        api: _API,
        git: _GitStore,
        archive: ObservationArchive,
        cycle_id: int,
    ) -> None:
        while True:
            async with self._store_lock:
                tasks = await archive.take_tasks(cycle_id, self.config.concurrency)
            if not tasks:
                return
            self._progress.phase("fetching")
            pull_git = [task for task in tasks if task.kind == "pull-git"]
            commit_objects = [task for task in tasks if task.kind == "commit-object"]
            ordinary = [
                task
                for task in tasks
                if task.kind not in {"pull-git", "commit-object"}
            ]
            errors = await asyncio.gather(
                *(self._guard_task(api, git, archive, task) for task in ordinary),
            )
            if pull_git:
                errors.extend(
                    await self._guard_pull_git_batch(git, archive, pull_git),
                )
            if commit_objects:
                errors.extend(
                    await self._guard_commit_batch(git, archive, commit_objects),
                )
            failures = [error for error in errors if error is not None]
            if failures:
                raise failures[0]

    async def _guard_task(
        self,
        api: _API,
        git: _GitStore,
        archive: ObservationArchive,
        task: SyncTask,
    ) -> Exception | None:
        try:
            if task.kind == "parent":
                await self._hydrate_parent(api, archive, task)
            elif task.kind == "closing-issues":
                await self._closing_issues(api, archive, task)
            elif task.kind == "git-refs":
                await self._git_refs(git, archive, task)
            else:
                raise RuntimeError(f"unknown sync task kind: {task.kind}")
        except Exception as exc:
            async with self._store_lock:
                await archive.record_task_error(task.id, _error_text(exc))
            return exc
        return None

    async def _guard_pull_git_batch(
        self,
        git: _GitStore,
        archive: ObservationArchive,
        tasks: list[SyncTask],
    ) -> list[Exception | None]:
        pending = []
        for task in tasks:
            if await self._publication(archive, task, "pull-git") is None:
                pending.append(task)
            else:
                await self._finish_task(archive, task)
        if not pending:
            return []
        self._progress.phase("syncing_git", f"pulls={len(pending)}")
        try:
            await self._prefetch_git(git, pending)
        except Exception as exc:
            for task in pending:
                async with self._store_lock:
                    await archive.record_task_error(task.id, _error_text(exc))
            return [exc]
        results = []
        for task in pending:
            try:
                await self._pull_git(git, archive, task)
            except Exception as exc:
                async with self._store_lock:
                    await archive.record_task_error(task.id, _error_text(exc))
                results.append(exc)
            else:
                results.append(None)
        return results

    async def _guard_commit_batch(
        self,
        git: _GitStore,
        archive: ObservationArchive,
        tasks: list[SyncTask],
    ) -> list[Exception | None]:
        pending = []
        for task in tasks:
            if await self._publication(archive, task, "commit-object") is None:
                pending.append(task)
            else:
                async with self._store_lock:
                    await archive.complete_task(task.id, _utc(self._now()))
        if not pending:
            return []
        shas = [_required_sha(task.payload.get("sha"), task.task_key) for task in pending]
        try:
            self._progress.phase("syncing_git", f"commits={len(shas)}")
            observed_from = _utc(self._now())
            results = await git.retain_commits(
                shas,
                heartbeat=self._progress.git_heartbeat,
                retry=self._progress.git_retry,
            )
            observed_until = _utc(self._now())
        except Exception as exc:
            for task in pending:
                async with self._store_lock:
                    await archive.record_task_error(task.id, _error_text(exc))
            return [exc]
        failures: list[Exception | None] = []
        for task, sha in zip(pending, shas, strict=True):
            try:
                result = results.get(sha)
                if not isinstance(result, dict):
                    raise IncompleteGitHubDataError(
                        f"Git retention returned no result for {sha}",
                    )
                status = result.get("status")
                if status == "available":
                    coverage = Coverage.COMPLETE
                elif status == "unavailable":
                    coverage = Coverage.UNAVAILABLE
                else:
                    raise GitStoreError(f"Git retention returned invalid status for {sha}")
                await self._publish(
                    archive,
                    task,
                    "commit-object",
                    (
                        FactDraft(
                            family="commit-object",
                            subject_key=f"commit:{sha}",
                            observed_from=observed_from,
                            observed_until=observed_until,
                            coverage=coverage,
                            origin=Origin.GIT,
                            payload={
                                "operation": "GitCommitRetention",
                                "repository": self.config.repository,
                                "sha": sha,
                                "value": result,
                            },
                        ),
                    ),
                    complete_task=True,
                )
            except Exception as exc:
                async with self._store_lock:
                    await archive.record_task_error(task.id, _error_text(exc))
                failures.append(exc)
            else:
                failures.append(None)
        return failures

    async def _publication(
        self,
        archive: ObservationArchive,
        task: SyncTask,
        operation: str,
    ) -> tuple[FactObservation, ...] | None:
        async with self._store_lock:
            return await archive.publication(_publication_key(task, operation))

    async def _current(
        self,
        archive: ObservationArchive,
        family: str,
        subject_key: str,
    ) -> FactObservation | None:
        async with self._store_lock:
            return await archive.current_fact(family, subject_key)

    async def _publish(
        self,
        archive: ObservationArchive,
        task: SyncTask,
        operation: str,
        facts: tuple[FactDraft, ...],
        *,
        complete_task: bool = False,
    ) -> tuple[FactObservation, ...]:
        async with self._store_lock:
            return await archive.publish(
                _publication_key(task, operation),
                "sync",
                _utc(self._now()),
                facts,
                cycle_id=task.cycle_id,
                task_id=task.id if complete_task else None,
            )

    async def _finish_task(
        self,
        archive: ObservationArchive,
        task: SyncTask,
    ) -> None:
        async with self._store_lock:
            await archive.complete_task(task.id, _utc(self._now()))

    async def _hydrate_parent(
        self,
        api: _API,
        archive: ObservationArchive,
        task: SyncTask,
    ) -> None:
        number = _task_number(task)
        async with self._store_lock:
            item = await archive.discovery_item(task.cycle_id, number)
            issue_signal, pull_signal = await archive.discovery_signals(
                task.cycle_id,
                number,
            )
        issue_fact = await self._issue_fact(api, archive, task, item)
        if issue_fact.coverage is not Coverage.COMPLETE:
            await self._finish_task(archive, task)
            return
        issue = _fact_object(issue_fact, f"issue #{number}")
        kind = "pull" if "pull_request" in issue else "issue"
        if item is not None and item.kind != kind:
            raise IncompleteGitHubDataError(
                f"catalog and root disagree on issue #{number} kind",
            )

        comments = await self._issue_comments(
            api,
            archive,
            task,
            issue_fact,
            force=issue_signal,
        )
        timeline = await self._rest_collection(
            api,
            archive,
            task,
            "issue-timeline",
            f"issue:{number}",
            "IssueTimeline",
            f"{self._base}/issues/{number}/timeline",
            number,
        )
        events = await self._rest_collection(
            api,
            archive,
            task,
            "issue-events",
            f"issue:{number}",
            "IssueEvents",
            f"{self._base}/issues/{number}/events",
            number,
        )
        await self._issue_reactions(api, archive, task, issue_fact)
        if comments.coverage is Coverage.COMPLETE:
            await self._comment_reactions(
                api,
                archive,
                task,
                comments,
                endpoint="issues/comments",
                family="issue-comment-reactions",
                subject_prefix="issue-comment",
            )

        sources = [timeline, events]
        if kind == "issue":
            await self._issue_relations(api, archive, task)
        else:
            sources.extend(
                await self._pull_facts(
                    api,
                    archive,
                    task,
                    force_review_comments=pull_signal,
                    cataloged=item is not None,
                ),
            )
        await self._structured_commits(archive, task, sources)
        await self._finish_task(archive, task)

    async def _issue_fact(
        self,
        api: _API,
        archive: ObservationArchive,
        task: SyncTask,
        item: DiscoveryItem | None,
    ) -> FactObservation:
        existing = await self._publication(archive, task, "issue")
        if existing is not None:
            return _single(existing, "issue")
        number = _task_number(task)
        subject = f"issue:{number}"
        if item is not None:
            _validate_issue(item.summary, number)
            return _single(
                await self._publish(
                    archive,
                    task,
                    "issue",
                    (
                        FactDraft(
                            family="issue",
                            subject_key=subject,
                            resource_number=number,
                            observed_from=item.observed_from,
                            observed_until=item.observed_until,
                            coverage=Coverage.COMPLETE,
                            origin=Origin.API,
                            payload=_source_payload(
                                "RepositoryIssueCatalog",
                                self.config.repository,
                                number,
                                item.summary,
                                "rest",
                                item.summary,
                                None,
                            ),
                        ),
                    ),
                ),
                "issue",
            )
        previous, cache = await self._previous(archive, "issue", subject)
        observed_from = _utc(self._now())
        path = f"{self._base}/issues/{number}"
        try:
            value, updated = await api.get_json_cached(
                path,
                previous=previous,
                cache=cache,
                accept=_CATALOG_ACCEPT,
            )
        except GitHubAPIError as exc:
            coverage = _coverage_error(exc, unauthenticated=False)
            if coverage is None:
                raise
            return await self._coverage_fact(
                archive,
                task,
                "issue",
                "issue",
                subject,
                number,
                observed_from,
                coverage,
                exc,
            )
        observed_until = _utc(self._now())
        issue = _object(value, f"issue #{number}")
        _validate_issue(issue, number)
        return _single(
            await self._publish(
                archive,
                task,
                "issue",
                (
                    FactDraft(
                        family="issue",
                        subject_key=subject,
                        resource_number=number,
                        observed_from=observed_from,
                        observed_until=observed_until,
                        coverage=Coverage.COMPLETE,
                        origin=Origin.API,
                        payload=_source_payload(
                            "Issue",
                            self.config.repository,
                            number,
                            issue,
                            "rest",
                            issue,
                            updated,
                        ),
                    ),
                ),
            ),
            "issue",
        )

    async def _issue_comments(
        self,
        api: _API,
        archive: ObservationArchive,
        task: SyncTask,
        issue_fact: FactObservation,
        *,
        force: bool,
    ) -> FactObservation:
        number = _task_number(task)
        issue = _fact_object(issue_fact, f"issue #{number}")
        if not force and _zero(issue.get("comments")):
            return await self._derived(
                archive,
                task,
                "issue-comments",
                "issue-comments",
                f"issue:{number}",
                number,
                [],
                issue_fact,
                "IssueCommentCount",
            )
        return await self._resource_collection(
            archive,
            task,
            "issue-comments",
            "issue-comments",
            f"issue:{number}",
            "IssueComments",
            number,
            lambda previous, cache: api.issue_comments(
                self._owner,
                self._repo,
                number,
                previous=previous,
                cache=cache,
            ),
        )

    async def _issue_reactions(
        self,
        api: _API,
        archive: ObservationArchive,
        task: SyncTask,
        issue_fact: FactObservation,
    ) -> FactObservation:
        number = _task_number(task)
        issue = _fact_object(issue_fact, f"issue #{number}")
        if _zero_count(issue.get("reactions")):
            return await self._derived(
                archive,
                task,
                "issue-reactions",
                "issue-reactions",
                f"issue:{number}",
                number,
                [],
                issue_fact,
                "IssueReactionCount",
            )
        return await self._resource_collection(
            archive,
            task,
            "issue-reactions",
            "issue-reactions",
            f"issue:{number}",
            "IssueReactions",
            number,
            lambda previous, cache: api.reactions(
                f"{self._base}/issues/{number}/reactions",
                _optional_string(issue.get("node_id")),
                previous=previous,
                cache=cache,
            ),
        )

    async def _comment_reactions(
        self,
        api: _API,
        archive: ObservationArchive,
        task: SyncTask,
        comments_fact: FactObservation,
        *,
        endpoint: str,
        family: str,
        subject_prefix: str,
    ) -> None:
        comments = _fact_list(comments_fact, family)
        for comment in comments:
            comment_id = comment.get("id")
            if type(comment_id) is not int or comment_id < 1:
                raise IncompleteGitHubDataError(f"{family} source has an invalid comment ID")
            operation = f"{family}:{comment_id}"
            subject = f"{subject_prefix}:{comment_id}"
            if _zero_count(comment.get("reactions")):
                await self._derived(
                    archive,
                    task,
                    operation,
                    family,
                    subject,
                    _task_number(task),
                    [],
                    comments_fact,
                    "CommentReactionCount",
                )
                continue
            await self._resource_collection(
                archive,
                task,
                operation,
                family,
                subject,
                "CommentReactions",
                _task_number(task),
                lambda previous, cache, comment_id=comment_id, comment=comment: api.reactions(
                    f"{self._base}/{endpoint}/{comment_id}/reactions",
                    _optional_string(comment.get("node_id")),
                    previous=previous,
                    cache=cache,
                ),
            )

    async def _pull_facts(
        self,
        api: _API,
        archive: ObservationArchive,
        task: SyncTask,
        *,
        force_review_comments: bool,
        cataloged: bool,
    ) -> list[FactObservation]:
        number = _task_number(task)
        subject = f"pull:{number}"
        detail = await self._resource_object(
            archive,
            task,
            "pull",
            "pull",
            subject,
            "PullRequest",
            number,
            lambda previous, cache: api.pull_request(
                self._owner,
                self._repo,
                number,
                previous=previous,
                cache=cache,
            ),
        )
        if detail.coverage is not Coverage.COMPLETE:
            return [detail]
        reviews = await self._resource_collection(
            archive,
            task,
            "pull-reviews",
            "pull-reviews",
            subject,
            "PullReviews",
            number,
            lambda previous, cache: api.pull_reviews(
                self._owner,
                self._repo,
                number,
                previous=previous,
                cache=cache,
            ),
        )
        threads = await self._review_threads(api, archive, task)
        review_comments = await self._review_comments(
            api,
            archive,
            task,
            detail,
            threads,
            force=force_review_comments,
        )
        commits = await self._pull_commits(api, archive, task, detail)
        await self._requested_reviewers(api, archive, task, detail)
        if review_comments.coverage is Coverage.COMPLETE:
            await self._comment_reactions(
                api,
                archive,
                task,
                review_comments,
                endpoint="pulls/comments",
                family="pull-review-comment-reactions",
                subject_prefix="pull-review-comment",
            )
        await self._enqueue_pull_git(archive, task, detail)
        if not cataloged:
            await self._enqueue_closing_issues(archive, task, number)
        return [reviews, threads, review_comments, commits]

    async def _review_threads(
        self,
        api: _API,
        archive: ObservationArchive,
        task: SyncTask,
    ) -> FactObservation:
        existing = await self._publication(archive, task, "pull-review-threads")
        if existing is not None:
            return _single(existing, "pull-review-threads")
        number = _task_number(task)
        observed_from = _utc(self._now())
        try:
            resource = await api.pull_review_threads(self._owner, self._repo, number)
        except GitHubAPIError as exc:
            coverage = _coverage_error(exc, unauthenticated=True)
            if coverage is None:
                raise
            return await self._coverage_fact(
                archive,
                task,
                "pull-review-threads",
                "pull-review-threads",
                f"pull:{number}",
                number,
                observed_from,
                coverage,
                exc,
            )
        observed_until = _utc(self._now())
        value = _object(resource.value, f"pull #{number} review threads")
        comments = _objects(value.get("review_comments"), "review thread comments")
        threads = _object(value.get("threads"), "review thread connection")
        _objects(threads.get("nodes"), "review threads")
        payload = _source_payload(
            "PullReviewThreads",
            self.config.repository,
            number,
            value,
            resource.source,
            resource.raw,
            resource.cache,
        )
        payload["comment_count"] = len(comments)
        return _single(
            await self._publish(
                archive,
                task,
                "pull-review-threads",
                (
                    FactDraft(
                        family="pull-review-threads",
                        subject_key=f"pull:{number}",
                        resource_number=number,
                        observed_from=observed_from,
                        observed_until=observed_until,
                        coverage=Coverage.COMPLETE,
                        origin=Origin.API,
                        payload=payload,
                    ),
                ),
            ),
            "pull-review-threads",
        )

    async def _review_comments(
        self,
        api: _API,
        archive: ObservationArchive,
        task: SyncTask,
        detail: FactObservation,
        threads: FactObservation,
        *,
        force: bool,
    ) -> FactObservation:
        number = _task_number(task)
        pull = _fact_object(detail, f"pull #{number}")
        if threads.coverage is Coverage.COMPLETE:
            value = _fact_object(threads, "review threads").get("review_comments")
            comments = _objects(value, "review thread comments")
            return await self._derived(
                archive,
                task,
                "pull-review-comments",
                "pull-review-comments",
                f"pull:{number}",
                number,
                comments,
                threads,
                "PullReviewThreads",
            )
        if not force and _zero(pull.get("review_comments")):
            return await self._derived(
                archive,
                task,
                "pull-review-comments",
                "pull-review-comments",
                f"pull:{number}",
                number,
                [],
                detail,
                "PullReviewCommentCount",
            )
        return await self._resource_collection(
            archive,
            task,
            "pull-review-comments",
            "pull-review-comments",
            f"pull:{number}",
            "PullReviewComments",
            number,
            lambda previous, cache: api.pull_review_comments(
                self._owner,
                self._repo,
                number,
                previous=previous,
                cache=cache,
            ),
        )

    async def _pull_commits(
        self,
        api: _API,
        archive: ObservationArchive,
        task: SyncTask,
        detail: FactObservation,
    ) -> FactObservation:
        number = _task_number(task)
        pull = _fact_object(detail, f"pull #{number}")
        expected = pull.get("commits")
        if _zero(expected):
            return await self._derived(
                archive,
                task,
                "pull-commits",
                "pull-commits",
                f"pull:{number}",
                number,
                [],
                detail,
                "PullCommitCount",
            )
        if type(expected) is not int or expected < 1:
            raise IncompleteGitHubDataError(f"pull #{number} has an invalid commit count")
        base, head = _comparison_shas(pull, number)
        return await self._resource_collection(
            archive,
            task,
            "pull-commits",
            "pull-commits",
            f"pull:{number}",
            "PullCommits",
            number,
            lambda previous, cache: api.pull_commits(
                self._owner,
                self._repo,
                number,
                expected=expected,
                base=base,
                head=head,
                previous=previous,
                cache=cache,
            ),
        )

    async def _requested_reviewers(
        self,
        api: _API,
        archive: ObservationArchive,
        task: SyncTask,
        detail: FactObservation,
    ) -> FactObservation:
        number = _task_number(task)
        pull = _fact_object(detail, f"pull #{number}")
        embedded = _embedded_review_requests(pull)
        if embedded is not None:
            return await self._derived(
                archive,
                task,
                "pull-requested-reviewers",
                "pull-requested-reviewers",
                f"pull:{number}",
                number,
                embedded,
                detail,
                "PullRequestDetail",
            )
        return await self._rest_object(
            api,
            archive,
            task,
            "pull-requested-reviewers",
            "pull-requested-reviewers",
            f"pull:{number}",
            "PullRequestedReviewers",
            f"{self._base}/pulls/{number}/requested_reviewers",
            number,
        )

    async def _issue_relations(
        self,
        api: _API,
        archive: ObservationArchive,
        task: SyncTask,
    ) -> FactObservation:
        existing = await self._publication(archive, task, "issue-relations")
        if existing is not None:
            return _single(existing, "issue-relations")
        number = _task_number(task)
        observed_from = _utc(self._now())
        try:
            resource = await api.issue_relations(self._owner, self._repo, number)
        except GitHubAPIError as exc:
            coverage = _coverage_error(exc, unauthenticated=True)
            if coverage is None:
                raise
            return await self._coverage_fact(
                archive,
                task,
                "issue-relations",
                "issue-relations",
                f"issue:{number}",
                number,
                observed_from,
                coverage,
                exc,
            )
        observed_until = _utc(self._now())
        value = _object(resource.value, f"issue #{number} relations")
        return _single(
            await self._publish(
                archive,
                task,
                "issue-relations",
                (
                    FactDraft(
                        family="issue-relations",
                        subject_key=f"issue:{number}",
                        resource_number=number,
                        observed_from=observed_from,
                        observed_until=observed_until,
                        coverage=Coverage.COMPLETE,
                        origin=Origin.API,
                        payload=_source_payload(
                            "IssueRelations",
                            self.config.repository,
                            number,
                            value,
                            resource.source,
                            resource.raw,
                            resource.cache,
                        ),
                    ),
                ),
            ),
            "issue-relations",
        )

    async def _resource_collection(
        self,
        archive: ObservationArchive,
        task: SyncTask,
        operation_key: str,
        family: str,
        subject_key: str,
        operation: str,
        resource_number: int,
        load: Callable[
            [list[dict[str, Any]] | None, dict[str, Any] | None],
            Awaitable[GitHubResource],
        ],
    ) -> FactObservation:
        existing = await self._publication(archive, task, operation_key)
        if existing is not None:
            return _single(existing, operation_key)
        previous, cache = await self._previous(archive, family, subject_key)
        previous_list = _optional_objects(previous)
        observed_from = _utc(self._now())
        try:
            resource = await load(previous_list, cache)
        except GitHubAPIError as exc:
            coverage = _coverage_error(exc, unauthenticated=False)
            if coverage is None:
                raise
            return await self._coverage_fact(
                archive,
                task,
                operation_key,
                family,
                subject_key,
                resource_number,
                observed_from,
                coverage,
                exc,
            )
        observed_until = _utc(self._now())
        value = _objects(resource.value, operation)
        return _single(
            await self._publish(
                archive,
                task,
                operation_key,
                (
                    FactDraft(
                        family=family,
                        subject_key=subject_key,
                        resource_number=resource_number,
                        observed_from=observed_from,
                        observed_until=observed_until,
                        coverage=Coverage.COMPLETE,
                        origin=Origin.API,
                        payload=_source_payload(
                            operation,
                            self.config.repository,
                            resource_number,
                            value,
                            resource.source,
                            resource.raw,
                            resource.cache,
                        ),
                    ),
                ),
            ),
            operation_key,
        )

    async def _resource_object(
        self,
        archive: ObservationArchive,
        task: SyncTask,
        operation_key: str,
        family: str,
        subject_key: str,
        operation: str,
        resource_number: int,
        load: Callable[
            [dict[str, Any] | None, dict[str, Any] | None],
            Awaitable[GitHubResource],
        ],
    ) -> FactObservation:
        existing = await self._publication(archive, task, operation_key)
        if existing is not None:
            return _single(existing, operation_key)
        previous, cache = await self._previous(archive, family, subject_key)
        previous_object = previous if isinstance(previous, dict) else None
        observed_from = _utc(self._now())
        try:
            resource = await load(previous_object, cache)
        except GitHubAPIError as exc:
            coverage = _coverage_error(exc, unauthenticated=False)
            if coverage is None:
                raise
            return await self._coverage_fact(
                archive,
                task,
                operation_key,
                family,
                subject_key,
                resource_number,
                observed_from,
                coverage,
                exc,
            )
        observed_until = _utc(self._now())
        value = _object(resource.value, operation)
        return _single(
            await self._publish(
                archive,
                task,
                operation_key,
                (
                    FactDraft(
                        family=family,
                        subject_key=subject_key,
                        resource_number=resource_number,
                        observed_from=observed_from,
                        observed_until=observed_until,
                        coverage=Coverage.COMPLETE,
                        origin=Origin.API,
                        payload=_source_payload(
                            operation,
                            self.config.repository,
                            resource_number,
                            value,
                            resource.source,
                            resource.raw,
                            resource.cache,
                        ),
                    ),
                ),
            ),
            operation_key,
        )

    async def _rest_collection(
        self,
        api: _API,
        archive: ObservationArchive,
        task: SyncTask,
        operation_key: str,
        subject_key: str,
        operation: str,
        path: str,
        resource_number: int,
    ) -> FactObservation:
        existing = await self._publication(archive, task, operation_key)
        if existing is not None:
            return _single(existing, operation_key)
        previous, cache = await self._previous(archive, operation_key, subject_key)
        observed_from = _utc(self._now())
        try:
            value, updated = await api.paginate_cached(
                path,
                previous=_optional_objects(previous),
                cache=cache,
            )
        except GitHubAPIError as exc:
            coverage = _coverage_error(exc, unauthenticated=False)
            if coverage is None:
                raise
            return await self._coverage_fact(
                archive,
                task,
                operation_key,
                operation_key,
                subject_key,
                resource_number,
                observed_from,
                coverage,
                exc,
            )
        observed_until = _utc(self._now())
        items = _objects(value, operation)
        return _single(
            await self._publish(
                archive,
                task,
                operation_key,
                (
                    FactDraft(
                        family=operation_key,
                        subject_key=subject_key,
                        resource_number=resource_number,
                        observed_from=observed_from,
                        observed_until=observed_until,
                        coverage=Coverage.COMPLETE,
                        origin=Origin.API,
                        payload=_source_payload(
                            operation,
                            self.config.repository,
                            resource_number,
                            items,
                            "rest",
                            items,
                            updated,
                        ),
                    ),
                ),
            ),
            operation_key,
        )

    async def _rest_object(
        self,
        api: _API,
        archive: ObservationArchive,
        task: SyncTask,
        operation_key: str,
        family: str,
        subject_key: str,
        operation: str,
        path: str,
        resource_number: int,
    ) -> FactObservation:
        existing = await self._publication(archive, task, operation_key)
        if existing is not None:
            return _single(existing, operation_key)
        previous, cache = await self._previous(archive, family, subject_key)
        observed_from = _utc(self._now())
        try:
            value, updated = await api.get_json_cached(
                path,
                previous=previous if isinstance(previous, dict) else None,
                cache=cache,
            )
        except GitHubAPIError as exc:
            coverage = _coverage_error(exc, unauthenticated=False)
            if coverage is None:
                raise
            return await self._coverage_fact(
                archive,
                task,
                operation_key,
                family,
                subject_key,
                resource_number,
                observed_from,
                coverage,
                exc,
            )
        observed_until = _utc(self._now())
        item = _object(value, operation)
        return _single(
            await self._publish(
                archive,
                task,
                operation_key,
                (
                    FactDraft(
                        family=family,
                        subject_key=subject_key,
                        resource_number=resource_number,
                        observed_from=observed_from,
                        observed_until=observed_until,
                        coverage=Coverage.COMPLETE,
                        origin=Origin.API,
                        payload=_source_payload(
                            operation,
                            self.config.repository,
                            resource_number,
                            item,
                            "rest",
                            item,
                            updated,
                        ),
                    ),
                ),
            ),
            operation_key,
        )

    async def _derived(
        self,
        archive: ObservationArchive,
        task: SyncTask,
        operation_key: str,
        family: str,
        subject_key: str,
        resource_number: int,
        value: Any,
        source: FactObservation,
        evidence: str,
    ) -> FactObservation:
        existing = await self._publication(archive, task, operation_key)
        if existing is not None:
            return _single(existing, operation_key)
        return _single(
            await self._publish(
                archive,
                task,
                operation_key,
                (
                    FactDraft(
                        family=family,
                        subject_key=subject_key,
                        resource_number=resource_number,
                        source_digest=source.payload_digest,
                        observed_from=source.observed_from,
                        observed_until=source.observed_until,
                        coverage=Coverage.COMPLETE,
                        origin=Origin.DERIVED,
                        payload={
                            "operation": evidence,
                            "repository": self.config.repository,
                            "resource_number": resource_number,
                            "source_observation_id": source.id,
                            "source_payload_digest": source.payload_digest,
                            "value": value,
                        },
                    ),
                ),
            ),
            operation_key,
        )

    async def _coverage_fact(
        self,
        archive: ObservationArchive,
        task: SyncTask,
        operation_key: str,
        family: str,
        subject_key: str,
        resource_number: int,
        observed_from: datetime,
        coverage: Coverage,
        error: GitHubAPIError,
    ) -> FactObservation:
        return _single(
            await self._publish(
                archive,
                task,
                operation_key,
                (
                    FactDraft(
                        family=family,
                        subject_key=subject_key,
                        resource_number=resource_number,
                        observed_from=observed_from,
                        observed_until=_utc(self._now()),
                        coverage=coverage,
                        origin=Origin.API,
                        payload={
                            "operation": operation_key,
                            "repository": self.config.repository,
                            "resource_number": resource_number,
                            "error": {
                                "type": type(error).__name__,
                                "message": str(error),
                                "status_code": error.status_code,
                                "url": error.url,
                            },
                        },
                    ),
                ),
            ),
            operation_key,
        )

    async def _previous(
        self,
        archive: ObservationArchive,
        family: str,
        subject_key: str,
    ) -> tuple[Any | None, dict[str, Any] | None]:
        previous = await self._current(archive, family, subject_key)
        if previous is None or previous.coverage is not Coverage.COMPLETE:
            return None, None
        value = previous.payload.get("value")
        cache = previous.payload.get("cache")
        return value, cache if isinstance(cache, dict) else None

    async def _closing_issues(
        self,
        api: _API,
        archive: ObservationArchive,
        task: SyncTask,
    ) -> None:
        existing = await self._publication(archive, task, "closing-issues")
        if existing is not None:
            await self._finish_task(archive, task)
            return
        value = task.payload.get("numbers")
        if (
            not isinstance(value, list)
            or not value
            or len(value) > 100
            or any(type(number) is not int or number < 1 for number in value)
            or len(set(value)) != len(value)
        ):
            raise RuntimeError("closing-issues task has invalid PR numbers")
        numbers = list(value)
        observed_from = _utc(self._now())
        try:
            results = await api.closing_issue_references(
                self._owner,
                self._repo,
                numbers,
            )
        except GitHubAPIError as exc:
            coverage = _coverage_error(exc, unauthenticated=True)
            if coverage is None:
                raise
            observed_until = _utc(self._now())
            facts = tuple(
                FactDraft(
                    family="pull-closing-issues",
                    subject_key=f"pull:{number}",
                    resource_number=number,
                    observed_from=observed_from,
                    observed_until=observed_until,
                    coverage=coverage,
                    origin=Origin.API,
                    payload={
                        "operation": "ClosingIssuesReferences",
                        "repository": self.config.repository,
                        "resource_number": number,
                        "error": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                            "status_code": exc.status_code,
                            "url": exc.url,
                        },
                    },
                )
                for number in numbers
            )
        else:
            observed_until = _utc(self._now())
            if set(results) != set(numbers):
                raise IncompleteGitHubDataError(
                    "closing issue response does not match its requested PRs",
                )
            facts = tuple(
                FactDraft(
                    family="pull-closing-issues",
                    subject_key=f"pull:{number}",
                    resource_number=number,
                    observed_from=observed_from,
                    observed_until=observed_until,
                    coverage=Coverage.COMPLETE,
                    origin=Origin.API,
                    payload=_source_payload(
                        "ClosingIssuesReferences",
                        self.config.repository,
                        number,
                        _objects(results[number], f"pull #{number} closing issues"),
                        "graphql",
                        results[number],
                        None,
                    ),
                )
                for number in numbers
            )
        await self._publish(
            archive,
            task,
            "closing-issues",
            facts,
            complete_task=True,
        )

    async def _git_refs(
        self,
        git: _GitStore,
        archive: ObservationArchive,
        task: SyncTask,
    ) -> None:
        existing = await self._publication(archive, task, "git-refs")
        if existing is not None:
            await self._finish_task(archive, task)
            return
        self._progress.phase("syncing_git", "upstream")
        observed_from = _utc(self._now())
        refs = await git.sync_upstream(
            heartbeat=self._progress.git_heartbeat,
            retry=self._progress.git_retry,
        )
        observed_until = _utc(self._now())
        await self._publish(
            archive,
            task,
            "git-refs",
            (
                FactDraft(
                    family="git-refs",
                    subject_key="repository",
                    resource_number=None,
                    observed_from=observed_from,
                    observed_until=observed_until,
                    coverage=Coverage.COMPLETE,
                    origin=Origin.GIT,
                    payload={
                        "operation": "GitRefObservation",
                        "repository": self.config.repository,
                        "value": _object(refs, "Git ref observation"),
                    },
                ),
            ),
            complete_task=True,
        )

    async def _enqueue_pull_git(
        self,
        archive: ObservationArchive,
        task: SyncTask,
        detail: FactObservation,
    ) -> None:
        number = _task_number(task)
        pull = _fact_object(detail, f"pull #{number}")
        async with self._store_lock:
            await archive.enqueue_tasks(
                task.cycle_id,
                (
                    TaskDraft(
                        task_key=f"pull-git:{number}",
                        kind="pull-git",
                        subject_key=f"pull:{number}",
                        resource_number=number,
                        payload={"number": number, "pull": pull},
                    ),
                ),
            )

    async def _enqueue_closing_issues(
        self,
        archive: ObservationArchive,
        task: SyncTask,
        number: int,
    ) -> None:
        async with self._store_lock:
            await archive.enqueue_tasks(
                task.cycle_id,
                (
                    TaskDraft(
                        task_key=f"closing:single:{number}",
                        kind="closing-issues",
                        subject_key=f"pull:{number}",
                        resource_number=number,
                        payload={"numbers": [number]},
                    ),
                ),
            )

    async def _prefetch_git(
        self,
        git: _GitStore,
        tasks: list[SyncTask],
    ) -> None:
        for offset in range(0, len(tasks), self.config.git_batch_size):
            await self._prefetch_git_group(
                git,
                tasks[offset : offset + self.config.git_batch_size],
            )

    async def _prefetch_git_group(
        self,
        git: _GitStore,
        tasks: list[SyncTask],
    ) -> None:
        pulls = {
            _task_number(task): _object(task.payload.get("pull"), task.task_key)
            for task in tasks
        }
        try:
            await git.prefetch(
                pulls,
                heartbeat=self._progress.git_heartbeat,
                retry=self._progress.git_retry,
                retry_transient=len(tasks) == 1,
            )
        except TransientGitStoreError:
            if len(tasks) == 1:
                raise
            middle = len(tasks) // 2
            await self._prefetch_git_group(git, tasks[:middle])
            await self._prefetch_git_group(git, tasks[middle:])

    async def _pull_git(
        self,
        git: _GitStore,
        archive: ObservationArchive,
        task: SyncTask,
    ) -> None:
        existing = await self._publication(archive, task, "pull-git")
        if existing is not None:
            await self._finish_task(archive, task)
            return
        number = _task_number(task)
        pull = _object(task.payload.get("pull"), task.task_key)
        observed_from = _utc(self._now())
        snapshot = await git.capture(
            number,
            pull,
            heartbeat=self._progress.git_heartbeat,
            retry=self._progress.git_retry,
        )
        observed_until = _utc(self._now())
        await self._publish(
            archive,
            task,
            "pull-git",
            (
                FactDraft(
                    family="pull-git",
                    subject_key=f"pull:{number}",
                    resource_number=number,
                    observed_from=observed_from,
                    observed_until=observed_until,
                    coverage=(
                        Coverage.PARTIAL
                        if snapshot.get("comparison_kind") == "unavailable"
                        else Coverage.COMPLETE
                    ),
                    origin=Origin.GIT,
                    payload={
                        "operation": "PullGitSnapshot",
                        "repository": self.config.repository,
                        "resource_number": number,
                        "value": _object(snapshot, f"pull #{number} Git snapshot"),
                    },
                ),
            ),
            complete_task=True,
        )

    async def _structured_commits(
        self,
        archive: ObservationArchive,
        task: SyncTask,
        sources: list[FactObservation],
    ) -> None:
        references: dict[str, list[CommitReference]] = {}
        for source in sources:
            if source.coverage is not Coverage.COMPLETE:
                continue
            selected = list(
                observation_commit_references(source.family, source.payload),
            )
            references[source.payload_digest] = selected
            operation = f"commit-references:{source.id}"
            if await self._publication(archive, task, operation) is None:
                await self._publish(
                    archive,
                    task,
                    operation,
                    (
                        FactDraft(
                            family="commit-references",
                            subject_key=f"payload:{source.payload_digest}",
                            resource_number=source.resource_number,
                            source_digest=source.payload_digest,
                            observed_from=source.observed_from,
                            observed_until=source.observed_until,
                            coverage=Coverage.COMPLETE,
                            origin=Origin.DERIVED,
                            payload={
                                "operation": "StructuredCommitReferenceScan",
                                "repository": self.config.repository,
                                "source_family": source.family,
                                "source_observation_id": source.id,
                                "source_payload_digest": source.payload_digest,
                                "references": [
                                    _reference_payload(reference)
                                    for reference in selected
                                ],
                            },
                        ),
                    ),
                )
        shas = sorted(
            {
                reference.sha
                for selected in references.values()
                for reference in selected
            },
        )
        if not shas:
            return
        async with self._store_lock:
            await archive.enqueue_tasks(
                task.cycle_id,
                tuple(
                    TaskDraft(
                        task_key=f"commit-object:{sha}",
                        kind="commit-object",
                        subject_key=f"commit:{sha}",
                        resource_number=None,
                        payload={"sha": sha},
                    )
                    for sha in shas
                ),
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


def _publication_key(task: SyncTask, operation: str) -> str:
    return f"sync:{task.cycle_id}:task:{task.id}:{operation}"


def _task_number(task: SyncTask) -> int:
    value = task.payload.get("number", task.resource_number)
    if type(value) is not int or value < 1:
        raise RuntimeError(f"{task.task_key} has no valid resource number")
    return value


def _validate_issue(value: dict[str, Any], number: int) -> None:
    if value.get("number") != number:
        raise IncompleteGitHubDataError(f"GitHub returned another parent for #{number}")
    created_at = value.get("created_at")
    updated_at = value.get("updated_at")
    if not isinstance(created_at, str) or not isinstance(updated_at, str):
        raise IncompleteGitHubDataError(f"issue #{number} has invalid timestamps")
    _stored_time(created_at)
    _stored_time(updated_at)


def _single(
    observations: tuple[FactObservation, ...],
    operation: str,
) -> FactObservation:
    if len(observations) != 1:
        raise RuntimeError(f"{operation} publication is not singular")
    return observations[0]


def _fact_object(fact: FactObservation, context: str) -> dict[str, Any]:
    if fact.coverage is not Coverage.COMPLETE:
        raise IncompleteGitHubDataError(f"{context} has no complete observation")
    return _object(fact.payload.get("value"), context)


def _fact_list(fact: FactObservation, context: str) -> list[dict[str, Any]]:
    if fact.coverage is not Coverage.COMPLETE:
        raise IncompleteGitHubDataError(f"{context} has no complete observation")
    return _objects(fact.payload.get("value"), context)


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IncompleteGitHubDataError(f"{context} is not an object")
    return value


def _objects(value: Any, context: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise IncompleteGitHubDataError(f"{context} is not an object collection")
    return value


def _optional_objects(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        return None
    return value


def _source_payload(
    operation: str,
    repository: str,
    resource_number: int,
    value: Any,
    source: str,
    raw: Any,
    cache: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = {
        "operation": operation,
        "repository": repository,
        "resource_number": resource_number,
        "source": source,
        "value": value,
        "raw": raw,
    }
    if cache is not None:
        payload["cache"] = cache
    return payload


def _coverage_error(
    error: GitHubAPIError,
    *,
    unauthenticated: bool,
) -> Coverage | None:
    if error.status_code in {401, 403}:
        return Coverage.FORBIDDEN
    if error.status_code == 404:
        return Coverage.UNAVAILABLE
    message = str(error).lower()
    if unauthenticated and "authenticat" in message and "require" in message:
        return Coverage.FORBIDDEN
    return None


def _comparison_shas(pull: dict[str, Any], number: int) -> tuple[str, str]:
    base = _object(pull.get("base"), f"pull #{number} base")
    head = _object(pull.get("head"), f"pull #{number} head")
    return (
        _required_sha(base.get("sha"), f"pull #{number} base"),
        _required_sha(head.get("sha"), f"pull #{number} head"),
    )


def _embedded_review_requests(pull: dict[str, Any]) -> dict[str, Any] | None:
    users = pull.get("requested_reviewers")
    teams = pull.get("requested_teams")
    if (
        not isinstance(users, list)
        or any(not isinstance(user, dict) for user in users)
        or not isinstance(teams, list)
        or teams
    ):
        return None
    return {"users": users, "teams": []}


def _zero(value: Any) -> bool:
    return type(value) is int and value == 0


def _zero_count(value: Any) -> bool:
    return isinstance(value, dict) and _zero(value.get("total_count"))


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _required_sha(value: Any, context: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise IncompleteGitHubDataError(f"{context} has no valid commit ID")
    return value


def _reference_payload(reference: CommitReference) -> dict[str, Any]:
    return {
        "sha": reference.sha,
        "field_path": reference.field_path,
        "source_kind": reference.source_kind,
        "source_id": reference.source_id,
    }


def _error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _stored_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("GitHub timestamp has no timezone")
    return parsed.astimezone(UTC)


def _iso_seconds(value: datetime) -> str:
    return _utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _token(configured: str | None) -> str | None:
    value = configured if configured is not None else os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    return value or None
