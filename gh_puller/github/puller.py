"""编排 GitHub 原始数据归档、覆盖水位和单次 Git 提交。

协议边界与时间语义见 ``gh_puller.github``；本模块不把归档解释为派生知识，也不
下载正文中链接的站外附件。
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .client import GitHubAPI

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_SCHEMA_VERSION = 1
_STATE_NAME = ".gh-puller-state.json"
_NUMBER_AT_END = re.compile(r"/(\d+)$")
_CATALOG_MODES = {"certified", "exhaustive"}


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
    repository: str  # GitHub ``owner/repo`` identity.
    destination: Path  # Dedicated Git worktree for the raw archive.
    token: str | None = None  # None reads GH_TOKEN, then GITHUB_TOKEN, at call time.
    api_url: str = "https://api.github.com"  # REST root, including Enterprise roots.
    graphql_url: str | None = None  # None derives GitHub's GraphQL endpoint from api_url.
    api_version: str = "2022-11-28"  # Version sent to GitHub's versioned REST API.
    concurrency: int = 4  # Concurrent item bundles; each bundle paginates serially.
    request_timeout: float = 30.0  # Per-request timeout in seconds.
    transient_retries: int = 5  # Network/5xx retry budget; rate limits wait separately.
    overlap_seconds: int = 2  # Replayed boundary for second-resolution GitHub timestamps.
    catalog_mode: str = "certified"  # ``exhaustive`` is the correctness oracle.

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
    target_at: datetime  # Requested coverage watermark and commit title.
    completed_at: datetime  # Actual completion time represented by Git metadata.
    commit: str  # Commit object id created for this invocation.
    changed_items: int  # Item bundles whose raw JSON changed.
    catalog_items: int  # Issues and PRs present at the requested watermark.
    requests: int  # HTTP attempts, including retries.

    @property
    def lag_seconds(self) -> float:
        """Return the non-negative completion lag behind the target watermark."""
        return max((self.completed_at - self.target_at).total_seconds(), 0.0)


@dataclass(frozen=True, slots=True)
class _CatalogPlan:
    catalog: list[dict[str, Any]]  # Certified current root objects.
    signals: set[str]  # Parents dirtied by repository-wide child feeds.
    force_all: bool  # Whether every surviving bundle must be fetched.


class GitHubPuller:
    """可复用的 GitHub 增量拉取操作。

    Args:
        config: 仓库、数据目录和请求策略。
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

    async def pull(self, target: datetime | None = None) -> PullResult:
        """完成一次增量拉取并创建一个 Git 提交。

        Args:
            target: 需要覆盖到的时刻；None 在进入本函数、任何 ``await`` 之前取当前
                UTC 时刻。显式值必须带时区。未来值会先预拉已有数据，再异步等待并
                做最终闭合。

        Returns:
            本次水位、实际完成时刻、唯一提交和数据规模。

        Raises:
            ValueError: target 不带时区。
            IncompleteGitHubDataError: GitHub 返回可检测的截断结果。
        """
        started_at = _as_utc(self._now())
        target_at = started_at if target is None else _as_utc(target)
        api, owned = self._make_api()
        request_start = api.request_count
        changed = 0
        try:
            now = _as_utc(self._now())
            if now < target_at:
                async with _archive_lock(self.config.destination):
                    await _ensure_git(self.config.destination)
                    state = self._load_state()
                    changed += await self._sync_pass(api, state, now)
                wait = (target_at - _as_utc(self._now())).total_seconds()
                if wait > 0:
                    await self._sleep(wait)
            async with _archive_lock(self.config.destination):
                await _ensure_git(self.config.destination)
                state = self._load_state()
                changed += await self._sync_pass(api, state, target_at)
                completed_at = _as_utc(self._now())
                covered = _parse_time(state.get("covered_until"))
                if covered is None or target_at > covered:
                    state["covered_until"] = _iso(target_at)
                state["last_pull"] = {
                    "target_at": _iso(target_at),
                    "started_at": _iso(started_at),
                    "completed_at": _iso(completed_at),
                    "lag_seconds": max((completed_at - target_at).total_seconds(), 0.0),
                }
                self._save_state(state)
                commit = await _commit(self.config.destination, _iso(target_at), completed_at)
                return PullResult(
                    target_at=target_at,
                    completed_at=completed_at,
                    commit=commit,
                    changed_items=changed,
                    catalog_items=state["catalog_count"],
                    requests=api.request_count - request_start,
                )
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

    async def _sync_pass(self, api: _API, state: dict[str, Any], cutoff: datetime) -> int:
        observed = _parse_time(state.get("observed_until"))
        if observed is not None and cutoff <= observed:
            return 0
        plan = await self._catalog_plan(api, state, cutoff, observed)
        catalog = plan.catalog
        summaries = {str(item["number"]): item for item in catalog}
        candidates = (
            set(summaries)
            if plan.force_all
            else {number for number, item in summaries.items() if self._needs_refresh(state, number, item)}
        )
        candidates.update(plan.signals)
        candidates.intersection_update(summaries)

        changed = await self._fetch_candidates(api, state, summaries, candidates)
        present = set(summaries)
        for number, record in state["objects"].items():
            if number in present:
                record.pop("missing_since", None)
                record["present"] = True
            elif record.get("present", True):
                record["present"] = False
                record["missing_since"] = _iso(cutoff)
        _write_json(self.config.destination / "data" / "catalog.json", catalog)
        state["catalog_count"] = len(catalog)
        state["catalog_digest"] = _json_digest(catalog)
        state["observed_until"] = _iso(cutoff)
        self._save_state(state)
        return changed

    async def _catalog_plan(
        self,
        api: _API,
        state: dict[str, Any],
        cutoff: datetime,
        observed: datetime | None,
    ) -> _CatalogPlan:
        if self.config.catalog_mode == "exhaustive":
            return _CatalogPlan(await self._stable_catalog(api, cutoff), set(), observed is not None)
        if observed is None:
            return _CatalogPlan(await self._counted_catalog(api, cutoff), set(), False)

        previous = self._load_catalog(state)
        if previous is None:
            return _CatalogPlan(await self._stable_catalog(api, cutoff), set(), True)

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

        catalog = _certified_merge(previous, delta, current_count, cutoff)
        if catalog is None:
            return _CatalogPlan(await self._stable_catalog(api, cutoff), set(), True)
        return _CatalogPlan(catalog, issue_signals | review_signals, False)

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

    def _load_catalog(self, state: dict[str, Any]) -> list[dict[str, Any]] | None:
        path = self.config.destination / "data" / "catalog.json"
        if not path.exists():
            return None
        try:
            catalog = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(catalog, list) or any(not isinstance(item, dict) for item in catalog):
            return None
        if state.get("catalog_count") != len(catalog):
            return None
        if state.get("catalog_digest") != _json_digest(catalog):
            return None
        return catalog

    async def _signal_numbers(
        self,
        api: _API,
        endpoint: str,
        url_field: str,
        since: str,
    ) -> set[str]:
        items = await api.paginate(
            f"{self._base}/{endpoint}",
            params={"sort": "created", "direction": "asc", "since": since},
        )
        numbers: set[str] = set()
        for item in items:
            match = _NUMBER_AT_END.search(item.get(url_field, ""))
            if match:
                numbers.add(match.group(1))
        return numbers

    async def _fetch_candidates(
        self,
        api: _API,
        state: dict[str, Any],
        summaries: dict[str, dict[str, Any]],
        candidates: set[str],
    ) -> int:
        semaphore = asyncio.Semaphore(self.config.concurrency)

        async def fetch(number: str) -> tuple[str, dict[str, Any]]:
            async with semaphore:
                return number, await self._fetch_bundle(api, summaries[number])

        tasks = [asyncio.create_task(fetch(number)) for number in sorted(candidates, key=int)]
        changed = 0
        try:
            for task in asyncio.as_completed(tasks):
                number, bundle = await task
                path = self._bundle_path(bundle["kind"], int(number))
                changed += _write_json(path, bundle)
                state["objects"][number] = {
                    "kind": bundle["kind"],
                    "updated_at": bundle["issue"].get("updated_at"),
                    "catalog_digest": _json_digest(summaries[number]),
                    "present": True,
                }
                self._save_state(state)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return changed

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
            "schema_version": _SCHEMA_VERSION,
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

    def _needs_refresh(self, state: dict[str, Any], number: str, summary: dict[str, Any]) -> bool:
        record = state["objects"].get(number)
        if record is None or not record.get("present", True):
            return True
        if record.get("kind") != ("pull" if "pull_request" in summary else "issue"):
            return True
        path = self._bundle_path(record["kind"], int(number))
        return (
            not path.exists()
            or record.get("updated_at") != summary.get("updated_at")
            or record.get("catalog_digest") != _json_digest(summary)
        )

    def _bundle_path(self, kind: str, number: int) -> Path:
        return self.config.destination / "data" / f"{kind}s" / f"{number}.json"

    def _load_state(self) -> dict[str, Any]:
        path = self.config.destination / _STATE_NAME
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("schema_version") != _SCHEMA_VERSION:
                raise ValueError("unsupported GitHub archive schema")
            if state.get("repository") != self.config.repository:
                raise ValueError("archive belongs to a different GitHub repository")
            state.setdefault(
                "catalog_count",
                sum(record.get("present", True) for record in state["objects"].values()),
            )
            state.setdefault("catalog_digest", None)
            return state
        return {
            "schema_version": _SCHEMA_VERSION,
            "repository": self.config.repository,
            "api_url": self.config.api_url.rstrip("/"),
            "covered_until": None,
            "observed_until": None,
            "catalog_count": 0,
            "catalog_digest": None,
            "objects": {},
        }

    def _save_state(self, state: dict[str, Any]) -> None:
        _write_json(self.config.destination / _STATE_NAME, state)


