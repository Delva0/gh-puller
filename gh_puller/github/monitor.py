"""Project systemd, journald, and SQLite state into a read-only writer view.

SQLite provides durable discovery, task, and fact progress. Journald adds only current
phase, quota, wait, and error details. Status commands never access GitHub or mutate an
archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING

from .archive_format import ARCHIVE_SCHEMA_VERSION
from .progress import RateQuota

if TYPE_CHECKING:
    from collections.abc import Sequence

_UNIT = re.compile(r"gh-puller-([0-9a-f]{12}|[0-9a-f]{64})\.service\Z")
_PROGRESS_TYPE = "github_sync_progress"
_JOURNAL_LINES = 512
_RECENT_TASK_SAMPLE = 2_048
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_OVERVIEW_MIN_WIDTHS = (12, 4, 16, 14, 9, 11, 7)
_OVERVIEW_FLEX_COLUMNS = (2, 3)

type _FileVersion = tuple[int, int, int, int] | None
type _ArchiveVersion = tuple[_FileVersion, _FileVersion]


@dataclass(frozen=True, slots=True)
class ManagedWriter:
    """Static identity read from one managed systemd unit."""

    unit: str
    identity: str
    repository: str
    database: Path


@dataclass(frozen=True, slots=True)
class ServiceState:
    """Relevant systemd process state."""

    active: str
    sub: str
    pid: int
    restarts: int


@dataclass(frozen=True, slots=True)
class ProgressState:
    """Latest disposable journal progress event."""

    event_at: datetime | None
    phase: str
    cycle_id: int | None
    maintenance_job_id: int | None
    checkpoint_from: datetime | None
    requests: int
    quotas: tuple[RateQuota, ...]
    wait_seconds: float | None
    detail: str | None


@dataclass(frozen=True, slots=True)
class ArchiveState:
    """Durable progress read from the current observation archive."""

    git_store: Path
    checkpoint: datetime | None
    cycle_id: int | None
    cycle_status: str | None
    cycle_started: datetime | None
    cycle_completed: datetime | None
    discovery_pages: int
    discovered_items: int
    discovery_complete: bool
    tasks_completed: int
    tasks_total: int
    task_counts: tuple[tuple[str, int, int], ...]
    task_rate: TaskRate | None
    parents_completed: int
    parents_total: int
    observations: int
    current_facts: int
    requests: int
    latest: tuple[str, str, datetime] | None
    updated_at: datetime | None
    last_error: str | None
    maintenance: MaintenanceState | None


@dataclass(frozen=True, slots=True)
class MaintenanceState:
    """Durable progress for the latest refresh or backfill job."""

    job_id: int
    kind: str
    status: str
    requested_at: datetime
    completed_at: datetime | None
    tasks_completed: int
    tasks_total: int
    outcomes: tuple[tuple[str, int], ...]
    latest: tuple[str, str, datetime] | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class TaskRate:
    """Bounded durable sample of recent task completions."""

    completions: int
    observed_from: datetime
    observed_until: datetime


@dataclass(frozen=True, slots=True)
class WriterStatus:
    """Combined read-only status of one managed writer."""

    writer: ManagedWriter
    service: ServiceState
    progress: ProgressState | None
    archive: ArchiveState | None
    archive_error: str | None = None


@dataclass(frozen=True, slots=True)
class _ArchiveCacheEntry:
    """Archive state associated with an unchanged database and WAL."""

    version: _ArchiveVersion
    state: ArchiveState | None
    error: str | None


def _watch_interval(value: str) -> float:
    try:
        interval = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if interval < 0.1:
        raise argparse.ArgumentTypeError("must be at least 0.1 seconds")
    return interval


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uv run -m gh_puller.github.monitor")
    parser.add_argument("--systemd-dir", type=Path, required=True)
    parser.add_argument("--systemctl", required=True)
    parser.add_argument("--journalctl", required=True)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument(
        "-n",
        "--interval",
        type=_watch_interval,
        default=2.0,
        metavar="SECONDS",
    )
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
    writers = []
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
            ManagedWriter(path.name, identity, repository, resolved),
        )
    return writers


def _unit_metadata(content: str) -> dict[str, str]:
    prefix = "# gh-puller-"
    metadata = {}
    for line in content.splitlines():
        if line.startswith(prefix) and "=" in line:
            key, value = line.removeprefix(prefix).split("=", 1)
            metadata[key] = value
    return metadata


def _collect(
    writers: Sequence[ManagedWriter],
    systemctl: str,
    journalctl: str,
    archive_cache: dict[Path, _ArchiveCacheEntry] | None = None,
) -> list[WriterStatus]:
    statuses = []
    for writer in writers:
        archive, error = _cached_archive_state(writer.database, archive_cache)
        statuses.append(
            WriterStatus(
                writer,
                _service_state(systemctl, writer.unit),
                _latest_progress(
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
                archive,
                error,
            ),
        )
    return statuses


def _cached_archive_state(
    path: Path,
    cache: dict[Path, _ArchiveCacheEntry] | None,
) -> tuple[ArchiveState | None, str | None]:
    if cache is None:
        return _archive_state(path)
    version = _archive_version(path)
    cached = cache.get(path)
    if cached is not None and cached.version == version:
        return cached.state, cached.error
    state, error = _archive_state(path)
    if error is None and version == _archive_version(path):
        cache[path] = _ArchiveCacheEntry(version, state, error)
    else:
        cache.pop(path, None)
    return state, error


def _archive_version(path: Path) -> _ArchiveVersion:
    return _file_version(path), _file_version(Path(f"{path}-wal"))


def _file_version(path: Path) -> _FileVersion:
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


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
        values.get("ActiveState", "unknown"),
        values.get("SubState", "unknown"),
        _int(values.get("MainPID")) or 0,
        _int(values.get("NRestarts")) or 0,
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
            cycle_id=_int(payload.get("cycle_id")),
            maintenance_job_id=_int(payload.get("maintenance_job_id")),
            checkpoint_from=_time(payload.get("checkpoint_from")),
            requests=_int(payload.get("requests")) or 0,
            quotas=_quotas(payload.get("quotas")),
            wait_seconds=_float(payload.get("wait_seconds")),
            detail=_text(payload.get("detail")),
        )
    return None


def _archive_state(path: Path) -> tuple[ArchiveState | None, str | None]:
    if not path.is_file():
        return None, None
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        metadata = dict(connection.execute("SELECT key, value FROM archive_meta"))
        if metadata.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
            raise ValueError(f"unsupported archive schema {metadata.get('schema_version')}")
        git_store = metadata.get("git_store")
        if not isinstance(git_store, str) or not git_store:
            raise ValueError("archive has no Git object store binding")
        cycle = connection.execute(
            """
            SELECT * FROM sync_cycles
            ORDER BY status = 'active' DESC, id DESC
            LIMIT 1
            """,
        ).fetchone()
        cycle_id = None if cycle is None else int(cycle["id"])
        task_counts = _task_counts(connection, cycle_id)
        task_index = {kind: (completed, total) for kind, completed, total in task_counts}
        tasks = (
            sum(completed for _, completed, _ in task_counts),
            sum(total for _, _, total in task_counts),
        )
        parents = task_index.get("parent", (0, 0))
        maintenance = _maintenance_state(connection)
        latest = connection.execute(
            """
            SELECT family, subject_key, observed_until
            FROM fact_observations ORDER BY id DESC LIMIT 1
            """,
        ).fetchone()
        updated = connection.execute(
            """
            SELECT MAX(value) FROM (
                SELECT MAX(published_at) AS value FROM fact_batches
                UNION ALL SELECT MAX(observed_until) FROM discovery_items
                UNION ALL SELECT MAX(completed_at) FROM sync_tasks
                UNION ALL SELECT MAX(completed_at) FROM sync_cycles
                UNION ALL SELECT MAX(started_at) FROM sync_cycles
                UNION ALL SELECT MAX(requested_at) FROM maintenance_jobs
                UNION ALL SELECT MAX(completed_at) FROM maintenance_jobs
                UNION ALL SELECT MAX(last_attempt_from) FROM maintenance_tasks
                UNION ALL SELECT MAX(last_attempt_until) FROM maintenance_tasks
            )
            """,
        ).fetchone()
        error = (
            None
            if cycle_id is None
            else connection.execute(
                """
                SELECT last_error FROM sync_tasks
                WHERE cycle_id = ? AND completed_at IS NULL AND last_error IS NOT NULL
                ORDER BY id LIMIT 1
                """,
                (cycle_id,),
            ).fetchone()
        )
        return (
            ArchiveState(
                git_store=Path(git_store),
                checkpoint=_time(metadata.get("discovery_checkpoint")),
                cycle_id=cycle_id,
                cycle_status=None if cycle is None else str(cycle["status"]),
                cycle_started=None if cycle is None else _time(cycle["started_at"]),
                cycle_completed=None if cycle is None else _time(cycle["completed_at"]),
                discovery_pages=0 if cycle is None else int(cycle["discovery_pages"]),
                discovered_items=0 if cycle is None else int(cycle["discovered_items"]),
                discovery_complete=(False if cycle is None else bool(cycle["discovery_complete"])),
                tasks_completed=tasks[0],
                tasks_total=tasks[1],
                task_counts=task_counts,
                task_rate=_recent_task_rate(connection, cycle_id),
                parents_completed=parents[0],
                parents_total=parents[1],
                observations=int(connection.execute("SELECT COUNT(*) FROM fact_observations").fetchone()[0]),
                current_facts=int(connection.execute("SELECT COUNT(*) FROM fact_heads").fetchone()[0]),
                requests=0 if cycle is None else int(cycle["request_count"]),
                latest=(
                    None
                    if latest is None
                    else (
                        str(latest["family"]),
                        str(latest["subject_key"]),
                        _required_time(latest["observed_until"], "latest observation"),
                    )
                ),
                updated_at=None if updated is None else _time(updated[0]),
                last_error=(
                    maintenance.last_error
                    if maintenance is not None and maintenance.last_error is not None
                    else None if error is None else str(error["last_error"])
                ),
                maintenance=maintenance,
            ),
            None,
        )
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        if connection is not None:
            connection.close()


def _task_counts(
    connection: sqlite3.Connection,
    cycle_id: int | None,
) -> tuple[tuple[str, int, int], ...]:
    if cycle_id is None:
        return ()
    rows = connection.execute(
        """
        SELECT kind, COUNT(completed_at), COUNT(*)
        FROM sync_tasks WHERE cycle_id = ?
        GROUP BY kind ORDER BY kind
        """,
        (cycle_id,),
    )
    return tuple((str(row[0]), int(row[1]), int(row[2])) for row in rows)


def _recent_task_rate(
    connection: sqlite3.Connection,
    cycle_id: int | None,
) -> TaskRate | None:
    if cycle_id is None:
        return None
    rows = connection.execute(
        """
        SELECT completed_at FROM sync_tasks
        WHERE cycle_id = ? AND completed_at IS NOT NULL
        ORDER BY completed_at DESC LIMIT ?
        """,
        (cycle_id, _RECENT_TASK_SAMPLE),
    ).fetchall()
    if len(rows) < 2:
        return None
    observed_until = _required_time(rows[0][0], "recent task completion")
    observed_from = _required_time(rows[-1][0], "recent task completion")
    if observed_from >= observed_until:
        return None
    return TaskRate(len(rows), observed_from, observed_until)


def _maintenance_state(connection: sqlite3.Connection) -> MaintenanceState | None:
    job = connection.execute(
        """
        SELECT * FROM maintenance_jobs
        ORDER BY status = 'active' DESC, id DESC
        LIMIT 1
        """,
    ).fetchone()
    if job is None:
        return None
    job_id = int(job["id"])
    outcomes = tuple(
        (str(row["outcome"]), int(row["count"]))
        for row in connection.execute(
            """
            SELECT outcome, COUNT(*) AS count
            FROM maintenance_tasks
            WHERE job_id = ? AND outcome IS NOT NULL
            GROUP BY outcome ORDER BY outcome
            """,
            (job_id,),
        )
    )
    latest = connection.execute(
        """
        SELECT kind, subject_key,
               COALESCE(last_attempt_until, last_attempt_from, completed_at) AS observed_at
        FROM maintenance_tasks
        WHERE job_id = ? AND COALESCE(last_attempt_until, last_attempt_from, completed_at) IS NOT NULL
        ORDER BY observed_at DESC, id DESC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    error = connection.execute(
        """
        SELECT last_error FROM maintenance_tasks
        WHERE job_id = ? AND completed_at IS NULL AND last_error IS NOT NULL
        ORDER BY COALESCE(last_attempt_until, last_attempt_from) DESC, id DESC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    return MaintenanceState(
        job_id=job_id,
        kind=str(job["kind"]),
        status=str(job["status"]),
        requested_at=_required_time(job["requested_at"], "maintenance request"),
        completed_at=_time(job["completed_at"]),
        tasks_completed=int(job["completed_tasks"]),
        tasks_total=int(job["total_tasks"]),
        outcomes=outcomes,
        latest=(
            None
            if latest is None
            else (
                str(latest["kind"]),
                str(latest["subject_key"]),
                _required_time(latest["observed_at"], "maintenance task"),
            )
        ),
        last_error=None if error is None else str(error["last_error"]),
    )


def _render_table(
    statuses: Sequence[WriterStatus],
    now: datetime | None = None,
    width: int | None = None,
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
            _short_phase(status),
            _short_parents(status.archive),
            _age(_updated_at(status), observed_at),
        )
        for status in statuses
    ]
    table = (headers, *rows)
    widths = _overview_widths(table, _terminal_width() if width is None else width)
    return "\n".join(_overview_line(row, widths) for row in table)


def _render_detail(
    status: WriterStatus,
    now: datetime | None = None,
    zone: tzinfo | None = None,
) -> str:
    observed_at = datetime.now(UTC) if now is None else now.astimezone(UTC)
    progress = status.progress
    archive = status.archive
    rows = [
        ("WRITER", status.writer.identity[:12]),
        ("STATE", _service_detail(status.service)),
        ("REPOSITORY", status.writer.repository),
        ("DATABASE", str(status.writer.database)),
        (
            "GIT STORE",
            str(Path(f"{status.writer.database}.git") if archive is None else archive.git_store),
        ),
        ("PID", str(status.service.pid) if status.service.pid else "-"),
        ("RESTARTS", str(status.service.restarts)),
        ("CYCLE", _cycle(archive, zone)),
        ("CHECKPOINT", _local_optional(None if archive is None else archive.checkpoint, zone)),
        ("MAINT", _maintenance(archive, zone)),
        ("M TASKS", _maintenance_tasks(archive, 20)),
        ("M OUTCOMES", _maintenance_outcomes(archive)),
        ("M LATEST", _maintenance_latest(archive, zone)),
        ("PHASE", _phase(status)),
        ("DISCOVERY", _discovery(archive)),
        ("PARENTS", _parents(archive, 20)),
        ("TASKS", _tasks(archive, 20)),
        ("GIT TASKS", _git_tasks(archive)),
        ("RATE", _task_rate(archive, observed_at)),
        ("FACTS", _facts(archive)),
        ("LATEST", _latest(archive, zone)),
        ("QUOTA", _quota(progress, zone)),
        ("UPDATED", _updated(status, observed_at, zone)),
    ]
    if progress is not None and progress.wait_seconds is not None:
        rows.append(("WAIT", _wait(progress, observed_at)))
    detail = None if progress is None else progress.detail
    if detail is None and archive is not None:
        detail = archive.last_error
    if detail is not None:
        rows.append(("DETAIL", detail))
    if status.archive_error is not None:
        rows.append(("ARCHIVE", status.archive_error))
    width = max(len(key) for key, _ in rows)
    lines = []
    for key, value in rows:
        parts = value.splitlines() or [""]
        lines.append(f"{key:<{width}}  {parts[0]}")
        lines.extend(f"{'':<{width}}  {part}" for part in parts[1:])
    return "\n".join(lines)


def _overview_widths(rows: Sequence[Sequence[str]], limit: int) -> tuple[int, ...]:
    widths = list(_OVERVIEW_MIN_WIDTHS)
    natural = tuple(max(len(row[index]) for row in rows) for index in range(len(widths)))
    remaining = max(limit - sum(widths) - len(widths) + 1, 0)
    while remaining:
        expanded = False
        for index in _OVERVIEW_FLEX_COLUMNS:
            if widths[index] >= natural[index]:
                continue
            widths[index] += 1
            remaining -= 1
            expanded = True
            if not remaining:
                break
        if not expanded:
            break
    return tuple(widths)


def _terminal_width() -> int:
    return shutil.get_terminal_size(fallback=(80, 24)).columns - 1


def _overview_line(values: Sequence[str], widths: Sequence[int]) -> str:
    cells = (f"{_fit(value, width):<{width}}" for value, width in zip(values, widths, strict=True))
    return " ".join(cells).rstrip()


def _fit(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    left = (width - 1) // 2
    return f"{value[:left]}…{value[-(width - left - 1) :]}"


def _short_service(service: ServiceState) -> str:
    return {
        "active": "up",
        "activating": "boot",
        "deactivating": "stop",
        "failed": "fail",
        "inactive": "down",
        "unknown": "?",
    }.get(service.active, service.active)


def _short_phase(status: WriterStatus) -> str:
    phase = _phase(status)
    return {
        "discovering": "discover",
        "rate_limit": "quota",
        "retry_wait": "retry",
        "syncing_git": "git",
    }.get(phase, phase)


def _short_parents(archive: ArchiveState | None) -> str:
    if archive is None:
        return "-"
    if archive.maintenance is not None and archive.maintenance.status == "active":
        return (
            f"{_short_count(archive.maintenance.tasks_completed)}/"
            f"{_short_count(archive.maintenance.tasks_total)}"
        )
    return f"{_short_count(archive.parents_completed)}/{_short_count(archive.parents_total)}"


def _short_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    scale, suffix = (1_000_000, "m") if value >= 1_000_000 else (1_000, "k")
    scaled = value / scale
    return f"{scaled:.1f}{suffix}" if scaled < 99.95 else f"{scaled:.0f}{suffix}"


def _service_detail(service: ServiceState) -> str:
    label = service.sub if service.active == "active" else service.active
    return f"{label} (active={service.active}, sub={service.sub})"


def _phase(status: WriterStatus) -> str:
    maintenance = None if status.archive is None else status.archive.maintenance
    if (
        maintenance is not None
        and maintenance.status == "active"
        and (
            status.progress is None
            or status.progress.maintenance_job_id != maintenance.job_id
        )
    ):
        return maintenance.kind
    if status.progress is not None:
        return status.progress.phase
    if status.archive is not None and status.archive.cycle_status == "active":
        return "recoverable"
    return "-"


def _cycle(archive: ArchiveState | None, zone: tzinfo | None) -> str:
    if archive is None or archive.cycle_id is None:
        return "-"
    started = _local_optional(archive.cycle_started, zone)
    return f"{archive.cycle_id} {archive.cycle_status} since {started}"


def _maintenance(archive: ArchiveState | None, zone: tzinfo | None) -> str:
    if archive is None or archive.maintenance is None:
        return "-"
    job = archive.maintenance
    requested = _local_time(job.requested_at, zone)
    return f"{job.job_id} {job.kind} {job.status} since {requested}"


def _maintenance_tasks(archive: ArchiveState | None, width: int) -> str:
    if archive is None or archive.maintenance is None:
        return "-"
    job = archive.maintenance
    return _meter("tasks", job.tasks_completed, job.tasks_total, width)


def _maintenance_outcomes(archive: ArchiveState | None) -> str:
    if archive is None or archive.maintenance is None:
        return "-"
    outcomes = archive.maintenance.outcomes
    return "pending" if not outcomes else " ".join(f"{key}={value:,}" for key, value in outcomes)


def _maintenance_latest(archive: ArchiveState | None, zone: tzinfo | None) -> str:
    if archive is None or archive.maintenance is None or archive.maintenance.latest is None:
        return "-"
    kind, subject, observed = archive.maintenance.latest
    return f"{kind} {subject} at {_local_time(observed, zone)}"


def _discovery(archive: ArchiveState | None) -> str:
    if archive is None:
        return "-"
    state = "complete" if archive.discovery_complete else "scanning"
    return f"{state} pages={archive.discovery_pages:,} items={archive.discovered_items:,}"


def _parents(archive: ArchiveState | None, width: int) -> str:
    if archive is None:
        return "-"
    return _meter("parents", archive.parents_completed, archive.parents_total, width)


def _tasks(archive: ArchiveState | None, width: int) -> str:
    if archive is None:
        return "-"
    return _meter("tasks", archive.tasks_completed, archive.tasks_total, width)


def _git_tasks(archive: ArchiveState | None) -> str:
    if archive is None:
        return "-"
    counts = {kind: (completed, total) for kind, completed, total in archive.task_counts}
    commits = counts.get("commit-object", (0, 0))
    pulls = counts.get("pull-git", (0, 0))
    return (
        f"commits={commits[0]:,}/{commits[1]:,} "
        f"pulls={pulls[0]:,}/{pulls[1]:,}"
    )


def _task_rate(archive: ArchiveState | None, now: datetime) -> str:
    if archive is None or archive.task_rate is None:
        return "-"
    sample = archive.task_rate
    seconds = (sample.observed_until - sample.observed_from).total_seconds()
    per_hour = (sample.completions - 1) * 3_600 / seconds
    age = _age(sample.observed_until, now)
    return f"recent {per_hour:,.0f} tasks/h over {_duration(seconds)}; last {age} ago"


def _facts(archive: ArchiveState | None) -> str:
    if archive is None:
        return "-"
    return f"current={archive.current_facts:,} observations={archive.observations:,}"


def _latest(archive: ArchiveState | None, zone: tzinfo | None) -> str:
    if archive is None or archive.latest is None:
        return "-"
    family, subject, observed = archive.latest
    return f"{family} {subject} at {_local_time(observed, zone)}"


def _quota(progress: ProgressState | None, zone: tzinfo | None) -> str:
    if progress is None or not progress.quotas:
        return "-"
    width = max(len(quota.resource) for quota in progress.quotas)
    return "\n".join(_quota_item(quota, width, zone) for quota in progress.quotas)


def _quota_item(quota: RateQuota, width: int, zone: tzinfo | None) -> str:
    remaining = "?" if quota.remaining is None else f"{quota.remaining:,}"
    limit = "?" if quota.limit is None else f"{quota.limit:,}"
    reset = "?" if quota.reset_at is None else _local_time(quota.reset_at, zone)
    return f"{quota.resource:<{width}}  {remaining}/{limit}  reset {reset}"


def _meter(label: str, completed: int, total: int, width: int) -> str:
    filled = width if total == 0 else min(width, int(width * completed / total))
    return f"{label} [{'#' * filled}{'-' * (width - filled)}] {completed:,}/{total:,}"


def _updated(
    status: WriterStatus,
    now: datetime,
    zone: tzinfo | None,
) -> str:
    value = _updated_at(status)
    if value is None:
        return "-"
    return f"{_local_time(value, zone)}; {_age(value, now)} ago"


def _updated_at(status: WriterStatus) -> datetime | None:
    values = [
        value
        for value in (
            None if status.progress is None else status.progress.event_at,
            None if status.archive is None else status.archive.updated_at,
        )
        if value is not None
    ]
    return max(values, default=None)


def _wait(progress: ProgressState, now: datetime) -> str:
    elapsed = (
        0.0
        if progress.event_at is None
        else max((now - progress.event_at).total_seconds(), 0.0)
    )
    return f"{_duration(max((progress.wait_seconds or 0.0) - elapsed, 0.0))} remaining"


def _duration(seconds: float) -> str:
    rounded = int(seconds)
    if rounded < 60:
        return f"{rounded}s"
    if rounded < 3_600:
        return f"{rounded // 60}m{rounded % 60:02d}s"
    return f"{rounded // 3_600}h{rounded % 3_600 // 60:02d}m"


def _local_optional(value: datetime | None, zone: tzinfo | None) -> str:
    return "-" if value is None else _local_time(value, zone)


def _local_time(value: datetime, zone: tzinfo | None) -> str:
    local = value.astimezone(zone)
    clock = local.strftime("%H:%M:%S")
    if local.microsecond:
        clock += f".{local.microsecond:06d}"
    name = local.tzname() or local.strftime("%z")
    return f"{_WEEKDAYS[local.weekday()]} {local:%Y-%m-%d} {clock} {name}"


def _age(event_at: datetime | None, now: datetime) -> str:
    if event_at is None:
        return "?"
    seconds = max(int((now - event_at).total_seconds()), 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3_600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3_600}h"
    return f"{seconds // 86_400}d"


def _required_time(value: object, context: str) -> datetime:
    parsed = _time(value)
    if parsed is None:
        raise ValueError(f"{context} is invalid")
    return parsed


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
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
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        resource = _text(item.get("resource"))
        if resource is not None:
            result.append(
                RateQuota(
                    resource,
                    _int(item.get("limit")),
                    _int(item.get("remaining")),
                    _time(item.get("reset_at")),
                ),
            )
    return tuple(result)


def _watch(
    writers: Sequence[ManagedWriter],
    systemctl: str,
    journalctl: str,
    interval: float,
    selected: bool,
) -> int:
    cache: dict[Path, _ArchiveCacheEntry] = {}
    deadline = time.monotonic()
    label = f" {writers[0].identity[:12]}" if selected else ""
    try:
        while True:
            statuses = _collect(writers, systemctl, journalctl, cache)
            content = _render_detail(statuses[0]) if selected else _render_table(statuses)
            observed_at = datetime.now().astimezone()
            header = (
                f"Every {interval:.1f}s: gh-puller status{label}  "
                f"{os.uname().nodename}: {observed_at:%a %Y-%m-%d %H:%M:%S %Z}"
            )
            prefix = "\x1b[H\x1b[2J" if sys.stdout.isatty() else ""
            print(f"{prefix}{header}\n\n{content}", flush=True)
            deadline += interval
            delay = deadline - time.monotonic()
            if delay <= 0:
                deadline = time.monotonic()
                continue
            time.sleep(delay)
    except KeyboardInterrupt:
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Render one or every managed database writer.

    Args:
        argv: Monitor arguments without the program name.

    Returns:
        Zero on success or two when an explicit selector has no writer.
    """
    args = _parser().parse_args(argv)
    database = None if args.database is None else args.database.resolve()
    writers = _managed_writers(args.systemd_dir, database, args.writer_id)
    selected = database is not None or args.writer_id is not None
    if selected and not writers:
        selector = f"ID: {args.writer_id}" if args.writer_id else f"database: {database}"
        print(f"No managed writer for {selector}", file=sys.stderr)
        return 2
    if args.watch:
        return _watch(writers, args.systemctl, args.journalctl, args.interval, selected)
    statuses = _collect(writers, args.systemctl, args.journalctl)
    print(_render_detail(statuses[0]) if selected else _render_table(statuses))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
