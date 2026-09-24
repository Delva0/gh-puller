"""Exercise custom session files, delta retention and atomic compaction across reader handles."""

import json

import pytest

from gh_puller.agent import sinks
from gh_puller.agent.events import DELTA_TYPES, fold_state


def event(kind, seq, **data):
    return {"session": "test/session", "seq": seq, "type": kind, "data": data}


def stream():
    return [
        event("session/start", 1),
        event("context/append/user", 2, items=[{
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "问题"}],
        }]),
        event("model/delta/text", 3, requestId="r", index=0, text="hello"),
        event("model/delta/reasoning", 4, requestId="r", index=0, text="think"),
        event("model/delta/tool-call", 5, requestId="r", index=0, callId="c", argumentsDelta="{}"),
        event("future/event", 6, extension={"keep": True}),
        event("session/end", 7, outcome="completed"),
    ]


@pytest.mark.asyncio
async def test_live_log_and_old_reader_survive_compaction(tmp_path):
    sink = sinks.FileSink(str(tmp_path))
    events = stream()
    path = tmp_path / "session.jsonl"
    for evt in events[:-1]:
        await sink.consume(evt)
    original = path.read_bytes()
    assert [json.loads(line) for line in original.splitlines()] == events[:-1]
    with path.open("rb") as slow_reader:
        first = slow_reader.readline()
        await sink.consume(events[-1])
        assert [json.loads(line) for line in (first + slow_reader.read()).splitlines()] == events
    compact = path.read_bytes()
    assert compact == b"".join(
        line for line in (original + json.dumps(events[-1], ensure_ascii=False).encode() + b"\n")
        .splitlines(keepends=True) if json.loads(line)["type"] not in DELTA_TYPES
    )
    assert [json.loads(line)["seq"] for line in compact.splitlines()] == [1, 2, 6, 7]
    assert fold_state(events) == fold_state([json.loads(line) for line in compact.splitlines()])
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.asyncio
async def test_incomplete_session_is_never_compacted(tmp_path):
    sink = sinks.FileSink(str(tmp_path))
    for evt in stream()[:-1]:
        await sink.consume(evt)
    assert len((tmp_path / "session.jsonl").read_text().splitlines()) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["replace", "fsync"])
async def test_compaction_failure_retains_full_log(tmp_path, monkeypatch, operation):
    sink = sinks.FileSink(str(tmp_path))
    messages = []

    def fail(*args):
        raise OSError("injected disk failure")

    monkeypatch.setattr(sinks.os, operation, fail)
    monkeypatch.setattr(sinks, "_log", messages.append)
    for evt in stream():
        await sink.consume(evt)
    path = tmp_path / "session.jsonl"
    assert [json.loads(line) for line in path.read_text().splitlines()] == stream()
    assert list(tmp_path.iterdir()) == [path]
    assert "complete source log retained" in messages[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("save_delta", [False, True])
async def test_file_consumer_queue_is_lossless(tmp_path, save_delta):
    sinks.configure(file_dir=str(tmp_path), save_delta=save_delta, ws_urls=[], otel_urls=[])
    bus = sinks.ensure_bus()
    bus.publish(event("session/start", 0))
    for seq in range(1, 6002):
        bus.publish(event("model/delta/text", seq, text="x", index=0, requestId="r"))
    await sinks.flush()
    assert len((tmp_path / "session.jsonl").read_text().splitlines()) == 6002
    bus.publish(event("session/end", 6002, outcome="failed", reasonCode="cancelled"))
    await sinks.flush()
    assert len((tmp_path / "session.jsonl").read_text().splitlines()) == (6003 if save_delta else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("save_delta", [False, True])
@pytest.mark.parametrize("outcome", ["completed", "failed"])
async def test_explicit_file_path_records_selected_history(tmp_path, save_delta, outcome):
    path = tmp_path / "nested/run/events.jsonl"
    sinks.configure(file_path=path, save_delta=save_delta, ws_urls=[], otel_urls=[])
    assert sinks.session_path("test/session") == path
    assert sinks.session_path("test/session", directory=tmp_path) == tmp_path / "session.jsonl"
    bus = sinks.ensure_bus()
    events = stream()
    events[-1]["data"]["outcome"] = outcome
    for evt in events[:-1]:
        bus.publish(evt)
    await sinks.flush()
    original = path.read_bytes()
    assert [json.loads(line) for line in original.splitlines()] == events[:-1]
    bus.publish(events[-1])
    bus.publish(event("model/delta/text", 8, requestId="r", index=0, text="outside session"))
    await sinks.flush()
    full = original + json.dumps(events[-1], ensure_ascii=False).encode() + b"\n"
    expected = full if save_delta else b"".join(
        line for line in full.splitlines(keepends=True) if json.loads(line)["type"] not in DELTA_TYPES
    )
    assert path.read_bytes() == expected
    assert list(path.parent.iterdir()) == [path]
    assert not (tmp_path / "session.jsonl").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("ended", [False, True])
async def test_explicit_file_rejects_another_session_without_changing_history(tmp_path, ended):
    path = tmp_path / "events.jsonl"
    sink = sinks.FileSink(file_path=str(path), save_delta=True)
    for evt in stream() if ended else stream()[:-1]:
        await sink.consume(evt)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="one session"):
        await sink.consume({**event("session/start", 0), "session": "another/session"})
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_touch_targets_the_explicit_file(tmp_path):
    path = tmp_path / "events.jsonl"
    sinks.configure(file_path=path, ws_urls=[], otel_urls=[])
    sinks.ensure_bus().publish(event("session/start", 1))
    await sinks.flush()
    original = path.read_bytes()
    sinks.os.utime(path, (1, 1))
    await sinks.touch("test/session")
    assert path.stat().st_mtime > 1
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_reconfigure_resets_explicit_path_and_delta_retention(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    sinks.configure(file_path=path, save_delta=True, ws_urls=[], otel_urls=[])
    sinks.ensure_bus()
    monkeypatch.setattr(sinks.envs, "AGENT_MONITOR_DIR", str(tmp_path / "default"))
    sinks.configure(ws_urls=[], otel_urls=[])
    bus = sinks.ensure_bus()
    for evt in stream():
        bus.publish(evt)
    await sinks.flush()
    default = tmp_path / "default/session.jsonl"
    assert sinks.session_path("test/session") == default
    assert [json.loads(line) for line in default.read_text().splitlines()] == [
        evt for evt in stream() if evt["type"] not in DELTA_TYPES
    ]
    assert not path.exists()


@pytest.mark.asyncio
async def test_conflicting_destinations_leave_current_sink_running(tmp_path):
    path = tmp_path / "events.jsonl"
    sinks.configure(file_path=path, save_delta=True, ws_urls=[], otel_urls=[])
    bus = sinks.ensure_bus()
    bus.publish(event("session/start", 1))
    with pytest.raises(ValueError, match="mutually exclusive"):
        sinks.configure(file_dir=tmp_path, file_path=path)
    with pytest.raises(ValueError, match="mutually exclusive"):
        sinks.FileSink(tmp_path, file_path=path)
    assert sinks.ensure_bus() is bus
    bus.publish(event("session/end", 2, outcome="completed"))
    await sinks.flush()
    assert len(path.read_text().splitlines()) == 2
