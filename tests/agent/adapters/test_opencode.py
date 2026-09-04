"""Test the OpenCode Agent adapter contract."""

import json
import shlex

import pytest

from gh_puller import agent
from gh_puller.agent.context import OPAQUE, instruction, mcp, tool_defs
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


def _opencode_script(tmp_path, events: list[dict], args_path=None) -> str:
    path = tmp_path / "fake-opencode"
    lines = " ".join(shlex.quote(json.dumps(event)) for event in events)
    record = (f"printf '%s\\n' \"$@\" >> {shlex.quote(str(args_path))}\n"
              if args_path else "")
    path.write_text(f"#!/bin/sh\n{record}printf '%s\\n' {lines}\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _opencode_events(value: str) -> list[dict]:
    return [
        {"type": "step_start", "sessionID": "native-session", "part": {"id": "start"}},
        {"type": "text", "part": {"id": "text", "text": value}},
        {"type": "step_finish", "part": {
            "id": "finish", "reason": "stop", "tokens": {"input": 1, "output": 1},
        }},
    ]


@pytest.mark.asyncio
async def test_opencode_is_multi_turn(tmp_path) -> None:
    events = await _capture(tmp_path / "logs")
    args_path = tmp_path / "args"
    binary = _opencode_script(tmp_path, _opencode_events("answer"), args_path)
    subject = agent.OpenCode({
        "model": "m", "opencode_bin": binary, "system_prompt": "system",
        "mcp_servers": [{"id": "graph", "command": "serve"}],
    })
    async with subject.session(session="opencode/s"):
        assert await _collect(subject.stream("q1")) == "answer"
        assert await subject.result("q2") == "answer"
    argv = args_path.read_text(encoding="utf-8").splitlines()
    assert argv.count("--session") == 1
    assert "native-session" in argv
    await _settle()
    assert [event["data"]["requestId"] for event in events
            if event["type"] == "model/request"] == ["r1", "r2"]
    assert next(event for event in events if event["type"] == "agent/set")["data"][
        "config"]["model"] == "m"
    system = next(event for event in events if event["type"] == "context/append/system")
    assert system["data"]["items"][0]["content"] == [
        instruction("system"), tool_defs([OPAQUE]), mcp("graph"),
    ]
    assert len([event for event in events if event["type"] == "context/append/assistant"]) == 2
    assert _context_labels(fold_state(events)["context"]) == [
        "system", "user", "assistant", "user", "assistant",
    ]
    assert _context_at_requests(events) == [
        ["system", "user"], ["system", "user", "assistant", "user"],
    ]
    _assert_inferences(events)

@pytest.mark.asyncio
async def test_opencode_normalizes_reasoning_message_and_tool(tmp_path) -> None:
    native = [
        {"type": "step_start", "sessionID": "native", "part": {
            "id": "s1", "modelID": "actual-1",
        }},
        {"type": "reasoning", "part": {"id": "r1", "text": "why"}},
        {"type": "text", "part": {"id": "m1", "text": "checking"}},
        {"type": "tool_use", "part": {
            "callID": "c1", "tool": "read", "state": {
                "status": "completed", "input": {"path": "a.py"}, "output": "file",
            },
        }},
        {"type": "step_finish", "part": {
            "id": "f1", "reason": "tool-calls", "tokens": {"input": 2, "output": 3},
        }},
        {"type": "step_start", "part": {"id": "s2", "modelID": "actual-2"}},
        {"type": "text", "part": {"id": "m2", "text": "done"}},
        {"type": "step_finish", "part": {"id": "f2", "reason": "stop"}},
    ]
    events = await _capture(tmp_path / "logs")
    subject = agent.OpenCode({"opencode_bin": _opencode_script(tmp_path, native)})
    async with subject.session(session="opencode/output"):
        assert await _collect(subject.stream("q")) == "checkingdone"
    await _settle()
    responses = [event for event in events if event["type"] == "model/response"]
    assert responses[0]["data"] == {
        "requestId": "r1",
        "output": [
            reasoning_item("why"),
            text_message("assistant", "checking"),
            function_call_item("c1", "read", {"path": "a.py"}),
        ],
        "model": "actual-1",
        "usage": {"input": 2, "output": 3},
        "stopReason": "tool-calls",
    }
    assert _context_at_requests(events) == [
        ["system", "user"],
        ["system", "user", "reasoning", "assistant", "function_call",
         "function_call_output"],
    ]
    _assert_inferences(events)
