"""Provide shared runtime wiring and disposable operational support.

This module holds cross-cutting process concerns used by multiple GitHub workflows.
Durable archive facts, API semantics, and Git storage policy remain in their owning
modules.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sys
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TextIO

from .git_store import (
    CommitFetchSource,
    GitObjectStore,
    default_git_url,
    git_store_path,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence

    from .client import GitHubPage, GitHubResource


@dataclass(frozen=True, slots=True)
class RateQuota:
    """A latest-known GitHub primary quota bucket."""

    resource: str
    limit: int | None
    remaining: int | None
    reset_at: datetime | None


@dataclass(frozen=True, slots=True)
class APIProgress:
    """A transport-level request, quota, or wait update."""

    request_count: int
    quotas: tuple[RateQuota, ...] = ()
    wait_seconds: float | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class SyncProgress:
    """A disposable operational snapshot for one sync process."""

    event_at: datetime
    phase: str
    cycle_id: int | None = None
    maintenance_job_id: int | None = None
    checkpoint_from: datetime | None = None
    requests: int = 0
    quotas: tuple[RateQuota, ...] = ()
    wait_seconds: float | None = None
    detail: str | None = None


type ProgressObserver = Callable[[SyncProgress], None]
type APIProgressObserver = Callable[[APIProgress], None]


class _SyncProgressTracker:
    def __init__(
        self,
        observer: ProgressObserver | None,
        now: Callable[[], datetime],
    ) -> None:
        self._observer = observer
        self._now = now
        self._work_phase = "starting"
        self._carried_requests = 0
        self._api_start = 0
        self._state = SyncProgress(_utc(now()), self._work_phase)

    def start(self) -> None:
        self._emit()

    def bind_cycle(
        self,
        cycle_id: int,
        checkpoint_from: datetime | None,
        carried_requests: int,
        api_start: int,
    ) -> None:
        self._carried_requests = carried_requests
        self._api_start = api_start
        self._emit(
            cycle_id=cycle_id,
            maintenance_job_id=None,
            checkpoint_from=checkpoint_from,
            requests=carried_requests,
        )

    def bind_maintenance(
        self,
        job_id: int,
        carried_requests: int,
        api_start: int,
    ) -> None:
        self._carried_requests = carried_requests
        self._api_start = api_start
        self._emit(
            cycle_id=None,
            maintenance_job_id=job_id,
            checkpoint_from=None,
            requests=carried_requests,
        )

    def phase(self, phase: str, detail: str | None = None) -> None:
        self._work_phase = phase
        self._emit(phase=phase, wait_seconds=None, detail=detail)

    def api_progress(self, progress: APIProgress) -> None:
        phase = self._work_phase
        if progress.wait_seconds is not None:
            phase = (
                "rate_limit"
                if "rate_limit" in (progress.detail or "")
                else "retry_wait"
            )
        self._emit(
            phase=phase,
            requests=self._carried_requests + progress.request_count - self._api_start,
            quotas=progress.quotas,
            wait_seconds=progress.wait_seconds,
            detail=progress.detail,
        )

    def git_heartbeat(self) -> None:
        self._emit(phase="syncing_git", wait_seconds=None)

    def git_retry(self, wait_seconds: float) -> None:
        self._emit(
            phase="retry_wait",
            wait_seconds=wait_seconds,
            detail="git_transient_retry",
        )

    def done(self, requests: int) -> None:
        self._work_phase = "idle"
        self._emit(
            phase="idle",
            requests=requests,
            wait_seconds=None,
            detail=None,
        )

    def error(self, error: Exception) -> None:
        self._work_phase = "error"
        message = str(error).strip()
        detail = type(error).__name__ if not message else f"{type(error).__name__}: {message}"
        self._emit(phase="error", wait_seconds=None, detail=detail)

    def _emit(self, **changes: Any) -> None:
        self._state = replace(self._state, event_at=_utc(self._now()), **changes)
        if self._observer is None:
            return
        try:
            self._observer(self._state)
        except Exception:
            self._observer = None


class ConsoleProgress:
    """Render synchronization progress to a terminal or structured log.

    Args:
        stream: Output stream, or stderr when omitted to preserve stdout JSON.
        interval: Minimum seconds between ordinary updates in one phase.
        tty: Whether to overwrite one terminal line, or infer from ``isatty``.
        monotonic: Monotonic clock used for throttling.
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        interval: float = 1.0,
        tty: bool | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stream = sys.stderr if stream is None else stream
        self._interval = interval
        self._tty = self._stream.isatty() if tty is None else tty
        self._monotonic = monotonic
        self._last_at: float | None = None
        self._last_phase: str | None = None

    def __call__(self, progress: SyncProgress) -> None:
        """Write one throttled progress snapshot.

        Args:
            progress: Current disposable sync-process state.
        """
        now = self._monotonic()
        urgent = (
            self._last_at is None
            or progress.phase != self._last_phase
            or progress.phase in {"error", "idle", "rate_limit", "retry_wait"}
        )
        if not urgent and now - self._last_at < self._interval:
            return
        if self._tty:
            final = progress.phase in {"error", "idle"}
            print(
                f"\r{_tty_line(progress)}\x1b[K",
                end="\n" if final else "",
                file=self._stream,
                flush=True,
            )
        else:
            print(
                json.dumps(_json_event(progress), ensure_ascii=False, sort_keys=True),
                file=self._stream,
                flush=True,
            )
        self._last_at = now
        self._last_phase = progress.phase


def _tty_line(progress: SyncProgress) -> str:
    quota = " ".join(
        f"{item.resource}={_count(item.remaining)}/{_count(item.limit)}"
        for item in progress.quotas
    )
    wait = "" if progress.wait_seconds is None else f" wait={progress.wait_seconds:.1f}s"
    detail = "" if progress.detail is None else f" {progress.detail}"
    operation = (
        f"job={progress.maintenance_job_id}"
        if progress.maintenance_job_id is not None
        else f"cycle={_count(progress.cycle_id)}"
    )
    return f"{progress.phase} {operation} requests={progress.requests:,} quota={quota or '?'}{wait}{detail}"


def _json_event(progress: SyncProgress) -> dict[str, Any]:
    payload = asdict(progress)
    payload["type"] = "github_sync_progress"
    return _json_value(payload)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _count(value: int | None) -> str:
    return "?" if value is None else f"{value:,}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("progress time must include a timezone")
    return value.astimezone(UTC)


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
        detail_count: int,
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
    git_batch_size: int
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
        # The client consumes progress contracts here, so construction imports it lazily.
        from .client import GitHubAPI

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
            ref_batch_size=self.config.git_batch_size,
            token=_token(self.config.token),
            sleep=self._sleep,
            now=self._now,
        )


def _token(configured: str | None) -> str | None:
    value = configured if configured is not None else os.getenv("GH_TOKEN") or os.getenv(
        "GITHUB_TOKEN",
    )
    return value or None


@asynccontextmanager
async def archive_lock(destination: Path) -> AsyncIterator[None]:
    """Acquire the single-writer lock for an archive pair.

    Args:
        destination: SQLite archive path that identifies the pair.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    path = destination.parent / f".{destination.name}.lock"
    file = path.open("a+")
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
