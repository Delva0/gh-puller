"""提供不参与事实提交的 GitHub 拉取进度事件与控制台呈现。

事件回调是同步、尽力而为的带外边界；回调失败不会改变拉取、恢复或发布结果。
SQLite 事实契约见 ``gh_puller.github``，HTTP 配额采样由 client 提供。
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, TextIO


@dataclass(frozen=True, slots=True)
class APIProgress:
    request_count: int  # HTTP attempts made by this client.
    quota_limit: int | None = None  # Latest GitHub resource limit.
    quota_remaining: int | None = None  # Latest remaining requests.
    quota_reset_at: datetime | None = None  # Latest primary reset time.
    quota_resource: str | None = None  # GitHub quota bucket name.
    wait_seconds: float | None = None  # Delay before the next attempt.
    detail: str | None = None  # Machine-readable wait or retry reason.


@dataclass(frozen=True, slots=True)
class PullProgress:
    event_at: datetime  # Event observation time.
    phase: str  # Current pull phase.
    target_at: datetime  # Requested coverage watermark T.
    run_id: int | None = None  # Durable pull run once allocated.
    pass_at: datetime | None = None  # Cutoff of the active closure pass.
    catalog_seen: int = 0  # Root rows scanned, then certified current objects.
    catalog_total: int | None = None  # Certified current object count when known.
    bundles_completed: int = 0  # Bundles durably staged in the active pass.
    bundles_total: int | None = None  # Bundles selected in the active pass.
    issues_completed: int = 0  # Durably staged Issue bundles.
    pulls_completed: int = 0  # Durably staged pull-request bundles.
    tombstones: int = 0  # Absences durably staged in the active pass.
    latest_number: int | None = None  # Latest durably staged parent number.
    latest_kind: str | None = None  # issue or pull for latest_number.
    requests: int = 0  # Attempts accumulated by this run.
    quota_limit: int | None = None  # Latest GitHub resource limit.
    quota_remaining: int | None = None  # Latest remaining requests.
    quota_reset_at: datetime | None = None  # Latest primary reset time.
    quota_resource: str | None = None  # GitHub quota bucket name.
    wait_seconds: float | None = None  # Current target, retry, or quota wait.
    reused: bool = False  # True when an existing committed run was returned.
    detail: str | None = None  # Machine-readable phase detail.


type ProgressObserver = Callable[[PullProgress], None]
type APIProgressObserver = Callable[[APIProgress], None]


class _PullProgressTracker:
    def __init__(
        self,
        target_at: datetime,
        observer: ProgressObserver | None,
        now: Callable[[], datetime],
    ) -> None:
        self._observer = observer
        self._now = now
        self._work_phase = "starting"
        self._api_start = 0
        self._carried_requests = 0
        self._state = PullProgress(
            event_at=_utc(now()),
            phase=self._work_phase,
            target_at=target_at,
        )

    def phase(
        self,
        phase: str,
        *,
        pass_at: datetime | None = None,
        wait_seconds: float | None = None,
        detail: str | None = None,
    ) -> None:
        self._work_phase = phase
        self._emit(
            phase=phase,
            pass_at=pass_at,
            wait_seconds=wait_seconds,
            detail=detail,
        )

    def bind_run(self, run_id: int, carried_requests: int, api_start: int) -> None:
        self._carried_requests = carried_requests
        self._api_start = api_start
        self._emit(run_id=run_id, requests=carried_requests)

    def start_pass(self, name: str, pass_at: datetime) -> None:
        self._work_phase = f"{name}_catalog"
        self._emit(
            phase=self._work_phase,
            pass_at=pass_at,
            catalog_seen=0,
            catalog_total=None,
            bundles_completed=0,
            bundles_total=None,
            issues_completed=0,
            pulls_completed=0,
            tombstones=0,
            latest_number=None,
            latest_kind=None,
            wait_seconds=None,
            detail=None,
        )

    def catalog_scan(self) -> None:
        self._emit(catalog_seen=0)

    def catalog_page(self, count: int) -> None:
        self._emit(catalog_seen=self._state.catalog_seen + count)

    def catalog_count(self, count: int | None) -> None:
        if count is not None:
            self._emit(catalog_total=count)

    def catalog_complete(self, count: int) -> None:
        self._emit(catalog_seen=count, catalog_total=count)

    def start_bundles(self, name: str, total: int, request_count: int) -> None:
        self._work_phase = f"{name}_bundles"
        self._emit(
            phase=self._work_phase,
            bundles_completed=0,
            bundles_total=total,
            issues_completed=0,
            pulls_completed=0,
            tombstones=0,
            latest_number=None,
            latest_kind=None,
            requests=self._run_requests(request_count),
            wait_seconds=None,
            detail=None,
        )

    def bundles_staged(self, resources: Iterable[tuple[int, str]], request_count: int) -> None:
        completed = list(resources)
        if not completed:
            return
        issue_count = sum(kind == "issue" for _, kind in completed)
        pull_count = len(completed) - issue_count
        number, kind = completed[-1]
        self._emit(
            bundles_completed=self._state.bundles_completed + len(completed),
            issues_completed=self._state.issues_completed + issue_count,
            pulls_completed=self._state.pulls_completed + pull_count,
            latest_number=number,
            latest_kind=kind,
            requests=self._run_requests(request_count),
        )

    def tombstones_staged(self, count: int, request_count: int) -> None:
        self._emit(
            tombstones=self._state.tombstones + count,
            requests=self._run_requests(request_count),
        )

    def api_progress(self, progress: APIProgress) -> None:
        phase = self._work_phase
        if progress.wait_seconds is not None:
            phase = "rate_limit" if "rate_limit" in (progress.detail or "") else "retry_wait"
        self._emit(
            phase=phase,
            requests=self._run_requests(progress.request_count),
            quota_limit=progress.quota_limit,
            quota_remaining=progress.quota_remaining,
            quota_reset_at=progress.quota_reset_at,
            quota_resource=progress.quota_resource,
            wait_seconds=progress.wait_seconds,
            detail=progress.detail,
        )

    def done(
        self,
        *,
        run_id: int,
        catalog_items: int,
        requests: int,
        reused: bool,
    ) -> None:
        self._work_phase = "done"
        self._emit(
            phase="done",
            run_id=run_id,
            catalog_seen=catalog_items,
            catalog_total=catalog_items,
            requests=requests,
            wait_seconds=None,
            reused=reused,
            detail=None,
        )

    def error(self, error: BaseException) -> None:
        self._work_phase = "error"
        self._emit(
            phase="error",
            wait_seconds=None,
            detail=type(error).__name__,
        )

    def _run_requests(self, request_count: int) -> int:
        return self._carried_requests + max(request_count - self._api_start, 0)

    def _emit(self, **changes: Any) -> None:
        self._state = replace(self._state, event_at=_utc(self._now()), **changes)
        if self._observer is None:
            return
        try:
            self._observer(self._state)
        except Exception:
            self._observer = None


class ConsoleProgress:
    """将拉取进度呈现到终端或结构化日志。

    Args:
        stream: 输出流；None 使用 stderr，避免污染 CLI 的最终 stdout JSON。
        interval: 同一阶段普通更新的最小输出间隔秒数。
        tty: 是否使用单行终端进度条；None 读取输出流的 ``isatty``。
        monotonic: 节流使用的单调时钟。
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

    def __call__(self, progress: PullProgress) -> None:
        """输出一份进度快照。

        Args:
            progress: 拉取器产生的不可变进度快照。
        """
        now = self._monotonic()
        urgent = (
            self._last_at is None
            or progress.phase != self._last_phase
            or progress.phase in {"done", "error"}
            or progress.wait_seconds is not None
        )
        if not urgent and now - self._last_at < self._interval:
            return
        if self._tty:
            final = progress.phase in {"done", "error"}
            end = "\n" if final else ""
            print(f"\r{_tty_line(progress)}\x1b[K", end=end, file=self._stream, flush=True)
        else:
            print(
                json.dumps(_json_event(progress), ensure_ascii=False, sort_keys=True),
                file=self._stream,
                flush=True,
            )
        self._last_at = now
        self._last_phase = progress.phase


