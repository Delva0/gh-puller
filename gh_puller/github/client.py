"""提供遵守 GitHub 限流恢复契约的异步 API 读取层。

本模块只负责 HTTP、条件校验、分页、仓库对象计数、PR 关闭关系与重试，不
解释 Issue/PR 数据，也不写归档。观测水位与持久化契约见 ``gh_puller.github``。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from .errors import GitHubAPIError
from .facts import (
    check_size,
    graphql_connection,
    graphql_parent,
    graphql_pull,
    node_connection,
    rest_commit,
    rest_issue_comment,
    rest_pull_detail,
    rest_reaction,
    rest_review,
    rest_review_comment,
)
from .progress import APIProgress, RateQuota
from .queries import (
    ISSUE_COMMENTS,
    PULL_COMMITS,
    PULL_REQUEST_DETAIL,
    PULL_REVIEW_COMMENTS,
    PULL_REVIEWS,
    REACTIONS,
    REPOSITORY_ITEM_COUNT,
    REVIEW_THREAD_COMMENTS,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from .progress import APIProgressObserver

_LOG = logging.getLogger(__name__)
_DEFAULT_ACCEPT = "application/vnd.github.full+json"
_GRAPHQL_PAGE_SIZE = 100
_QUOTA_RESET_JITTER_SECONDS = 5
_TRANSIENT_DELAYS = (1, 2, 4, 8, 16, 30)


class _Transport(StrEnum):
    REST = "rest"
    GRAPHQL = "graphql"


class _PrimaryRateLimitError(RuntimeError):
    def __init__(self, resource: str) -> None:
        super().__init__(resource)
        self.resource = resource


@dataclass(frozen=True, slots=True)
class GitHubPage:
    items: list[dict[str, Any]]  # Validated raw objects from one REST page.
    next_url: str | None  # Opaque GitHub Link cursor for the next page.


@dataclass(frozen=True, slots=True)
class GitHubResource:
    value: Any  # Stable operation shape consumed by the puller.
    source: str  # API transport that produced raw.
    raw: Any  # Exact source-native data selected by the operation.
    cache: dict[str, Any] | None = None  # REST validators paired with value.


class GitHubAPI:
    """异步 GitHub REST 与 GraphQL 客户端。

    Args:
        token: GitHub token；空值只允许访问公开资源并使用匿名限额。
        base_url: REST API 根地址，支持 GitHub Enterprise。
        graphql_url: GraphQL API 地址；None 从 REST 根地址推导。
        api_version: 发送到 ``X-GitHub-Api-Version`` 的版本。
        timeout: 单次请求超时秒数。
        client: 测试或宿主注入的 ``httpx.AsyncClient``。
        sleep: 限流与退避使用的异步等待函数。
        now: 计算限流恢复时刻使用的 UTC 时钟。
        progress: HTTP 尝试、配额与等待的同步带外观察器。
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str = "https://api.github.com",
        graphql_url: str | None = None,
        api_version: str = "2022-11-28",
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        progress: APIProgressObserver | None = None,
    ) -> None:
        headers = {
            "Accept": _DEFAULT_ACCEPT,
            "User-Agent": "gh-puller/0.1",
            "X-GitHub-Api-Version": api_version,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            follow_redirects=True,
            headers=headers,
            timeout=timeout,
        )
        if client:
            self._client.headers.update(headers)
        self._owns_client = client is None
        self._base_url = str(self._client.base_url).rstrip("/")
        self._authenticated = bool(token or self._client.headers.get("Authorization"))
        self._graphql_url = graphql_url or _graphql_endpoint(base_url)
        self._sleep = sleep
        self._now = now
        self._progress = progress
        self._primary_blocked_until: dict[str, float] = {}
        self._secondary_blocked_until = 0.0
        self._gate_lock = asyncio.Lock()
        self._quotas: dict[str, RateQuota] = {}
        self.request_count = 0

    async def close(self) -> None:
        """关闭由本对象创建的 HTTP client；注入的 client 由调用方管理。"""
        if self._owns_client:
            await self._client.aclose()

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        accept: str | None = None,
    ) -> Any:
        """读取单个 JSON 响应。

        Args:
            path: 相对 API 路径或 GitHub ``Link`` 返回的绝对 URL。
            params: 查询参数。
            accept: 覆盖默认 full JSON media type。

        Returns:
            GitHub 返回的原始 JSON 值。
        """
        response = await self._request("GET", path, params=params, accept=accept)
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise GitHubAPIError(f"GitHub returned invalid JSON for {response.url}") from exc

    async def get_json_cached(
        self,
        path: str,
        *,
        previous: Any | None,
        cache: dict[str, Any] | None,
        params: Mapping[str, Any] | None = None,
        accept: str | None = None,
    ) -> tuple[Any, dict[str, Any] | None]:
        """用 HTTP validator 校验并读取单个 JSON 响应。

        Args:
            path: 相对 API 路径。
            previous: 与 cache 同一次成功响应的未改写 JSON；None 强制完整读取。
            cache: 本客户端产生并与 previous 原子持久化的传输元数据。
            params: 查询参数。
            accept: 覆盖默认 full JSON media type。

        Returns:
            当前原始 JSON 及其新传输元数据。304 返回 previous；200 总是解析新响应。
        """
        return await self._get_json_cached(
            path,
            previous=previous,
            cache=cache,
            params=params,
            accept=accept,
            primary_wait=True,
        )

    async def _get_json_cached(
        self,
        path: str,
        *,
        previous: Any | None,
        cache: dict[str, Any] | None,
        params: Mapping[str, Any] | None = None,
        accept: str | None = None,
        primary_wait: bool,
    ) -> tuple[Any, dict[str, Any] | None]:
        key = self._cache_key(path, params, accept, "json")
        headers = _validator_headers(cache, key) if previous is not None else None
        response = await self._request(
            "GET",
            path,
            params=params,
            accept=accept,
            request_headers=headers,
            primary_wait=primary_wait,
        )
        if response.status_code == 304:
            if headers is None:
                raise GitHubAPIError(f"GitHub returned unsolicited 304 for {response.url}")
            updated = _response_cache(response, key, cache)
            if updated is None:
                raise GitHubAPIError(f"GitHub returned 304 without a validator for {response.url}")
            return previous, updated
        try:
            value = response.json()
        except json.JSONDecodeError as exc:
            raise GitHubAPIError(f"GitHub returned invalid JSON for {response.url}") from exc
        return value, _response_cache(response, key)

    async def get_page(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        accept: str | None = None,
    ) -> GitHubPage:
        """读取一个可独立持久化的 GitHub REST page。

        Args:
            path: 首页相对路径或 GitHub ``Link`` 返回的绝对 URL。
            params: 仅用于首页的查询参数；``per_page`` 缺省固定为 100。
            accept: 覆盖默认 JSON media type。

        Returns:
            当前页原始对象与服务端给出的不透明下一页 URL。
        """
        query = None if params is None else dict(params)
        if query is not None:
            query.setdefault("per_page", 100)
        response = await self._request("GET", path, params=query, accept=accept)
        return GitHubPage(
            items=_decode_page(response),
            next_url=response.links.get("next", {}).get("url"),
        )

    async def paginate(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        page_observer: Callable[[int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """沿 GitHub ``Link`` 响应头读取完整列表。

        Args:
            path: 首个分页 API 路径。
            params: 首页查询参数；``per_page`` 缺省固定为 GitHub 上限 100。
            page_observer: 每个已验证页面的条目数同步观察器。

        Returns:
            按服务端顺序拼接、字段不裁剪的所有条目。

        Raises:
            GitHubAPIError: 任一分页响应不是 JSON 对象数组。
        """
        items, _ = await self.paginate_cached(
            path,
            previous=None,
            cache=None,
            params=params,
            page_observer=page_observer,
        )
        return items

    async def paginate_cached(
        self,
        path: str,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
        params: Mapping[str, Any] | None = None,
        page_observer: Callable[[int], None] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """逐页校验一个已有完整集合，变化时重新完整分页。

        Args:
            path: 首个分页 API 路径。
            previous: 与 cache 配对的完整旧集合。
            cache: 本客户端产生并与 previous 配对的分页 validator 元数据。
            params: 首页查询参数；``per_page`` 固定为 100。
            page_observer: 每个已验证页面的条目数同步观察器。

        Returns:
            当前完整集合及其分页传输元数据。末页恰好为 100 条时不产生缓存，
            使后续请求必须探测可能新增的下一页。

        Raises:
            GitHubAPIError: 任一 200 响应不是 JSON 对象数组。
        """
        return await self._paginate_cached(
            path,
            previous=previous,
            cache=cache,
            params=params,
            page_observer=page_observer,
            primary_wait=True,
        )

    async def _paginate_cached(
        self,
        path: str,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
        params: Mapping[str, Any] | None = None,
        page_observer: Callable[[int], None] | None = None,
        primary_wait: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        query = dict(params or {})
        query.setdefault("per_page", 100)
        key = self._cache_key(path, query, None, "page")
        pages = _cached_pages(cache, key, previous)
        if pages is not None:
            updated_pages = []
            changed: tuple[int, httpx.Response] | None = None
            for index, page in enumerate(pages):
                headers = _validator_headers(page, str(page["key"]))
                response = await self._request(
                    "GET",
                    str(page["url"]),
                    request_headers=headers,
                    primary_wait=primary_wait,
                )
                if response.status_code != 304:
                    changed = index, response
                    break
                updated = _response_cache(response, str(page["key"]), page)
                if updated is None:
                    raise GitHubAPIError(
                        f"GitHub returned 304 without a validator for {response.url}",
                    )
                updated_pages.append(
                    updated | {"size": int(page["size"]), "url": str(page["url"])},
                )
            if changed is None:
                if page_observer is not None:
                    for page in pages:
                        page_observer(int(page["size"]))
                return previous, {
                    "key": key,
                    "pages": updated_pages,
                    "size": len(previous),
                }
            if changed[0] == 0:
                return await self._paginate_response(
                    changed[1],
                    key,
                    page_observer,
                    primary_wait=primary_wait,
                )

        response = await self._request(
            "GET",
            path,
            params=query,
            primary_wait=primary_wait,
        )
        return await self._paginate_response(
            response,
            key,
            page_observer,
            primary_wait=primary_wait,
        )

    async def _paginate_response(
        self,
        response: httpx.Response,
        key: str,
        page_observer: Callable[[int], None] | None,
        *,
        primary_wait: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        items: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        page_count = 0
        while True:
            page = _decode_page(response)
            page_count += 1
            items.extend(page)
            page_key = self._cache_key(str(response.url), None, None, "page")
            validator = _response_cache(response, page_key)
            if validator is not None:
                pages.append(
                    validator | {"size": len(page), "url": str(response.url)},
                )
            if page_observer is not None:
                page_observer(len(page))
            next_url = response.links.get("next", {}).get("url")
            if not next_url:
                break
            response = await self._request(
                "GET",
                next_url,
                primary_wait=primary_wait,
            )
        sizes = [int(page["size"]) for page in pages]
        if len(pages) == page_count and all(size == 100 for size in sizes[:-1]) and sizes[-1] < 100:
            return items, {"key": key, "pages": pages, "size": len(items)}
        return items, None

    def _cache_key(
        self,
        path: str,
        params: Mapping[str, Any] | None,
        accept: str | None,
        kind: str,
    ) -> str:
        payload = {
            "accept": accept or self._client.headers.get("Accept"),
            "api_version": self._client.headers.get("X-GitHub-Api-Version"),
            "base_url": str(self._client.base_url),
            "kind": kind,
            "params": dict(params or {}),
            "path": path,
        }
        return json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))

    async def repository_item_count(self, owner: str, repo: str) -> int | None:
        """读取仓库当前 Issue 与 PR 的精确总数。

        Args:
            owner: 仓库 owner。
            repo: 仓库名。

        Returns:
            两类对象的 GraphQL ``totalCount`` 之和；匿名客户端返回 None。该值只
            用作冷启动进度估计。
        """
        if not self._authenticated:
            return None
        payload = await self._graphql(
            REPOSITORY_ITEM_COUNT,
            {"owner": owner, "repo": repo},
        )
        try:
            repository = payload["data"]["repository"]
            issues = repository["issues"]["totalCount"]
            pulls = repository["pullRequests"]["totalCount"]
        except (KeyError, TypeError) as exc:
            raise GitHubAPIError("GitHub returned incomplete repository counts") from exc
        if not isinstance(issues, int) or not isinstance(pulls, int):
            raise GitHubAPIError("GitHub returned non-integer repository counts")
        return issues + pulls

    async def pull_request(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        previous: dict[str, Any] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource:
        """读取一个 PR 的稳定详情事实，并自动选择主配额池。

        Args:
            owner: 仓库 owner。
            repo: 仓库名。
            number: 仓库内 PR number。
            previous: 与 cache 配对的上一版稳定详情；None 表示冷读取。
            cache: 本操作先前返回的 REST validator；GraphQL 结果没有 validator。

        Returns:
            REST 兼容的稳定详情、事实来源、来源原文与可复用 validator。
        """
        if number < 1:
            raise ValueError("number must be positive")
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{number}"
        )

        async def rest(wait_primary: bool) -> GitHubResource:
            value, updated = await self._get_json_cached(
                path,
                previous=previous,
                cache=cache,
                primary_wait=wait_primary,
            )
            if not isinstance(value, dict):
                raise GitHubAPIError(f"GitHub returned a non-object for {path}")
            return GitHubResource(value, _Transport.REST, value, updated)

        async def graphql(wait_primary: bool) -> GitHubResource:
            payload = await self._graphql(
                PULL_REQUEST_DETAIL,
                {"owner": owner, "repo": repo, "number": number},
                primary_wait=wait_primary,
            )
            raw = graphql_pull(payload, number)
            return GitHubResource(
                rest_pull_detail(raw, self._base_url, owner, repo),
                _Transport.GRAPHQL,
                raw,
            )

        return await self._either(rest, graphql, rest_cached=cache is not None)

    async def pull_reviews(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource:
        """读取一个 PR 的完整 review 集合，并自动选择主配额池。

        Args:
            owner: 仓库 owner。
            repo: 仓库名。
            number: 仓库内 PR number。
            previous: 与 cache 配对的上一版稳定集合；None 表示冷读取。
            cache: 本操作先前返回的 REST 分页 validator。

        Returns:
            REST 兼容的稳定 review 集合、事实来源、来源原文与 validator。
        """
        if number < 1:
            raise ValueError("number must be positive")
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{number}/reviews"
        )

        async def rest(wait_primary: bool) -> GitHubResource:
            value, updated = await self._paginate_cached(
                path,
                previous=previous,
                cache=cache,
                primary_wait=wait_primary,
            )
            return GitHubResource(value, _Transport.REST, value, updated)

        async def graphql(wait_primary: bool) -> GitHubResource:
            raw = await self._pull_connection(
                PULL_REVIEWS,
                "reviews",
                owner,
                repo,
                number,
                primary_wait=wait_primary,
            )
            value = [
                rest_review(review, self._base_url, owner, repo, number)
                for review in raw
            ]
            return GitHubResource(value, _Transport.GRAPHQL, raw)

        return await self._either(rest, graphql, rest_cached=cache is not None)

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
    ) -> GitHubResource:
        """读取一个 PR 的完整 commit 集合，并自动选择主配额池。

        Args:
            owner: 仓库 owner。
            repo: 仓库名。
            number: 仓库内 PR number。
            expected: PR detail 声明的 commit 总数。
            base: REST comparison 回退使用的 base SHA。
            head: REST comparison 回退使用的 head SHA。
            previous: 与 cache 配对的上一版稳定集合；None 表示冷读取。
            cache: 本操作先前返回的 REST 分页 validator。

        Returns:
            REST 兼容的稳定 commit 集合、事实来源、来源原文与 validator。
        """
        if number < 1 or expected < 1:
            raise ValueError("number and expected must be positive")
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{number}/commits"
        )

        async def rest(wait_primary: bool) -> GitHubResource:
            if expected > 250:
                value = await self._compare_commits(
                    owner,
                    repo,
                    base,
                    head,
                    primary_wait=wait_primary,
                )
                check_size(number, "commits", expected, value)
                return GitHubResource(value, _Transport.REST, value)
            value, updated = await self._paginate_cached(
                path,
                previous=previous,
                cache=cache,
                primary_wait=wait_primary,
            )
            check_size(number, "commits", expected, value)
            return GitHubResource(value, _Transport.REST, value, updated)

        async def graphql(wait_primary: bool) -> GitHubResource:
            raw = await self._pull_connection(
                PULL_COMMITS,
                "commits",
                owner,
                repo,
                number,
                primary_wait=wait_primary,
            )
            value = [
                rest_commit(item, self._base_url, owner, repo, number)
                for item in raw
            ]
            check_size(number, "commits", expected, value)
            return GitHubResource(value, _Transport.GRAPHQL, raw)

        rest_cached = expected <= 250 and cache is not None
        return await self._either(rest, graphql, rest_cached=rest_cached)

    async def pull_review_comments(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource:
        """读取一个 PR 的完整 review-comment 集合，并自动选择主配额池。

        Args:
            owner: 仓库 owner。
            repo: 仓库名。
            number: 仓库内 PR number。
            previous: 与 cache 配对的上一版稳定集合；None 表示冷读取。
            cache: 本操作先前返回的 REST 分页 validator。

        Returns:
            REST 兼容的稳定 review-comment 集合、来源、来源原文与 validator。
        """
        if number < 1:
            raise ValueError("number must be positive")
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{number}/comments"
        )

        async def rest(wait_primary: bool) -> GitHubResource:
            value, updated = await self._paginate_cached(
                path,
                previous=previous,
                cache=cache,
                primary_wait=wait_primary,
            )
            return GitHubResource(value, _Transport.REST, value, updated)

        async def graphql(wait_primary: bool) -> GitHubResource:
            raw = await self._graphql_review_comments(
                owner,
                repo,
                number,
                primary_wait=wait_primary,
            )
            value = [
                rest_review_comment(comment, self._base_url, owner, repo, number)
                for comment in raw
            ]
            return GitHubResource(value, _Transport.GRAPHQL, raw)

        return await self._either(rest, graphql, rest_cached=cache is not None)

    async def issue_comments(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource:
        """读取一个 Issue/PR 的完整 conversation-comment 集合并选择配额池。

        Args:
            owner: 仓库 owner。
            repo: 仓库名。
            number: 仓库内 Issue/PR number。
            previous: 与 cache 配对的上一版稳定集合；None 表示冷读取。
            cache: 本操作先前返回的 REST 分页 validator。

        Returns:
            REST 兼容的稳定 comment 集合、来源、来源原文与 validator。
        """
        if number < 1:
            raise ValueError("number must be positive")
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/issues/{number}/comments"
        )

        async def rest(wait_primary: bool) -> GitHubResource:
            value, updated = await self._paginate_cached(
                path,
                previous=previous,
                cache=cache,
                primary_wait=wait_primary,
            )
            return GitHubResource(value, _Transport.REST, value, updated)

        async def graphql(wait_primary: bool) -> GitHubResource:
            raw = await self._parent_connection(
                ISSUE_COMMENTS,
                "comments",
                owner,
                repo,
                number,
                primary_wait=wait_primary,
            )
            value = [
                rest_issue_comment(comment, self._base_url, owner, repo, number)
                for comment in raw
            ]
            return GitHubResource(value, _Transport.GRAPHQL, raw)

        return await self._either(rest, graphql, rest_cached=cache is not None)

    async def reactions(
        self,
        path: str,
        node_id: str | None,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource:
        """读取一个 reactable 的完整 reaction 集合并选择配额池。

        Args:
            path: REST reaction collection 的相对路径。
            node_id: 同一 reactable 的 GraphQL node ID；None 使操作固定使用 REST。
            previous: 与 cache 配对的上一版稳定集合；None 表示冷读取。
            cache: 本操作先前返回的 REST 分页 validator。

        Returns:
            REST 兼容的稳定 reaction 集合、来源、来源原文与 validator。
        """

        async def rest(wait_primary: bool) -> GitHubResource:
            value, updated = await self._paginate_cached(
                path,
                previous=previous,
                cache=cache,
                primary_wait=wait_primary,
            )
            return GitHubResource(value, _Transport.REST, value, updated)

        if not self._authenticated or not node_id:
            return await rest(True)

        async def graphql(wait_primary: bool) -> GitHubResource:
            raw = await self._node_items(
                REACTIONS,
                "reactions",
                node_id,
                primary_wait=wait_primary,
            )
            return GitHubResource(
                [rest_reaction(reaction) for reaction in raw],
                _Transport.GRAPHQL,
                raw,
            )

        return await self._either(rest, graphql, rest_cached=cache is not None)

    async def compare_commits(
        self,
        owner: str,
        repo: str,
        base: str,
        head: str,
    ) -> list[dict[str, Any]]:
        """通过可分页 comparison 读取两个 commit 之间的完整列表。

        Args:
            owner: 仓库 owner。
            repo: 仓库名。
            base: comparison 的 base commit SHA。
            head: comparison 的 head commit SHA。

        Returns:
            按 GitHub comparison 顺序排列、字段不裁剪的 commit 对象。

        Raises:
            GitHubAPIError: 分页的总数、cursor 或 commit identity 不一致。
        """
        return await self._compare_commits(
            owner,
            repo,
            base,
            head,
            primary_wait=True,
        )

    async def _compare_commits(
        self,
        owner: str,
        repo: str,
        base: str,
        head: str,
        *,
        primary_wait: bool,
    ) -> list[dict[str, Any]]:
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/compare/"
            f"{quote(base, safe='')}...{quote(head, safe='')}"
        )
        params: Mapping[str, Any] | None = {"per_page": 100}
        commits: list[dict[str, Any]] = []
        seen: set[str] = set()
        visited: set[str] = set()
        expected: int | None = None
        while True:
            response = await self._request(
                "GET",
                path,
                params=params,
                primary_wait=primary_wait,
            )
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise GitHubAPIError(
                    f"GitHub returned invalid comparison JSON for {response.url}",
                ) from exc
            if not isinstance(payload, dict):
                raise GitHubAPIError(f"GitHub returned a non-object comparison for {response.url}")
            page = payload.get("commits")
            total = payload.get("total_commits")
            if (
                not isinstance(page, list)
                or any(not isinstance(item, dict) for item in page)
                or type(total) is not int
                or total < 0
            ):
                raise GitHubAPIError(f"GitHub returned incomplete comparison data for {response.url}")
            if expected is None:
                expected = total
            elif expected != total:
                raise GitHubAPIError("GitHub comparison total changed while paging")
            for commit in page:
                sha = commit.get("sha")
                if not isinstance(sha, str) or not sha or sha in seen:
                    raise GitHubAPIError("GitHub comparison returned invalid commit identities")
                seen.add(sha)
                commits.append(commit)
            next_url = response.links.get("next", {}).get("url")
            if not next_url:
                if len(commits) != expected:
                    raise GitHubAPIError(
                        f"GitHub comparison advertised {expected} commits, got {len(commits)}",
                    )
                return commits
            current_url = str(response.url)
            if next_url == current_url or next_url in visited:
                raise GitHubAPIError("GitHub comparison repeated a pagination cursor")
            visited.add(current_url)
            path = next_url
            params = None

    async def closing_issue_references(
        self,
        owner: str,
        repo: str,
        numbers: Sequence[int],
    ) -> dict[int, list[dict[str, Any]]]:
        """读取 GitHub 标记为可能由一批 PR 关闭的 Issue。

        Args:
            owner: PR 所属仓库 owner。
            repo: PR 所属仓库名。
            numbers: 至多 100 个不同的正整数 PR number。

        Returns:
            每个输入 PR 对应的完整 GraphQL ``closingIssuesReferences`` 节点列表。

        Raises:
            GitHubAPIError: 客户端未认证，或 GraphQL 返回不完整或不一致的数据。
            ValueError: numbers 违反批量调用约束。
        """
        batch = list(numbers)
        if not batch:
            return {}
        if (
            len(batch) > _GRAPHQL_PAGE_SIZE
            or len(set(batch)) != len(batch)
            or any(type(number) is not int or number < 1 for number in batch)
        ):
            raise ValueError("numbers must contain at most 100 unique positive integers")
        if not self._authenticated:
            raise GitHubAPIError("closing issue references require GitHub authentication")

        results = {number: [] for number in batch}
        totals: dict[int, int] = {}
        cursors: dict[int, str | None] = dict.fromkeys(batch)
        seen_cursors: dict[int, set[str]] = {number: set() for number in batch}
        seen_nodes: dict[int, set[str]] = {number: set() for number in batch}
        while cursors:
            active = list(cursors)
            variables: dict[str, Any] = {"owner": owner, "repo": repo}
            for index, number in enumerate(active):
                variables[f"number{index}"] = number
                variables[f"cursor{index}"] = cursors[number]
            payload = await self._graphql(_closing_issues_query(len(active)), variables)
            try:
                repository = payload["data"]["repository"]
            except (KeyError, TypeError) as exc:
                raise GitHubAPIError("GitHub returned incomplete closing issue data") from exc
            if not isinstance(repository, dict):
                raise GitHubAPIError("GitHub returned no repository for closing issue data")

            next_cursors: dict[int, str | None] = {}
            for index, number in enumerate(active):
                connection = _closing_issues_connection(repository, index, number)
                nodes = connection["nodes"]
                total = connection["totalCount"]
                page_info = connection["pageInfo"]
                previous_total = totals.setdefault(number, total)
                if previous_total != total:
                    raise GitHubAPIError(f"pull #{number} closing issue count changed while paging")
                for node in nodes:
                    node_id = _closing_issue_id(node, number)
                    if node_id in seen_nodes[number]:
                        raise GitHubAPIError(f"pull #{number} has duplicate closing issue {node_id}")
                    seen_nodes[number].add(node_id)
                    results[number].append(node)
                if len(results[number]) > total:
                    raise GitHubAPIError(f"pull #{number} returned too many closing issues")
                if page_info["hasNextPage"]:
                    cursor = page_info["endCursor"]
                    if not isinstance(cursor, str) or not cursor or cursor in seen_cursors[number]:
                        raise GitHubAPIError(f"pull #{number} has an invalid closing issue cursor")
                    seen_cursors[number].add(cursor)
                    next_cursors[number] = cursor
                elif len(results[number]) != total:
                    raise GitHubAPIError(
                        f"pull #{number} advertised {total} closing issues, got {len(results[number])}",
                    )
            cursors = next_cursors
        return results

    async def _graphql(
        self,
        query: str,
        variables: Mapping[str, Any],
        *,
        primary_wait: bool = True,
    ) -> dict[str, Any]:
        secondary_attempt = 0
        while True:
            response = await self._request(
                "POST",
                self._graphql_url,
                json_body={"query": query, "variables": variables},
                resource="graphql",
                primary_wait=primary_wait,
            )
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise GitHubAPIError(
                    f"GitHub returned invalid GraphQL JSON for {response.url}",
                ) from exc
            if not isinstance(payload, dict):
                raise GitHubAPIError(f"GitHub returned invalid GraphQL data for {response.url}")
            errors = payload.get("errors")
            if not errors:
                return payload
            if self._is_rate_limited(response):
                if (
                    not primary_wait
                    and response.headers.get("x-ratelimit-remaining") == "0"
                ):
                    raise _PrimaryRateLimitError("graphql")
                await self._wait_after_rate_limit(response, secondary_attempt, "graphql")
                secondary_attempt += 1
                continue
            raise GitHubAPIError(f"GitHub GraphQL error for {response.url}: {errors!r}")

    async def _pull_connection(
        self,
        query: str,
        field: str,
        owner: str,
        repo: str,
        number: int,
        *,
        primary_wait: bool,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        expected: int | None = None
        while True:
            payload = await self._graphql(
                query,
                {"owner": owner, "repo": repo, "number": number, "cursor": cursor},
                primary_wait=primary_wait,
            )
            pull = graphql_pull(payload, number)
            connection = graphql_connection(pull, field, number)
            total = connection["totalCount"]
            if expected is None:
                expected = total
            elif expected != total:
                raise GitHubAPIError(
                    f"pull #{number} {field} count changed while paging",
                )
            for node in connection["nodes"]:
                node_id = node.get("id")
                if not isinstance(node_id, str) or not node_id or node_id in seen:
                    raise GitHubAPIError(
                        f"pull #{number} has invalid {field} identities",
                    )
                seen.add(node_id)
                items.append(node)
            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"]:
                if len(items) != expected:
                    raise GitHubAPIError(
                        f"pull #{number} advertised {expected} {field}, got {len(items)}",
                    )
                return items
            next_cursor = page_info["endCursor"]
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise GitHubAPIError(f"pull #{number} has an invalid {field} cursor")
            cursor = next_cursor

    async def _parent_connection(
        self,
        query: str,
        field: str,
        owner: str,
        repo: str,
        number: int,
        *,
        primary_wait: bool,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        expected: int | None = None
        while True:
            payload = await self._graphql(
                query,
                {"owner": owner, "repo": repo, "number": number, "cursor": cursor},
                primary_wait=primary_wait,
            )
            parent = graphql_parent(payload, number)
            connection = node_connection(parent, field, f"parent #{number}")
            total = connection["totalCount"]
            if expected is None:
                expected = total
            elif expected != total:
                raise GitHubAPIError(f"parent #{number} {field} count changed while paging")
            for node in connection["nodes"]:
                node_id = node.get("id")
                if not isinstance(node_id, str) or not node_id or node_id in seen:
                    raise GitHubAPIError(f"parent #{number} has invalid {field} identities")
                seen.add(node_id)
                items.append(node)
            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"]:
                if len(items) != expected:
                    raise GitHubAPIError(
                        f"parent #{number} advertised {expected} {field}, got {len(items)}",
                    )
                return items
            next_cursor = page_info["endCursor"]
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise GitHubAPIError(f"parent #{number} has an invalid {field} cursor")
            cursor = next_cursor

    async def _graphql_review_comments(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        primary_wait: bool,
    ) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        seen_comments: set[str] = set()
        seen_threads: set[str] = set()
        cursor: str | None = None
        expected_threads: int | None = None
        while True:
            payload = await self._graphql(
                PULL_REVIEW_COMMENTS,
                {"owner": owner, "repo": repo, "number": number, "cursor": cursor},
                primary_wait=primary_wait,
            )
            pull = graphql_pull(payload, number)
            connection = graphql_connection(pull, "reviewThreads", number)
            total = connection["totalCount"]
            if expected_threads is None:
                expected_threads = total
            elif expected_threads != total:
                raise GitHubAPIError(
                    f"pull #{number} reviewThreads count changed while paging",
                )
            for thread in connection["nodes"]:
                thread_id = thread.get("id")
                if (
                    not isinstance(thread_id, str)
                    or not thread_id
                    or thread_id in seen_threads
                ):
                    raise GitHubAPIError(
                        f"pull #{number} has invalid reviewThreads identities",
                    )
                seen_threads.add(thread_id)
                page = node_connection(thread, "comments", f"review thread {thread_id}")
                thread_comments = await self._finish_node_connection(
                    thread_id,
                    page,
                    primary_wait=primary_wait,
                )
                for comment in thread_comments:
                    comment_id = comment.get("id")
                    if (
                        not isinstance(comment_id, str)
                        or not comment_id
                        or comment_id in seen_comments
                    ):
                        raise GitHubAPIError(
                            f"pull #{number} has invalid review-comment identities",
                        )
                    seen_comments.add(comment_id)
                    comments.append(comment)
            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"]:
                if len(seen_threads) != expected_threads:
                    raise GitHubAPIError(
                        f"pull #{number} advertised {expected_threads} reviewThreads, "
                        f"got {len(seen_threads)}",
                    )
                return comments
            next_cursor = page_info["endCursor"]
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise GitHubAPIError(f"pull #{number} has an invalid reviewThreads cursor")
            cursor = next_cursor

    async def _finish_node_connection(
        self,
        node_id: str,
        first: dict[str, Any],
        *,
        primary_wait: bool,
    ) -> list[dict[str, Any]]:
        expected = first["totalCount"]
        items = list(first["nodes"])
        seen: set[str] = set()
        for item in items:
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id or item_id in seen:
                raise GitHubAPIError(f"node {node_id} has invalid comment identities")
            seen.add(item_id)
        page_info = first["pageInfo"]
        cursor = page_info["endCursor"]
        while page_info["hasNextPage"]:
            if not isinstance(cursor, str) or not cursor:
                raise GitHubAPIError(f"node {node_id} has an invalid comments cursor")
            payload = await self._graphql(
                REVIEW_THREAD_COMMENTS,
                {"id": node_id, "cursor": cursor},
                primary_wait=primary_wait,
            )
            node = payload.get("data", {}).get("node")
            if not isinstance(node, dict) or node.get("id") != node_id:
                raise GitHubAPIError(f"GitHub returned no matching review thread {node_id}")
            page = node_connection(node, "comments", f"review thread {node_id}")
            if page["totalCount"] != expected:
                raise GitHubAPIError(
                    f"review thread {node_id} comment count changed while paging",
                )
            for item in page["nodes"]:
                item_id = item.get("id")
                if not isinstance(item_id, str) or not item_id or item_id in seen:
                    raise GitHubAPIError(
                        f"review thread {node_id} has invalid comment identities",
                    )
                seen.add(item_id)
                items.append(item)
            next_cursor = page["pageInfo"]["endCursor"]
            if page["pageInfo"]["hasNextPage"] and next_cursor == cursor:
                raise GitHubAPIError(f"node {node_id} repeated its comments cursor")
            page_info = page["pageInfo"]
            cursor = next_cursor
        if len(items) != expected:
            raise GitHubAPIError(
                f"review thread {node_id} advertised {expected} comments, got {len(items)}",
            )
        return items

    async def _node_items(
        self,
        query: str,
        field: str,
        node_id: str,
        *,
        primary_wait: bool,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        expected: int | None = None
        while True:
            payload = await self._graphql(
                query,
                {"id": node_id, "cursor": cursor},
                primary_wait=primary_wait,
            )
            node = payload.get("data", {}).get("node")
            if not isinstance(node, dict) or node.get("id") != node_id:
                raise GitHubAPIError(f"GitHub returned no matching node {node_id}")
            connection = node_connection(node, field, f"node {node_id}")
            total = connection["totalCount"]
            if expected is None:
                expected = total
            elif expected != total:
                raise GitHubAPIError(f"node {node_id} {field} count changed while paging")
            for item in connection["nodes"]:
                item_id = item.get("id")
                if not isinstance(item_id, str) or not item_id or item_id in seen:
                    raise GitHubAPIError(f"node {node_id} has invalid {field} identities")
                seen.add(item_id)
                items.append(item)
            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"]:
                if len(items) != expected:
                    raise GitHubAPIError(
                        f"node {node_id} advertised {expected} {field}, got {len(items)}",
                    )
                return items
            next_cursor = page_info["endCursor"]
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise GitHubAPIError(f"node {node_id} has an invalid {field} cursor")
            cursor = next_cursor

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        accept: str | None = None,
        json_body: Mapping[str, Any] | None = None,
        request_headers: Mapping[str, str] | None = None,
        resource: str = "core",
        primary_wait: bool = True,
    ) -> httpx.Response:
        transient_attempt = 0
        secondary_attempt = 0
        while True:
            await self._wait_for_limit(resource, primary_wait=primary_wait)
            headers = dict(request_headers or {})
            if accept:
                headers["Accept"] = accept
            try:
                self.request_count += 1
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    headers=headers or None,
                    json=json_body,
                )
            except httpx.RequestError:
                transient_attempt += 1
                await self._wait_transient(
                    transient_attempt,
                    f"GitHub request failed: {path}",
                )
                continue

            self._remember_quota(response)
            self._emit_progress()
            if response.status_code in {403, 429} and self._is_rate_limited(response):
                self._remember_primary_limit(response, resource)
                if (
                    not primary_wait
                    and response.headers.get("x-ratelimit-remaining") == "0"
                ):
                    raise _PrimaryRateLimitError(resource)
                await self._wait_after_rate_limit(response, secondary_attempt, resource)
                secondary_attempt += 1
                continue
            self._remember_primary_limit(response, resource)
            if response.status_code >= 500:
                transient_attempt += 1
                await self._wait_transient(
                    transient_attempt,
                    f"GitHub returned {response.status_code} for {response.url}",
                )
                continue
            if response.is_error:
                detail = response.text[:500]
                raise GitHubAPIError(
                    f"GitHub returned {response.status_code} for {response.url}: {detail}",
                    status_code=response.status_code,
                    url=str(response.url),
                )
            return response

    async def _wait_transient(self, attempt: int, failure: str) -> None:
        wait = _TRANSIENT_DELAYS[min(attempt - 1, len(_TRANSIENT_DELAYS) - 1)]
        _LOG.warning("%s; retrying in %.1fs", failure, wait)
        self._emit_progress(wait_seconds=wait, detail="transient_retry")
        await self._sleep(wait)

    async def _wait_for_limit(self, resource: str, *, primary_wait: bool) -> None:
        async with self._gate_lock:
            primary_until = self._primary_blocked_until.get(resource, 0.0)
            secondary_until = self._secondary_blocked_until
        blocked_until = max(primary_until, secondary_until)
        wait = blocked_until - self._now().timestamp()
        if wait <= 0:
            return
        primary = primary_until >= secondary_until
        if primary and not primary_wait:
            raise _PrimaryRateLimitError(resource)
        detail = "primary_rate_limit" if primary else "secondary_rate_limit"
        _LOG.warning("GitHub %s blocked; waiting %.1fs", resource, wait)
        self._emit_progress(wait_seconds=wait, detail=detail)
        await self._sleep(wait)
        async with self._gate_lock:
            if self._primary_blocked_until.get(resource) == primary_until:
                self._primary_blocked_until.pop(resource, None)
            if self._secondary_blocked_until == secondary_until:
                self._secondary_blocked_until = 0.0

    async def _either(
        self,
        rest: Callable[[bool], Awaitable[GitHubResource]],
        graphql: Callable[[bool], Awaitable[GitHubResource]],
        *,
        rest_cached: bool,
    ) -> GitHubResource:
        operations = {
            _Transport.REST: rest,
            _Transport.GRAPHQL: graphql,
        }
        order = self._transport_order(rest_cached)
        limited: set[_Transport] = set()
        failures: list[GitHubAPIError] = []
        for transport in order:
            try:
                return await operations[transport](False)
            except _PrimaryRateLimitError:
                limited.add(transport)
            except GitHubAPIError as exc:
                failures.append(exc)
        if limited:
            transport = next(
                transport
                for transport in self._transport_order(rest_cached)
                if transport in limited
            )
            return await operations[transport](True)
        raise failures[0]

    def _transport_order(self, rest_cached: bool) -> tuple[_Transport, _Transport]:
        if not self._authenticated:
            return _Transport.REST, _Transport.GRAPHQL
        # NOTE: Do not prioritize an earlier reset in isolation. Flexible calls share
        # each quota with transport-only calls whose future demand is unknown here.
        rest_capacity = self._quota_capacity(_Transport.REST)
        if rest_cached and rest_capacity > 0:
            return _Transport.REST, _Transport.GRAPHQL
        graphql_capacity = self._quota_capacity(_Transport.GRAPHQL)
        if rest_capacity > graphql_capacity:
            return _Transport.REST, _Transport.GRAPHQL
        if rest_capacity == graphql_capacity == 0 and self._quota_wait(
            _Transport.REST,
        ) < self._quota_wait(_Transport.GRAPHQL):
            return _Transport.REST, _Transport.GRAPHQL
        return _Transport.GRAPHQL, _Transport.REST

    def _quota_capacity(self, transport: _Transport) -> float:
        resource = "core" if transport is _Transport.REST else "graphql"
        quota = self._quotas.get(resource)
        if quota is None or quota.remaining is None or quota.limit in {None, 0}:
            return 1.0
        if quota.reset_at is not None and quota.reset_at <= self._now():
            return 1.0
        return quota.remaining / quota.limit

    def _quota_wait(self, transport: _Transport) -> float:
        resource = "core" if transport is _Transport.REST else "graphql"
        quota = self._quotas.get(resource)
        if quota is None or quota.reset_at is None:
            return 0.0
        return max((quota.reset_at - self._now()).total_seconds(), 0.0)

    async def _wait_after_rate_limit(
        self,
        response: httpx.Response,
        attempt: int,
        resource: str,
    ) -> None:
        wait, wait_source = self._rate_limit_wait(response, attempt, resource)
        blocked_until = self._now().timestamp() + wait
        primary = response.headers.get("x-ratelimit-remaining") == "0"
        bucket = response.headers.get("x-ratelimit-resource", resource)
        async with self._gate_lock:
            if primary:
                self._primary_blocked_until[bucket] = max(
                    self._primary_blocked_until.get(bucket, 0.0),
                    blocked_until,
                )
            else:
                self._secondary_blocked_until = max(
                    self._secondary_blocked_until,
                    blocked_until,
                )
        detail = "primary_rate_limit" if primary else "secondary_rate_limit"
        effective_reset = self._primary_reset(response, resource) if primary else None
        _LOG.warning(
            "GitHub %s: resource=%s status=%d remaining=%s reset=%s "
            "effective_reset=%s retry_after=%s wait_source=%s wait=%.1fs url=%s",
            detail,
            bucket,
            response.status_code,
            response.headers.get("x-ratelimit-remaining", "-"),
            response.headers.get("x-ratelimit-reset", "-"),
            "-" if effective_reset is None else f"{effective_reset:.0f}",
            response.headers.get("retry-after", "-"),
            wait_source,
            wait,
            response.url,
        )
        self._emit_progress(wait_seconds=wait, detail=detail)
        await self._sleep(wait)
        async with self._gate_lock:
            if primary:
                if self._primary_blocked_until.get(bucket) == blocked_until:
                    self._primary_blocked_until.pop(bucket, None)
            elif self._secondary_blocked_until == blocked_until:
                self._secondary_blocked_until = 0.0

    def _remember_primary_limit(self, response: httpx.Response, resource: str) -> None:
        if response.headers.get("x-ratelimit-remaining") != "0":
            return
        reset = self._primary_reset(response, resource)
        if reset is None:
            return
        bucket = response.headers.get("x-ratelimit-resource", resource)
        self._primary_blocked_until[bucket] = max(
            self._primary_blocked_until.get(bucket, 0.0),
            reset + 1,
        )

    def _remember_quota(self, response: httpx.Response) -> None:
        limit = _integer_header(response, "x-ratelimit-limit")
        remaining = _integer_header(response, "x-ratelimit-remaining")
        reset = _integer_header(response, "x-ratelimit-reset")
        reset_at = None if reset is None else datetime.fromtimestamp(reset, UTC)
        resource = response.headers.get("x-ratelimit-resource")
        if resource is None:
            return
        previous = self._quotas.get(resource)
        if previous is None:
            self._quotas[resource] = RateQuota(resource, limit, remaining, reset_at)
            return
        if reset_at is not None and previous.reset_at is not None:
            reset_delta = (reset_at - previous.reset_at).total_seconds()
            if reset_delta < -_QUOTA_RESET_JITTER_SECONDS:
                return
            if reset_delta > _QUOTA_RESET_JITTER_SECONDS:
                self._quotas[resource] = RateQuota(resource, limit, remaining, reset_at)
                return
            reset_at = max(reset_at, previous.reset_at)
        self._quotas[resource] = RateQuota(
            resource,
            limit if limit is not None else previous.limit,
            (
                previous.remaining
                if remaining is None
                else remaining
                if previous.remaining is None
                else min(previous.remaining, remaining)
            ),
            reset_at if reset_at is not None else previous.reset_at,
        )

    def _primary_reset(self, response: httpx.Response, resource: str) -> float | None:
        bucket = response.headers.get("x-ratelimit-resource", resource)
        quota = self._quotas.get(bucket)
        if quota is not None and quota.remaining == 0 and quota.reset_at is not None:
            return quota.reset_at.timestamp()
        try:
            return float(response.headers["x-ratelimit-reset"])
        except (KeyError, ValueError):
            return None

    def _emit_progress(
        self,
        *,
        wait_seconds: float | None = None,
        detail: str | None = None,
    ) -> None:
        if self._progress is None:
            return
        event = APIProgress(
            request_count=self.request_count,
            quotas=tuple(sorted(self._quotas.values(), key=lambda quota: quota.resource)),
            wait_seconds=wait_seconds,
            detail=detail,
        )
        try:
            self._progress(event)
        except Exception:
            self._progress = None

    def _rate_limit_wait(
        self,
        response: httpx.Response,
        attempt: int,
        resource: str,
    ) -> tuple[float, str]:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return max(float(retry_after), 1.0), "retry_after"
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after).astimezone(UTC)
                except (TypeError, ValueError):
                    pass
                else:
                    return max((retry_at - self._now()).total_seconds(), 1.0), "retry_after"
        reset = self._primary_reset(response, resource)
        if response.headers.get("x-ratelimit-remaining") == "0" and reset is not None:
            return max(reset - self._now().timestamp() + 1, 1.0), "reset"
        return min(60 * 2**attempt, 900), "backoff"

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429 or response.headers.get("x-ratelimit-remaining") == "0":
            return True
        if "retry-after" in response.headers:
            return True
        return "rate limit" in response.text.lower() or "abuse detection" in response.text.lower()


def _decode_page(response: httpx.Response) -> list[dict[str, Any]]:
    try:
        page = response.json()
    except json.JSONDecodeError as exc:
        raise GitHubAPIError(f"GitHub returned invalid JSON for {response.url}") from exc
    if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
        raise GitHubAPIError(f"GitHub returned a non-object page for {response.url}")
    return page


def _closing_issues_query(size: int) -> str:
    variables = ["$owner: String!", "$repo: String!"]
    pulls = []
    for index in range(size):
        variables.extend((f"$number{index}: Int!", f"$cursor{index}: String"))
        pulls.append(
            f"""p{index}: pullRequest(number: $number{index}) {{
      number
      closingIssuesReferences(
        first: {_GRAPHQL_PAGE_SIZE}
        after: $cursor{index}
        excludeUserLinked: false
      ) {{
        totalCount
        nodes {{
          id
          number
          title
          state
          url
          repository {{ id nameWithOwner url }}
        }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}""",
        )
    selections = "\n    ".join(pulls)
    return f"""query PullClosingIssues({', '.join(variables)}) {{
  repository(owner: $owner, name: $repo) {{
    {selections}
  }}
}}"""


def _closing_issues_connection(
    repository: dict[str, Any],
    index: int,
    number: int,
) -> dict[str, Any]:
    pull = repository.get(f"p{index}")
    if not isinstance(pull, dict) or pull.get("number") != number:
        raise GitHubAPIError(f"GitHub returned no matching pull request #{number}")
    connection = pull.get("closingIssuesReferences")
    if not isinstance(connection, dict):
        raise GitHubAPIError(f"pull #{number} has no closing issue connection")
    nodes = connection.get("nodes")
    total = connection.get("totalCount")
    page_info = connection.get("pageInfo")
    if (
        not isinstance(nodes, list)
        or any(not isinstance(node, dict) for node in nodes)
        or type(total) is not int
        or total < 0
        or not isinstance(page_info, dict)
        or type(page_info.get("hasNextPage")) is not bool
    ):
        raise GitHubAPIError(f"pull #{number} has invalid closing issue pagination")
    return connection


def _closing_issue_id(node: dict[str, Any], pull_number: int) -> str:
    repository = node.get("repository")
    values = (node.get("id"), node.get("title"), node.get("state"), node.get("url"))
    repository_values = (
        repository.get("id") if isinstance(repository, dict) else None,
        repository.get("nameWithOwner") if isinstance(repository, dict) else None,
        repository.get("url") if isinstance(repository, dict) else None,
    )
    if (
        any(not isinstance(value, str) or not value for value in (*values, *repository_values))
        or type(node.get("number")) is not int
        or node["number"] < 1
    ):
        raise GitHubAPIError(f"pull #{pull_number} has an invalid closing issue node")
    return str(node["id"])




def _validator_headers(cache: dict[str, Any] | None, key: str) -> dict[str, str] | None:
    if cache is None or cache.get("key") != key:
        return None
    etag = cache.get("etag")
    return {"If-None-Match": etag} if isinstance(etag, str) else None


def _cached_pages(
    cache: dict[str, Any] | None,
    key: str,
    previous: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    if previous is None or cache is None or cache.get("key") != key:
        return None
    pages = cache.get("pages")
    if not isinstance(pages, list) or not pages or cache.get("size") != len(previous):
        return None
    total = 0
    urls: set[str] = set()
    for index, page in enumerate(pages):
        if not isinstance(page, dict):
            return None
        page_key = page.get("key")
        url = page.get("url")
        size = page.get("size")
        if (
            not isinstance(page_key, str)
            or not isinstance(url, str)
            or not url
            or url in urls
            or type(size) is not int
            or size < 0
            or size > 100
            or (index < len(pages) - 1 and size != 100)
            or _validator_headers(page, page_key) is None
        ):
            return None
        urls.add(url)
        total += size
    if total != len(previous) or pages[-1]["size"] == 100:
        return None
    return pages


def _response_cache(
    response: httpx.Response,
    key: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    etag = response.headers.get("etag")
    if previous is not None:
        etag = etag or previous.get("etag")
    if not isinstance(etag, str):
        return None
    return {"key": key, "etag": etag}


def _graphql_endpoint(base_url: str) -> str:
    root = base_url.rstrip("/")
    if root.endswith("/api/v3"):
        return f"{root.removesuffix('/api/v3')}/api/graphql"
    return f"{root}/graphql"


def _integer_header(response: httpx.Response, name: str) -> int | None:
    try:
        return int(response.headers[name])
    except (KeyError, ValueError):
        return None
