"""Tests for the local compact-history projection."""

import json
import os
import time

from gh_puller.agent.events import new_event

from hub import Hub


def _event(event_type: str, seq: int, session: str = "s", **data) -> dict:
    return {**new_event(event_type, **data), "session": session, "seq": seq}


def _start(seq: int = 0, session: str = "s") -> dict:
    return _event(
        "session/start",
        seq,
        session,
        label="demo",
        runId="run-1",
    )


def _write(path, events: list[dict]) -> None:
    path.write_text(
        "".join(f"{json.dumps(event)}\n" for event in events),
        encoding="utf-8",
    )


def test_ingest_projects_metadata_and_retains_only_compact_events() -> None:
    hub = Hub()
    assert hub.ingest(
        [
            _start(),
            _event("agent/set", 1, agent="custom", config={"model": "configured"}),
            _event("model/request", 2, requestId="r1", model="actual"),
            _event("model/delta/text", 3, requestId="r1", index=0, text="a"),
            _event("session/end", 4, outcome="completed", durationMs=1),
        ],
    )

    row = hub.index()[0]
    assert (row["session"], row["run_id"], row["label"], row["agent"]) == (
        "s",
        "run-1",
        "demo",
        "custom",
    )
    assert (row["state"], row["num_events"]) == ("completed", 5)
    assert row["ts"] <= row["last_ts"]
    events, has_more, next_before = hub.history("s")
    assert [event["seq"] for event in events] == [0, 1, 2, 4]
    assert (has_more, next_before) == (False, None)


def test_history_is_paginated_and_missing_sessions_are_empty() -> None:
    hub = Hub()
    hub.ingest(
        [
            _start(),
            _event("model/request", 1, requestId="r1"),
            _event("context/append/assistant", 2, content=[{"type": "text", "text": "a"}]),
            _event("session/end", 3, outcome="completed", durationMs=1),
        ],
    )

    latest, has_more, next_before = hub.history("s", limit=2)
    older, older_has_more, older_before = hub.history("s", before=next_before, limit=3)
    assert [event["seq"] for event in latest] == [2, 3]
    assert (has_more, next_before) == (True, 2)
    assert [event["seq"] for event in older] == [0, 1]
    assert (older_has_more, older_before) == (False, None)
    assert hub.history("missing") == ([], False, None)


def test_scan_discovers_files_created_after_startup(tmp_path) -> None:
    hub = Hub(tmp_path)
    assert hub.scan() is False
    path = tmp_path / "seed.jsonl"
    _write(
        path,
        [
            _start(session="ns/seed"),
            _event("agent/set", 1, "ns/seed", agent="custom", config={"model": "m"}),
            _event("context/append/user", 2, "ns/seed", content=[{"type": "text", "text": "q"}]),
        ],
    )

    assert hub.scan() is True
    assert hub.scan() is False
    row = hub.index()[0]
    assert row["session"] == "ns/seed"
    assert (row["agent"], row["num_events"]) == ("custom", 3)
    assert [event["seq"] for event in hub.history("ns/seed")[0]] == [0, 1, 2]


def test_scan_updates_lease_progress_and_terminal_state(tmp_path) -> None:
    path = tmp_path / "lease.jsonl"
    _write(path, [_start(session="ns/lease")])
    stale = time.time() - 1000
    os.utime(path, (stale, stale))
    hub = Hub(tmp_path, lease_secs=10)

    assert hub.scan() is True
    assert hub.index()[0]["state"] == "aborted"

    now = time.time()
    os.utime(path, (now, now))
    assert hub.scan() is True
    assert hub.index()[0]["state"] == "running"

    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{json.dumps(_event('session/end', 1, 'ns/lease', outcome='completed'))}\n")
    assert hub.scan() is True
    assert hub.index()[0]["state"] == "completed"
    assert hub.last_seq("ns/lease") == 1


def test_delete_removes_projection_and_history(tmp_path) -> None:
    path = tmp_path / "delete.jsonl"
    _write(path, [_start(session="ns/delete")])
    hub = Hub(tmp_path)
    hub.scan()

    assert hub.delete("ns/delete") is True
    assert hub.index() == []
    assert not path.exists()
    assert hub.delete("ns/delete") is False
