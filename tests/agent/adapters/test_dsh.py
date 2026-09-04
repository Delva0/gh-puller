"""Test the DeepSeek Harness Agent adapter contract."""

import asyncio
import sys
import types
from typing import ClassVar

import pytest

from gh_puller import agent
from gh_puller.agent.context import instruction, tool_defs
from gh_puller.agent.events import fold_state, function_call_item, reasoning_item, text_message
from tests.agent._support import (
    assert_inferences as _assert_inferences,
)
from tests.agent._support import (
    capture as _capture,
)
from tests.agent._support import (
    collect as _collect,
)
from tests.agent._support import (
    context_at_requests as _context_at_requests,
)
from tests.agent._support import (
    context_labels as _context_labels,
)
from tests.agent._support import (
    settle as _settle,
)


class _DshNotification:
    def __init__(self, payload):
        self.method = "session.event"
        self.payload = payload


class _DshHarness:
    scripts: ClassVar[list[list[dict]]] = []
    index = 0

    def __init__(self, **_kwargs):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def run(self, _prompt, *, session_id, on_notification):
        script = type(self).scripts[type(self).index]
        type(self).index += 1
        for event in script:
            on_notification(_DshNotification({"sessionId": session_id, "event": event}))
        return types.SimpleNamespace(
            finish_reason="completed", final_response="answer", session_id=session_id)


def _install_dsh(monkeypatch, scripts: list[list[dict]]) -> None:
    module = types.ModuleType("deepseek_harness")
    module.DeepSeekHarness = _DshHarness
    module.RunResult = types.SimpleNamespace
    _DshHarness.scripts = scripts
    _DshHarness.index = 0
    monkeypatch.setitem(sys.modules, "deepseek_harness", module)


