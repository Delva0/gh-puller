"""编排 GitHub 原始事实拉取、覆盖水位与 SQLite 原子发布。

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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .client import GitHubAPI
from .store import (
    ArchivedRun,
    SQLiteArchive,
    StagedResource,
    StoredHead,
    json_digest,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_NUMBER_AT_END = re.compile(r"/(\d+)$")
_CATALOG_MODES = {"certified", "exhaustive"}
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

    async def get_text(self, path: str, *, accept: str) -> str: ...

    async def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    async def repository_item_count(self, owner: str, repo: str) -> int | None: ...


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
    items: dict[int, dict[str, Any]]  # Full summaries observed in this pass.
    present: set[int]  # Certified membership through the cutoff.
    signals: set[int]  # Parents dirtied by repository-wide child feeds.
    force_all: bool  # Whether every surviving bundle must be fetched.


class GitHubPuller:
    """可复用的 GitHub 增量拉取操作。

    Args:
        config: 仓库、SQLite 事实库和请求策略。
        api: 测试或宿主提供的 GitHub API 读取对象。
        now: 在函数入口冻结默认目标及记录完成时刻的 UTC 时钟。
        sleep: 等待未来目标使用的异步等待函数。
    """

    def __init__(
        self,
        config: GitHubPullConfig,
        *,
        api: _API | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self._api = api
        self._now = now
        self._sleep = sleep
        self._owner, self._repo = config.repository.split("/", 1)
        self._base = f"/repos/{self._owner}/{self._repo}"

    async def pull(
        self,
        target: datetime | None = None,
        *,
        series: str | None = None,
    ) -> PullResult:
        """完成一次增量拉取并原子发布一个事实库 run。

        Args:
            target: 需要覆盖到的时刻；None 在进入本函数、任何 await 之前取当前
                UTC 时刻。显式值必须带时区。未来值会先预拉已有数据，再等待并
                做最终闭合。
            series: 调度器提供的可选稳定标签；拉取协议不解释其含义。

        Returns:
            本次水位、实际完成时刻、run identity 和数据规模。同一 ``(series, T)``
            已 committed 时返回原 run，不执行 HTTP 请求。

        Raises:
            ValueError: target 不带时区。
            IncompleteGitHubDataError: GitHub 返回可检测的截断结果。
        """
        started_at = _as_utc(self._now())
        target_at = started_at if target is None else _as_utc(target)
        async with (
            _archive_lock(self.config.destination),
            SQLiteArchive(
                self.config.destination,
                self.config.repository,
            ) as archive,
        ):
            existing = await archive.committed_run(_iso(target_at), series)
            if existing is not None:
                return _pull_result(existing)
            return await self._pull_new(archive, started_at, target_at, series)

    async def _pull_new(
        self,
        archive: SQLiteArchive,
        started_at: datetime,
        target_at: datetime,
        series: str | None,
    ) -> PullResult:
        api, owned = self._make_api()
        request_start = api.request_count
        accounted = False
        try:
            run = await archive.start_run(
                _iso(target_at),
                _iso(started_at),
                series,
            )
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
                    )
                    wait = (target_at - _as_utc(self._now())).total_seconds()
                    while wait > 0:
                        await self._sleep(wait)
                        wait = (target_at - _as_utc(self._now())).total_seconds()
                await self._sync_pass(
                    api,
                    archive,
                    run.id,
                    target_at,
                    observed,
                )
                attempt_requests = api.request_count - request_start
                await archive.add_requests(run.id, attempt_requests)
                accounted = True
                completed_at = _as_utc(self._now())
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

    def _make_api(self) -> tuple[_API, bool]:
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
        ), True

    async def _sync_pass(
        self,
        api: _API,
        archive: SQLiteArchive,
        run_id: int,
        cutoff: datetime,
        observed: datetime | None,
    ) -> datetime | None:
        if observed is not None and cutoff <= observed:
            return observed
        heads = await archive.load_heads(run_id)
        plan = await self._catalog_plan(api, heads, cutoff, observed)
        candidates = (
            set(plan.present)
            if plan.force_all
            else {number for number, item in plan.items.items() if _needs_refresh(heads.get(number), item)}
        )
        candidates.update(plan.signals)
        candidates.intersection_update(plan.present)

        await self._fetch_candidates(
            api,
            archive,
            run_id,
            heads,
            plan.items,
            candidates,
            _iso(cutoff),
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
        await archive.update_observed(run_id, _iso(cutoff))
        return cutoff

    async def _catalog_plan(
        self,
        api: _API,
        heads: dict[int, StoredHead],
        cutoff: datetime,
        observed: datetime | None,
    ) -> _CatalogPlan:
        if self.config.catalog_mode == "exhaustive":
            catalog = await self._stable_catalog(api, cutoff)
            return _full_plan(catalog, force_all=observed is not None)
        if observed is None:
            return _full_plan(await self._counted_catalog(api, cutoff), force_all=False)

        previous = {number: head for number, head in heads.items() if head.present}
        if not previous:
            return _full_plan(await self._stable_catalog(api, cutoff), force_all=True)

        since = _iso_seconds(observed - timedelta(seconds=self.config.overlap_seconds))
        catalog_task = asyncio.create_task(
            api.paginate(
                f"{self._base}/issues",
                params={
                    "state": "all",
                    "sort": "created",
                    "direction": "asc",
                    "since": since,
                },
            ),
        )
        issue_signal_task = asyncio.create_task(
            self._signal_numbers(api, "issues/comments", "issue_url", since),
        )
        review_signal_task = asyncio.create_task(
            self._signal_numbers(api, "pulls/comments", "pull_request_url", since),
        )
        count_task = asyncio.create_task(api.repository_item_count(self._owner, self._repo))
        tasks = [catalog_task, issue_signal_task, review_signal_task, count_task]
        try:
            delta, issue_signals, review_signals, current_count = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        certified = _certified_head_merge(previous, delta, current_count, cutoff)
        if certified is None:
            return _full_plan(await self._stable_catalog(api, cutoff), force_all=True)
        present, items = certified
        return _CatalogPlan(
            items=items,
            present=present,
            signals=issue_signals | review_signals,
            force_all=False,
        )

    async def _counted_catalog(
        self,
        api: _API,
        cutoff: datetime,
    ) -> list[dict[str, Any]]:
        catalog_task = asyncio.create_task(self._catalog_scan(api))
        count_task = asyncio.create_task(api.repository_item_count(self._owner, self._repo))
        tasks = [catalog_task, count_task]
        try:
            catalog, current_count = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        certified = _certified_full_catalog(catalog, current_count, cutoff)
        if certified is not None:
            return certified
        return await self._stable_catalog(api, cutoff, previous_catalog=catalog)

    async def _stable_catalog(
        self,
        api: _API,
        cutoff: datetime,
        *,
        previous_catalog: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        previous = _catalog_signature(previous_catalog, cutoff) if previous_catalog is not None else None
        while True:
            catalog = await self._catalog_scan(api)
            signature = _catalog_signature(catalog, cutoff)
            if signature is None:
                previous = None
                continue
            if signature == previous:
                return _visible_catalog(catalog, cutoff)
            previous = signature

    async def _catalog_scan(self, api: _API) -> list[dict[str, Any]]:
        return await api.paginate(
            f"{self._base}/issues",
            params={"state": "all", "sort": "created", "direction": "asc"},
        )

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
    ) -> None:
        semaphore = asyncio.Semaphore(self.config.concurrency)

        async def fetch(number: int) -> tuple[int, dict[str, Any], dict[str, Any] | None]:
            summary = summaries.get(number)
            if summary is None:
                summary = _minimal_summary(heads[number])
                stored_summary = None
            else:
                stored_summary = summary
            async with semaphore:
                bundle = await self._fetch_bundle(api, summary)
            return number, bundle, stored_summary

        tasks = [asyncio.create_task(fetch(number)) for number in sorted(candidates)]
        staged: list[StagedResource] = []
        try:
            for task in asyncio.as_completed(tasks):
                number, bundle, summary = await task
                old = heads.get(number)
                head = _candidate_head(summary, old, bundle)
                staged.append(
                    StagedResource(
                        head=head,
                        observed_at=observed_at,
                        summary=summary,
                        bundle=bundle,
                    ),
                )
                if len(staged) >= _STAGE_BATCH_SIZE:
                    batch, staged = staged, []
                    await archive.stage(run_id, batch)
            if staged:
                batch, staged = staged, []
                await archive.stage(run_id, batch)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if staged:
                await archive.stage(run_id, staged)
            raise

    async def _fetch_bundle(self, api: _API, summary: dict[str, Any]) -> dict[str, Any]:
        number = int(summary["number"])
        issue_path = f"{self._base}/issues/{number}"
        issue = await api.get_json(issue_path)
        comments = await api.paginate(f"{issue_path}/comments")
        timeline = await api.paginate(f"{issue_path}/timeline")
        events = await api.paginate(f"{issue_path}/events")
        reactions = await api.paginate(f"{issue_path}/reactions")
        comment_reactions = await self._reactions(api, "issues/comments", comments)
        summary_updated = _parse_time(summary.get("updated_at"))
        detail_updated = _parse_time(issue.get("updated_at"))
        if summary_updated and detail_updated and detail_updated < summary_updated:
            raise IncompleteGitHubDataError(
                f"issue #{number} detail is older than its catalog entry",
            )
        expected_comments = issue.get("comments")
        if isinstance(expected_comments, int) and len(comments) < expected_comments:
            raise IncompleteGitHubDataError(
                f"issue #{number} advertised {expected_comments} comments, got {len(comments)}",
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
            bundle["pull_request"] = await self._fetch_pull(api, number)
        return bundle

    async def _fetch_pull(self, api: _API, number: int) -> dict[str, Any]:
        path = f"{self._base}/pulls/{number}"
        pull = await api.get_json(path)
        reviews = await api.paginate(f"{path}/reviews")
        review_comments = await api.paginate(f"{path}/comments")
        commits = await api.paginate(f"{path}/commits")
        files = await api.paginate(f"{path}/files")
        requested_reviewers = await api.get_json(f"{path}/requested_reviewers")
        review_reactions = await self._reactions(api, "pulls/comments", review_comments)
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
        expected_review_comments = pull.get("review_comments")
        if isinstance(expected_review_comments, int) and len(review_comments) < expected_review_comments:
            raise IncompleteGitHubDataError(
                f"pull #{number} advertised {expected_review_comments} review comments, got {len(review_comments)}",
            )
        return {
            "detail": pull,
            "reviews": reviews,
            "review_comments": review_comments,
            "review_comment_reactions": review_reactions,
            "commits": commits,
            "files": files,
            "requested_reviewers": requested_reviewers,
            "diff": await api.get_text(path, accept="application/vnd.github.diff"),
            "patch": await api.get_text(path, accept="application/vnd.github.patch"),
        }

    async def _reactions(
        self,
        api: _API,
        endpoint: str,
        comments: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for comment in comments:
            reaction_summary = comment.get("reactions")
            if isinstance(reaction_summary, dict) and reaction_summary.get("total_count") == 0:
                result[str(comment["id"])] = []
                continue
            comment_id = int(comment["id"])
            result[str(comment_id)] = await api.paginate(
                f"{self._base}/{endpoint}/{comment_id}/reactions",
            )
            _check_count(comment_id, "comment reactions", reaction_summary, result[str(comment_id)])
        return result


async def incremental_pull(
    config: GitHubPullConfig,
    target: datetime | None = None,
    *,
    series: str | None = None,
) -> PullResult:
    """执行一次完整的增量拉取。

    Args:
        config: 仓库、SQLite 事实库与请求策略。
        target: 覆盖目标；None 在函数入口冻结为当前 UTC 时刻。
        series: 调度器提供的可选稳定标签。

    Returns:
        成功发布或由幂等键复用的水位、实际完成时刻和统计信息。
    """
    target_at = datetime.now(UTC) if target is None else target
    return await GitHubPuller(config).pull(target_at, series=series)


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


def _needs_refresh(head: StoredHead | None, summary: dict[str, Any]) -> bool:
    if head is None or not head.present or head.bundle_digest is None:
        return True
    return (
        head.kind != _kind(summary)
        or head.updated_at != summary.get("updated_at")
        or head.summary_digest != json_digest(summary)
    )


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
