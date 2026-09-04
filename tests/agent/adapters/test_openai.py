"""Test the OpenAI-compatible Agent adapter contract."""

import json
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


class _HttpResponse:
    def __init__(self, packets: list[dict]):
        self.packets = packets

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for packet in self.packets:
            yield f"data: {json.dumps(packet)}"
        yield "data: [DONE]"


class _HttpClient:
    scripts: ClassVar[list[list[dict]]] = []
    requests: ClassVar[list[dict]] = []

    def __init__(self, **_kwargs):
        self.index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def stream(self, *_args, **kwargs):
        type(self).requests.append(kwargs["json"])
        response = _HttpResponse(type(self).scripts[self.index])
        self.index += 1

        class Context:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *_exc):
                return False

        return Context()


@pytest.mark.asyncio
async def test_openai_is_multi_turn(monkeypatch, tmp_path) -> None:
    from gh_puller.agent.adapters import openai

    _HttpClient.scripts = [
        [
            {"choices": [{"delta": {"reasoning_content": "why"}}]},
            {"choices": [{"delta": {"content": "a1"}, "finish_reason": "stop"}]},
        ],
        [{"choices": [{"delta": {"content": "a2"}, "finish_reason": "stop"}]}],
    ]
    _HttpClient.requests = []
    monkeypatch.setattr(openai.httpx, "AsyncClient", _HttpClient)
    events = await _capture(tmp_path)
    tools = [{"type": "function", "function": {
        "name": "read", "description": "Read a file",
        "parameters": {"type": "object"},
    }}]
    subject = agent.OpenAI({
        "model": "m", "base_url": "http://fake", "provider": "p",
        "system_prompt": "system", "tools": tools,
        "parameters": {"temperature": 0},
    })
    async with subject.session(session="openai/s"):
        assert await subject.result("q1") == "a1"
        assert await _collect(subject.stream("q2")) == "a2"
    await _settle()
    assert not [event for event in events if event["type"] == "context/set"]
    assert next(event for event in events if event["type"] == "agent/set")["data"][
        "config"] == {
            "model": "m", "base_url": "http://fake", "provider": "p",
            "system_prompt": "system", "tools": tools, "parameters": {"temperature": 0},
        }
    assert [
        {key: value for key, value in event["data"].items() if key != "requestId"}
        for event in events if event["type"] == "model/request"
    ] == [
        {"model": "m", "parameters": {"temperature": 0}, "provider": "p"},
        {"model": "m", "parameters": {"temperature": 0}, "provider": "p"},
    ]
    context = fold_state(events)["context"]
    assert context[0] == {
        "type": "message", "role": "system",
        "content": [
            instruction("system"),
            tool_defs([{
                "name": "read", "description": "Read a file",
                "inputSchema": {"type": "object"},
            }]),
        ],
    }
    assert _context_labels(context) == [
        "system", "user", "reasoning", "assistant", "user", "assistant",
    ]
    assert [event["data"]["requestId"] for event in events
            if event["type"] == "model/request"] == ["r1", "r2"]
    assert _context_at_requests(events) == [
        ["system", "user"],
        ["system", "user", "reasoning", "assistant", "user"],
    ]
    assert _HttpClient.requests == [
        {
            "temperature": 0,
            "model": "m",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "q1"},
            ],
            "stream": True,
            "tools": tools,
        },
        {
            "temperature": 0,
            "model": "m",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1", "reasoning_content": "why"},
                {"role": "user", "content": "q2"},
            ],
            "stream": True,
            "tools": tools,
        },
    ]
    _assert_inferences(events)


@pytest.mark.asyncio
async def test_openai_normalizes_one_complete_inference(monkeypatch, tmp_path) -> None:
    from gh_puller.agent.adapters import openai

    _HttpClient.scripts = [[
        {"model": "actual", "choices": [{"delta": {"reasoning_content": "why"}}]},
        {"choices": [{"delta": {"content": "answer"}}]},
        {"usage": {"prompt_tokens": 2, "completion_tokens": 3}, "choices": [{
            "delta": {"tool_calls": [{
                "index": 0, "id": "c1",
                "function": {"name": "read", "arguments": '{"path":"a.py"}'},
            }]},
            "finish_reason": "tool_calls",
        }]},
    ]]
    monkeypatch.setattr(openai.httpx, "AsyncClient", _HttpClient)
    events = await _capture(tmp_path)
    subject = agent.OpenAI({
        "model": "configured", "base_url": "http://fake", "api_key": "secret",
    })
    async with subject.session(session="openai/output"):
        assert await subject.result("question") == "answer"
    await _settle()
    response = next(event for event in events if event["type"] == "model/response")
    assert response["data"] == {
        "requestId": "r1",
        "output": [
            reasoning_item("why"),
            text_message("assistant", "answer"),
            function_call_item("c1", "read", '{"path":"a.py"}'),
        ],
        "model": "actual",
        "usage": {"input": 2, "output": 3},
        "stopReason": "tool_calls",
    }
    config = next(event for event in events if event["type"] == "agent/set")["data"]["config"]
    assert config["api_key"] == "<redacted>"
    _assert_inferences(events)
