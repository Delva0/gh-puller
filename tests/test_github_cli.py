"""GitHub CLI 的整点调度、恢复、输出与信号退出测试。"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from gh_puller.github import PullResult
from gh_puller.github import __main__ as cli

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
            commit=f"commit-{len(self.targets)}",
            changed_items=0,
            catalog_items=10,
            requests=4,
        )


def _iso(value: datetime) -> str:
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.astimezone(UTC).isoformat(timespec=timespec).replace("+00:00", "Z")


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_state(path: Path, target: datetime) -> None:
    path.mkdir()
    _git(path, "init", "--quiet")
    state = {"last_pull": {"target_at": _iso(target)}}
    (path / ".gh-puller-state.json").write_text(json.dumps(state), encoding="utf-8")
    _git(path, "add", ".gh-puller-state.json")
    _git(
        path,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "--message",
        _iso(target),
    )


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
        ],
    )

    config = cli._config(args)
    assert args.target == _T0 + timedelta(minutes=30)
    assert config.repository == "acme/widgets"
    assert config.concurrency == 8
    assert config.catalog_mode == "exhaustive"


def test_parser_rejects_naive_target() -> None:
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            ["once", "acme/widgets", "/tmp/widgets", "--target", "2026-09-02T12:00:00"],
        )


@pytest.mark.asyncio
async def test_hourly_starts_at_latest_due_hour_and_never_skips(tmp_path: Path) -> None:
    puller = StubPuller()
    emitted: list[PullResult] = []

    await cli._run_hourly(
        puller,
        tmp_path / "new-archive",
        now=lambda: _T0 + timedelta(minutes=37),
        emit=emitted.append,
        max_runs=4,
    )

    assert puller.targets == [_T0 + timedelta(hours=offset) for offset in range(4)]
    assert [result.target_at for result in emitted] == puller.targets


@pytest.mark.asyncio
async def test_hourly_restart_uses_committed_title_then_retries_pending_state(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _commit_state(archive, _T0)

    assert await cli._hourly_start(archive, _T0 + timedelta(minutes=37)) == _T0 + timedelta(hours=1)

    pending = _T0 + timedelta(minutes=45)
    state = {"last_pull": {"target_at": _iso(pending)}}
    (archive / ".gh-puller-state.json").write_text(json.dumps(state), encoding="utf-8")
    assert await cli._hourly_start(archive, _T0 + timedelta(hours=2)) == pending

    puller = StubPuller()
    await cli._run_hourly(
        puller,
        archive,
        now=lambda: _T0 + timedelta(hours=2),
        emit=lambda _: None,
        max_runs=2,
    )
    assert puller.targets == [pending, _T0 + timedelta(hours=1)]


def test_emit_writes_machine_readable_result(capsys: pytest.CaptureFixture[str]) -> None:
    result = PullResult(
        target_at=_T0,
        completed_at=_T0 + timedelta(seconds=3),
        commit="abc",
        changed_items=2,
        catalog_items=11,
        requests=7,
    )

    cli._emit(result)

    assert json.loads(capsys.readouterr().out) == {
        "catalog_items": 11,
        "changed_items": 2,
        "commit": "abc",
        "completed_at": "2026-09-02T12:00:03Z",
        "lag_seconds": 3.0,
        "requests": 7,
        "target_at": "2026-09-02T12:00:00Z",
    }


def test_hourly_process_lock_rejects_duplicate_scheduler(tmp_path: Path) -> None:
    destination = tmp_path / "archive"

    with (
        cli._hourly_lock(destination),
        pytest.raises(RuntimeError, match="already runs"),
        cli._hourly_lock(destination),
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
