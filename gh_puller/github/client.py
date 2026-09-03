"""提供遵守 GitHub 限流恢复契约的异步 API 读取层。

本模块只负责 HTTP、条件校验、分页、仓库对象精确计数、PR 关闭关系与重试，不
解释 Issue/PR 数据，也不写归档。观测水位与持久化契约见 ``gh_puller.github``。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

import httpx

from .progress import APIProgress, RateQuota

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from .progress import APIProgressObserver

_LOG = logging.getLogger(__name__)
_DEFAULT_ACCEPT = "application/vnd.github.full+json"
_GRAPHQL_PAGE_SIZE = 100
_REPOSITORY_ITEM_COUNT = """
query RepositoryItemCount($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    issues(states: [OPEN, CLOSED]) { totalCount }
    pullRequests(states: [OPEN, CLOSED, MERGED]) { totalCount }
  }
}
"""


class GitHubAPIError(RuntimeError):
    """GitHub 返回不可恢复响应或瞬时故障超过重试预算。"""


class GitHubAPI:
    """异步 GitHub REST 与 GraphQL 客户端。

    Args:
        token: GitHub token；空值只允许访问公开资源并使用匿名限额。
        base_url: REST API 根地址，支持 GitHub Enterprise。
        graphql_url: GraphQL API 地址；None 从 REST 根地址推导。
        api_version: 发送到 ``X-GitHub-Api-Version`` 的版本。
        timeout: 单次请求超时秒数。
        transient_retries: 网络错误和 5xx 的重试次数；限流不消耗此预算。
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
        transient_retries: int = 5,
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
        self._authenticated = bool(token or self._client.headers.get("Authorization"))
        self._graphql_url = graphql_url or _graphql_endpoint(base_url)
        self._transient_retries = transient_retries
        self._sleep = sleep
        self._now = now
        self._progress = progress
        self._blocked_until = 0.0
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
        key = self._cache_key(path, params, accept, "json")
        headers = _validator_headers(cache, key) if previous is not None else None
        response = await self._request(
            "GET",
            path,
            params=params,
            accept=accept,
            request_headers=headers,
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

    async def get_text(self, path: str, *, accept: str) -> str:
        """读取自定义 media type 的文本响应。

        Args:
            path: 相对 API 路径。
            accept: 请求的 GitHub media type。

        Returns:
            未改写的响应文本。
        """
        return (await self._request("GET", path, accept=accept)).text

    async def get_text_cached(
        self,
        path: str,
        *,
        accept: str,
        previous: str | None,
        cache: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any] | None]:
        """用 ETag 校验并读取一个自定义 media type 响应。

        Args:
            path: 相对 API 路径。
            accept: 请求的 GitHub media type，也是 validator identity 的一部分。
            previous: 与 cache 配对的完整旧文本；None 强制完整读取。
            cache: 本客户端产生并与 previous 原子持久化的传输元数据。

        Returns:
            当前未改写文本及其传输元数据；304 精确复用 previous。
        """
        key = self._cache_key(path, None, accept, "text")
        headers = _validator_headers(cache, key) if previous is not None else None
        response = await self._request(
            "GET",
            path,
            accept=accept,
            request_headers=headers,
        )
        if response.status_code == 304:
            if headers is None:
                raise GitHubAPIError(f"GitHub returned unsolicited 304 for {response.url}")
            updated = _response_cache(response, key, cache)
            if updated is None:
                raise GitHubAPIError(f"GitHub returned 304 without a validator for {response.url}")
            return previous, updated
        return response.text, _response_cache(response, key)

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
                return await self._paginate_response(changed[1], key, page_observer)

        response = await self._request(
            "GET",
            path,
            params=query,
        )
        return await self._paginate_response(response, key, page_observer)

    async def _paginate_response(
        self,
        response: httpx.Response,
        key: str,
        page_observer: Callable[[int], None] | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        items: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        page_count = 0
        while True:
            try:
                page = response.json()
            except json.JSONDecodeError as exc:
                raise GitHubAPIError(f"GitHub returned invalid JSON for {response.url}") from exc
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise GitHubAPIError(f"GitHub returned a non-object page for {response.url}")
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
            response = await self._request("GET", next_url)
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
            两类对象的 GraphQL ``totalCount`` 之和；匿名客户端返回 None，使调用方
            使用无需认证的全目录证明。
        """
        if not self._authenticated:
            return None
        payload = await self._graphql(
            _REPOSITORY_ITEM_COUNT,
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
    ) -> dict[str, Any]:
        secondary_attempt = 0
        while True:
            response = await self._request(
                "POST",
                self._graphql_url,
                json_body={"query": query, "variables": variables},
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
                await self._wait_after_rate_limit(response, secondary_attempt)
                secondary_attempt += 1
                continue
            raise GitHubAPIError(f"GitHub GraphQL error for {response.url}: {errors!r}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        accept: str | None = None,
        json_body: Mapping[str, Any] | None = None,
        request_headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        transient_attempt = 0
        secondary_attempt = 0
        while True:
            await self._wait_for_primary_limit()
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
            except httpx.RequestError as exc:
                if transient_attempt >= self._transient_retries:
                    raise GitHubAPIError(f"GitHub request failed: {path}") from exc
                transient_attempt += 1
                wait = min(2 ** (transient_attempt - 1), 30)
                self._emit_progress(wait_seconds=wait, detail="transient_retry")
                await self._sleep(wait)
                continue

            self._remember_quota(response)
            self._emit_progress()
            if response.status_code in {403, 429} and self._is_rate_limited(response):
                await self._wait_after_rate_limit(response, secondary_attempt)
                secondary_attempt += 1
                continue
            self._remember_primary_limit(response)
            if response.status_code >= 500:
                if transient_attempt >= self._transient_retries:
                    raise GitHubAPIError(
                        f"GitHub returned {response.status_code} for {response.url}",
                    )
                transient_attempt += 1
                wait = min(2 ** (transient_attempt - 1), 30)
                self._emit_progress(wait_seconds=wait, detail="transient_retry")
                await self._sleep(wait)
                continue
            if response.is_error:
                detail = response.text[:500]
                raise GitHubAPIError(
                    f"GitHub returned {response.status_code} for {response.url}: {detail}",
                )
            return response

    async def _wait_for_primary_limit(self) -> None:
        async with self._gate_lock:
            blocked_until = self._blocked_until
        wait = blocked_until - self._now().timestamp()
        if wait <= 0:
            return
        _LOG.warning("GitHub primary quota exhausted; waiting %.1fs", wait)
        self._emit_progress(wait_seconds=wait, detail="primary_rate_limit")
        await self._sleep(wait)
        async with self._gate_lock:
            if self._blocked_until == blocked_until:
                self._blocked_until = 0.0

    async def _wait_after_rate_limit(self, response: httpx.Response, attempt: int) -> None:
        wait = self._rate_limit_wait(response, attempt)
        blocked_until = self._now().timestamp() + wait
        async with self._gate_lock:
            self._blocked_until = max(self._blocked_until, blocked_until)
        _LOG.warning("GitHub rate limited %s; retrying in %.1fs", response.url, wait)
        self._emit_progress(wait_seconds=wait, detail="secondary_rate_limit")
        await self._sleep(wait)
        async with self._gate_lock:
            if self._blocked_until == blocked_until:
                self._blocked_until = 0.0

    def _remember_primary_limit(self, response: httpx.Response) -> None:
        if response.headers.get("x-ratelimit-remaining") != "0":
            return
        try:
            reset = float(response.headers["x-ratelimit-reset"])
        except (KeyError, ValueError):
            return
        self._blocked_until = max(self._blocked_until, reset + 1)

    def _remember_quota(self, response: httpx.Response) -> None:
        limit = _integer_header(response, "x-ratelimit-limit")
        remaining = _integer_header(response, "x-ratelimit-remaining")
        reset = _integer_header(response, "x-ratelimit-reset")
        reset_at = None if reset is None else datetime.fromtimestamp(reset, UTC)
        resource = response.headers.get("x-ratelimit-resource")
        if resource is None:
            return
        previous = self._quotas.get(resource)
        if (
            previous is not None
            and reset_at is not None
            and previous.reset_at is not None
            and reset_at < previous.reset_at
        ):
            return
        new_window = previous is None or (
            reset_at is not None and (previous.reset_at is None or reset_at > previous.reset_at)
        )
        if new_window:
            self._quotas[resource] = RateQuota(resource, limit, remaining, reset_at)
            return
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

    def _rate_limit_wait(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return max(float(retry_after), 1.0)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after).astimezone(UTC)
                except (TypeError, ValueError):
                    pass
                else:
                    return max((retry_at - self._now()).total_seconds(), 1.0)
        reset = response.headers.get("x-ratelimit-reset")
        if response.headers.get("x-ratelimit-remaining") == "0" and reset:
            try:
                return max(float(reset) - self._now().timestamp() + 1, 1.0)
            except ValueError:
                pass
        return min(60 * 2**attempt, 900)

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429 or response.headers.get("x-ratelimit-remaining") == "0":
            return True
        if "retry-after" in response.headers:
            return True
        return "rate limit" in response.text.lower() or "abuse detection" in response.text.lower()


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
