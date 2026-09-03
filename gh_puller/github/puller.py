"""编排 GitHub 原始事实拉取、观测水位与 SQLite 原子发布。

协议边界与时间语义见 gh_puller.github；持久化细节由 store 负责。本模块不把
归档解释为派生知识，也不下载正文中链接的站外附件。
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .client import GitHubAPI, GitHubAPIError, GitHubPage, GitHubResource
from .git_store import GitObjectStore, default_git_url, git_store_path
from .progress import _PullProgressTracker
from .store import (
    ArchivedRun,
    CatalogItem,
    PullPass,
    PullTask,
    SQLiteArchive,
    StagedResource,
    StoredHead,
    json_digest,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from .progress import ProgressObserver

_NUMBER_AT_END = re.compile(r"/(\d+)$")
_CLOSING_REFERENCE_BATCH_SIZE = 100
_CATALOG_ACCEPT = "application/vnd.github.raw+json"
_BUNDLE_SCHEMA_VERSION = 5


class IncompleteGitHubDataError(RuntimeError):
    """GitHub 响应存在拉取器能够证明的不一致或截断。"""


class _API(Protocol):
    request_count: int

    async def get_json_cached(
        self,
        path: str,
        *,
        previous: Any | None,
        cache: dict[str, Any] | None,
        params: dict[str, Any] | None = None,
        accept: str | None = None,
    ) -> tuple[Any, dict[str, Any] | None]: ...

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

    async def get_page(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str | None = None,
    ) -> GitHubPage: ...

    async def repository_item_count(self, owner: str, repo: str) -> int | None: ...

    async def closing_issue_references(
        self,
        owner: str,
        repo: str,
        numbers: list[int],
    ) -> dict[int, list[dict[str, Any]]]: ...

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

    async def pull_review_comments(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource: ...

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


class _GitStore(Protocol):
    async def prefetch(
        self,
        numbers: list[int],
        *,
        heartbeat: Callable[[], None] | None = None,
    ) -> None: ...

    async def capture(self, number: int, pull: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class GitHubPullConfig:
    repository: str  # GitHub owner/repo identity.
    destination: Path  # SQLite raw-fact database.
    token: str | None = None  # None reads GH_TOKEN, then GITHUB_TOKEN, at call time.
    api_url: str = "https://api.github.com"  # REST root, including Enterprise roots.
    graphql_url: str | None = None  # None derives GitHub's GraphQL endpoint from api_url.
    api_version: str = "2022-11-28"  # Version sent to GitHub's versioned REST API.
    concurrency: int = 4  # Concurrent item bundles; each bundle paginates serially.
    request_timeout: float = 30.0  # Per-request timeout in seconds.
    overlap_seconds: int = 2  # Replayed boundary for second-resolution GitHub timestamps.
    git_url: str | None = None  # None uses repository's GitHub.com HTTPS URL.

    def __post_init__(self) -> None:
        parts = self.repository.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("repository must be 'owner/repo'")
        if self.concurrency < 1:
            raise ValueError("concurrency must be positive")
        if self.overlap_seconds < 1:
            raise ValueError("overlap_seconds must be positive")
        if self.git_url is not None and not self.git_url:
            raise ValueError("git_url cannot be empty")
        object.__setattr__(self, "destination", Path(self.destination))


@dataclass(frozen=True, slots=True)
class PullResult:
    target_at: datetime  # Requested observation watermark.
    completed_at: datetime  # Actual completion time C.
    run_id: int  # Committed SQLite pull-run identity.
    changed_items: int  # Object versions or tombstones published by this run.
    catalog_items: int  # Issues and PRs currently present.
    requests: int  # HTTP attempts accumulated across run recovery.

    @property
    def lag_seconds(self) -> float:
        """Return the non-negative completion lag behind the target watermark."""
        return max((self.completed_at - self.target_at).total_seconds(), 0.0)


class GitHubPuller:
    """可复用的 GitHub 增量拉取操作。

    Args:
        config: 仓库、SQLite 事实库和请求策略。
        api: 测试或宿主提供的 GitHub API 读取对象。
        git: 测试或宿主提供的持久化 Git 对象库。
        now: 在函数入口冻结默认目标及记录完成时刻的 UTC 时钟。
        sleep: 等待未来目标使用的异步等待函数。
        observer: 同步带外进度观察器；失败不影响事实拉取。
    """

    def __init__(
        self,
        config: GitHubPullConfig,
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
        self._owner, self._repo = config.repository.split("/", 1)
        self._base = f"/repos/{self._owner}/{self._repo}"

    async def pull(self, target: datetime | None = None) -> PullResult:
        """完成一次增量拉取并原子发布一个事实库 run。

        Args:
            target: 需要观测到的时刻；None 在进入本函数、任何 await 之前取当前
                UTC 时刻。显式值必须带时区。未来值会先预拉已有数据，再等待并
                做最终闭合。

        Returns:
            本次水位、实际完成时刻、run identity 和数据规模。同一 T 已
            committed 时返回原 run，不执行 HTTP 请求。

        Raises:
            ValueError: target 不带时区。
            IncompleteGitHubDataError: GitHub 返回可检测的截断结果。
        """
        started_at = _as_utc(self._now())
        target_at = started_at if target is None else _as_utc(target)
        progress = _PullProgressTracker(target_at, self._observer, self._now)
        progress.phase("waiting_lock")
        try:
            async with (
                _archive_lock(self.config.destination),
                SQLiteArchive(
                    self.config.destination,
                    self.config.repository,
                ) as archive,
            ):
                progress.phase("checking")
                existing = await archive.committed_run(_iso(target_at))
                if existing is not None:
                    result = _pull_result(existing)
                    progress.done(
                        run_id=result.run_id,
                        catalog_items=result.catalog_items,
                        requests=result.requests,
                        reused=True,
                    )
                    return result
                result = await self._pull_new(
                    archive,
                    started_at,
                    target_at,
                    progress,
                )
                progress.done(
                    run_id=result.run_id,
                    catalog_items=result.catalog_items,
                    requests=result.requests,
                    reused=False,
                )
                return result
        except BaseException as exc:
            progress.error(exc)
            raise

    async def _pull_new(
        self,
        archive: SQLiteArchive,
        started_at: datetime,
        target_at: datetime,
        progress: _PullProgressTracker,
    ) -> PullResult:
        api, owned = self._make_api(progress)
        git = self._make_git()
        request_start = api.request_count
        accounted = False
        try:
            run = await archive.start_run(_iso(target_at), _iso(started_at))
            progress.bind_run(run.id, run.request_count, request_start)
            observed = _parse_time(run.observed_until)
            try:
                active = await archive.active_pass(run.id)
                if active is not None:
                    observed = await self._sync_pass(
                        api,
                        git,
                        archive,
                        run.id,
                        _parse_required_time(active.cutoff_at),
                        observed,
                        active.name,
                        progress,
                    )
                now = _as_utc(self._now())
                if now < target_at:
                    if observed is None or now > observed:
                        observed = await self._sync_pass(
                            api,
                            git,
                            archive,
                            run.id,
                            now,
                            observed,
                            "prefetch",
                            progress,
                        )
                    wait = (target_at - _as_utc(self._now())).total_seconds()
                    while wait > 0:
                        progress.phase(
                            "waiting_target",
                            pass_at=target_at,
                            wait_seconds=wait,
                        )
                        await self._sleep(wait)
                        wait = (target_at - _as_utc(self._now())).total_seconds()
                if observed is None or target_at > observed:
                    await self._sync_pass(
                        api,
                        git,
                        archive,
                        run.id,
                        target_at,
                        observed,
                        "closing",
                        progress,
                    )
                attempt_requests = api.request_count - request_start
                await archive.add_requests(run.id, attempt_requests)
                accounted = True
                completed_at = _as_utc(self._now())
                progress.phase("finalizing", pass_at=target_at)
                changed, catalog, requests = await archive.finalize(
                    run.id,
                    _iso(completed_at),
                )
                return PullResult(
                    target_at=target_at,
                    completed_at=completed_at,
                    run_id=run.id,
                    changed_items=changed,
                    catalog_items=catalog,
                    requests=requests,
                )
            except BaseException:
                if not accounted:
                    await archive.add_requests(
                        run.id,
                        api.request_count - request_start,
                    )
                raise
        finally:
            if owned:
                await api.close()

    def _make_api(self, progress: _PullProgressTracker) -> tuple[_API, bool]:
        if self._api is not None:
            return self._api, False
        return GitHubAPI(
            token=_token(self.config.token),
            base_url=self.config.api_url,
            graphql_url=self.config.graphql_url,
            api_version=self.config.api_version,
            timeout=self.config.request_timeout,
            progress=progress.api_progress,
        ), True

    def _make_git(self) -> _GitStore:
        if self._git is not None:
            return self._git
        return GitObjectStore(
            git_store_path(self.config.destination),
            self.config.repository,
            self.config.git_url or default_git_url(self.config.repository),
            token=_token(self.config.token),
        )

    async def _sync_pass(
        self,
        api: _API,
        git: _GitStore,
        archive: SQLiteArchive,
        run_id: int,
        cutoff: datetime,
        observed: datetime | None,
        pass_name: str,
        progress: _PullProgressTracker,
    ) -> datetime | None:
        progress.start_pass(pass_name, cutoff)
        heads, staged = await archive.load_head_state(run_id)
        progress.restore_items(sum(head.present for head in heads.values()))
        progress.restore_staged((head.number, head.kind, head.present) for head in staged.values())
        state = await archive.active_pass(run_id)
        if state is None:
            if observed is not None and cutoff <= observed:
                return observed
            state = await archive.start_pass(
                run_id,
                pass_name,
                _iso(cutoff),
                "full" if observed is None else "delta",
            )
        elif (state.name, state.cutoff_at) != (pass_name, _iso(cutoff)):
            raise RuntimeError("another observation pass must finish first")
        if not state.prepared:
            state = await self._prepare_pass(api, archive, state, observed, progress)
        completed, total = await archive.task_progress(run_id)
        progress.catalog_restore(
            state.catalog_items,
            state.expected_count,
            complete=state.catalog_complete,
            objects_completed=completed,
            objects_total=total if state.catalog_complete else None,
        )

        changed = asyncio.Event()
        store_lock = asyncio.Lock()
        producer = asyncio.create_task(
            self._produce_catalog(
                api,
                archive,
                state,
                observed,
                progress,
                changed,
                store_lock,
            ),
        )
        try:
            while True:
                async with store_lock:
                    tasks = await archive.pending_catalog_tasks(run_id)
                if tasks:
                    await self._consume_tasks(
                        api,
                        git,
                        archive,
                        run_id,
                        heads,
                        tasks,
                        _iso(cutoff),
                        progress,
                        store_lock,
                    )
                    continue
                if producer.done():
                    await producer
                    break
                await changed.wait()
                changed.clear()
            while True:
                tasks = await archive.pending_signal_tasks(run_id)
                if not tasks:
                    break
                await self._consume_tasks(
                    api,
                    git,
                    archive,
                    run_id,
                    heads,
                    tasks,
                    _iso(cutoff),
                    progress,
                    store_lock,
                )
            await archive.finish_pass(run_id, _iso(cutoff))
        finally:
            if not producer.done():
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
        return cutoff

    async def _prepare_pass(
        self,
        api: _API,
        archive: SQLiteArchive,
        state: PullPass,
        observed: datetime | None,
        progress: _PullProgressTracker,
    ) -> PullPass:
        if state.mode == "full":
            expected = await self._repository_item_count(api, progress)
            return await archive.prepare_pass(state.run_id, (), expected)
        if observed is None:
            raise RuntimeError("delta discovery requires a prior observation watermark")
        since = _iso_seconds(observed - timedelta(seconds=self.config.overlap_seconds))
        signals = await self._signal_numbers(api, "issues/comments", "issue_url", since)
        signals.update(
            await self._signal_numbers(
                api,
                "pulls/comments",
                "pull_request_url",
                since,
            ),
        )
        return await archive.prepare_pass(state.run_id, signals, None)

    async def _produce_catalog(
        self,
        api: _API,
        archive: SQLiteArchive,
        state: PullPass,
        observed: datetime | None,
        progress: _PullProgressTracker,
        changed: asyncio.Event,
        store_lock: asyncio.Lock,
    ) -> None:
        if state.catalog_complete:
            return
        path = state.next_url if state.catalog_started else f"{self._base}/issues"
        if path is None:
            raise RuntimeError("unfinished catalog traversal has no next cursor")
        params = None if state.catalog_started else _catalog_params(state.mode, observed, self.config)
        visited: set[str] = set()
        restarted = False
        while True:
            try:
                page = await api.get_page(path, params=params, accept=_CATALOG_ACCEPT)
            except GitHubAPIError as exc:
                if restarted or not state.catalog_started or exc.status_code not in {404, 410, 422}:
                    raise
                async with store_lock:
                    state = await archive.restart_catalog(state.run_id)
                path = f"{self._base}/issues"
                params = _catalog_params(state.mode, observed, self.config)
                visited.clear()
                restarted = True
                continue
            if page.next_url == path or page.next_url in visited:
                raise IncompleteGitHubDataError("GitHub catalog repeated a pagination cursor")
            items = [_catalog_item(item) for item in page.items]
            try:
                async with store_lock:
                    state = await archive.stage_catalog_page(state.run_id, items, page.next_url)
                    task_progress = await archive.task_progress(state.run_id) if state.catalog_complete else None
            except ValueError as exc:
                raise IncompleteGitHubDataError(str(exc)) from exc
            progress.catalog_restore(
                state.catalog_items,
                state.expected_count,
                complete=state.catalog_complete,
                objects_completed=None if task_progress is None else task_progress[0],
                objects_total=None if task_progress is None else task_progress[1],
            )
            changed.set()
            if page.next_url is None:
                return
            visited.add(path)
            path = page.next_url
            params = None

    async def _repository_item_count(
        self,
        api: _API,
        progress: _PullProgressTracker,
    ) -> int | None:
        count = await api.repository_item_count(self._owner, self._repo)
        progress.catalog_count(count)
        return count

    async def _signal_numbers(
        self,
        api: _API,
        endpoint: str,
        url_field: str,
        since: str,
    ) -> set[int]:
        items = await api.paginate(
            f"{self._base}/{endpoint}",
            params={"sort": "created", "direction": "asc", "since": since},
        )
        numbers: set[int] = set()
        for item in items:
            match = _NUMBER_AT_END.search(item.get(url_field, ""))
            if match:
                numbers.add(int(match.group(1)))
        return numbers

    async def _consume_tasks(
        self,
        api: _API,
        git: _GitStore,
        archive: SQLiteArchive,
        run_id: int,
        heads: dict[int, StoredHead],
        tasks: list[PullTask],
        observed_at: str,
        progress: _PullProgressTracker,
        store_lock: asyncio.Lock,
    ) -> None:
        selected = []
        cutoff = _parse_required_time(observed_at)
        for task in tasks:
            if task.created_at is not None and _parse_required_time(task.created_at) > cutoff:
                async with store_lock:
                    completed = await archive.complete_task(
                        run_id,
                        task.number,
                        task.summary_digest,
                    )
                if completed:
                    progress.object_completed(api.request_count)
                continue
            if (
                task.summary is not None
                and not task.force_comments
                and not _needs_refresh(heads.get(task.number), task.summary)
            ):
                async with store_lock:
                    completed = await archive.complete_task(
                        run_id,
                        task.number,
                        task.summary_digest,
                    )
                if completed:
                    progress.object_completed(api.request_count)
                continue
            selected.append(task)
        if selected:
            await self._fetch_candidates(
                api,
                git,
                archive,
                run_id,
                heads,
                selected,
                observed_at,
                progress,
                store_lock,
            )

    async def _fetch_candidates(
        self,
        api: _API,
        git: _GitStore,
        archive: SQLiteArchive,
        run_id: int,
        heads: dict[int, StoredHead],
        tasks: list[PullTask],
        observed_at: str,
        progress: _PullProgressTracker,
        store_lock: asyncio.Lock,
    ) -> None:
        indexed = {task.number: task for task in tasks}
        summaries = {
            task.number: task.summary for task in tasks if task.summary is not None
        }
        preloaded = set(summaries)
        previous_states: dict[
            int,
            tuple[dict[str, Any] | None, dict[str, Any] | None],
        ] = {}
        root_caches: dict[int, dict[str, Any]] = {}
        for task in tasks:
            previous = heads.get(task.number)
            if task.number in summaries or (previous is not None and previous.kind == "issue"):
                continue
            issue_path = f"{self._base}/issues/{task.number}"
            previous_bundle = None
            previous_cache = None
            if previous is not None:
                async with store_lock:
                    previous_bundle, previous_cache = await archive.load_bundle_state(
                        previous.bundle_digest,
                    )
                previous_states[task.number] = previous_bundle, previous_cache
            try:
                summary, root_cache = await self._cached_json(
                    api,
                    issue_path,
                    _dict_field(previous_bundle, "issue"),
                    _dict_field(previous_cache, "issue"),
                )
            except GitHubAPIError as exc:
                if not _is_parent_absence(exc, issue_path):
                    raise
                if previous is None:
                    async with store_lock:
                        completed = await archive.complete_task(
                            run_id,
                            task.number,
                            task.summary_digest,
                        )
                    if completed:
                        progress.object_completed(api.request_count)
                else:
                    resource = StagedResource(
                        head=_absent_head(previous, observed_at),
                        observed_at=observed_at,
                        summary=None,
                        bundle=None,
                    )
                    async with store_lock:
                        stored = await archive.stage_task(
                            run_id,
                            task.number,
                            task.summary_digest,
                            resource,
                        )
                    if stored:
                        heads[task.number] = resource.head
                        progress.absence_staged(api.request_count)
                indexed.pop(task.number)
                continue
            summaries[task.number] = summary
            preloaded.add(task.number)
            if root_cache is not None:
                root_caches[task.number] = root_cache

        async def fetch(
            number: int,
            closing_references: dict[int, list[dict[str, Any]]],
        ) -> tuple[
            int,
            dict[str, Any] | None,
            dict[str, Any] | None,
            dict[str, Any] | None,
        ]:
            summary = summaries.get(number)
            if summary is None:
                summary = _minimal_summary(heads.get(number))
            stored_summary = summary if number in preloaded else None
            previous = heads.get(number)
            previous_bundle, previous_cache = previous_states.get(number, (None, None))
            if previous is not None and number not in previous_states:
                async with store_lock:
                    previous_bundle, previous_cache = await archive.load_bundle_state(
                        previous.bundle_digest,
                    )
            try:
                bundle, http_cache = await self._fetch_bundle(
                    api,
                    git,
                    summary,
                    previous_bundle,
                    previous_cache,
                    catalog_issue=summary if stored_summary is not None else None,
                    force_comments=indexed[number].force_comments,
                    closing_references=closing_references.get(number),
                )
            except GitHubAPIError as exc:
                if not _is_parent_absence(exc, f"{self._base}/issues/{number}"):
                    raise
                return number, None, None, None
            if number in root_caches:
                http_cache = dict(http_cache or {})
                http_cache["issue"] = root_caches[number]
            if stored_summary is None:
                detail = bundle.get("issue")
                if not isinstance(detail, dict):
                    raise IncompleteGitHubDataError(f"issue #{number} bundle has no root object")
                if previous is None or previous.summary_digest != json_digest(detail):
                    stored_summary = detail
            return number, bundle, stored_summary, http_cache

        async def fetch_batch(
            batch: list[int],
            closing_references: dict[int, list[dict[str, Any]]],
        ) -> None:
            numbers = iter(batch)
            completed: asyncio.Queue[asyncio.Task[Any]] = asyncio.Queue()
            pending: set[asyncio.Task[Any]] = set()

            def start(number: int) -> None:
                task = asyncio.create_task(fetch(number, closing_references))
                task.add_done_callback(completed.put_nowait)
                pending.add(task)

            for number in islice(numbers, self.config.concurrency):
                start(number)
            try:
                while pending:
                    task = await completed.get()
                    pending.remove(task)
                    number, bundle, summary, http_cache = task.result()
                    old = heads.get(number)
                    if bundle is None:
                        if old is None:
                            raise IncompleteGitHubDataError(
                                f"unobserved parent #{number} became unavailable",
                            )
                        head = _absent_head(old, observed_at)
                    else:
                        head = _candidate_head(summary, old, bundle)
                    resource = StagedResource(
                        head=head,
                        observed_at=observed_at,
                        summary=summary,
                        bundle=bundle,
                        http_cache=http_cache,
                    )
                    discovery = indexed[number]
                    async with store_lock:
                        stored = await archive.stage_task(
                            run_id,
                            number,
                            discovery.summary_digest,
                            resource,
                        )
                    if stored:
                        new_item = old is None or not old.present
                        heads[number] = head
                        if head.present:
                            progress.bundles_staged(
                                ((head.number, head.kind),),
                                api.request_count,
                                new_items=int(new_item),
                            )
                        else:
                            progress.absence_staged(api.request_count)
                    next_number = next(numbers, None)
                    if next_number is not None:
                        start(next_number)
            except BaseException:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise

        def kind(number: int) -> str:
            summary = summaries.get(number)
            return heads[number].kind if summary is None else _kind(summary)

        for batch in _closing_reference_batches(sorted(indexed), kind):
            pull_numbers = [number for number in batch if kind(number) == "pull"]
            if pull_numbers:
                progress.git_fetch(len(pull_numbers))
                try:
                    await git.prefetch(pull_numbers, heartbeat=progress.git_heartbeat)
                finally:
                    progress.git_done()
            closing_references = (
                await api.closing_issue_references(self._owner, self._repo, pull_numbers)
                if pull_numbers
                else {}
            )
            await fetch_batch(batch, closing_references)

    async def _fetch_bundle(
        self,
        api: _API,
        git: _GitStore,
        summary: dict[str, Any],
        previous: dict[str, Any] | None,
        previous_cache: dict[str, Any] | None,
        *,
        catalog_issue: dict[str, Any] | None,
        force_comments: bool,
        closing_references: list[dict[str, Any]] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        number = int(summary["number"])
        issue_path = f"{self._base}/issues/{number}"
        http_cache: dict[str, Any] = {}
        if catalog_issue is None:
            issue, cache = await self._cached_json(
                api,
                issue_path,
                _dict_field(previous, "issue"),
                _dict_field(previous_cache, "issue"),
            )
            _set_cache(http_cache, "issue", cache)
        else:
            issue = catalog_issue
        comment_resource: GitHubResource | None = None
        if not force_comments and _is_zero_integer(issue.get("comments")):
            comments = []
        else:
            comment_resource = await api.issue_comments(
                self._owner,
                self._repo,
                number,
                previous=_list_field(previous, "issue_comments"),
                cache=_dict_field(previous_cache, "issue_comments"),
            )
            comments = comment_resource.value
            cache = comment_resource.cache
            if not isinstance(comments, list) or any(
                not isinstance(comment, dict) for comment in comments
            ):
                raise IncompleteGitHubDataError(
                    f"GitHub returned invalid issue comments for {issue_path}",
                )
            _set_cache(http_cache, "issue_comments", cache)
        comments = _canonical_comments(comments)
        timeline, cache = await self._cached_page(
            api,
            f"{issue_path}/timeline",
            _list_field(previous, "timeline"),
            _dict_field(previous_cache, "timeline"),
        )
        _set_cache(http_cache, "timeline", cache)
        events, cache = await self._cached_page(
            api,
            f"{issue_path}/events",
            _list_field(previous, "events"),
            _dict_field(previous_cache, "events"),
        )
        _set_cache(http_cache, "events", cache)
        reaction_resource: GitHubResource | None = None
        if _is_zero_count(issue.get("reactions")):
            reactions = []
        else:
            reaction_resource = await api.reactions(
                f"{issue_path}/reactions",
                _str_field(issue, "node_id"),
                previous=_list_field(previous, "reactions"),
                cache=_dict_field(previous_cache, "reactions"),
            )
            reactions = reaction_resource.value
            cache = reaction_resource.cache
            if not isinstance(reactions, list) or any(
                not isinstance(reaction, dict) for reaction in reactions
            ):
                raise IncompleteGitHubDataError(
                    f"GitHub returned invalid reactions for {issue_path}",
                )
            _set_cache(http_cache, "reactions", cache)
        comment_reactions, cache, comment_reaction_sources = await self._reactions(
            api,
            "issues/comments",
            comments,
            _dict_field(previous, "issue_comment_reactions"),
            _dict_field(previous_cache, "issue_comment_reactions"),
        )
        _set_cache(http_cache, "issue_comment_reactions", cache)
        summary_updated = _parse_time(summary.get("updated_at"))
        detail_updated = _parse_time(issue.get("updated_at"))
        if summary_updated and detail_updated and detail_updated < summary_updated:
            raise IncompleteGitHubDataError(
                f"issue #{number} detail is older than its catalog entry",
            )
        _check_count(number, "issue reactions", issue.get("reactions"), reactions)
        api_sources: dict[str, Any] = {}
        if comment_resource is not None:
            api_sources["issue_comments"] = _api_source(comment_resource)
        if reaction_resource is not None:
            api_sources["reactions"] = _api_source(reaction_resource)
        if comment_reaction_sources:
            api_sources["issue_comment_reactions"] = comment_reaction_sources
        bundle: dict[str, Any] = {
            "schema_version": _BUNDLE_SCHEMA_VERSION,
            "repository": self.config.repository,
            "number": number,
            "kind": "pull" if "pull_request" in issue else "issue",
            "issue": issue,
            "issue_comments": comments,
            "timeline": timeline,
            "events": events,
            "reactions": reactions,
            "issue_comment_reactions": comment_reactions,
            "api_sources": api_sources,
        }
        if bundle["kind"] == "pull":
            pull, cache = await self._fetch_pull(
                api,
                git,
                number,
                _dict_field(previous, "pull_request"),
                _dict_field(previous_cache, "pull_request"),
                force_comments=force_comments,
                closing_references=closing_references,
            )
            bundle["pull_request"] = pull
            _set_cache(http_cache, "pull_request", cache)
        return bundle, http_cache or None

    async def _fetch_pull(
        self,
        api: _API,
        git: _GitStore,
        number: int,
        previous: dict[str, Any] | None,
        previous_cache: dict[str, Any] | None,
        *,
        force_comments: bool,
        closing_references: list[dict[str, Any]] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if closing_references is None:
            raise IncompleteGitHubDataError(f"pull #{number} has no closing issue references")
        path = f"{self._base}/pulls/{number}"
        http_cache: dict[str, Any] = {}
        detail = await api.pull_request(
            self._owner,
            self._repo,
            number,
            previous=_dict_field(previous, "detail"),
            cache=_dict_field(previous_cache, "detail"),
        )
        pull = detail.value
        cache = detail.cache
        if not isinstance(pull, dict):
            raise IncompleteGitHubDataError(f"GitHub returned a non-object for {path}")
        _set_cache(http_cache, "detail", cache)
        review_resource = await api.pull_reviews(
            self._owner,
            self._repo,
            number,
            previous=_list_field(previous, "reviews"),
            cache=_dict_field(previous_cache, "reviews"),
        )
        reviews = review_resource.value
        cache = review_resource.cache
        if not isinstance(reviews, list) or any(
            not isinstance(review, dict) for review in reviews
        ):
            raise IncompleteGitHubDataError(f"GitHub returned invalid reviews for {path}")
        _set_cache(http_cache, "reviews", cache)
        review_comment_resource: GitHubResource | None = None
        if not force_comments and _is_zero_integer(pull.get("review_comments")):
            review_comments = []
        else:
            review_comment_resource = await api.pull_review_comments(
                self._owner,
                self._repo,
                number,
                previous=_list_field(previous, "review_comments"),
                cache=_dict_field(previous_cache, "review_comments"),
            )
            review_comments = review_comment_resource.value
            cache = review_comment_resource.cache
            if not isinstance(review_comments, list) or any(
                not isinstance(comment, dict) for comment in review_comments
            ):
                raise IncompleteGitHubDataError(
                    f"GitHub returned invalid review comments for {path}",
                )
            _set_cache(http_cache, "review_comments", cache)
        review_comments = _canonical_comments(review_comments)
        expected_commits = pull.get("commits")
        commit_resource: GitHubResource | None = None
        if _is_zero_integer(expected_commits):
            commits = []
        elif isinstance(expected_commits, int) and expected_commits > 0:
            base, head = _comparison_shas(pull, number)
            commit_resource = await api.pull_commits(
                self._owner,
                self._repo,
                number,
                expected=expected_commits,
                base=base,
                head=head,
                previous=_list_field(previous, "commits"),
                cache=_dict_field(previous_cache, "commits"),
            )
            commits = commit_resource.value
            cache = commit_resource.cache
            if not isinstance(commits, list) or any(
                not isinstance(commit, dict) for commit in commits
            ):
                raise IncompleteGitHubDataError(f"GitHub returned invalid commits for {path}")
            _set_cache(http_cache, "commits", cache)
        requested_reviewers = _embedded_review_requests(pull)
        if requested_reviewers is None:
            requested_reviewers, cache = await self._cached_json(
                api,
                f"{path}/requested_reviewers",
                _dict_field(previous, "requested_reviewers"),
                _dict_field(previous_cache, "requested_reviewers"),
            )
            _set_cache(http_cache, "requested_reviewers", cache)
        review_reactions, cache, review_reaction_sources = await self._reactions(
            api,
            "pulls/comments",
            review_comments,
            _dict_field(previous, "review_comment_reactions"),
            _dict_field(previous_cache, "review_comment_reactions"),
        )
        _set_cache(http_cache, "review_comment_reactions", cache)
        if isinstance(expected_commits, int) and len(commits) < expected_commits:
            raise IncompleteGitHubDataError(
                f"pull #{number} advertised {expected_commits} commits, got {len(commits)}",
            )
        git_snapshot = await git.capture(number, pull)
        api_sources = {
            "detail": _api_source(detail),
            "reviews": _api_source(review_resource),
        }
        if commit_resource is not None:
            api_sources["commits"] = _api_source(commit_resource)
        if review_comment_resource is not None:
            api_sources["review_comments"] = _api_source(review_comment_resource)
        if review_reaction_sources:
            api_sources["review_comment_reactions"] = review_reaction_sources
        result = {
            "detail": pull,
            "reviews": reviews,
            "review_comments": review_comments,
            "review_comment_reactions": review_reactions,
            "commits": commits,
            "git": git_snapshot,
            "requested_reviewers": requested_reviewers,
            "closing_issues_references": closing_references,
            "api_sources": api_sources,
        }
        return result, http_cache or None

    async def _cached_json(
        self,
        api: _API,
        path: str,
        previous: dict[str, Any] | None,
        cache: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        value, updated = await api.get_json_cached(
            path,
            previous=previous,
            cache=cache,
        )
        if not isinstance(value, dict):
            raise IncompleteGitHubDataError(f"GitHub returned a non-object for {path}")
        return value, updated

    async def _cached_page(
        self,
        api: _API,
        path: str,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        return await api.paginate_cached(
            path,
            previous=previous,
            cache=cache,
        )

    async def _reactions(
        self,
        api: _API,
        endpoint: str,
        comments: list[dict[str, Any]],
        previous: dict[str, Any] | None,
        previous_cache: dict[str, Any] | None,
    ) -> tuple[
        dict[str, list[dict[str, Any]]],
        dict[str, Any] | None,
        dict[str, dict[str, Any]],
    ]:
        result: dict[str, list[dict[str, Any]]] = {}
        http_cache: dict[str, Any] = {}
        sources: dict[str, dict[str, Any]] = {}
        for comment in comments:
            reaction_summary = comment.get("reactions")
            if isinstance(reaction_summary, dict) and reaction_summary.get("total_count") == 0:
                result[str(comment["id"])] = []
                continue
            comment_id = int(comment["id"])
            key = str(comment_id)
            resource = await api.reactions(
                f"{self._base}/{endpoint}/{comment_id}/reactions",
                _str_field(comment, "node_id"),
                previous=_list_field(previous, key),
                cache=_dict_field(previous_cache, key),
            )
            reactions = resource.value
            cache = resource.cache
            if not isinstance(reactions, list) or any(
                not isinstance(reaction, dict) for reaction in reactions
            ):
                raise IncompleteGitHubDataError(
                    f"GitHub returned invalid reactions for comment {comment_id}",
                )
            result[key] = reactions
            sources[key] = _api_source(resource)
            _set_cache(http_cache, key, cache)
            _check_count(comment_id, "comment reactions", reaction_summary, reactions)
        return result, http_cache or None, sources


async def incremental_pull(
    config: GitHubPullConfig,
    target: datetime | None = None,
    *,
    observer: ProgressObserver | None = None,
) -> PullResult:
    """执行一次增量观测并原子发布结果。

    Args:
        config: 仓库、SQLite 事实库与请求策略。
        target: 观测目标；None 在函数入口冻结为当前 UTC 时刻。
        observer: 同步带外进度观察器；失败不影响事实拉取。

    Returns:
        成功发布或由幂等键复用的水位、实际完成时刻和统计信息。
    """
    target_at = datetime.now(UTC) if target is None else target
    return await GitHubPuller(config, observer=observer).pull(target_at)


def _pull_result(run: ArchivedRun) -> PullResult:
    return PullResult(
        target_at=_parse_required_time(run.target_at),
        completed_at=_parse_required_time(run.completed_at),
        run_id=run.id,
        changed_items=run.changed_items,
        catalog_items=run.catalog_items,
        requests=run.request_count,
    )


def _catalog_params(
    mode: str,
    observed: datetime | None,
    config: GitHubPullConfig,
) -> dict[str, Any]:
    if mode == "full":
        return {"state": "all", "sort": "created", "direction": "desc"}
    if observed is None:
        raise RuntimeError("delta discovery requires a prior observation watermark")
    return {
        "state": "all",
        "sort": "updated",
        "direction": "asc",
        "since": _iso_seconds(observed - timedelta(seconds=config.overlap_seconds)),
    }


def _catalog_item(item: dict[str, Any]) -> CatalogItem:
    number = item.get("number")
    github_id = item.get("id")
    created_at = item.get("created_at")
    updated_at = item.get("updated_at")
    if type(number) is not int or number < 1 or type(github_id) is not int or github_id < 1:
        raise IncompleteGitHubDataError("GitHub catalog item has invalid identity")
    if not isinstance(created_at, str) or not isinstance(updated_at, str):
        raise IncompleteGitHubDataError(f"GitHub item #{number} has invalid timestamps")
    _parse_required_time(created_at)
    _parse_required_time(updated_at)
    return CatalogItem(
        number=number,
        github_id=github_id,
        kind=_kind(item),
        created_at=created_at,
        updated_at=updated_at,
        summary=item,
    )


def _needs_refresh(head: StoredHead | None, summary: dict[str, Any]) -> bool:
    if head is None or not head.present or head.bundle_digest is None:
        return True
    return (
        head.kind != _kind(summary)
        or head.updated_at != summary.get("updated_at")
        or head.summary_digest != json_digest(summary)
    )


def _dict_field(value: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    item = value.get(key) if isinstance(value, dict) else None
    return item if isinstance(item, dict) else None


def _list_field(value: dict[str, Any] | None, key: str) -> list[dict[str, Any]] | None:
    item = value.get(key) if isinstance(value, dict) else None
    if not isinstance(item, list) or any(not isinstance(entry, dict) for entry in item):
        return None
    return item


def _str_field(value: dict[str, Any] | None, key: str) -> str | None:
    item = value.get(key) if isinstance(value, dict) else None
    return item if isinstance(item, str) else None


def _set_cache(
    target: dict[str, Any],
    key: str,
    value: dict[str, Any] | None,
) -> None:
    if value is not None:
        target[key] = value


def _api_source(resource: GitHubResource) -> dict[str, Any]:
    source = {"source": resource.source}
    if resource.source != "rest":
        source["raw"] = resource.raw
    return source


def _canonical_comments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    for item in items:
        item_id = item.get("id")
        if type(item_id) is not int or item_id in seen:
            raise IncompleteGitHubDataError("GitHub comment collection has invalid identities")
        seen.add(item_id)
    return sorted(items, key=lambda item: int(item["id"]))


def _is_zero_count(value: Any) -> bool:
    return isinstance(value, dict) and _is_zero_integer(value.get("total_count"))


def _is_zero_integer(value: Any) -> bool:
    return type(value) is int and value == 0


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


def _comparison_shas(pull: dict[str, Any], number: int) -> tuple[str, str]:
    base = _dict_field(pull, "base")
    head = _dict_field(pull, "head")
    base_sha = _str_field(base, "sha")
    head_sha = _str_field(head, "sha")
    if not base_sha or not head_sha:
        raise IncompleteGitHubDataError(f"pull #{number} has no complete comparison range")
    return base_sha, head_sha


def _head_from_summary(
    summary: dict[str, Any],
    bundle: dict[str, Any],
) -> StoredHead:
    number = summary.get("number")
    github_id = summary.get("id")
    created_at = summary.get("created_at")
    updated_at = summary.get("updated_at")
    if type(number) is not int or type(github_id) is not int:
        raise IncompleteGitHubDataError("GitHub catalog item has invalid identity")
    if not isinstance(created_at, str) or not isinstance(updated_at, str):
        raise IncompleteGitHubDataError(f"GitHub item #{number} has invalid timestamps")
    _parse_required_time(created_at)
    _parse_required_time(updated_at)
    kind = _kind(summary)
    if bundle.get("kind") != kind:
        raise IncompleteGitHubDataError(f"GitHub item #{number} changed kind while fetching")
    return StoredHead(
        number=number,
        github_id=github_id,
        kind=kind,
        created_at=created_at,
        updated_at=updated_at,
        summary_digest=json_digest(summary),
        bundle_digest=json_digest(bundle),
        present=True,
        missing_since=None,
    )


def _candidate_head(
    summary: dict[str, Any] | None,
    previous: StoredHead | None,
    bundle: dict[str, Any],
) -> StoredHead:
    if summary is not None:
        return _head_from_summary(summary, bundle)
    if previous is None:
        raise IncompleteGitHubDataError("dirty parent has no previously observed root")
    if bundle.get("kind") != previous.kind:
        raise IncompleteGitHubDataError(
            f"GitHub item #{previous.number} changed kind while fetching",
        )
    return StoredHead(
        number=previous.number,
        github_id=previous.github_id,
        kind=previous.kind,
        created_at=previous.created_at,
        updated_at=previous.updated_at,
        summary_digest=previous.summary_digest,
        bundle_digest=json_digest(bundle),
        present=True,
        missing_since=None,
    )


def _absent_head(previous: StoredHead, observed_at: str) -> StoredHead:
    return StoredHead(
        number=previous.number,
        github_id=previous.github_id,
        kind=previous.kind,
        created_at=previous.created_at,
        updated_at=previous.updated_at,
        summary_digest=previous.summary_digest,
        bundle_digest=previous.bundle_digest,
        present=False,
        missing_since=previous.missing_since or observed_at,
    )


def _is_parent_absence(error: GitHubAPIError, path: str) -> bool:
    return (
        error.status_code in {404, 410}
        and error.url is not None
        and error.url.rstrip("/").endswith(path)
    )


def _minimal_summary(head: StoredHead | None) -> dict[str, Any]:
    if head is None:
        raise IncompleteGitHubDataError("dirty parent has no previously observed root")
    summary: dict[str, Any] = {
        "id": head.github_id,
        "number": head.number,
        "created_at": head.created_at,
        "updated_at": head.updated_at,
    }
    if head.kind == "pull":
        summary["pull_request"] = {}
    return summary


def _kind(item: dict[str, Any]) -> str:
    return "pull" if "pull_request" in item else "issue"


def _closing_reference_batches(
    numbers: list[int],
    kind: Callable[[int], str],
) -> Iterator[list[int]]:
    batch: list[int] = []
    pulls = 0
    for number in numbers:
        is_pull = kind(number) == "pull"
        if is_pull and pulls == _CLOSING_REFERENCE_BATCH_SIZE:
            yield batch
            batch = []
            pulls = 0
        batch.append(number)
        pulls += is_pull
    if batch:
        yield batch


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("target must be timezone-aware")
    return value.astimezone(UTC)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return _as_utc(datetime.fromisoformat(value))


def _parse_required_time(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))


def _check_count(
    number: int,
    resource: str,
    summary: Any,
    items: list[dict[str, Any]],
) -> None:
    expected = summary.get("total_count") if isinstance(summary, dict) else None
    if isinstance(expected, int) and len(items) < expected:
        raise IncompleteGitHubDataError(
            f"#{number} advertised {expected} {resource}, got {len(items)}",
        )


def _iso(value: datetime) -> str:
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.astimezone(UTC).isoformat(timespec=timespec).replace("+00:00", "Z")


def _iso_seconds(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _token(configured: str | None) -> str | None:
    return configured or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


@asynccontextmanager
async def _archive_lock(destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.parent / f".{destination.name}.lock"
    file = lock_path.open("a+")
    try:
        while True:
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.1)
        yield
    finally:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        file.close()