def _dsh_events(value: str) -> list[dict]:
    return [
        {"type": "request/header", "data": {"header": {
            "config": {"provider": "p", "model": "m"},
            "system": "system", "tools": [],
        }}},
        {"type": "turn/start", "data": {}},
        {"type": "step/start", "data": {}},
        {"type": "user/message", "data": {
            "role": "user", "content": [{"type": "text", "text": "q"}],
        }},
        {"type": "request/context", "data": {}},
        {"type": "assistant/chunk", "data": {
            "chunk": {"type": "text-delta", "index": 0, "text": value},
        }},
        {"type": "assistant/message", "data": {
            "message": {"role": "assistant", "content": [{"type": "text", "text": value}]},
        }},
        {"type": "step/end", "data": {}},
        {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
    ]


@pytest.mark.asyncio
async def test_dsh_is_retained_as_multi_turn_adapter(monkeypatch, tmp_path) -> None:
    _install_dsh(monkeypatch, [_dsh_events("a1"), _dsh_events("a2")])

    async def immediate(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate)
    events = await _capture(tmp_path)
    subject = agent.Dsh({
        "provider": "p", "model": "m", "system_prompt": "system",
        "cordis": "/tmp/fake.yml",
    })
    async with subject.session(session="dsh/s"):
        assert await _collect(subject.stream("q1")) == "a1"
        assert await subject.result("q2") == "answer"
    await _settle()
    assert [event["data"]["requestId"] for event in events
            if event["type"] == "model/request"] == ["r1", "r2"]
    assert next(event for event in events if event["type"] == "agent/set")["data"][
        "config"]["model"] == "m"
    assert len([event for event in events if event["type"] == "context/append/system"]) == 1
    assert len([event for event in events if event["type"] == "context/append/assistant"]) == 2
    assert _context_labels(fold_state(events)["context"]) == [
        "system", "user", "assistant", "user", "assistant",
    ]
    assert _context_at_requests(events) == [
        ["system", "user"], ["system", "user", "assistant", "user"],
    ]
    _assert_inferences(events)


@pytest.mark.asyncio
async def test_dsh_normalizes_model_tools_before_local_activity(monkeypatch, tmp_path) -> None:
    script = [
        {"type": "request/header", "data": {"header": {
            "config": {"provider": "p2", "model": "m2", "temperature": 0},
            "system": "system",
            "tools": [{"name": "read", "description": "Read a file",
                       "parameters": {"type": "object"}}],
        }}},
        {"type": "turn/start", "data": {}},
        {"type": "step/start", "data": {}},
        {"type": "user/message", "data": {
            "role": "user", "content": [{"type": "text", "text": "q"}],
        }},
        {"type": "assistant/message", "data": {"message": {
            "role": "assistant", "content": [
                {"type": "thinking", "text": "why"},
                {"type": "text", "text": "checking"},
                {"type": "tool-call", "id": "c1", "name": "read",
                 "arguments": {"path": "a.py"}},
            ],
        }}},
        {"type": "tool/call", "data": {
            "callId": "c1", "name": "read", "arguments": {"path": "a.py"},
        }},
        {"type": "tool/result", "data": {"message": {
            "role": "user", "source": {"kind": "tool", "callId": "c1"},
            "content": [{"type": "tool-result", "toolCallId": "c1", "isError": False,
                         "content": [{"type": "text", "text": "file"}]}],
        }}},
        {"type": "step/end", "data": {}},
        {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
    ]
    _install_dsh(monkeypatch, [script])

    async def immediate(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate)
    events = await _capture(tmp_path)
    subject = agent.Dsh({"provider": "p", "model": "m", "cordis": "/tmp/fake.yml"})
    async with subject.session(session="dsh/tool"):
        assert await _collect(subject.stream("q")) == ""
    await _settle()
    types_ = [event["type"] for event in events]
    assistant = types_.index("context/append/assistant")
    start = types_.index("tool/start")
    end = types_.index("tool/end")
    result = types_.index("context/append/tool")
    assert assistant < start < end < result
    assert events[assistant]["data"]["items"] == [
        reasoning_item("why"),
        text_message("assistant", "checking"),
        function_call_item("c1", "read", {"path": "a.py"}),
    ]
    request = next(event for event in events if event["type"] == "model/request")
    assert request["data"] == {
        "requestId": "r1", "model": "m2", "provider": "p2",
        "parameters": {"temperature": 0},
    }
    state = fold_state(events)
    assert state["context"][0] == {
        "type": "message", "role": "system", "content": [
            instruction("system"),
            tool_defs([{
                "name": "read", "description": "Read a file",
                "inputSchema": {"type": "object"},
            }]),
        ],
    }
    _assert_inferences(events)


@pytest.mark.asyncio
async def test_dsh_surface_replacement_becomes_context_set(monkeypatch, tmp_path) -> None:
    script = [
        {"type": "turn/start", "seq": 0, "data": {}},
        {"type": "step/start", "seq": 1, "data": {}},
        {"type": "user/message", "seq": 2, "surfaceOp": "append", "data": {
            "role": "user", "content": [{"type": "text", "text": "old question"}],
        }},
        {"type": "assistant/message", "seq": 3, "surfaceOp": "append", "data": {
            "message": {
                "role": "assistant", "content": [{"type": "text", "text": "old answer"}],
            },
        }},
        {"type": "user/message", "seq": 4, "surfaceOp": {
            "op": "replace", "start": 2, "end": 3,
        }, "data": {
            "role": "user", "content": [{"type": "text", "text": "summary"}],
        }},
        {"type": "assistant/message", "seq": 5, "surfaceOp": "append", "data": {
            "message": {
                "role": "assistant", "content": [{"type": "text", "text": "final"}],
            },
        }},
        {"type": "step/end", "seq": 6, "data": {}},
        {"type": "turn/end", "seq": 7, "data": {"reason": {"kind": "completed"}}},
    ]
    _install_dsh(monkeypatch, [script])

    async def immediate(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate)
    events = await _capture(tmp_path)
    subject = agent.Dsh({"provider": "p", "model": "m", "cordis": "/tmp/fake.yml"})
    async with subject.session(session="dsh/replace"):
        assert await _collect(subject.stream("old question")) == ""
    await _settle()
    assert len([event for event in events if event["type"] == "context/set"]) == 1
    assert fold_state(events)["context"] == [
        text_message("user", "summary"),
        text_message("assistant", "final"),
    ]
