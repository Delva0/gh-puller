"""Test disposable JSON and terminal sync progress."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from io import StringIO

from gh_puller.github.progress import (
    APIProgress,
    ConsoleProgress,
    RateQuota,
    SyncProgress,
    _SyncProgressTracker,
)

_T0 = datetime(2026, 9, 5, 10, tzinfo=UTC)


def test_console_progress_emits_new_journal_contract_without_snapshot_fields() -> None:
    stream = StringIO()
    progress = SyncProgress(
        event_at=_T0,
        phase="rate_limit",
        cycle_id=2,
        checkpoint_from=_T0 - timedelta(hours=1),
        requests=50,
        quotas=(RateQuota("core", 5_000, 0, _T0 + timedelta(minutes=5)),),
        wait_seconds=300,
        detail="core_rate_limit",
    )

    ConsoleProgress(stream, tty=False)(progress)

    payload = json.loads(stream.getvalue())
    assert payload["type"] == "github_sync_progress"
    assert payload["cycle_id"] == 2
    assert payload["checkpoint_from"] == "2026-09-05T09:00:00Z"
    assert payload["quotas"][0]["resource"] == "core"
    assert "target_at" not in payload
    assert "run_id" not in payload


def test_tracker_combines_resumed_and_current_process_requests() -> None:
    events = []
    tracker = _SyncProgressTracker(events.append, lambda: _T0)
    tracker.start()
    tracker.bind_cycle(4, _T0 - timedelta(hours=1), 70, 10)

    tracker.api_progress(
        APIProgress(
            request_count=13,
            quotas=(RateQuota("graphql", 5_000, 4_900, _T0 + timedelta(hours=1)),),
        ),
    )
    tracker.done(73)

    assert events[-2].requests == 73
    assert events[-1].phase == "idle"
    assert events[-1].requests == 73


def test_tracker_distinguishes_maintenance_from_sync_cycles() -> None:
    events = []
    tracker = _SyncProgressTracker(events.append, lambda: _T0)

    tracker.bind_maintenance(9, 4, 0)

    assert events[-1].maintenance_job_id == 9
    assert events[-1].cycle_id is None
    assert events[-1].checkpoint_from is None


def test_tty_progress_finishes_idle_and_error_lines() -> None:
    stream = StringIO()
    console = ConsoleProgress(stream, tty=True, interval=60)

    console(SyncProgress(_T0, "starting"))
    console(SyncProgress(_T0, "idle", cycle_id=1))

    assert "starting" in stream.getvalue()
    assert "idle" in stream.getvalue()
    assert stream.getvalue().endswith("\n")
