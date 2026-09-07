"""Test read-only rendering of managed observation-writer progress."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from gh_puller.github import monitor
from gh_puller.github.observations import (
    Coverage,
    DiscoveryItemDraft,
    FactDraft,
    ObservationArchive,
    Origin,
    TaskDraft,
)
from gh_puller.github.progress import RateQuota

_EVENT_AT = datetime(2026, 9, 5, 10, tzinfo=UTC)
_LOCAL = timezone(timedelta(hours=8), "CST")


def _writer(database: Path, repository: str = "acme/widgets") -> monitor.ManagedWriter:
    resolved = database.resolve()
    identity = hashlib.sha256(os.fsencode(resolved)).hexdigest()
    return monitor.ManagedWriter(
        f"gh-puller-{identity[:12]}.service",
        identity,
        repository,
        resolved,
    )


def _progress(**changes: object) -> monitor.ProgressState:
    values = {
        "event_at": _EVENT_AT,
        "phase": "fetching",
        "cycle_id": 3,
        "maintenance_job_id": None,
        "checkpoint_from": _EVENT_AT - timedelta(hours=1),
        "requests": 41,
        "quotas": (
            RateQuota("core", 5_000, 4_200, _EVENT_AT + timedelta(hours=1)),
            RateQuota("graphql", 5_000, 4_800, _EVENT_AT + timedelta(minutes=30)),
        ),
        "wait_seconds": None,
        "detail": None,
    }
    values.update(changes)
    return monitor.ProgressState(**values)


def _archive(database: Path) -> monitor.ArchiveState:
    return monitor.ArchiveState(
        git_store=Path(f"{database}.git").resolve(),
        checkpoint=_EVENT_AT - timedelta(hours=1),
        cycle_id=3,
        cycle_status="active",
        cycle_started=_EVENT_AT,
        cycle_completed=None,
        discovery_pages=12,
        discovered_items=1_200,
        discovery_complete=False,
        tasks_completed=700,
        tasks_total=1_202,
        task_counts=(
            ("commit-object", 0, 1),
            ("parent", 699, 1_200),
            ("pull-git", 1, 1),
        ),
        task_rate=monitor.TaskRate(
            501,
            _EVENT_AT - timedelta(minutes=5),
            _EVENT_AT,
        ),
        parents_completed=699,
        parents_total=1_200,
        observations=8_500,
        current_facts=7_900,
        requests=40,
        latest=("issue-comments", "issue:42", _EVENT_AT),
        updated_at=_EVENT_AT,
        last_error=None,
        maintenance=None,
    )


def _status(database: Path) -> monitor.WriterStatus:
    return monitor.WriterStatus(
        _writer(database),
        monitor.ServiceState("active", "running", 1234, 0),
        _progress(),
        _archive(database),
    )


def test_table_is_a_compact_writer_overview(tmp_path: Path) -> None:
    status = _status(tmp_path / "facts.sqlite3")

    output = monitor._render_table([status], now=_EVENT_AT + timedelta(minutes=5))

    lines = output.splitlines()
    assert lines[0].split() == ["ID", "ST", "REPO", "DB", "PHASE", "DONE", "UPDATED"]
    assert "up" in lines[1]
    assert "acme/widgets" in lines[1]
    assert "facts.sqlite3" in lines[1]
    assert "699/1.2k" in lines[1]
    assert lines[1].endswith("5m")
    assert max(map(len, lines)) <= 79
    for omitted in ("GIT", "CYCLE", "CHECKPOINT", "QUOTA", "REQUESTS"):
        assert omitted not in lines[0]


def test_detail_combines_durable_work_and_disposable_quota(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"

    output = monitor._render_detail(
        _status(database),
        now=_EVENT_AT + timedelta(seconds=3),
        zone=_LOCAL,
    )

    assert "STATE       running (active=active, sub=running)" in output
    assert f"DATABASE    {database.resolve()}" in output
    assert f"GIT STORE   {database.resolve()}.git" in output
    assert "CYCLE       3 active since Sat 2026-09-05 18:00:00 CST" in output
    assert "DISCOVERY   scanning pages=12 items=1,200" in output
    assert "PARENTS     parents [###########---------] 699/1,200" in output
    assert "TASKS       tasks [###########---------] 700/1,202" in output
    assert "GIT TASKS   commits=0/1 pulls=1/1" in output
    assert "RATE        recent 6,000 tasks/h over 5m00s; last 3s ago" in output
    assert "FACTS       current=7,900 observations=8,500" in output
    assert "LATEST      issue-comments issue:42" in output
    assert (
        "QUOTA       core     4,200/5,000  reset Sat 2026-09-05 19:00:00 CST\n"
        "            graphql  4,800/5,000  reset Sat 2026-09-05 18:30:00 CST"
    ) in output
    assert "UPDATED     Sat 2026-09-05 18:00:00 CST; 3s ago" in output
    assert "TARGET" not in output
    assert "RUN" not in output


def test_latest_progress_ignores_unrelated_journal_lines() -> None:
    event = {
        "type": "github_sync_progress",
        "event_at": "2026-09-05T10:00:00Z",
        "phase": "rate_limit",
        "cycle_id": 3,
        "checkpoint_from": "2026-09-05T09:00:00Z",
        "requests": 41,
        "wait_seconds": 30,
        "detail": "core_rate_limit",
        "quotas": [
            {
                "resource": "core",
                "limit": 5_000,
                "remaining": 0,
                "reset_at": "2026-09-05T11:00:00Z",
            },
        ],
    }

    progress = monitor._latest_progress(
        "\n".join((json.dumps({"type": "old"}), "traceback", json.dumps(event))),
    )

    assert progress is not None
    assert progress.phase == "rate_limit"
    assert progress.requests == 41
    assert progress.quotas == (
        RateQuota("core", 5_000, 0, _EVENT_AT + timedelta(hours=1)),
    )


@pytest.mark.asyncio
async def test_archive_progress_is_recovered_without_journal(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    git_store = tmp_path / "objects.git"
    summary = {
        "id": 10,
        "number": 1,
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-05T09:00:00Z",
    }
    async with ObservationArchive(database, "acme/widgets", git_store) as archive:
        cycle = await archive.start_cycle(_EVENT_AT)
        cursor = await archive.begin_discovery(cycle.id, "/page/1")
        assert cursor == "/page/1"
        await archive.save_discovery_page(
            cycle.id,
            cursor,
            None,
            (
                DiscoveryItemDraft(1, "issue", _EVENT_AT, _EVENT_AT, summary),
                DiscoveryItemDraft(2, "issue", _EVENT_AT, _EVENT_AT, summary | {"id": 20, "number": 2}),
            ),
            (
                TaskDraft("parent:1", "parent", "issue:1", {"number": 1}, 1),
                TaskDraft("parent:2", "parent", "issue:2", {"number": 2}, 2),
            ),
        )
        first, second = await archive.take_tasks(cycle.id, 2)
        await archive.publish(
            "sync:test:1",
            "sync",
            _EVENT_AT,
            (
                FactDraft(
                    "issue",
                    "issue:1",
                    _EVENT_AT,
                    _EVENT_AT,
                    Coverage.COMPLETE,
                    Origin.API,
                    {"value": summary},
                    resource_number=1,
                ),
            ),
            cycle_id=cycle.id,
            task_id=first.id,
        )
        await archive.record_task_error(second.id, "GitHubAPIError: transient")

    state, error = monitor._archive_state(database)

    assert error is None
    assert state is not None
    assert state.git_store == git_store.resolve()
    assert state.discovery_complete
    assert (state.parents_completed, state.parents_total) == (1, 2)
    assert state.task_counts == (("parent", 1, 2),)
    assert state.task_rate is None
    assert (state.observations, state.current_facts) == (1, 1)
    assert state.last_error == "GitHubAPIError: transient"
    assert state.latest is not None and state.latest[:2] == ("issue", "issue:1")


@pytest.mark.asyncio
async def test_detail_recovers_active_maintenance_progress_from_sqlite(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    async with ObservationArchive(database, "acme/widgets") as archive:
        job = await archive.start_maintenance_job(
            "backfill:test",
            "backfill",
            _EVENT_AT,
            {"operation": "test"},
            (
                TaskDraft(
                    "batch:1",
                    "commit-object-batch",
                    "commits:x",
                    {"source_observation_cutoff": 0, "shas": ["a" * 40]},
                ),
            ),
        )
        (task,) = await archive.take_maintenance_tasks(job.id, 1, _EVENT_AT)
        await archive.record_maintenance_task_error(
            task.id,
            _EVENT_AT + timedelta(seconds=2),
            "GitStoreError: disconnected",
        )

    archive, error = monitor._archive_state(database)
    assert error is None
    assert archive is not None and archive.maintenance is not None
    assert archive.maintenance.kind == "backfill"
    assert archive.maintenance.last_error == "GitStoreError: disconnected"
    status = monitor.WriterStatus(
        _writer(database),
        monitor.ServiceState("inactive", "dead", 0, 0),
        None,
        archive,
    )

    output = monitor._render_detail(status, now=_EVENT_AT + timedelta(seconds=3), zone=_LOCAL)

    assert "MAINT       1 backfill active" in output
    assert "M TASKS     tasks [--------------------] 0/1" in output
    assert "M OUTCOMES  pending" in output
    assert "PHASE       backfill" in output
    assert "DETAIL      GitStoreError: disconnected" in output


def test_managed_writers_accept_database_or_short_identity(tmp_path: Path) -> None:
    units = tmp_path / "units"
    database = tmp_path / "facts.sqlite3"
    writer = _writer(database)
    units.mkdir()
    (units / writer.unit).write_text(
        f"# gh-puller-repository={writer.repository}\n"
        f"# gh-puller-database={writer.database}\n",
    )

    assert monitor._managed_writers(units, database)[0].database == database.resolve()
    assert monitor._managed_writers(units, writer_id=writer.identity[:12])[0] == writer
