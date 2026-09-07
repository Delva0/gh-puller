"""Test current sync commands and scheduler-only interval semantics."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from gh_puller.github import __main__ as cli
from gh_puller.github.syncer import SyncResult

if TYPE_CHECKING:
    from pathlib import Path

_T0 = datetime(2026, 9, 5, 10, 7, tzinfo=UTC)


@dataclass
class _Clock:
    current: datetime
    sleeps: list[float] = field(default_factory=list)

    def __call__(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


class _Syncer:
    def __init__(self) -> None:
        self.calls = 0

    async def sync(self) -> SyncResult:
        self.calls += 1
        return SyncResult(
            cycle_id=self.calls,
            started_at=_T0,
            completed_at=_T0,
            checkpoint_from=None if self.calls == 1 else _T0,
            discovered_items=3,
            requests=5,
        )


def test_parser_exposes_only_current_runtime_commands(tmp_path: Path) -> None:
    once = cli._parser().parse_args(
        ["once", "acme/widgets", str(tmp_path / "facts.sqlite3")],
    )
    schedule = cli._parser().parse_args(
        ["schedule", "acme/widgets", str(tmp_path / "facts.sqlite3")],
    )
    refresh = cli._parser().parse_args(
        [
            "refresh",
            "acme/widgets",
            str(tmp_path / "facts.sqlite3"),
            "--pull",
            "7",
            "--family",
            "pull-review-threads",
        ],
    )
    backfill = cli._parser().parse_args(
        ["backfill", "acme/widgets", str(tmp_path / "facts.sqlite3")],
    )

    assert once.command == "once"
    assert not hasattr(once, "target")
    assert (once.git_batch_size, once.git_ref_batch_size) == (8, 16)
    assert (cli._config(once).git_batch_size, cli._config(once).git_ref_batch_size) == (
        8,
        16,
    )
    assert schedule.command == "schedule"
    assert (refresh.command, refresh.pull, refresh.families) == (
        "refresh",
        [7],
        ["pull-review-threads"],
    )
    assert backfill.command == "backfill"
    for removed in ("import-v9", "migrate"):
        with pytest.raises(SystemExit):
            cli._parser().parse_args([removed])


@pytest.mark.asyncio
async def test_schedule_syncs_now_then_waits_for_utc_boundary() -> None:
    clock = _Clock(_T0)
    syncer = _Syncer()
    emitted = []

    await cli._run_schedule(
        syncer,
        timedelta(hours=1),
        now=clock,
        sleep=clock.sleep,
        emit=emitted.append,
        max_cycles=2,
    )

    assert syncer.calls == 2
    assert [result.cycle_id for result in emitted] == [1, 2]
    assert clock.sleeps == [53 * 60]
    assert clock.current == datetime(2026, 9, 5, 11, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30m", timedelta(minutes=30)),
        ("1h", timedelta(hours=1)),
        ("2d", timedelta(days=2)),
    ],
)
def test_interval_parser(value: str, expected: timedelta) -> None:
    assert cli._parse_interval(value) == expected


def test_result_json_describes_cycle_not_target(capsys: pytest.CaptureFixture[str]) -> None:
    cli._emit(_SYNC_RESULT)

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "checkpoint_from": None,
        "completed_at": "2026-09-05T10:07:00Z",
        "cycle_id": 1,
        "discovered_items": 3,
        "requests": 5,
        "started_at": "2026-09-05T10:07:00Z",
    }
    assert "target_at" not in payload


_SYNC_RESULT = SyncResult(1, _T0, _T0, None, 3, 5)
