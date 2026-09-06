"""Construct repository-bound API and Git resources for GitHub workflows.

Sync and maintenance own different orchestration state but share this dependency
boundary. Injected readers remain caller-owned; resources constructed here inherit
the configured request, retry, authentication, and storage policy.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any, Protocol

from .client import GitHubAPI, GitHubPage, GitHubResource
from .git_store import (
    CommitFetchSource,
    GitObjectStore,
    default_git_url,
    git_store_path,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from .progress import APIProgressObserver


class GitHubAPIReader(Protocol):
    """Describe the API operations consumed by discovery and fact collection."""

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


class GitObjectWriter(Protocol):
    """Describe the Git operations consumed by fact collection."""

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
        sources: Mapping[str, Sequence[CommitFetchSource]] | None = None,
        heartbeat: Callable[[], None] | None = None,
        retry: Callable[[float], None] | None = None,
    ) -> dict[str, dict[str, Any]]: ...


class _RuntimeConfig(Protocol):
    repository: str
    destination: Path
    token: str | None
    api_url: str
    graphql_url: str | None
    api_version: str
    request_timeout: float
    git_url: str | None
    git_destination: Path | None


class GitHubRuntime:
    """Own dependency construction and the shared archive serialization lock.

    Args:
        config: Repository, transport, and storage settings.
        api: Optional caller-owned API reader.
        git: Optional caller-owned Git writer.
        now: Timezone-aware clock shared with rate-limit handling.
        sleep: Async retry wait operation.
    """

    def __init__(
        self,
        config: _RuntimeConfig,
        *,
        api: GitHubAPIReader | None,
        git: GitObjectWriter | None,
        now: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self.config = config
        self._api = api
        self._git = git
        self._now = now
        self._sleep = sleep
        self.store_lock = asyncio.Lock()

    @property
    def git_destination(self) -> Path:
        """Return the configured or database-derived bare Git store path."""
        return self.config.git_destination or git_store_path(self.config.destination)

    def make_api(
        self,
        progress: APIProgressObserver | None,
    ) -> tuple[GitHubAPIReader, bool]:
        """Return an API reader and whether this runtime owns its lifetime."""
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
                progress=progress,
            ),
            True,
        )

    def make_git(self, *, upstream_synced: bool = False) -> GitObjectWriter:
        """Return the injected or configured repository Git writer.

        Args:
            upstream_synced: Whether the active cycle already completed its durable
                upstream refs task.
        """
        if self._git is not None:
            return self._git
        return GitObjectStore(
            self.git_destination,
            self.config.repository,
            self.config.git_url or default_git_url(self.config.repository),
            upstream_synced=upstream_synced,
            token=_token(self.config.token),
            sleep=self._sleep,
            now=self._now,
        )


def _token(configured: str | None) -> str | None:
    value = configured if configured is not None else os.getenv("GH_TOKEN") or os.getenv(
        "GITHUB_TOKEN",
    )
    return value or None
