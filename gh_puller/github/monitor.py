"""将 systemd 状态与 journald 进度事件投影为只读写者状态。

本模块只观察由 daemon installer 管理的 unit，不打开 SQLite、不访问 GitHub，
也不把运维状态写回事实库。进度事件契约见 progress，服务身份契约见运维脚本。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING

from .git_store import git_store_path
from .progress import RateQuota

if TYPE_CHECKING:
    from collections.abc import Sequence

_UNIT = re.compile(r"gh-puller-([0-9a-f]{12}|[0-9a-f]{64})\.service\Z")
_PROGRESS_TYPE = "github_pull_progress"
_JOURNAL_LINES = 512
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_OVERVIEW_WIDTHS = (12, 4, 17, 15, 7, 11, 7)


@dataclass(frozen=True, slots=True)
class ManagedWriter:
    unit: str  # Path-hash systemd unit name.
    identity: str  # Full database-path digest.
    repository: str  # Bound GitHub owner/repo.
    database: Path  # Canonical SQLite destination.
    git_store: Path  # Database-derived bare Git object store.


@dataclass(frozen=True, slots=True)
class ServiceState:
    active: str  # systemd ActiveState.
    sub: str  # systemd SubState.
    pid: int  # Main process ID, or zero when absent.
    restarts: int  # systemd restart counter.


@dataclass(frozen=True, slots=True)
class ProgressState:
    event_at: datetime | None  # Observation time of the journal event.
    phase: str  # Puller phase name.
    target_at: str | None  # Requested observation watermark T.
    run_id: int | None  # Durable run identity, if allocated.
    catalog_seen: int  # Unique root rows durably discovered in the active pass.
    catalog_total: int | None  # Cold-start count estimate when available.
    catalog_complete: bool  # Terminal catalog page is durable.
    objects_completed: int  # Durable discovery tasks completed in the active pass.
    objects_total: int | None  # Exact task count after catalog discovery closes.
    bundles_completed: int  # Durable bundles completed for the current run plan.
    issues_completed: int  # Issue bundles in bundles_completed.
    pulls_completed: int  # Pull-request bundles in bundles_completed.
    tombstones: int  # Durable directly observed absences.
    latest_number: int | None  # Latest durably staged parent number.
    latest_kind: str | None  # Kind of latest_number.
    quotas: tuple[RateQuota, ...]  # Latest independent GitHub resource buckets.
    wait_seconds: float | None  # Current target, retry, or rate-limit wait.
    detail: str | None  # Machine-readable phase detail.
    items: int | None = None  # Current or cold-start estimated Issue/PR head count.


@dataclass(frozen=True, slots=True)
class WriterStatus:
    writer: ManagedWriter  # Static installer configuration.
    service: ServiceState  # Current systemd process state.
    progress: ProgressState | None  # Latest valid progress journal event.


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uv run -m gh_puller.github.monitor")
    parser.add_argument("--systemd-dir", type=Path, required=True)
    parser.add_argument("--systemctl", required=True)
    parser.add_argument("--journalctl", required=True)
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--database", type=Path)
    selector.add_argument("--writer-id")
    return parser


def _managed_writers(
    systemd_dir: Path,
    database: Path | None = None,
    writer_id: str | None = None,
) -> list[ManagedWriter]:
    selected = None if database is None else database.resolve()
    writers: list[ManagedWriter] = []
    for path in sorted(systemd_dir.glob("gh-puller-*.service")):
        match = _UNIT.fullmatch(path.name)
        if match is None:
            continue
        metadata = _unit_metadata(path.read_text())
        repository = metadata.get("repository")
        destination = metadata.get("database")
        if repository is None or destination is None:
            continue
        resolved = Path(destination).resolve()
        identity = hashlib.sha256(os.fsencode(resolved)).hexdigest()
        if (
            not identity.startswith(match[1])
            or (selected is not None and resolved != selected)
            or (writer_id is not None and identity[:12] != writer_id)
        ):
            continue
        writers.append(
            ManagedWriter(
                unit=path.name,
                identity=identity,
                repository=repository,
                database=resolved,
                git_store=git_store_path(resolved),
            ),
        )
    return writers


def _unit_metadata(content: str) -> dict[str, str]:
    prefix = "# gh-puller-"
    metadata: dict[str, str] = {}
    for line in content.splitlines():
        if line.startswith(prefix) and "=" in line:
            key, value = line.removeprefix(prefix).split("=", 1)
            metadata[key] = value
    return metadata


def _collect(
    writers: Sequence[ManagedWriter],
    systemctl: str,
    journalctl: str,
) -> list[WriterStatus]:
    return [
        WriterStatus(
            writer=writer,
            service=_service_state(systemctl, writer.unit),
            progress=_latest_progress(
                _output(
                    [
                        journalctl,
                        "--unit",
                        writer.unit,
                        "--output=cat",
                        f"--lines={_JOURNAL_LINES}",
                        "--no-pager",
                    ],
                ),
            ),
        )
        for writer in writers
    ]


def _service_state(systemctl: str, unit: str) -> ServiceState:
    output = _output(
        [
            systemctl,
            "show",
            unit,
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            "--property=NRestarts",
            "--no-pager",
        ],
    )
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    return ServiceState(
        active=values.get("ActiveState", "unknown"),
        sub=values.get("SubState", "unknown"),
        pid=_int(values.get("MainPID")) or 0,
        restarts=_int(values.get("NRestarts")) or 0,
    )


def _output(command: Sequence[str]) -> str:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    return result.stdout if result.returncode == 0 else ""


def _latest_progress(output: str) -> ProgressState | None:
    for line in reversed(output.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("type") != _PROGRESS_TYPE:
            continue
        return ProgressState(
            event_at=_time(payload.get("event_at")),
            phase=_text(payload.get("phase")) or "unknown",
            target_at=_text(payload.get("target_at")),
            run_id=_int(payload.get("run_id")),
            catalog_seen=_int(payload.get("catalog_seen")) or 0,
            catalog_total=_int(payload.get("catalog_total")),
            catalog_complete=payload.get("catalog_complete") is True,
            objects_completed=_int(payload.get("objects_completed")) or 0,
            objects_total=_int(payload.get("objects_total")),
            bundles_completed=_int(payload.get("bundles_completed")) or 0,
            issues_completed=_int(payload.get("issues_completed")) or 0,
            pulls_completed=_int(payload.get("pulls_completed")) or 0,
            tombstones=_int(payload.get("tombstones")) or 0,
            latest_number=_int(payload.get("latest_number")),
            latest_kind=_text(payload.get("latest_kind")),
            quotas=_quotas(payload.get("quotas")),
            wait_seconds=_float(payload.get("wait_seconds")),
            detail=_text(payload.get("detail")),
            items=_int(payload.get("items")),
        )
    return None


def _render_table(
    statuses: Sequence[WriterStatus],
    now: datetime | None = None,
) -> str:
    if not statuses:
        return "No managed GitHub writers."
    observed_at = datetime.now(UTC) if now is None else now.astimezone(UTC)
    headers = ("ID", "ST", "REPO", "DB", "PHASE", "DONE", "UPDATED")
    rows = [
        (
            status.writer.identity[:12],
            _short_service(status.service),
            status.writer.repository,
            status.writer.database.name,
            _short_phase(status.progress),
            _short_objects(status.progress),
            _age(status.progress.event_at, observed_at) if status.progress else "-",
        )
        for status in statuses
    ]
    return "\n".join(_overview_line(row) for row in (headers, *rows))


def _render_detail(
    status: WriterStatus,
    now: datetime | None = None,
    zone: tzinfo | None = None,
) -> str:
    observed_at = datetime.now(UTC) if now is None else now.astimezone(UTC)
    progress = status.progress
    rows = [
        ("WRITER", status.writer.identity[:12]),
        ("STATE", _service_detail(status.service)),
        ("REPOSITORY", status.writer.repository),
        ("DATABASE", str(status.writer.database)),
        ("GIT STORE", str(status.writer.git_store)),
        ("PID", str(status.service.pid) if status.service.pid else "-"),
        ("RESTARTS", str(status.service.restarts)),
        ("RUN", _show(progress.run_id if progress else None)),
        ("TARGET T", _target(progress, zone)),
        ("ITEMS", _items(progress)),
        ("PHASE", progress.phase if progress else "-"),
        ("CATALOG", _catalog(progress)),
        ("OBJECTS", _objects(progress, 20)),
        ("QUOTA", _quota(progress, zone)),
        ("STAGED", _staged(progress)),
        ("LATEST", _latest(progress)),
        ("UPDATED", _updated(progress, observed_at, zone)),
    ]
    if progress and progress.wait_seconds is not None:
        rows.append(("WAIT", _wait(progress, observed_at)))
    if progress and progress.detail:
        rows.append(("DETAIL", progress.detail))
    width = max(len(key) for key, _ in rows)
    lines = []
    for key, value in rows:
        parts = value.splitlines() or [""]
        lines.append(f"{key:<{width}}  {parts[0]}")
        lines.extend(f"{'':<{width}}  {part}" for part in parts[1:])
    return "\n".join(lines)


def _overview_line(values: Sequence[str]) -> str:
    cells = (
        f"{_fit(value, width):<{width}}"
        for value, width in zip(values, _OVERVIEW_WIDTHS, strict=True)
    )
    return " ".join(cells).rstrip()


def _fit(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    left = (width - 1) // 2
    return f"{value[:left]}…{value[-(width - left - 1):]}"


def _short_service(service: ServiceState) -> str:
    labels = {
        "active": "up",
        "activating": "boot",
        "deactivating": "stop",
        "failed": "fail",
        "inactive": "down",
        "unknown": "?",
    }
    return labels.get(service.active, service.active)


def _short_phase(progress: ProgressState | None) -> str:
    if progress is None:
        return "-"
    labels = {
        "rate_limit": "quota",
        "retry_wait": "retry",
        "starting": "start",
        "syncing_git": "git",
    }
    if progress.phase.endswith(("_catalog", "_bundles")):
        return "objects" if progress.catalog_complete else "catalog"
    return labels.get(progress.phase, progress.phase)


def _short_objects(progress: ProgressState | None) -> str:
    if progress is None:
        return "-"
    total = "?" if progress.objects_total is None else _short_count(progress.objects_total)
    return f"{_short_count(progress.objects_completed)}/{total}"


def _short_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    scale, suffix = (1_000_000, "m") if value >= 1_000_000 else (1_000, "k")
    scaled = value / scale
    return f"{scaled:.1f}{suffix}" if scaled < 99.95 else f"{scaled:.0f}{suffix}"


def _service_label(service: ServiceState) -> str:
    return service.sub if service.active == "active" else service.active


def _service_detail(service: ServiceState) -> str:
    return f"{_service_label(service)} (active={service.active}, sub={service.sub})"


def _items(progress: ProgressState | None) -> str:
    if progress is None:
        return "?"
    items = progress.items if progress.items is not None else progress.catalog_total
    return "?" if items is None else f"{items:,}"


def _catalog(progress: ProgressState | None) -> str:
    if progress is None:
        return "-"
    seen = f"{progress.catalog_seen:,}"
    if progress.catalog_complete:
        estimate = progress.catalog_total
        if estimate is not None and estimate != progress.catalog_seen:
            return f"complete {seen} (initial estimate {estimate:,})"
        return f"complete {seen}"
    estimate = "?" if progress.catalog_total is None else f"~{progress.catalog_total:,}"
    return f"scanning {seen}/{estimate}"


def _objects(progress: ProgressState | None, width: int) -> str:
    if progress is None:
        return "-"
    return _meter("objects", progress.objects_completed, progress.objects_total, width)


def _target(progress: ProgressState | None, zone: tzinfo | None = None) -> str:
    if progress is None or progress.target_at is None:
        return "-"
    target = _time(progress.target_at)
    return progress.target_at if target is None else _local_time(target, zone)


def _quota(progress: ProgressState | None, zone: tzinfo | None = None) -> str:
    if progress is None or not progress.quotas:
        return "-"
    width = max(len(quota.resource) for quota in progress.quotas)
    return "\n".join(_quota_item(quota, width, zone) for quota in progress.quotas)


def _quota_item(quota: RateQuota, width: int, zone: tzinfo | None) -> str:
    remaining = "?" if quota.remaining is None else f"{quota.remaining:,}"
    limit = "?" if quota.limit is None else f"{quota.limit:,}"
    reset_at = "?" if quota.reset_at is None else _local_time(quota.reset_at, zone)
    return f"{quota.resource:<{width}}  {remaining}/{limit}  reset {reset_at}"


def _meter(label: str, completed: int, total: int | None, width: int) -> str:
    if total is None:
        return f"{label} {completed:,}/?"
    filled = width if total == 0 else min(width, int(width * completed / total))
    return f"{label} [{'#' * filled}{'-' * (width - filled)}] {completed:,}/{total:,}"


def _staged(progress: ProgressState | None) -> str:
    if progress is None:
        return "-"
    return f"issues={progress.issues_completed:,} pulls={progress.pulls_completed:,} tombstones={progress.tombstones:,}"


def _latest(progress: ProgressState | None) -> str:
    if progress is None or progress.latest_number is None:
        return "-"
    return f"{progress.latest_kind or 'item'}#{progress.latest_number}"


def _updated(
    progress: ProgressState | None,
    now: datetime,
    zone: tzinfo | None = None,
) -> str:
    if progress is None or progress.event_at is None:
        return "-"
    return f"{_local_time(progress.event_at, zone)}; {_age(progress.event_at, now)} ago"


def _local_time(value: datetime, zone: tzinfo | None = None) -> str:
    local = value.astimezone(zone)
    clock = local.strftime("%H:%M:%S")
    if local.microsecond:
        clock = f"{clock}.{local.microsecond:06d}"
    name = local.tzname() or local.strftime("%z")
    return f"{_WEEKDAYS[local.weekday()]} {local:%Y-%m-%d} {clock} {name}"


def _wait(progress: ProgressState | None, now: datetime) -> str:
    if progress is None or progress.wait_seconds is None:
        return "-"
    elapsed = 0.0 if progress.event_at is None else max((now - progress.event_at).total_seconds(), 0.0)
    return f"{_duration(max(progress.wait_seconds - elapsed, 0.0))} remaining"


def _duration(seconds: float) -> str:
    rounded = int(seconds)
    if rounded < 60:
        return f"{rounded}s"
    if rounded < 3600:
        return f"{rounded // 60}m{rounded % 60:02d}s"
    return f"{rounded // 3600}h{rounded % 3600 // 60:02d}m"


def _age(event_at: datetime | None, now: datetime) -> str:
    if event_at is None:
        return "?"
    seconds = max(int((now - event_at).total_seconds()), 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _show(value: object | None) -> str:
    return "-" if value is None else str(value)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else _integer_text(value)


def _integer_text(value: object) -> int | None:
    try:
        return int(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _quotas(value: object) -> tuple[RateQuota, ...]:
    if not isinstance(value, list):
        return ()
    quotas = []
    for item in value:
        if not isinstance(item, dict):
            continue
        resource = _text(item.get("resource"))
        if resource is None:
            continue
        quotas.append(
            RateQuota(
                resource=resource,
                limit=_int(item.get("limit")),
                remaining=_int(item.get("remaining")),
                reset_at=_time(item.get("reset_at")),
            ),
        )
    return tuple(quotas)


def main(argv: Sequence[str] | None = None) -> int:
    """呈现一个或全部受管数据库写者。

    Args:
        argv: 不含程序名的监控参数；None 使用当前进程参数。

    Returns:
        成功为 0；指定数据库没有受管写者为 2。
    """
    args = _parser().parse_args(argv)
    database = None if args.database is None else args.database.resolve()
    writers = _managed_writers(args.systemd_dir, database, args.writer_id)
    selected = database is not None or args.writer_id is not None
    if selected and not writers:
        selector = f"ID: {args.writer_id}" if args.writer_id else f"database: {database}"
        print(f"No managed writer for {selector}", file=sys.stderr)
        return 2
    statuses = _collect(writers, args.systemctl, args.journalctl)
    print(_render_detail(statuses[0]) if selected else _render_table(statuses))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
