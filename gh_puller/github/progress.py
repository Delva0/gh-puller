"""Emit disposable progress signals for GitHub operations and console display.

SQLite cycles, tasks, and observations remain the recovery authority. The API client
reports responses, retries, and quota waits; executors report phases and Git heartbeats.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, TextIO


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
