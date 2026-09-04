"""Test GitHub pull progress rendering."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from io import StringIO

from gh_puller.github import (
    ConsoleProgress,
    PullProgress,
    RateQuota,
)
from tests.github._puller_support import (
    _T0,
    _iso,
)


def test_console_progress_uses_throttled_json_for_logs_and_a_tty_bar() -> None:
    progress = PullProgress(
        event_at=_T0,
        phase="closing_bundles",
        target_at=_T0,
        catalog_seen=2,
        catalog_total=2,
        catalog_complete=True,
        objects_completed=1,
        objects_total=2,
        bundles_completed=1,
        issues_completed=1,
        latest_number=1,
        latest_kind="issue",
        requests=7,
        quotas=(
            RateQuota("core", 5000, 4993, _T0 + timedelta(hours=1)),
            RateQuota("graphql", 5000, 4998, _T0 + timedelta(minutes=30)),
        ),
    )
    log = StringIO()
    ticks = iter((0.0, 0.1, 0.2))
    observer = ConsoleProgress(log, interval=10, tty=False, monotonic=lambda: next(ticks))

    observer(progress)
    observer(
        replace(
            progress,
            event_at=_T0 + timedelta(seconds=1),
            objects_completed=2,
            bundles_completed=2,
        ),
    )
    observer(
        replace(
            progress,
            event_at=_T0 + timedelta(seconds=2),
            phase="done",
            objects_completed=2,
            bundles_completed=2,
        ),
    )

    lines = [json.loads(line) for line in log.getvalue().splitlines()]
    assert len(lines) == 2
    assert lines[0]["type"] == "github_pull_progress"
    assert lines[0]["event_at"] == _iso(_T0)
    assert lines[0]["quotas"] == [
        {
            "resource": "core",
            "limit": 5000,
            "remaining": 4993,
            "reset_at": _iso(_T0 + timedelta(hours=1)),
        },
        {
            "resource": "graphql",
            "limit": 5000,
            "remaining": 4998,
            "reset_at": _iso(_T0 + timedelta(minutes=30)),
        },
    ]
    assert lines[1]["phase"] == "done"

    terminal = StringIO()
    tty = ConsoleProgress(terminal, interval=0, tty=True, monotonic=lambda: 0.0)
    tty(progress)
    tty(replace(progress, phase="done", objects_completed=2, bundles_completed=2))
    rendered = terminal.getvalue()
    assert "objects=[##########----------] 1/2" in rendered
    assert "objects=[####################] 2/2" in rendered
    assert "issues=1 pulls=0" in rendered
    assert "latest=issue#1" in rendered
    assert "core=4,993/5,000 graphql=4,998/5,000" in rendered
    assert rendered.endswith("\n")
