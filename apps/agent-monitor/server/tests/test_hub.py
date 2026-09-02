"""Protocol tests for persisted and live canonical event delivery."""

import asyncio
import json
import os
import time

import httpx
from gh_puller.agent.events import new_event

from hub import _Hub, _viewer_frame, create_app


def _event(event_type: str, seq: int, session: str = "s", **data) -> dict:
    return {**new_event(event_type, **data), "session": session, "seq": seq}


def _start(seq: int = 0, session: str = "s") -> dict:
    return _event(
        "session/start", seq, session, generator="custom", label="demo", runId="run-1")


class _Socket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def test_live_subscription_forwards_raw_events_and_terminal_state() -> None:
    hub = _Hub()
    viewer = _Socket()

    async def scenario() -> None:
        await hub.feed_batch([
            _start(),
            _event("model/set", 1, model="m", provider="p", parameters={}),
            _event("context/append/user", 2, content=[{"type": "text", "text": "q"}]),
        ])
        await _viewer_frame(viewer, hub, {"type": "subscribe", "session": "s"})
        await hub.feed_batch([
            _event("model/request", 3, requestId="r1"),
            _event("model/delta/text", 4, requestId="r1", index=0, text="a"),
            _event("session/end", 5, outcome="completed", durationMs=1),
        ])

    asyncio.run(scenario())
    assert viewer.sent[0] == {"type": "evt_ready", "session": "s", "lastSeq": 2}
    assert [event["seq"] for event in viewer.sent[1]["events"]] == [3, 4, 5]
    assert hub.index()[0] == {
        "session": "s", "run_id": "run-1", "label": "demo", "generator": "custom",
        "provider": "p", "model": "m", "state": "completed",
        "ts": hub.index()[0]["ts"], "last_ts": hub.index()[0]["last_ts"],
        "num_events": 6,
    }


def test_history_is_compact_paginated_and_handles_missing_sessions() -> None:
    hub = _Hub()
    viewer = _Socket()

    async def scenario() -> None:
        await hub.feed_batch([
            _start(),
            _event("model/request", 1, requestId="r1"),
            _event("model/delta/text", 2, requestId="r1", index=0, text="ignored"),
            _event("context/append/assistant", 3, content=[{"type": "text", "text": "a"}]),
            _event("session/end", 4, outcome="completed", durationMs=1),
        ])
        await _viewer_frame(viewer, hub, {"type": "history", "session": "s", "max": 2})
        await _viewer_frame(viewer, hub, {
            "type": "history", "session": "s", "beforeSeq": 3, "max": 3,
        })
        await _viewer_frame(viewer, hub, {"type": "history", "session": "missing"})

    asyncio.run(scenario())
    latest, older, missing = viewer.sent
    assert [event["seq"] for event in latest["events"]] == [3, 4]
    assert latest["hasMore"] is True and latest["nextBeforeSeq"] == 3
    assert [event["seq"] for event in older["events"]] == [0, 1]
    assert missing["events"] == [] and missing["hasMore"] is False


def test_seed_restores_index_metadata_and_history(tmp_path) -> None:
    events = [
        _start(session="ns/seed"),
        _event("model/set", 1, "ns/seed", model="m", provider="p", parameters={}),
        _event("context/append/user", 2, "ns/seed",
               content=[{"type": "text", "text": "q"}]),
        _event("session/end", 3, "ns/seed", outcome="failed", durationMs=1),
    ]
    (tmp_path / "seed.jsonl").write_text(
        "".join(f"{json.dumps(event)}\n" for event in events), encoding="utf-8")
    hub = _Hub()
    hub.seed(str(tmp_path))
    row = hub.index()[0]
    assert row["session"] == "ns/seed"
    assert row["provider"] == "p" and row["model"] == "m"
    assert row["state"] == "aborted"
    viewer = _Socket()
    asyncio.run(_viewer_frame(viewer, hub, {"type": "history", "session": "ns/seed"}))
    assert [event["seq"] for event in viewer.sent[0]["events"]] == [0, 1, 2, 3]


def test_lease_marks_stale_sessions_and_recovers_on_file_progress(tmp_path) -> None:
    path = tmp_path / "lease.jsonl"
    path.write_text(f"{json.dumps(_start(session='ns/lease'))}\n", encoding="utf-8")
    stale = time.time() - 1000
    os.utime(path, (stale, stale))
    hub = _Hub(lease_secs=10)
    hub.seed(str(tmp_path))
    assert hub.index()[0]["state"] == "aborted"

    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{json.dumps(_event('model/request', 1, 'ns/lease', requestId='r1'))}\n")
    now = time.time()
    os.utime(path, (now, now))
    asyncio.run(hub.scan())
    assert hub.index()[0]["state"] == "running"

    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{json.dumps(_event('session/end', 2, 'ns/lease', outcome='completed'))}\n")
    os.utime(path, (now + 2, now + 2))
    asyncio.run(hub.scan())
    assert hub.index()[0]["state"] == "completed"


def test_delete_removes_memory_disk_and_broadcasts(tmp_path) -> None:
    path = tmp_path / "delete.jsonl"
    path.write_text(f"{json.dumps(_start(session='ns/delete'))}\n", encoding="utf-8")
    hub = _Hub()
    hub.seed(str(tmp_path))
    viewer = _Socket()
    hub.viewers.add(viewer)
    asyncio.run(_viewer_frame(viewer, hub, {"type": "delete", "session": "ns/delete"}))
    assert "ns/delete" not in hub.sessions
    assert not path.exists()
    assert viewer.sent[-1] == {"type": "index", "sessions": []}


def test_http_routes_serve_the_built_viewer(tmp_path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "agent_monitor_viewer.html").write_text("<html>monitor</html>", encoding="utf-8")
    app = create_app(_Hub(), static_root=static)

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert "monitor" in (await client.get("/")).text
            assert (await client.get("/viewer")).status_code == 200
            assert (await client.get("/missing")).status_code == 404

    asyncio.run(scenario())
