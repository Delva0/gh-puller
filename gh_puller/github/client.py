"""提供遵守 GitHub 限流恢复契约的异步 API 读取层。

本模块只负责 HTTP、分页、仓库对象精确计数与重试，不解释 Issue/PR 数据，也不
写归档。覆盖水位与持久化契约见 ``gh_puller.github``。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

_LOG = logging.getLogger(__name__)
_DEFAULT_ACCEPT = "application/vnd.github.full+json"
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
        self._blocked_until = 0.0
        self._gate_lock = asyncio.Lock()
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

    async def get_text(self, path: str, *, accept: str) -> str:
        """读取自定义 media type 的文本响应。

        Args:
            path: 相对 API 路径。
            accept: 请求的 GitHub media type。

        Returns:
            未改写的响应文本。
        """
        return (await self._request("GET", path, accept=accept)).text

    async def paginate(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """沿 GitHub ``Link`` 响应头读取完整列表。

        Args:
            path: 首个分页 API 路径。
            params: 首页查询参数；``per_page`` 缺省固定为 GitHub 上限 100。

        Returns:
            按服务端顺序拼接、字段不裁剪的所有条目。

        Raises:
            GitHubAPIError: 任一分页响应不是 JSON 对象数组。
        """
        query: dict[str, Any] | None = dict(params or {})
        query.setdefault("per_page", 100)
        url: str | None = path
        items: list[dict[str, Any]] = []
        while url:
            response = await self._request("GET", url, params=query)
            query = None
            try:
                page = response.json()
            except json.JSONDecodeError as exc:
                raise GitHubAPIError(f"GitHub returned invalid JSON for {response.url}") from exc
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise GitHubAPIError(f"GitHub returned a non-object page for {response.url}")
            items.extend(page)
            url = response.links.get("next", {}).get("url")
        return items

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
        secondary_attempt = 0
        while True:
            response = await self._request(
                "POST",
                self._graphql_url,
                json_body={
                    "query": _REPOSITORY_ITEM_COUNT,
                    "variables": {"owner": owner, "repo": repo},
                },
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
            if errors:
                if self._is_rate_limited(response):
                    await self._wait_after_rate_limit(response, secondary_attempt)
                    secondary_attempt += 1
                    continue
                raise GitHubAPIError(f"GitHub GraphQL error for {response.url}: {errors!r}")
            try:
                repository = payload["data"]["repository"]
                issues = repository["issues"]["totalCount"]
                pulls = repository["pullRequests"]["totalCount"]
            except (KeyError, TypeError) as exc:
                raise GitHubAPIError(
                    f"GitHub returned incomplete GraphQL data for {response.url}",
                ) from exc
            if not isinstance(issues, int) or not isinstance(pulls, int):
                raise GitHubAPIError(f"GitHub returned non-integer counts for {response.url}")
            return issues + pulls

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        accept: str | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        transient_attempt = 0
        secondary_attempt = 0
        while True:
            await self._wait_for_primary_limit()
            headers = {"Accept": accept} if accept else None
            try:
                self.request_count += 1
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    headers=headers,
                    json=json_body,
                )
            except httpx.RequestError as exc:
                if transient_attempt >= self._transient_retries:
                    raise GitHubAPIError(f"GitHub request failed: {path}") from exc
                transient_attempt += 1
                await self._sleep(min(2 ** (transient_attempt - 1), 30))
                continue

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
                await self._sleep(min(2 ** (transient_attempt - 1), 30))
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


def _graphql_endpoint(base_url: str) -> str:
    root = base_url.rstrip("/")
    if root.endswith("/api/v3"):
        return f"{root.removesuffix('/api/v3')}/api/graphql"
    return f"{root}/graphql"