async def incremental_pull(
    config: GitHubPullConfig,
    target: datetime | None = None,
) -> PullResult:
    """执行一次完整的增量拉取。

    Args:
        config: 仓库、归档 Git worktree 与请求策略。
        target: 覆盖目标；None 在函数入口冻结为当前 UTC 时刻。

    Returns:
        成功提交的水位、实际完成时刻和统计信息。
    """
    target_at = datetime.now(UTC) if target is None else target
    return await GitHubPuller(config).pull(target_at)


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
        visible: dict[str, dict[str, Any]] = {}
        future: set[str] = set()
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
    visible_count = current_count - len(future)
    if visible_count != len(old) + len(additions):
        return None
    merged = old | visible
    return _sort_catalog(list(merged.values()))


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
    catalog: list[dict[str, Any]],
    cutoff: datetime,
) -> list[tuple[int, str]] | None:
    if _unique_catalog(catalog) is None:
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


def _unique_catalog(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        number = item.get("number")
        if type(number) is not int or number < 1 or str(number) in result:
            return None
        result[str(number)] = item
    return result


def _same_item(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left.get("id") == right.get("id")
        and left.get("created_at") == right.get("created_at")
        and ("pull_request" in left) == ("pull_request" in right)
    )


def _sort_catalog(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(catalog, key=lambda item: (_item_time(item, "created_at"), int(item["number"])))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("target must be timezone-aware")
    return value.astimezone(UTC)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def _item_time(item: dict[str, Any], field: str) -> datetime:
    value = _parse_time(item.get(field))
    if value is None:
        raise IncompleteGitHubDataError(f"GitHub item has no {field}: {item.get('url')}")
    return value


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


def _json_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _iso(value: datetime) -> str:
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.astimezone(UTC).isoformat(timespec=timespec).replace("+00:00", "Z")


def _iso_seconds(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_json(path: Path, value: Any) -> int:
    data = f"{json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)}\n".encode()
    if path.exists() and path.read_bytes() == data:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return 1


@asynccontextmanager
async def _archive_lock(destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.parent / f".{destination.name}.gh-puller.lock"
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


async def _git(
    destination: Path,
    *args: str,
    environment: dict[str, str] | None = None,
) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(destination),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=None if environment is None else os.environ | environment,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        detail = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return stdout.decode().strip()


async def _ensure_git(destination: Path) -> None:
    _mkdir(destination)
    if not (destination / ".git").exists():
        await _git(destination, "init", "--quiet")


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


async def _commit(destination: Path, title: str, completed_at: datetime) -> str:
    await _git(destination, "add", "--all", "--", "data", _STATE_NAME)
    git_date = f"@{int(completed_at.timestamp())} +0000"
    await _git(
        destination,
        "-c",
        "user.name=gh-puller",
        "-c",
        "user.email=gh-puller@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--allow-empty",
        "--only",
        "--message",
        title,
        "--",
        "data",
        _STATE_NAME,
        environment={"GIT_AUTHOR_DATE": git_date, "GIT_COMMITTER_DATE": git_date},
    )
    return await _git(destination, "rev-parse", "HEAD")
