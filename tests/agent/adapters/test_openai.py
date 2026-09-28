"""Test the OpenAI-compatible Agent adapter contract."""

import json
from copy import deepcopy
from typing import ClassVar

import httpx
import pytest

from gh_puller import agent
from gh_puller.agent.adapters.openai import ChatCompletion
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
        {key: value for key, value in event["data"].items() if key not in {"requestId", "requestSha256"}}
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
    request_hashes = [event["data"]["requestSha256"] for event in events
                      if event["type"] == "model/request"]
    assert len(set(request_hashes)) == 2 and all(len(value) == 64 for value in request_hashes)
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
        "rawUsage": {"prompt_tokens": 2, "completion_tokens": 3},
        "stopReason": "tool_calls",
    }
    config = next(event for event in events if event["type"] == "agent/set")["data"]["config"]
    assert config["api_key"] == "<redacted>"
    _assert_inferences(events)


@pytest.mark.asyncio
async def test_openai_correlates_a_failed_stream(monkeypatch, tmp_path) -> None:
    from gh_puller.agent.adapters import openai

    _HttpClient.scripts = [[{"choices": [{"delta": {"content": "partial"}}]}]]
    monkeypatch.setattr(openai.httpx, "AsyncClient", _HttpClient)
    events = await _capture(tmp_path)
    subject = agent.OpenAI({"model": "m", "base_url": "http://fake"})
    with pytest.raises(agent.RequestFailedError, match="finish normally"):
        async with subject.session(session="openai/failure"):
            await subject.result("question")
    await _settle()
    request = next(event for event in events if event["type"] == "model/request")
    failure = next(event for event in events if event["type"] == "model/error")
    assert failure["data"]["requestId"] == request["data"]["requestId"]
    assert failure["data"]["error"]["type"] == "RequestFailedError"
    assert not any(event["type"] == "model/response" for event in events)


@pytest.mark.asyncio
async def test_images_retry_as_placeholders_without_changing_history_or_limits(tmp_path):
    events = await _capture(tmp_path)
    requests = []
    body = {"model": "any-model", "reasoning_effort": "provider-custom-value", "max_tokens": 0,
            "max_completion_tokens": 0, "tools": [], "messages": [
                {"role": "tool", "tool_call_id": "evidence", "content": "original evidence"},
                {"role": "user", "content": [{"type": "text", "text": "inspect"},
                                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]},
            ]}
    original = deepcopy(body)

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(400, json={"error": "vision unsupported"})
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\n')

    subject = agent.OpenAI({"model": "any-model", "base_url": "https://model.example"})
    async with subject.session(session="images"), httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        completion = ChatCompletion(client, subject._require_event_recorder(), body=body,
                                    base_url="https://model.example")
        assert (await completion.result())["content"] == "answer"
    await _settle()
    assert body == original
    assert requests[0]["messages"][1]["content"][1]["type"] == "image_url"
    assert requests[1]["messages"][1]["content"][1] == {"type": "text", "text": "<image>"}
    for request in requests:
        assert "max_tokens" not in request and "max_completion_tokens" not in request
        assert request["reasoning_effort"] == "provider-custom-value"
        assert request["messages"][0] == original["messages"][0]
    starts = [event["data"] for event in events if event["type"] == "model/request"]
    assert len(starts) == 2 and starts[1]["imageFallback"]
    assert starts[0]["requestSha256"] != starts[1]["requestSha256"]
    ends = {event["type"]: event["data"] for event in events if event["type"] in {"model/error", "model/response"}}
    assert ends["model/error"]["requestId"] == starts[0]["requestId"]
    assert ends["model/response"]["requestId"] == starts[1]["requestId"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["authentication", "partial", "rejected_twice"])
async def test_image_fallback_does_not_retry_auth_partial_output_or_loop(tmp_path, failure):
    await _capture(tmp_path)
    requests = []

    def respond(request):
        requests.append(request)
        if failure == "partial":
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                                  'data: {"error":"image unsupported"}\n\n')
        return httpx.Response(401 if failure == "authentication" else 422, text="unsupported")

    body = {"model": "any", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://example.org/a.png"}},
    ]}]}
    subject = agent.OpenAI({"model": "any", "base_url": "https://model.example"})
    async with subject.session(session="failure"), httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises((httpx.HTTPStatusError, agent.RequestFailedError)):
            await ChatCompletion(client, subject._require_event_recorder(), body=body,
                                 base_url="https://model.example").result()
    assert len(requests) == (2 if failure == "rejected_twice" else 1)
