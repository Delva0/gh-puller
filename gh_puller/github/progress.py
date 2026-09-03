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
class RateQuota:
    resource: str  # GitHub primary quota bucket.
    limit: int | None  # Bucket capacity reported by GitHub.
    remaining: int | None  # Latest conservative remaining value in this window.
    reset_at: datetime | None  # Current primary window reset time.


@dataclass(frozen=True, slots=True)
class APIProgress:
    request_count: int  # HTTP attempts made by this client.
    quotas: tuple[RateQuota, ...] = ()  # Latest independent resource buckets.
    wait_seconds: float | None = None  # Delay before the next attempt.
    detail: str | None = None  # Machine-readable wait or retry reason.


@dataclass(frozen=True, slots=True)
class PullProgress:
    event_at: datetime  # Event observation time.
    phase: str  # Current pull phase.
    target_at: datetime  # Requested observation watermark T.
    run_id: int | None = None  # Durable pull run once allocated.
    pass_at: datetime | None = None  # Cutoff of the active observation pass.
    catalog_seen: int = 0  # Unique root rows durably discovered in the active pass.
    catalog_total: int | None = None  # Cold-start count estimate when available.
    catalog_complete: bool = False  # True after the terminal catalog page is durable.
    objects_completed: int = 0  # Durable discovery tasks completed in the active pass.
    objects_total: int | None = None  # Exact task count once catalog discovery closes.
    items: int | None = None  # Current or cold-start estimated Issue/PR head count.
    bundles_completed: int = 0  # Durable bundles completed for the current run plan.
    issues_completed: int = 0  # Completed Issue bundles in bundles_completed.
    pulls_completed: int = 0  # Completed pull-request bundles in bundles_completed.
    tombstones: int = 0  # Durable directly observed absences.
    latest_number: int | None = None  # Latest durably staged parent number.
    latest_kind: str | None = None  # issue or pull for latest_number.
    requests: int = 0  # Attempts accumulated by this run.
    quotas: tuple[RateQuota, ...] = ()  # Latest independent GitHub resource buckets.
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
        self._git_detail: str | None = None
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
            catalog_complete=False,
            objects_completed=0,
            objects_total=None,
            bundles_completed=0,
            issues_completed=0,
            pulls_completed=0,
            tombstones=0,
            latest_number=None,
            latest_kind=None,
            wait_seconds=None,
            detail=None,
        )

    def catalog_count(self, count: int | None) -> None:
        if count is not None:
            self._emit(catalog_total=count, items=count)

    def catalog_restore(
        self,
        count: int,
        total: int | None,
        *,
        complete: bool,
        objects_completed: int | None = None,
        objects_total: int | None = None,
    ) -> None:
        changes: dict[str, Any] = {
            "catalog_seen": count,
            "catalog_total": total,
            "catalog_complete": complete,
        }
        if total is not None:
            changes["items"] = count if complete else total
        if objects_completed is not None:
            changes["objects_completed"] = objects_completed
        if objects_total is not None:
            changes["objects_total"] = objects_total
        self._emit(**changes)

    def object_completed(self, request_count: int) -> None:
        self._emit(
            objects_completed=self._state.objects_completed + 1,
            requests=self._run_requests(request_count),
        )

    def restore_items(self, count: int) -> None:
        self._emit(items=count)

    def restore_staged(self, resources: Iterable[tuple[int, str, bool]]) -> None:
        """将 pending run 的最新 durable stage 恢复到进度快照。

        Args:
            resources: number、kind 与当前存在状态组成的 durable stage。
        """
        staged = list(resources)
        completed = [(number, kind) for number, kind, present in staged if present]
        issue_count = sum(kind == "issue" for _, kind in completed)
        latest_number, latest_kind = completed[-1] if completed else (None, None)
        self._emit(
            bundles_completed=len(completed),
            issues_completed=issue_count,
            pulls_completed=len(completed) - issue_count,
            tombstones=sum(not present for _, _, present in staged),
            latest_number=latest_number,
            latest_kind=latest_kind,
        )

    def git_fetch(self, pulls: int) -> None:
        self._git_detail = f"pull_refs={pulls}"
        self._emit(
            phase="syncing_git",
            wait_seconds=None,
            detail=self._git_detail,
        )

    def git_heartbeat(self) -> None:
        self._emit(
            phase="syncing_git",
            wait_seconds=None,
            detail=self._git_detail,
        )

    def git_retry(self, wait_seconds: float) -> None:
        self._emit(
            phase="retry_wait",
            wait_seconds=wait_seconds,
            detail="git_transient_retry",
        )

    def git_done(self) -> None:
        self._git_detail = None
        self._emit(phase=self._work_phase, wait_seconds=None, detail=None)

    def bundles_staged(
        self,
        resources: Iterable[tuple[int, str]],
        request_count: int,
        *,
        new_items: int = 0,
    ) -> None:
        completed = list(resources)
        if not completed:
            return
        issue_count = sum(kind == "issue" for _, kind in completed)
        pull_count = len(completed) - issue_count
        number, kind = completed[-1]
        items = self._state.items
        if items is not None and self._state.catalog_total is None:
            items += new_items
        self._emit(
            objects_completed=self._state.objects_completed + len(completed),
            bundles_completed=self._state.bundles_completed + len(completed),
            issues_completed=self._state.issues_completed + issue_count,
            pulls_completed=self._state.pulls_completed + pull_count,
            latest_number=number,
            latest_kind=kind,
            items=items,
            requests=self._run_requests(request_count),
        )

    def absence_staged(self, request_count: int) -> None:
        items = self._state.items
        self._emit(
            items=None if items is None else max(items - 1, 0),
            objects_completed=self._state.objects_completed + 1,
            tombstones=self._state.tombstones + 1,
            requests=self._run_requests(request_count),
        )

    def api_progress(self, progress: APIProgress) -> None:
        phase = self._work_phase
        if progress.wait_seconds is not None:
            phase = "rate_limit" if "rate_limit" in (progress.detail or "") else "retry_wait"
        self._emit(
            phase=phase,
            requests=self._run_requests(progress.request_count),
            quotas=progress.quotas,
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
            catalog_complete=True,
            items=catalog_items,
            objects_total=self._state.objects_completed,
            requests=requests,
            wait_seconds=None,
            reused=reused,
            detail=None,
        )

    def error(self, error: BaseException) -> None:
        self._work_phase = "error"
        name = type(error).__name__
        message = str(error).strip()
        self._emit(
            phase="error",
            wait_seconds=None,
            detail=name if not message else f"{name}: {message}",
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
    completed = progress.objects_completed
    total = progress.objects_total
    if total is None:
        bar = f"{completed:,}/?"
    else:
        width = 20
        filled = width if total == 0 else min(width, int(width * completed / total))
        bar = f"[{'#' * filled}{'-' * (width - filled)}] {completed:,}/{total:,}"
    catalog = (
        f"complete:{progress.catalog_seen:,}"
        if progress.catalog_complete
        else f"scanning:{progress.catalog_seen:,}/{_count(progress.catalog_total)}"
    )
    latest = "-"
    if progress.latest_number is not None:
        latest = f"{progress.latest_kind}#{progress.latest_number}"
    quota = (
        " ".join(f"{item.resource}={_count(item.remaining)}/{_count(item.limit)}" for item in progress.quotas) or "?"
    )
    wait = "" if progress.wait_seconds is None else f" wait={progress.wait_seconds:.1f}s"
    return (
        f"{progress.phase}  objects={bar}  catalog={catalog} "
        f"items={_count(progress.items)} "
        f"issues={progress.issues_completed:,} pulls={progress.pulls_completed:,} "
        f"tombstones={progress.tombstones:,} latest={latest} "
        f"requests={progress.requests:,} {quota}{wait}"
    )


def _count(value: int | None) -> str:
    return "?" if value is None else f"{value:,}"


def _json_event(progress: PullProgress) -> dict[str, Any]:
    payload = asdict(progress)
    payload["type"] = "github_pull_progress"
    return _json_value(payload)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)
