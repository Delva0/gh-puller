"""Test the Claude Code Agent adapter contract."""

import sys
import types
from typing import ClassVar

import pytest

from gh_puller import agent
from gh_puller.agent.context import OPAQUE, instruction, mcp, skill_list, tool_defs
from gh_puller.agent.events import fold_state, text_message
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


class _ClaudeOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _ClaudeStream:
    def __init__(self, event):
        self.event = event


class _ClaudeAssistant:
    def __init__(self, content, *, stop_reason="end_turn", usage=None,
                 message_id=None, model="m"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage
        self.message_id = message_id
        self.model = model


class _ClaudeResult:
    def __init__(self, result):
        self.result = result
        self.is_error = False
        self.errors = []
        self.subtype = "success"
        self.stop_reason = "end_turn"
        self.usage = None
        self.total_cost_usd = None


class _ClaudeUser:
    def __init__(self, content):
        self.content = content


class _ClaudeToolResult:
    def __init__(self, call_id: str, content: str):
        self.tool_use_id = call_id
        self.content = content
        self.is_error = False


class _ClaudeClient:
    scripts: ClassVar[list[list]] = []

    def __init__(self, options=None):
        self.options = options
        self.index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def query(self, _prompt):
        return None

    async def receive_response(self):
        script = type(self).scripts[self.index]
        self.index += 1
        for message in script:
            yield message


def _install_claude(monkeypatch, scripts: list[list]) -> None:
    module = types.ModuleType("claude_agent_sdk")
    module.ClaudeAgentOptions = _ClaudeOptions
    module.ClaudeSDKClient = _ClaudeClient
    module.StreamEvent = _ClaudeStream
    module.AssistantMessage = _ClaudeAssistant
    module.ResultMessage = _ClaudeResult
    module.UserMessage = _ClaudeUser
    module.ToolResultBlock = _ClaudeToolResult
    _ClaudeClient.scripts = scripts
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)


def _claude_text(value: str) -> list:
    return [
        _ClaudeStream({
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": value},
        }),
        _ClaudeAssistant([types.SimpleNamespace(type="text", text=value)]),
        _ClaudeResult(value),
    ]


@pytest.mark.asyncio
async def test_claude_code_is_multi_turn(monkeypatch, tmp_path) -> None:
    def fallback(value: str) -> list:
        return [
            _ClaudeAssistant([types.SimpleNamespace(type="text", text=value)]),
            _ClaudeResult(value),
        ]

    _install_claude(monkeypatch, [_claude_text("a1"), fallback("a2"), fallback("a3")])
    events = await _capture(tmp_path)
    subject = agent.ClaudeCode({
        "model": "m", "system_prompt": "system", "cwd": str(tmp_path),
        "allowed_tools": ["Read", "mcp__graph__query"],
        "mcp_servers": {"graph": {"command": "serve"}},
        "skills": ["review"],
    })
    async with subject.session(session="cc/s", session_name="cc"):
        assert await _collect(subject.stream("q1")) == "a1"
        assert await _collect(subject.stream("q2")) == "a2"
        assert await subject.result("q3") == "a3"
    await _settle()
    assert [event["data"]["requestId"] for event in events
            if event["type"] == "model/request"] == ["r1", "r2", "r3"]
    assert next(event for event in events if event["type"] == "agent/set")["data"][
        "config"]["model"] == "m"
    system = next(event for event in events if event["type"] == "context/append/system")
    assert system["data"]["items"][0]["content"] == [
        instruction("system"), tool_defs(["Read", OPAQUE]),
        mcp("graph"), skill_list(["review"]),
    ]
    assert _context_labels(fold_state(events)["context"]) == [
        "system", "user", "assistant", "user", "assistant", "user", "assistant",
    ]
    assert _context_at_requests(events) == [
        ["system", "user"],
        ["system", "user", "assistant", "user"],
        ["system", "user", "assistant", "user", "assistant", "user"],
    ]
    _assert_inferences(events)

@pytest.mark.asyncio
async def test_claude_fragments_share_one_model_response(monkeypatch, tmp_path) -> None:
    script = [
        _ClaudeStream({
            "type": "message_start",
            "message": {"role": "assistant", "id": "m1", "model": "actual"},
        }),
        _ClaudeAssistant(
            [types.SimpleNamespace(type="thinking", thinking="why")],
            stop_reason=None, usage={"input_tokens": 2}, message_id="m1", model="actual"),
        _ClaudeStream({
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "answer"},
        }),
        _ClaudeAssistant(
            [types.SimpleNamespace(type="text", text="answer")],
            stop_reason=None, usage={"output_tokens": 1}, message_id="m1", model="actual"),
        _ClaudeStream({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
        _ClaudeStream({"type": "message_stop"}),
        _ClaudeResult("answer"),
    ]
    _install_claude(monkeypatch, [script])
    events = await _capture(tmp_path)
    subject = agent.ClaudeCode({})
    async with subject.session(session="cc/fragments"):
        assert await _collect(subject.stream("q")) == "answer"
    await _settle()
    responses = [event for event in events if event["type"] == "model/response"]
    assert len(responses) == 1
    assert responses[0]["data"] == {
        "requestId": "r1",
        "output": [
            {"type": "reasoning", "content": [
                {"type": "reasoning_text", "text": "why"},
            ]},
            text_message("assistant", "answer"),
        ],
        "model": "actual",
        "usage": {"input": 2, "output": 1},
        "stopReason": "end_turn",
    }
    assert _context_labels(fold_state(events)["context"]) == [
        "system", "user", "reasoning", "assistant",
    ]


@pytest.mark.asyncio
async def test_claude_tool_result_is_activity_then_context(monkeypatch, tmp_path) -> None:
    tool = types.SimpleNamespace(
        type="tool_use", id="c1", name="read", input={"path": "a.py"})
    script = [
        _ClaudeAssistant([tool], stop_reason="tool_use"),
        _ClaudeUser([_ClaudeToolResult("c1", "file")]),
        _ClaudeStream({"type": "message_start", "message": {"role": "assistant"}}),
        _ClaudeAssistant([types.SimpleNamespace(type="text", text="done")]),
        _ClaudeResult("done"),
    ]
    _install_claude(monkeypatch, [script])
    events = await _capture(tmp_path)
    subject = agent.ClaudeCode({"model": "m"})
    async with subject.session(session="cc/tool"):
        assert await _collect(subject.stream("q")) == "done"
    await _settle()
    types_ = [event["type"] for event in events]
    assistant = types_.index("context/append/assistant")
    start = types_.index("tool/start")
    end = types_.index("tool/end")
    result = types_.index("context/append/tool")
    assert assistant < start < end < result
    assert _context_at_requests(events) == [
        ["system", "user"],
        ["system", "user", "function_call", "function_call_output"],
    ]
    _assert_inferences(events)
