"""Verify read-only rendering of managed GitHub writer progress."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from gh_puller.github import monitor

if TYPE_CHECKING:
    from pathlib import Path

_EVENT_AT = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


def _writer(database: Path, repository: str = "acme/widgets") -> monitor.ManagedWriter:
    resolved = database.resolve()
    identity = hashlib.sha256(os.fsencode(resolved)).hexdigest()
    return monitor.ManagedWriter(
        unit=f"gh-puller-{identity}.service",
        identity=identity,
        repository=repository,
        database=resolved,
    )


def _progress(**changes: object) -> monitor.ProgressState:
    state = monitor.ProgressState(
        event_at=_EVENT_AT,
        phase="closing_bundles",
        target_at="2026-09-02T05:37:49.940630Z",
        run_id=1,
        catalog_seen=54_083,
        catalog_total=54_083,
        bundles_completed=224,
        bundles_total=51_549,
        issues_completed=117,
        pulls_completed=107,
        tombstones=0,
        latest_number=2886,
        latest_kind="pull",
        wait_seconds=None,
        detail=None,
    )
    return replace(state, **changes)


def _status(database: Path, progress: monitor.ProgressState | None = None) -> monitor.WriterStatus:
    return monitor.WriterStatus(
        writer=_writer(database),
        service=monitor.ServiceState(active="active", sub="running", pid=1234, restarts=0),
        progress=_progress() if progress is None else progress,
    )


def _write_unit(database: Path, units: Path, repository: str = "acme/widgets") -> Path:
    writer = _writer(database, repository)
    units.mkdir(exist_ok=True)
    path = units / writer.unit
    path.write_text(
        f"# gh-puller-repository={repository}\n# gh-puller-database={database.resolve()}\n[Unit]\n",
    )
    return path


def test_table_prioritizes_target_items_progress_and_database(tmp_path: Path) -> None:
    progress = _progress(
        phase="rate_limit",
        wait_seconds=600.0,
        detail="primary_rate_limit",
    )

    output = monitor._render_table(
        [_status(tmp_path / "facts.sqlite3", progress)],
        now=_EVENT_AT + timedelta(minutes=5),
    )

    assert "TARGET T" in output
    assert "ITEMS" in output
    assert "54,083" in output
    assert "2026-09-02T05:37:49.940630Z" in output
    assert "bundles [----------] 224/51,549" in output
    assert "5m00s remaining" in output
    assert str((tmp_path / "facts.sqlite3").resolve()) in output
    assert "REQUEST" not in output
    assert "QUOTA" not in output


def test_table_output_is_stable_for_external_watch(tmp_path: Path) -> None:
    statuses = [_status(tmp_path / "facts.sqlite3")]

    first = monitor._render_table(statuses, now=_EVENT_AT + timedelta(seconds=5))
    second = monitor._render_table(statuses, now=_EVENT_AT + timedelta(seconds=5))

    assert first == second
    assert "\x1b" not in first


def test_detail_uses_honest_unknown_catalog_denominator(tmp_path: Path) -> None:
    progress = _progress(
        phase="closing_catalog",
        catalog_seen=27_400,
        catalog_total=None,
        bundles_completed=0,
        bundles_total=None,
        issues_completed=0,
        pulls_completed=0,
        latest_number=None,
        latest_kind=None,
    )

    output = monitor._render_detail(
        _status(tmp_path / "facts.sqlite3", progress),
        now=_EVENT_AT,
    )

    assert "ITEMS       ?" in output
    assert "PROGRESS    catalog 27,400/?" in output
    assert "DATABASE" in output
    assert str((tmp_path / "facts.sqlite3").resolve()) in output


def test_detail_shows_process_and_pass_local_object_progress(tmp_path: Path) -> None:
    output = monitor._render_detail(
        _status(tmp_path / "facts.sqlite3"),
        now=_EVENT_AT + timedelta(seconds=3),
    )

    assert "STATE       running (active=active, sub=running)" in output
    assert "PID         1234" in output
    assert "TARGET T    2026-09-02T05:37:49.940630Z" in output
    assert "ITEMS       54,083" in output
    assert "STAGED      issues=117 pulls=107 tombstones=0" in output
    assert "LATEST      pull#2886" in output
    assert "UPDATED     3s ago" in output
    assert "PASS" not in output
    assert "SERIES" not in output


def test_latest_progress_ignores_raw_logs_and_request_fields() -> None:
    old = {
        "type": "github_pull_progress",
        "event_at": "2026-09-02T09:59:00Z",
        "phase": "starting",
        "target_at": "2026-09-02T05:37:49Z",
        "run_id": 1,
    }
    latest = {
        "type": "github_pull_progress",
        "event_at": "2026-09-02T10:00:00Z",
        "phase": "closing_catalog",
        "target_at": "2026-09-02T05:37:49Z",
        "run_id": 1,
        "catalog_seen": 200,
        "requests": 24_946,
        "quota_remaining": 4_498,
    }
    output = "\n".join((json.dumps(old), "Traceback: diagnostic", json.dumps(latest)))

    progress = monitor._latest_progress(output)

    assert progress is not None
    assert progress.phase == "closing_catalog"
    assert progress.catalog_seen == 200
    assert not hasattr(progress, "requests")
    assert not hasattr(progress, "quota_remaining")


def test_managed_writers_require_matching_path_identity(tmp_path: Path) -> None:
    units = tmp_path / "units"
    first = tmp_path / "a.sqlite3"
    second = tmp_path / "b.sqlite3"
    _write_unit(first, units)
    _write_unit(second, units, "acme/other")
    (units / f"gh-puller-{'0' * 64}.service").write_text(
        f"# gh-puller-repository=acme/invalid\n# gh-puller-database={first.resolve()}\n",
    )
    (units / "gh-puller-acme-legacy.service").write_text("[Unit]\n")

    writers = monitor._managed_writers(units)
    selected = monitor._managed_writers(units, second)

    assert {writer.database for writer in writers} == {first.resolve(), second.resolve()}
    assert [writer.database for writer in selected] == [second.resolve()]


def test_collection_reads_systemd_and_latest_journal_event(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    writer = _writer(tmp_path / "facts.sqlite3")
    commands: list[list[str]] = []
    event = {
        "type": "github_pull_progress",
        "event_at": "2026-09-02T10:00:00Z",
        "phase": "closing_bundles",
        "target_at": "2026-09-02T05:37:49Z",
        "run_id": 7,
        "catalog_total": 100,
        "bundles_completed": 3,
        "bundles_total": 10,
    }

    def output(command: list[str]) -> str:
        commands.append(command)
        if command[0] == "systemctl-test":
            return "ActiveState=active\nSubState=running\nMainPID=42\nNRestarts=2\n"
        return json.dumps(event)

    monkeypatch.setattr(monitor, "_output", output)

    statuses = monitor._collect([writer], "systemctl-test", "journalctl-test")

    assert statuses[0].service == monitor.ServiceState("active", "running", 42, 2)
    assert statuses[0].progress is not None
    assert statuses[0].progress.run_id == 7
    assert commands[0][:3] == ["systemctl-test", "show", writer.unit]
    assert commands[1][:3] == ["journalctl-test", "--unit", writer.unit]
