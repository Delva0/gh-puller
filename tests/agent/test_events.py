"""Test Agent event recording and observation sinks."""

import asyncio
import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from gh_puller import agent
from gh_puller.agent.events import EventBus, EventRecorder, fold_state, set_active_bus, text_message
from gh_puller.agent.sinks import FileSink, OtelSink
from tests.agent._support import (
    capture as _capture,
)
from tests.agent._support import (
    context_labels as _context_labels,
)
from tests.agent._support import (
    event as _event,
)
from tests.agent._support import (
    receiver as _receiver,
)
from tests.agent._support import (
    settle as _settle,
)


@pytest.mark.asyncio
async def test_event_bus_preserves_order_for_every_sink() -> None:
    first: list[dict] = []
    second: list[dict] = []
    bus = EventBus()
    bus.add(_receiver(first))
    bus.add(_receiver(second))
    for index in range(5):
        bus.publish({"index": index})
    await _settle()
    assert first == second == [{"index": index} for index in range(5)]
    bus.shutdown()


@pytest.mark.asyncio
async def test_event_bus_backlog_never_drops_compact_events() -> None:
    received: list[dict] = []
    bus = EventBus()
    bus.add(_receiver(received))
    for index in range(5100):
        bus.publish({"type": "model/delta/text", "index": index})
    committed = {"type": "context/append/user", "items": []}
    bus.publish(committed)
    for _ in range(100):
        if committed in received:
            break
        await asyncio.sleep(0.01)
    assert received[-1] == committed
    assert len(received) < 5101
    bus.shutdown()


@pytest.mark.asyncio
async def test_recorder_supports_multiple_loose_turns_and_replay() -> None:
    events: list[dict] = []
    bus = EventBus()
    bus.add(_receiver(events))
    set_active_bus(bus)
    recorder = EventRecorder("s", agent="custom", config={"mode": "default"})
    recorder.start()
    for index in range(2):
        recorder.begin_turn()
        recorder.append_context(text_message("user", f"q{index}"))
        recorder.begin_step()
        request_id = recorder.model_request(model=f"m{index}")
        recorder.text(f"a{index}", request_id=request_id)
        output = [text_message("assistant", f"a{index}")]
        recorder.model_response(output, request_id=request_id)
        recorder.append_context(output, role="assistant")
        recorder.end_turn()
    recorder.finish(True)
    await _settle()
    state = fold_state(events)
    assert state["agent"] == {"agent": "custom", "config": {"mode": "default"}}
    assert _context_labels(state["context"]) == [
        "user", "assistant", "user", "assistant",
    ]
    assert [event["data"]["requestId"] for event in events
            if event["type"] == "model/request"] == ["r1", "r2"]
    assert len([event for event in events if event["type"] == "turn/start"]) == 2
    bus.shutdown()


@pytest.mark.asyncio
async def test_recorder_correlates_interleaved_model_activity() -> None:
    events: list[dict] = []
    bus = EventBus()
    bus.add(_receiver(events))
    set_active_bus(bus)
    recorder = EventRecorder("s")
    left = recorder.model_request(request_id="left", model="planner")
    right = recorder.model_request(request_id="right", model="writer")
    recorder.text("R", request_id=right)
    recorder.text("L", request_id=left)
    recorder.model_response(
        [text_message("assistant", "L")],
        request_id=left,
    )
    recorder.model_response(
        [text_message("assistant", "R")],
        request_id=right,
    )
    await _settle()
    assert [event["data"]["requestId"] for event in events] == [
        "left", "right", "right", "left", "left", "right",
    ]
    bus.shutdown()


@pytest.mark.asyncio
async def test_base_agent_records_config_without_inferring_context(tmp_path) -> None:
    class BareAgent(agent.BaseAgent):
        agent = "bare"

        async def _enter(self) -> None:
            return None

        async def _exit(self, _exc) -> None:
            return None

    config = {
        "model": "configured",
        "mode": "plan",
        "system_prompt": "not applied by this adapter",
        "custom": {"value": 1},
    }
    events = await _capture(tmp_path)
    subject = BareAgent(config)
    async with subject.session(session="bare/s"):
        pass
    await _settle()
    assert next(event for event in events if event["type"] == "agent/set")["data"] == {
        "agent": "bare", "config": config,
    }
    assert not any(event["type"].startswith("context/") for event in events)


@pytest.mark.asyncio
async def test_file_sink_compact_and_raw_have_identical_state(tmp_path) -> None:
    compact = FileSink(str(tmp_path / "compact"))
    raw = FileSink(str(tmp_path / "raw"), raw=True)
    events = [
        _event("session/start", 0, label="x"),
        _event("agent/set", 1, agent="x", config={"model": "m"}),
        _event("context/append/user", 2, items=[text_message("user", "q")]),
        _event("model/request", 3, requestId="r1"),
        _event("model/delta/text", 4, requestId="r1", index=0, text="a"),
        _event("context/append/assistant", 5, items=[text_message("assistant", "a")]),
        _event("session/end", 6, outcome="completed", durationMs=1),
    ]
    for event in events:
        await compact.consume(event)
        await raw.consume(event)
    compact_events = [json.loads(line) for line in (tmp_path / "compact" / "s.jsonl")
                      .read_text(encoding="utf-8").splitlines()]
    raw_events = [json.loads(line) for line in (tmp_path / "raw" / "s.jsonl")
                  .read_text(encoding="utf-8").splitlines()]
    assert [event["type"] for event in compact_events] == [
        "session/start", "agent/set", "context/append/user", "model/request",
        "context/append/assistant", "session/end",
    ]
    assert fold_state(compact_events) == fold_state(raw_events)


@pytest.mark.asyncio
async def test_otel_uses_request_and_call_correlations() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    sink = OtelSink("", tracer=provider.get_tracer("test"))
    events = [
        _event("session/start", 0, label="run"),
        _event("agent/set", 1, agent="x", config={"model": "configured"}),
        _event("model/request", 2, requestId="r1", model="m", provider="p"),
        _event("model/delta/text", 3, requestId="r1", index=0, text="ok"),
        _event("model/response", 4, requestId="r1",
               output=[text_message("assistant", "ok")],
               usage={"input": 2, "output": 1}),
        _event("tool/start", 5, callId="c1", name="read", arguments={"path": "a"}),
        _event("tool/end", 6, callId="c1", error={"type": "IOError", "message": "bad"}),
        _event("session/end", 7, outcome="failed", durationMs=2),
    ]
    for event in events:
        await sink.consume(event)
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["run"].attributes["gh_puller.agent"] == "x"
    assert spans["model:r1"].attributes["gen_ai.request.model"] == "m"
    assert spans["model:r1"].attributes["gh_puller.text_preview"] == "ok"
    assert spans["tool:read"].status.status_code is StatusCode.ERROR
    assert spans["run"].status.status_code is StatusCode.ERROR