def _tty_line(progress: PullProgress) -> str:
    completed = progress.bundles_completed
    total = progress.bundles_total
    if total is None:
        bar = "bundles=?"
    else:
        width = 20
        filled = width if total == 0 else min(width, int(width * completed / total))
        bar = f"[{'#' * filled}{'-' * (width - filled)}] {completed}/{total}"
    catalog_total = "?" if progress.catalog_total is None else str(progress.catalog_total)
    catalog = f"{progress.catalog_seen}/{catalog_total}"
    latest = "-"
    if progress.latest_number is not None:
        latest = f"{progress.latest_kind}#{progress.latest_number}"
    quota = "?"
    if progress.quota_remaining is not None:
        quota = str(progress.quota_remaining)
        if progress.quota_limit is not None:
            quota = f"{quota}/{progress.quota_limit}"
    wait = "" if progress.wait_seconds is None else f" wait={progress.wait_seconds:.1f}s"
    return (
        f"{progress.phase} {bar} catalog={catalog} "
        f"issues={progress.issues_completed} prs={progress.pulls_completed} "
        f"tombstones={progress.tombstones} latest={latest} "
        f"requests={progress.requests} quota={quota}{wait}"
    )


def _json_event(progress: PullProgress) -> dict[str, Any]:
    payload = asdict(progress)
    payload["type"] = "github_pull_progress"
    for key, value in tuple(payload.items()):
        if isinstance(value, datetime):
            payload[key] = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return payload


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)
