"""编排 GitHub 原始事实拉取、覆盖水位与 SQLite 原子发布。

协议边界与时间语义见 gh_puller.github；持久化细节由 store 负责。本模块不把
归档解释为派生知识，也不下载正文中链接的站外附件。
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import math
import os
import re
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .client import _CATALOG_HINT, GitHubAPI, GitHubAPIError
from .progress import _PullProgressTracker
from .store import (
    ArchivedRun,
    SQLiteArchive,
    StagedResource,
    StoredHead,
    json_digest,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .progress import ProgressObserver

_NUMBER_AT_END = re.compile(r"/(\d+)$")
_CATALOG_MODES = {"certified", "exhaustive"}
_BUNDLE_MODES = {"optimized", "exhaustive"}
_STAGE_BATCH_SIZE = 32


class IncompleteGitHubDataError(RuntimeError):
    """GitHub 的可检测截断会破坏全量契约。"""


class _API(Protocol):
    request_count: int

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str | None = None,
    ) -> Any: ...

    async def get_json_cached(
        self,
        path: str,
        *,
        previous: Any | None,
        cache: dict[str, Any] | None,
        params: dict[str, Any] | None = None,
        accept: str | None = None,
    ) -> tuple[Any, dict[str, Any] | None]: ...

    async def get_text(self, path: str, *, accept: str) -> str: ...

    async def get_text_cached(
        self,
        path: str,
        *,
        accept: str,
        previous: str | None,
        cache: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any] | None]: ...

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

    async def collection_size(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> int | None: ...

    async def repository_item_count(self, owner: str, repo: str) -> int | None: ...

    async def repository_catalog(
        self,
        owner: str,
        repo: str,
        *,
        page_observer: Callable[[int], None] | None = None,
    ) -> tuple[list[dict[str, Any]], int] | None: ...


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
    transient_retries: int = 5  # Network/5xx retry budget; rate limits wait separately.
    overlap_seconds: int = 2  # Replayed boundary for second-resolution GitHub timestamps.
    catalog_mode: str = "certified"  # exhaustive is the correctness oracle.
    bundle_mode: str = "optimized"  # exhaustive is the per-parent transport oracle.

    def __post_init__(self) -> None:
        parts = self.repository.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("repository must be 'owner/repo'")
        if self.concurrency < 1:
            raise ValueError("concurrency must be positive")
        if self.overlap_seconds < 1:
            raise ValueError("overlap_seconds must be positive")
        if self.catalog_mode not in _CATALOG_MODES:
            raise ValueError(f"catalog_mode must be one of {sorted(_CATALOG_MODES)}")
        if self.bundle_mode not in _BUNDLE_MODES:
            raise ValueError(f"bundle_mode must be one of {sorted(_BUNDLE_MODES)}")
        object.__setattr__(self, "destination", Path(self.destination))


@dataclass(frozen=True, slots=True)
class PullResult:
    target_at: datetime  # Requested coverage watermark.
    completed_at: datetime  # Actual completion time C.
    run_id: int  # Committed SQLite pull-run identity.
    changed_items: int  # Object versions or tombstones published by this run.
    catalog_items: int  # Issues and PRs currently present.
    requests: int  # HTTP attempts accumulated across run recovery.

    @property
    def lag_seconds(self) -> float:
        """Return the non-negative completion lag behind the target watermark."""
        return max((self.completed_at - self.target_at).total_seconds(), 0.0)


@dataclass(frozen=True, slots=True)
class _CatalogPlan:
    items: dict[int, dict[str, Any]]  # REST summaries or compact GraphQL hints.
    present: set[int]  # Certified membership through the cutoff.
    signals: set[int]  # Parents dirtied by repository-wide child feeds.
    force_all: bool  # Whether every surviving bundle must be fetched.


@dataclass(frozen=True, slots=True)
class _BundleFeeds:
    issue_comments: dict[int, list[dict[str, Any]]] | None
    review_comments: dict[int, list[dict[str, Any]]] | None


class GitHubPuller:
    """可复用的 GitHub 增量拉取操作。

    Args:
        config: 仓库、SQLite 事实库和请求策略。
        api: 测试或宿主提供的 GitHub API 读取对象。
        now: 在函数入口冻结默认目标及记录完成时刻的 UTC 时钟。
        sleep: 等待未来目标使用的异步等待函数。
        observer: 同步带外进度观察器；失败不影响事实拉取。
    """

    def __init__(
        self,
        config: GitHubPullConfig,
        *,
        api: _API | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        observer: ProgressObserver | None = None,
    ) -> None:
        self.config = config
        self._api = api
        self._now = now
        self._sleep = sleep
        self._observer = observer
        self._owner, self._repo = config.repository.split("/", 1)
        self._base = f"/repos/{self._owner}/{self._repo}"
        self._feed_counts: dict[str, int] = {}

    async def pull(self, target: datetime | None = None) -> PullResult:
        """完成一次增量拉取并原子发布一个事实库 run。

        Args:
            target: 需要覆盖到的时刻；None 在进入本函数、任何 await 之前取当前
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
        request_start = api.request_count
        accounted = False
        try:
            run = await archive.start_run(_iso(target_at), _iso(started_at))
            progress.bind_run(run.id, run.request_count, request_start)
            observed = _parse_time(run.observed_until)
            try:
                now = _as_utc(self._now())
                if now < target_at:
                    observed = await self._sync_pass(
                        api,
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
                await self._sync_pass(
                    api,
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
        token = self.config.token
        if token is None:
            token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        return GitHubAPI(
            token=token,
            base_url=self.config.api_url,
            graphql_url=self.config.graphql_url,
            api_version=self.config.api_version,
            timeout=self.config.request_timeout,
            transient_retries=self.config.transient_retries,
            progress=progress.api_progress,
        ), True

    async def _sync_pass(
        self,
        api: _API,
        archive: SQLiteArchive,
        run_id: int,
        cutoff: datetime,
        observed: datetime | None,
        pass_name: str,
        progress: _PullProgressTracker,
    ) -> datetime | None:
        progress.start_pass(pass_name, cutoff)
        heads, staged = await archive.load_head_state(run_id)
        progress.restore_staged((head.number, head.kind, head.present) for head in staged.values())
        if observed is not None and cutoff <= observed:
            return observed
        plan = await self._catalog_plan(api, heads, cutoff, observed, progress)
        candidates = (
            set(plan.present)
            if plan.force_all
            else {number for number, item in plan.items.items() if _needs_refresh(heads.get(number), item)}
        )
        candidates.update(plan.signals)
        candidates.intersection_update(plan.present)
        carried = [
            (head.number, head.kind)
            for number, head in staged.items()
            if head.present and number in plan.present and number not in candidates
        ]
        carried_tombstones = sum(not head.present and number not in plan.present for number, head in staged.items())
        progress.catalog_complete(len(plan.present))
        progress.start_bundles(
            pass_name,
            len(candidates),
            carried,
            carried_tombstones,
            api.request_count,
        )

        feeds = await self._bundle_feeds(
            api,
            heads,
            plan.items,
            candidates,
            cutoff,
            pass_name,
            progress,
        )
        progress.resume_bundles(pass_name, api.request_count)

        await self._fetch_candidates(
            api,
            archive,
            run_id,
            heads,
            plan.items,
            candidates,
            _iso(cutoff),
            progress,
            feeds,
        )
        tombstones = [
            StagedResource(
                head=StoredHead(
                    number=head.number,
                    github_id=head.github_id,
                    kind=head.kind,
                    created_at=head.created_at,
                    updated_at=head.updated_at,
                    summary_digest=head.summary_digest,
                    bundle_digest=head.bundle_digest,
                    present=False,
                    missing_since=_iso(cutoff),
                ),
                observed_at=_iso(cutoff),
                summary=None,
                bundle=None,
            )
            for number, head in heads.items()
            if head.present and number not in plan.present
        ]
        if tombstones:
            await archive.stage(run_id, tombstones)
            progress.tombstones_staged(len(tombstones), api.request_count)
        await archive.update_observed(run_id, _iso(cutoff))
        return cutoff

    async def _catalog_plan(
        self,
        api: _API,
        heads: dict[int, StoredHead],
        cutoff: datetime,
        observed: datetime | None,
        progress: _PullProgressTracker,
    ) -> _CatalogPlan:
        if observed is None:
            if self.config.catalog_mode == "exhaustive":
                catalog = await self._stable_catalog(api, cutoff, progress)
                return _full_plan(catalog, force_all=False)
            return _full_plan(await self._counted_catalog(api, cutoff, progress), force_all=False)

        previous = {number: head for number, head in heads.items() if head.present}
        if not previous:
            return _full_plan(await self._stable_catalog(api, cutoff, progress), force_all=False)

        since = _iso_seconds(observed - timedelta(seconds=self.config.overlap_seconds))
        progress.catalog_scan()
        catalog_task = asyncio.create_task(
            api.paginate(
                f"{self._base}/issues",
                params={
                    "state": "all",
                    "sort": "created",
                    "direction": "asc",
                    "since": since,
                },
                page_observer=progress.catalog_page,
            ),
        )
        issue_signal_task = asyncio.create_task(
            self._signal_numbers(api, "issues/comments", "issue_url", since),
        )
        review_signal_task = asyncio.create_task(
            self._signal_numbers(api, "pulls/comments", "pull_request_url", since),
        )
        count_task = asyncio.create_task(self._repository_item_count(api, progress))
        tasks = [catalog_task, issue_signal_task, review_signal_task, count_task]
        try:
            delta, issue_signals, review_signals, current_count = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        certified = _certified_head_merge(previous, delta, current_count, cutoff)
        signals = issue_signals | review_signals
        if self.config.catalog_mode == "exhaustive":
            full = _full_plan(await self._stable_catalog(api, cutoff, progress), force_all=True)
            return _overlay_plan(full, delta, signals, cutoff)
        if certified is None:
            full = _full_plan(await self._stable_catalog(api, cutoff, progress), force_all=False)
            return _overlay_plan(full, delta, signals, cutoff)
        present, items = certified
        return _CatalogPlan(
            items=items,
            present=present,
            signals=signals,
            force_all=False,
        )

    async def _counted_catalog(
        self,
        api: _API,
        cutoff: datetime,
        progress: _PullProgressTracker,
    ) -> list[dict[str, Any]]:
        progress.catalog_scan()
        catalog, current_count = await self._catalog_scan(api, progress)
        if current_count is None:
            current_count = await self._repository_item_count(api, progress)
        certified = _certified_full_catalog(catalog, current_count, cutoff)
        if certified is not None:
            return certified
        return await self._stable_catalog(api, cutoff, progress, previous_catalog=catalog)

    async def _stable_catalog(
        self,
        api: _API,
        cutoff: datetime,
        progress: _PullProgressTracker,
        *,
        previous_catalog: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        previous = _catalog_signature(previous_catalog, cutoff) if previous_catalog is not None else None
        while True:
            progress.catalog_scan()
            catalog, _ = await self._catalog_scan(api, progress)
            signature = _catalog_signature(catalog, cutoff)
            if signature is None:
                previous = None
                continue
            if signature == previous:
                return _visible_catalog(catalog, cutoff)
            previous = signature

    async def _catalog_scan(
        self,
        api: _API,
        progress: _PullProgressTracker,
    ) -> tuple[list[dict[str, Any]], int | None]:
        catalog = await api.repository_catalog(
            self._owner,
            self._repo,
            page_observer=progress.catalog_page,
        )
        if catalog is not None:
            items, count = catalog
            progress.catalog_count(count)
            return items, count
        items = await api.paginate(
            f"{self._base}/issues",
            params={"state": "all", "sort": "created", "direction": "desc"},
            page_observer=progress.catalog_page,
        )
        return items, None

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

    async def _fetch_candidates(
        self,
        api: _API,
        archive: SQLiteArchive,
        run_id: int,
        heads: dict[int, StoredHead],
        summaries: dict[int, dict[str, Any]],
        candidates: set[int],
        observed_at: str,
        progress: _PullProgressTracker,
        feeds: _BundleFeeds,
    ) -> None:
        async def fetch(
            number: int,
        ) -> tuple[
            int,
            dict[str, Any],
            dict[str, Any] | None,
            dict[str, Any] | None,
        ]:
            summary = summaries.get(number)
            if summary is None:
                summary = _minimal_summary(heads[number])
                stored_summary = None
            else:
                stored_summary = summary
            previous = heads.get(number)
            previous_bundle = None
            previous_cache = None
            if self.config.bundle_mode == "optimized" and previous is not None:
                previous_bundle, previous_cache = await archive.load_bundle_state(
                    previous.bundle_digest,
                )
            catalog_hint = summary.get(_CATALOG_HINT) is True
            bundle, http_cache = await self._fetch_bundle(
                api,
                summary,
                feeds,
                previous_bundle,
                previous_cache,
            )
            if catalog_hint:
                if (
                    previous is None
                    or not previous.present
                    or previous.updated_at != summary.get("updated_at")
                    or previous.kind != bundle.get("kind")
                ):
                    detail = bundle.get("issue")
                    if not isinstance(detail, dict):
                        raise IncompleteGitHubDataError(
                            f"issue #{number} bundle has no detail summary",
                        )
                    stored_summary = detail
                else:
                    stored_summary = None
            return number, bundle, stored_summary, http_cache

        numbers = iter(sorted(candidates))
        pending = deque(asyncio.create_task(fetch(number)) for number in islice(numbers, self.config.concurrency))
        staged: list[StagedResource] = []
        try:
            while pending:
                number, bundle, summary, http_cache = await pending.popleft()
                old = heads.get(number)
                head = _candidate_head(summary, old, bundle)
                staged.append(
                    StagedResource(
                        head=head,
                        observed_at=observed_at,
                        summary=summary,
                        bundle=bundle,
                        http_cache=http_cache,
                    ),
                )
                next_number = next(numbers, None)
                if next_number is not None:
                    pending.append(asyncio.create_task(fetch(next_number)))
                if len(staged) >= _STAGE_BATCH_SIZE:
                    batch, staged = staged, []
                    await archive.stage(run_id, batch)
                    progress.bundles_staged(
                        ((resource.head.number, resource.head.kind) for resource in batch),
                        api.request_count,
                    )
            if staged:
                batch, staged = staged, []
                await archive.stage(run_id, batch)
                progress.bundles_staged(
                    ((resource.head.number, resource.head.kind) for resource in batch),
                    api.request_count,
                )
        except BaseException:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if staged:
                await archive.stage(run_id, staged)
                progress.bundles_staged(
                    ((resource.head.number, resource.head.kind) for resource in staged),
                    api.request_count,
                )
            raise

    async def _bundle_feeds(
        self,
        api: _API,
        heads: dict[int, StoredHead],
        summaries: dict[int, dict[str, Any]],
        candidates: set[int],
        cutoff: datetime,
        pass_name: str,
        progress: _PullProgressTracker,
    ) -> _BundleFeeds:
        if self.config.bundle_mode == "exhaustive":
            return _BundleFeeds(None, None)
        pulls = {number for number in candidates if _candidate_kind(number, heads, summaries) == "pull"}
        issue_comments = await self._safe_pooled_feed(
            api,
            "issues/comments",
            "issue_url",
            candidates,
            cutoff,
            pass_name,
            progress,
        )
        review_comments = await self._safe_pooled_feed(
            api,
            "pulls/comments",
            "pull_request_url",
            pulls,
            cutoff,
            pass_name,
            progress,
        )
        return _BundleFeeds(issue_comments, review_comments)

    async def _safe_pooled_feed(
        self,
        api: _API,
        endpoint: str,
        url_field: str,
        parents: set[int],
        cutoff: datetime,
        pass_name: str,
        progress: _PullProgressTracker,
    ) -> dict[int, list[dict[str, Any]]] | None:
        try:
            return await self._pooled_feed(
                api,
                endpoint,
                url_field,
                parents,
                cutoff,
                pass_name,
                progress,
            )
        except GitHubAPIError:
            progress.feed_fallback(endpoint, api.request_count)
            return None

    async def _pooled_feed(
        self,
        api: _API,
        endpoint: str,
        url_field: str,
        parents: set[int],
        cutoff: datetime,
        pass_name: str,
        progress: _PullProgressTracker,
    ) -> dict[int, list[dict[str, Any]]] | None:
        # A certified feed needs at least two requests, so tiny candidate sets win per parent.
        if len(parents) <= 2:
            return None
        path = f"{self._base}/{endpoint}"
        params = {"sort": "created", "direction": "asc"}
        progress.start_feed(pass_name, endpoint, 0, None, api.request_count)
        count = self._feed_counts.get(endpoint)
        if count is None:
            count = await api.collection_size(path, params=params)
            if count is None:
                return None
            self._feed_counts[endpoint] = count
        expected_pages = max(1, math.ceil(count / 100))
        if 2 * expected_pages >= len(parents):
            return None

        progress.start_feed(pass_name, endpoint, 1, expected_pages, api.request_count)
        previous, validator = await api.paginate_cached(
            path,
            previous=None,
            cache=None,
            params=params,
            page_observer=progress.feed_page,
        )
        self._feed_counts[endpoint] = len(previous)
        expected_pages = max(1, math.ceil(len(previous) / 100))
        if expected_pages >= len(parents):
            return None
        previous_digest = _feed_prefix_digest(
            _canonical_feed(previous, url_field),
            cutoff,
        )
        scan = 1
        while True:
            scan += 1
            progress.start_feed(pass_name, endpoint, scan, expected_pages, api.request_count)
            current, validator = await api.paginate_cached(
                path,
                previous=previous,
                cache=validator,
                params=params,
                page_observer=progress.feed_page,
            )
            canonical = _canonical_feed(current, url_field)
            current_digest = _feed_prefix_digest(canonical, cutoff)
            if current_digest == previous_digest:
                self._feed_counts[endpoint] = len(current)
                return _group_feed(canonical, url_field, parents)
            previous = current
            previous_digest = current_digest
            expected_pages = max(1, math.ceil(len(current) / 100))

    async def _fetch_bundle(
        self,
        api: _API,
        summary: dict[str, Any],
        feeds: _BundleFeeds,
        previous: dict[str, Any] | None,
        previous_cache: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        number = int(summary["number"])
        issue_path = f"{self._base}/issues/{number}"
        http_cache: dict[str, Any] = {}
        issue, cache = await self._cached_json(
            api,
            issue_path,
            _dict_field(previous, "issue"),
            _dict_field(previous_cache, "issue"),
        )
        _set_cache(http_cache, "issue", cache)
        if feeds.issue_comments is None:
            comments, cache = await self._cached_page(
                api,
                f"{issue_path}/comments",
                _list_field(previous, "issue_comments"),
                _dict_field(previous_cache, "issue_comments"),
            )
            _set_cache(http_cache, "issue_comments", cache)
        else:
            comments = feeds.issue_comments.get(number, [])
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
        if self.config.bundle_mode == "optimized" and _is_zero_count(issue.get("reactions")):
            reactions = []
        else:
            reactions, cache = await self._cached_page(
                api,
                f"{issue_path}/reactions",
                _list_field(previous, "reactions"),
                _dict_field(previous_cache, "reactions"),
            )
            _set_cache(http_cache, "reactions", cache)
        comment_reactions, cache = await self._reactions(
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
        bundle: dict[str, Any] = {
            "schema_version": 1,
            "repository": self.config.repository,
            "number": number,
            "kind": "pull" if "pull_request" in issue else "issue",
            "issue": issue,
            "issue_comments": comments,
            "timeline": timeline,
            "events": events,
            "reactions": reactions,
            "issue_comment_reactions": comment_reactions,
        }
        if bundle["kind"] == "pull":
            pull, cache = await self._fetch_pull(
                api,
                number,
                feeds,
                _dict_field(previous, "pull_request"),
                _dict_field(previous_cache, "pull_request"),
            )
            bundle["pull_request"] = pull
            _set_cache(http_cache, "pull_request", cache)
        return bundle, http_cache or None

    async def _fetch_pull(
        self,
        api: _API,
        number: int,
        feeds: _BundleFeeds,
        previous: dict[str, Any] | None,
        previous_cache: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        path = f"{self._base}/pulls/{number}"
        http_cache: dict[str, Any] = {}
        pull, cache = await self._cached_json(
            api,
            path,
            _dict_field(previous, "detail"),
            _dict_field(previous_cache, "detail"),
        )
        _set_cache(http_cache, "detail", cache)
        reviews, cache = await self._cached_page(
            api,
            f"{path}/reviews",
            _list_field(previous, "reviews"),
            _dict_field(previous_cache, "reviews"),
        )
        _set_cache(http_cache, "reviews", cache)
        if feeds.review_comments is None:
            review_comments, cache = await self._cached_page(
                api,
                f"{path}/comments",
                _list_field(previous, "review_comments"),
                _dict_field(previous_cache, "review_comments"),
            )
            _set_cache(http_cache, "review_comments", cache)
        else:
            review_comments = feeds.review_comments.get(number, [])
        review_comments = _canonical_comments(review_comments)
        if self.config.bundle_mode == "optimized" and _is_zero_integer(pull.get("commits")):
            commits = []
        else:
            commits, cache = await self._cached_page(
                api,
                f"{path}/commits",
                _list_field(previous, "commits"),
                _dict_field(previous_cache, "commits"),
            )
            _set_cache(http_cache, "commits", cache)
        if self.config.bundle_mode == "optimized" and _is_zero_integer(
            pull.get("changed_files"),
        ):
            files = []
        else:
            files, cache = await self._cached_page(
                api,
                f"{path}/files",
                _list_field(previous, "files"),
                _dict_field(previous_cache, "files"),
            )
            _set_cache(http_cache, "files", cache)
        requested_reviewers, cache = await self._cached_json(
            api,
            f"{path}/requested_reviewers",
            _dict_field(previous, "requested_reviewers"),
            _dict_field(previous_cache, "requested_reviewers"),
        )
        _set_cache(http_cache, "requested_reviewers", cache)
        review_reactions, cache = await self._reactions(
            api,
            "pulls/comments",
            review_comments,
            _dict_field(previous, "review_comment_reactions"),
            _dict_field(previous_cache, "review_comment_reactions"),
        )
        _set_cache(http_cache, "review_comment_reactions", cache)
        expected_files = pull.get("changed_files")
        if isinstance(expected_files, int) and len(files) < expected_files:
            raise IncompleteGitHubDataError(
                f"pull #{number} advertised {expected_files} files, got {len(files)}",
            )
        expected_commits = pull.get("commits")
        if isinstance(expected_commits, int) and len(commits) < expected_commits:
            raise IncompleteGitHubDataError(
                f"pull #{number} advertised {expected_commits} commits, got {len(commits)}",
            )
        commit_media_cache: dict[tuple[str, str], tuple[str | None, str | None]] = {}
        diff, diff_fallback, cache = await self._pull_media(
            api,
            path,
            commits,
            primary="diff",
            fallback="patch",
            previous=_str_field(previous, "diff"),
            validator=_dict_field(previous_cache, "diff"),
            commit_cache=commit_media_cache,
        )
        _set_cache(http_cache, "diff", cache)
        patch, patch_fallback, cache = await self._pull_media(
            api,
            path,
            commits,
            primary="patch",
            fallback="diff",
            previous=_str_field(previous, "patch"),
            validator=_dict_field(previous_cache, "patch"),
            commit_cache=commit_media_cache,
        )
        _set_cache(http_cache, "patch", cache)
        result = {
            "detail": pull,
            "reviews": reviews,
            "review_comments": review_comments,
            "review_comment_reactions": review_reactions,
            "commits": commits,
            "files": files,
            "requested_reviewers": requested_reviewers,
            "diff": diff,
            "patch": patch,
        }
        if diff_fallback is not None:
            result["diff_fallback"] = diff_fallback
        if patch_fallback is not None:
            result["patch_fallback"] = patch_fallback
        return result, http_cache or None

    async def _cached_json(
        self,
        api: _API,
        path: str,
        previous: dict[str, Any] | None,
        cache: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if self.config.bundle_mode == "exhaustive":
            value = await api.get_json(path)
            return value, None
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
        if self.config.bundle_mode == "exhaustive":
            return await api.paginate(path), None
        return await api.paginate_cached(
            path,
            previous=previous,
            cache=cache,
        )

    async def _pull_media(
        self,
        api: _API,
        path: str,
        commits: list[dict[str, Any]],
        *,
        primary: str,
        fallback: str,
        previous: str | None,
        validator: dict[str, Any] | None,
        commit_cache: dict[tuple[str, str], tuple[str | None, str | None]],
    ) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
        accept = f"application/vnd.github.{primary}"
        try:
            value, cache = await self._cached_text(
                api,
                path,
                accept,
                previous,
                validator,
            )
        except GitHubAPIError as error:
            parts = []
            for commit in commits:
                sha = commit.get("sha")
                if not isinstance(sha, str) or not sha:
                    raise IncompleteGitHubDataError("pull commit is missing sha") from error
                parts.append(
                    await self._commit_media(
                        api,
                        sha,
                        primary=primary,
                        fallback=fallback,
                        cache=commit_cache,
                    ),
                )
            if not parts:
                raise
            return None, {"error": str(error), "commits": parts}, None
        else:
            return value, None, cache

    async def _cached_text(
        self,
        api: _API,
        path: str,
        accept: str,
        previous: str | None,
        cache: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any] | None]:
        if self.config.bundle_mode == "exhaustive":
            return await api.get_text(path, accept=accept), None
        return await api.get_text_cached(
            path,
            accept=accept,
            previous=previous,
            cache=cache,
        )

    async def _commit_media(
        self,
        api: _API,
        sha: str,
        *,
        primary: str,
        fallback: str,
        cache: dict[tuple[str, str], tuple[str | None, str | None]],
    ) -> dict[str, Any]:
        value, error = await self._cached_commit_media(api, sha, primary, cache)
        result = {"sha": sha, primary: value}
        if error is None:
            return result
        result[f"{primary}_error"] = error
        value, fallback_error = await self._cached_commit_media(api, sha, fallback, cache)
        result[fallback] = value
        if fallback_error is not None:
            result[f"{fallback}_error"] = fallback_error
            raise IncompleteGitHubDataError(
                f"commit {sha} has no complete diff or patch representation",
            )
        return result

    async def _cached_commit_media(
        self,
        api: _API,
        sha: str,
        media: str,
        cache: dict[tuple[str, str], tuple[str | None, str | None]],
    ) -> tuple[str | None, str | None]:
        key = sha, media
        if key not in cache:
            path = f"{self._base}/commits/{sha}"
            try:
                cache[key] = (
                    await api.get_text(path, accept=f"application/vnd.github.{media}"),
                    None,
                )
            except GitHubAPIError as error:
                cache[key] = None, str(error)
        return cache[key]

    async def _reactions(
        self,
        api: _API,
        endpoint: str,
        comments: list[dict[str, Any]],
        previous: dict[str, Any] | None,
        previous_cache: dict[str, Any] | None,
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any] | None]:
        result: dict[str, list[dict[str, Any]]] = {}
        http_cache: dict[str, Any] = {}
        for comment in comments:
            reaction_summary = comment.get("reactions")
            if isinstance(reaction_summary, dict) and reaction_summary.get("total_count") == 0:
                result[str(comment["id"])] = []
                continue
            comment_id = int(comment["id"])
            key = str(comment_id)
            reactions, cache = await self._cached_page(
                api,
                f"{self._base}/{endpoint}/{comment_id}/reactions",
                _list_field(previous, key),
                _dict_field(previous_cache, key),
            )
            result[key] = reactions
            _set_cache(http_cache, key, cache)
            _check_count(comment_id, "comment reactions", reaction_summary, reactions)
        return result, http_cache or None


async def incremental_pull(
    config: GitHubPullConfig,
    target: datetime | None = None,
    *,
    observer: ProgressObserver | None = None,
) -> PullResult:
    """执行一次完整的增量拉取。

    Args:
        config: 仓库、SQLite 事实库与请求策略。
        target: 覆盖目标；None 在函数入口冻结为当前 UTC 时刻。
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


def _certified_head_merge(
    previous: dict[int, StoredHead],
    delta: list[dict[str, Any]],
    current_count: int | None,
    cutoff: datetime,
) -> tuple[set[int], dict[int, dict[str, Any]]] | None:
    if type(current_count) is not int or current_count < 0:
        return None
    changed = _unique_catalog(delta)
    if changed is None:
        return None
    visible: dict[int, dict[str, Any]] = {}
    future: set[int] = set()
    try:
        for number, item in changed.items():
            created_at = _item_time(item, "created_at")
            _item_time(item, "updated_at")
            prior = previous.get(number)
            if prior is not None and not _same_head(prior, item):
                return None
            if created_at <= cutoff:
                visible[number] = item
            elif prior is not None:
                return None
            else:
                future.add(number)
        for head in previous.values():
            if _parse_required_time(head.created_at) > cutoff:
                return None
            _parse_required_time(head.updated_at)
    except (IncompleteGitHubDataError, TypeError, ValueError):
        return None

    additions = set(visible) - set(previous)
    if current_count - len(future) != len(previous) + len(additions):
        return None
    return set(previous) | set(visible), visible


def _certified_merge(
    previous: list[dict[str, Any]],
    delta: list[dict[str, Any]],
    current_count: int | None,
    cutoff: datetime,
) -> list[dict[str, Any]] | None:
    if type(current_count) is not int or current_count < 0:
        return None
    try:
        old = _unique_catalog(previous)
        changed = _unique_catalog(delta)
        if old is None or changed is None:
            return None
        visible: dict[int, dict[str, Any]] = {}
        future: set[int] = set()
        for number, item in changed.items():
            created_at = _item_time(item, "created_at")
            _item_time(item, "updated_at")
            prior = old.get(number)
            if prior is not None and not _same_item(prior, item):
                return None
            if created_at <= cutoff:
                visible[number] = item
            elif prior is not None:
                return None
            else:
                future.add(number)
        for item in old.values():
            if _item_time(item, "created_at") > cutoff:
                return None
            _item_time(item, "updated_at")
    except (IncompleteGitHubDataError, TypeError, ValueError):
        return None

    additions = set(visible) - set(old)
    if current_count - len(future) != len(old) + len(additions):
        return None
    return _sort_catalog(list((old | visible).values()))


def _certified_full_catalog(
    catalog: list[dict[str, Any]],
    current_count: int | None,
    cutoff: datetime,
) -> list[dict[str, Any]] | None:
    if type(current_count) is not int or current_count < 0:
        return None
    indexed = _unique_catalog(catalog)
    if indexed is None or len(indexed) != current_count:
        return None
    try:
        return _visible_catalog(catalog, cutoff)
    except (IncompleteGitHubDataError, TypeError, ValueError):
        return None


def _catalog_signature(
    catalog: list[dict[str, Any]] | None,
    cutoff: datetime,
) -> list[tuple[int, str]] | None:
    if catalog is None or _unique_catalog(catalog) is None:
        return None
    try:
        visible = _visible_catalog(catalog, cutoff)
    except (IncompleteGitHubDataError, TypeError, ValueError):
        return None
    return [(int(item["number"]), item["created_at"]) for item in visible]


def _visible_catalog(
    catalog: list[dict[str, Any]],
    cutoff: datetime,
) -> list[dict[str, Any]]:
    visible = []
    for item in catalog:
        created_at = _item_time(item, "created_at")
        _item_time(item, "updated_at")
        if created_at <= cutoff:
            visible.append(item)
    return _sort_catalog(visible)


def _unique_catalog(items: list[dict[str, Any]]) -> dict[int, dict[str, Any]] | None:
    result: dict[int, dict[str, Any]] = {}
    for item in items:
        number = item.get("number")
        if type(number) is not int or number < 1 or number in result:
            return None
        result[number] = item
    return result


def _full_plan(catalog: list[dict[str, Any]], *, force_all: bool) -> _CatalogPlan:
    indexed = _unique_catalog(catalog)
    if indexed is None:
        raise IncompleteGitHubDataError("GitHub catalog contains duplicate or invalid numbers")
    return _CatalogPlan(
        items=indexed,
        present=set(indexed),
        signals=set(),
        force_all=force_all,
    )


def _overlay_plan(
    plan: _CatalogPlan,
    delta: list[dict[str, Any]],
    signals: set[int],
    cutoff: datetime,
) -> _CatalogPlan:
    items = dict(plan.items)
    changed = _unique_catalog(delta)
    if changed is not None:
        for number, item in changed.items():
            if number not in plan.present or _item_time(item, "created_at") > cutoff:
                continue
            catalog_item = items[number]
            if catalog_item.get("created_at") != item.get("created_at") or _kind(catalog_item) != _kind(item):
                raise IncompleteGitHubDataError(
                    f"GitHub catalog sources disagree for #{number}",
                )
            items[number] = item
    return _CatalogPlan(
        items=items,
        present=plan.present,
        signals=signals,
        force_all=plan.force_all,
    )


def _needs_refresh(head: StoredHead | None, summary: dict[str, Any]) -> bool:
    if head is None or not head.present or head.bundle_digest is None:
        return True
    if summary.get(_CATALOG_HINT) is True:
        return (
            head.kind != _kind(summary)
            or head.created_at != summary.get("created_at")
            or head.updated_at != summary.get("updated_at")
        )
    return (
        head.kind != _kind(summary)
        or head.updated_at != summary.get("updated_at")
        or head.summary_digest != json_digest(summary)
    )


def _candidate_kind(
    number: int,
    heads: dict[int, StoredHead],
    summaries: dict[int, dict[str, Any]],
) -> str:
    summary = summaries.get(number)
    if summary is not None:
        return _kind(summary)
    head = heads.get(number)
    if head is None:
        raise IncompleteGitHubDataError(f"candidate #{number} has no catalog identity")
    return head.kind


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


def _canonical_comments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    for item in items:
        item_id = item.get("id")
        if type(item_id) is not int or item_id in seen:
            raise IncompleteGitHubDataError("GitHub comment collection has invalid identities")
        seen.add(item_id)
    return sorted(items, key=lambda item: int(item["id"]))


def _canonical_feed(
    items: list[dict[str, Any]],
    url_field: str,
) -> list[dict[str, Any]]:
    canonical = _canonical_comments(items)
    for item in canonical:
        if _feed_number(item, url_field) is None:
            raise IncompleteGitHubDataError(f"GitHub comment has no valid {url_field}")
    return sorted(
        canonical,
        key=lambda item: (
            _feed_number(item, url_field),
            int(item["id"]),
        ),
    )


def _group_feed(
    items: list[dict[str, Any]],
    url_field: str,
    parents: set[int],
) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for item in items:
        number = _feed_number(item, url_field)
        if number in parents:
            grouped.setdefault(number, []).append(item)
    return grouped


def _feed_prefix_digest(items: list[dict[str, Any]], cutoff: datetime) -> str:
    digest = hashlib.sha256()
    count = 0
    for item in items:
        if _item_time(item, "created_at") > cutoff:
            continue
        digest.update(bytes.fromhex(json_digest(item)))
        count += 1
    digest.update(count.to_bytes(8, "big"))
    return digest.hexdigest()


def _feed_number(item: dict[str, Any], url_field: str) -> int | None:
    value = item.get(url_field)
    match = _NUMBER_AT_END.search(value) if isinstance(value, str) else None
    return int(match.group(1)) if match else None


def _is_zero_count(value: Any) -> bool:
    return isinstance(value, dict) and _is_zero_integer(value.get("total_count"))


def _is_zero_integer(value: Any) -> bool:
    return type(value) is int and value == 0


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
        raise IncompleteGitHubDataError("dirty parent is absent from the certified catalog")
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


def _minimal_summary(head: StoredHead | None) -> dict[str, Any]:
    if head is None:
        raise IncompleteGitHubDataError("dirty parent is absent from the certified catalog")
    summary: dict[str, Any] = {
        "id": head.github_id,
        "number": head.number,
        "created_at": head.created_at,
        "updated_at": head.updated_at,
    }
    if head.kind == "pull":
        summary["pull_request"] = {}
    return summary


def _same_head(head: StoredHead, item: dict[str, Any]) -> bool:
    return head.github_id == item.get("id") and head.created_at == item.get("created_at") and head.kind == _kind(item)


def _same_item(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left.get("id") == right.get("id")
        and left.get("created_at") == right.get("created_at")
        and _kind(left) == _kind(right)
    )


def _kind(item: dict[str, Any]) -> str:
    return "pull" if "pull_request" in item else "issue"


def _sort_catalog(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(catalog, key=lambda item: (_item_time(item, "created_at"), int(item["number"])))


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


def _item_time(item: dict[str, Any], field: str) -> datetime:
    value = item.get(field)
    if not isinstance(value, str):
        raise IncompleteGitHubDataError(f"GitHub item has no {field}: {item.get('url')}")
    return _parse_required_time(value)


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
