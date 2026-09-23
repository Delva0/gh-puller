"""Exercise live recording and atomic compaction across reader handles."""

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
async def test_file_consumer_queue_is_lossless(tmp_path):
    sinks.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    bus = sinks.ensure_bus()
    bus.publish(event("session/start", 0))
    for seq in range(1, 6002):
        bus.publish(event("model/delta/text", seq, text="x", index=0, requestId="r"))
    await sinks.flush()
    assert len((tmp_path / "session.jsonl").read_text().splitlines()) == 6002
    bus.publish(event("session/end", 6002, outcome="failed", reasonCode="cancelled"))
    await sinks.flush()
    assert len((tmp_path / "session.jsonl").read_text().splitlines()) == 2
