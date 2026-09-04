"""Test the Codex Agent adapter contract."""

import sys
import types
from enum import StrEnum
from typing import ClassVar

import pytest

from gh_puller import agent
from gh_puller.agent.context import OPAQUE, instruction, mcp, tool_defs
from gh_puller.agent.events import fold_state, function_call_item, reasoning_item
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


class _Sandbox(StrEnum):
    read_only = "read-only"


class _Approval(StrEnum):
    deny_all = "deny_all"


class _CodexConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _Notification:
    def __init__(self, method: str, **payload):
        self.method = method
        self.payload = types.SimpleNamespace(**payload)


class _TurnHandle:
    def __init__(self, script):
        self.script = script

    async def stream(self):
        for notification in self.script:
            yield notification


class _Thread:
    scripts: ClassVar[list[list]] = []
    index = 0

    async def turn(self, _prompt, **_kwargs):
        script = type(self).scripts[type(self).index]
        type(self).index += 1
        return _TurnHandle(script)


class _CodexClient:
    starts = 0

    def __init__(self, config=None):
        self.config = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def thread_start(self, **_kwargs):
        type(self).starts += 1
        return _Thread()

    async def login_api_key(self, _token):
        return None


def _install_codex(monkeypatch, scripts: list[list]) -> None:
    module = types.ModuleType("openai_codex")
    module.AsyncCodex = _CodexClient
    module.CodexConfig = _CodexConfig
    module.Sandbox = _Sandbox
    module.ApprovalMode = _Approval
    module.JsonRpcError = RuntimeError
    _Thread.scripts = scripts
    _Thread.index = 0
    _CodexClient.starts = 0
    monkeypatch.setitem(sys.modules, "openai_codex", module)


def _codex_text(value: str, item_id: str) -> list:
    item = types.SimpleNamespace(type="agentMessage", id=item_id, text=value, phase=None)
    return [
        _Notification("item/started", item=item),
        _Notification("item/agentMessage/delta", item_id=item_id, delta=value),
        _Notification("item/completed", item=item),
        _Notification("turn/completed", turn=types.SimpleNamespace(status="completed")),
    ]


@pytest.mark.asyncio
async def test_codex_is_multi_turn(monkeypatch, tmp_path) -> None:
    _install_codex(monkeypatch, [_codex_text("a1", "a1"), _codex_text("a2", "a2")])
    events = await _capture(tmp_path)
    subject = agent.Codex({
        "model": "m", "codex_home": str(tmp_path / "codex"),
        "base_instructions": "system", "developer_instructions": "developer",
        "mcp_servers": [{"id": "graph", "command": "serve"}],
    })
    async with subject.session(session="codex/s"):
        assert await _collect(subject.stream("q1")) == "a1"
        assert await subject.result("q2") == "a2"
    assert _CodexClient.starts == 1
    await _settle()
    assert [event["data"]["requestId"] for event in events
            if event["type"] == "model/request"] == ["r1", "r2"]
    assert next(event for event in events if event["type"] == "agent/set")["data"][
        "config"]["model"] == "m"
    system = next(event for event in events if event["type"] == "context/append/system")
    assert system["data"]["items"] == [
        {"type": "message", "role": "system", "content": [
            instruction("system"), tool_defs([OPAQUE]), mcp("graph"),
        ]},
        {"type": "message", "role": "developer", "content": [
            instruction("developer"),
        ]},
    ]
    assert not [event for event in events if event["type"] == "context/append"]
    assert _context_labels(fold_state(events)["context"]) == [
        "system", "developer", "user", "assistant", "user", "assistant",
    ]
    assert _context_at_requests(events) == [
        ["system", "developer", "user"],
        ["system", "developer", "user", "assistant", "user"],
    ]
    _assert_inferences(events)

@pytest.mark.asyncio
async def test_codex_keeps_response_and_session_usage_scopes(monkeypatch, tmp_path) -> None:
    item = types.SimpleNamespace(type="agentMessage", id="a1", text="done", phase=None)
    usage = types.SimpleNamespace(
        last=types.SimpleNamespace(
            input_tokens=2,
            output_tokens=1,
            cached_input_tokens=1,
            cache_write_input_tokens=0,
            reasoning_output_tokens=1,
        ),
        total=types.SimpleNamespace(
            input_tokens=20,
            output_tokens=10,
            cached_input_tokens=8,
            cache_write_input_tokens=3,
            reasoning_output_tokens=5,
        ),
    )
    script = [
        _Notification("item/started", item=item),
        _Notification("item/agentMessage/delta", item_id="a1", delta="done"),
        _Notification("item/completed", item=item),
        _Notification("thread/tokenUsage/updated", token_usage=usage),
        _Notification("turn/completed", turn=types.SimpleNamespace(status="completed")),
    ]
    _install_codex(monkeypatch, [script])
    events = await _capture(tmp_path)
    subject = agent.Codex({"model": "m", "codex_home": str(tmp_path / "codex")})
    async with subject.session(session="codex/usage"):
        assert await _collect(subject.stream("q")) == "done"
    await _settle()
    response = next(event for event in events if event["type"] == "model/response")
    terminal = next(event for event in events if event["type"] == "session/end")
    assert response["data"]["usage"] == {
        "input": 2,
        "output": 1,
        "cacheRead": 1,
        "cacheWrite": 0,
        "reasoning": 1,
    }
    assert terminal["data"]["usage"] == {
        "input": 20,
        "output": 10,
        "cacheRead": 8,
        "cacheWrite": 3,
        "reasoning": 5,
    }


@pytest.mark.asyncio
async def test_codex_batches_model_tool_calls_before_execution(monkeypatch, tmp_path) -> None:
    reasoning = types.SimpleNamespace(type="reasoning", id="reasoning-1", content=["why"])
    tool = types.SimpleNamespace(
        type="commandExecution", id="c1", command="ls", cwd="/tmp",
        aggregated_output="ok", exit_code=0, status="completed")
    script = [
        _Notification("item/started", item=reasoning),
        _Notification("item/reasoning/textDelta", item_id="reasoning-1", delta="why"),
        _Notification("item/completed", item=reasoning),
        _Notification("item/completed", item=tool),
        *_codex_text("done", "a1"),
    ]
    _install_codex(monkeypatch, [script])
    events = await _capture(tmp_path)
    subject = agent.Codex({"model": "m", "codex_home": str(tmp_path / "codex")})
    async with subject.session(session="codex/tool"):
        assert await _collect(subject.stream("q")) == "done"
    await _settle()
    types_ = [event["type"] for event in events]
    assistant = types_.index("context/append/assistant")
    start = types_.index("tool/start")
    end = types_.index("tool/end")
    result = types_.index("context/append/tool")
    assert assistant < start < end < result
    assert events[assistant]["data"]["items"] == [
        reasoning_item("why"),
        function_call_item("c1", "shell", {"command": "ls", "cwd": "/tmp"}),
    ]
    assert _context_at_requests(events) == [
        ["system", "user"],
        ["system", "user", "reasoning", "function_call", "function_call_output"],
    ]
    _assert_inferences(events)
