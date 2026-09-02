"""GitHub CLI 的固定间隔调度、恢复、输出与信号退出测试。"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from gh_puller.github import PullResult
from gh_puller.github import __main__ as cli
from gh_puller.github.store import SQLiteArchive, schedule_state

if TYPE_CHECKING:
    from pathlib import Path

_T0 = datetime(2026, 9, 2, 12, tzinfo=UTC)


@dataclass
class StubPuller:
    targets: list[datetime] = field(default_factory=list)

    async def pull(self, target: datetime | None = None) -> PullResult:
        assert target is not None
        self.targets.append(target)
        return PullResult(
            target_at=target,
            completed_at=target,
            run_id=len(self.targets),
            changed_items=0,
            catalog_items=10,
            requests=4,
        )


def _iso(value: datetime) -> str:
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.astimezone(UTC).isoformat(timespec=timespec).replace("+00:00", "Z")


async def _seed_run(
    path: Path,
    target: datetime,
    *,
    committed: bool,
) -> None:
    async with SQLiteArchive(path, "acme/widgets") as archive:
        run = await archive.start_run(_iso(target), _iso(target))
        if committed:
            await archive.update_observed(run.id, _iso(target))
            await archive.finalize(run.id, _iso(target))


def test_parser_builds_once_config_and_normalizes_target() -> None:
    args = cli._parser().parse_args(
        [
            "once",
            "acme/widgets",
            "/tmp/widgets",
            "--target",
            "2026-09-02T20:30:00+08:00",
            "--concurrency",
            "8",
            "--catalog-mode",
            "exhaustive",
            "--no-progress",
        ],
    )

    config = cli._config(args)
    assert args.target == _T0 + timedelta(minutes=30)
    assert config.repository == "acme/widgets"
    assert config.concurrency == 8
    assert config.catalog_mode == "exhaustive"
    assert args.no_progress is True


def test_parser_rejects_naive_target() -> None:
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            ["once", "acme/widgets", "/tmp/widgets", "--target", "2026-09-02T12:00:00"],
        )


def test_parser_accepts_configurable_schedule_interval() -> None:
    args = cli._parser().parse_args(
        ["schedule", "acme/widgets", "/tmp/widgets", "--interval", "90m"],
    )

    assert args.interval == timedelta(minutes=90)


@pytest.mark.parametrize("value", ["0s", "1.5h", "hour", "-1h"])
def test_parser_rejects_invalid_schedule_interval(value: str) -> None:
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            ["schedule", "acme/widgets", "/tmp/widgets", "--interval", value],
        )


@pytest.mark.asyncio
async def test_schedule_starts_at_latest_due_boundary(tmp_path: Path) -> None:
    puller = StubPuller()
    emitted: list[PullResult] = []

    await cli._run_schedule(
        puller,
        tmp_path / "new-archive",
        timedelta(minutes=30),
        now=lambda: _T0 + timedelta(minutes=37),
        emit=emitted.append,
        max_runs=4,
    )

    assert puller.targets == [_T0 + timedelta(minutes=30 * offset) for offset in range(1, 5)]
    assert [result.target_at for result in emitted] == puller.targets


@pytest.mark.asyncio
async def test_schedule_restart_uses_committed_then_retries_pending_run(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    await _seed_run(archive, _T0, committed=True)
    pending = _T0 + timedelta(hours=1)
    await _seed_run(archive, pending, committed=False)

    puller = StubPuller()
    await cli._run_schedule(
        puller,
        archive,
        timedelta(hours=1),
        now=lambda: _T0 + timedelta(hours=2),
        emit=lambda _: None,
        max_runs=2,
    )
    assert puller.targets == [pending, _T0 + timedelta(hours=2)]


@pytest.mark.asyncio
async def test_schedule_coalesces_missed_targets_to_latest_boundary(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    await _seed_run(archive, _T0, committed=True)

    puller = StubPuller()
    await cli._run_schedule(
        puller,
        archive,
        timedelta(hours=1),
        now=lambda: _T0 + timedelta(hours=3, minutes=37),
        emit=lambda _: None,
        max_runs=1,
    )

    assert puller.targets == [_T0 + timedelta(hours=3)]


@pytest.mark.asyncio
async def test_schedule_state_uses_greatest_target_not_latest_run_id(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    greatest = _T0 + timedelta(microseconds=900)
    await _seed_run(archive, greatest, committed=True)
    await _seed_run(archive, _T0 + timedelta(microseconds=100), committed=True)

    state = await schedule_state(archive)

    assert state.committed_target == _iso(greatest)


def test_emit_writes_machine_readable_result(capsys: pytest.CaptureFixture[str]) -> None:
    result = PullResult(
        target_at=_T0,
        completed_at=_T0 + timedelta(seconds=3),
        run_id=42,
        changed_items=2,
        catalog_items=11,
        requests=7,
    )

    cli._emit(result)

    assert json.loads(capsys.readouterr().out) == {
        "catalog_items": 11,
        "changed_items": 2,
        "completed_at": "2026-09-02T12:00:03Z",
        "lag_seconds": 3.0,
        "requests": 7,
        "run_id": 42,
        "target_at": "2026-09-02T12:00:00Z",
    }


def test_schedule_process_lock_rejects_duplicate_scheduler(tmp_path: Path) -> None:
    destination = tmp_path / "archive"

    with (
        cli._schedule_lock(destination),
        pytest.raises(RuntimeError, match="already runs"),
        cli._schedule_lock(destination),
    ):
        pytest.fail("duplicate scheduler acquired the lock")


@pytest.mark.asyncio
async def test_sigterm_cancels_active_command_and_returns_143(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def dispatch(_: argparse.Namespace) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    real_loop = asyncio.get_running_loop()

    class LoopProxy:
        def __init__(self) -> None:
            self.handlers: dict[signal.Signals, tuple[Any, tuple[Any, ...]]] = {}
            self.removed: list[signal.Signals] = []

        def add_signal_handler(self, sig: signal.Signals, callback: Any, *args: Any) -> None:
            self.handlers[sig] = (callback, args)

        def remove_signal_handler(self, sig: signal.Signals) -> None:
            self.removed.append(sig)

    proxy = LoopProxy()
    monkeypatch.setattr(cli, "_dispatch", dispatch)
    monkeypatch.setattr(cli.asyncio, "get_running_loop", lambda: proxy)
    task = real_loop.create_task(cli._run_with_signals(argparse.Namespace()))
    await started.wait()
    callback, args = proxy.handlers[signal.SIGTERM]
    callback(*args)

    assert await task == 143
    assert stopped.is_set()
    assert set(proxy.removed) == {signal.SIGINT, signal.SIGTERM}
