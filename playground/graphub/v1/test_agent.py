"""Check the minimal agent's actual HTTP protocol, tool loop and canonical observations."""
# ruff: noqa: S101 - Research tests use pytest assertions.

import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
import pytest_asyncio

from gh_puller.agent.base import BaseAgent, RequestFailedError
from gh_puller.agent.events import EventBus, fold_state, set_active_bus

from .agent import DEFAULT_LIMITS, BudgetExceededError, Limits, MinimalAgent, Tool

CONFIG = {"model": "test-model", "base_url": "https://provider.invalid/v1", "system_prompt": "Use static evidence."}
SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}},
          "required": ["path"], "additionalProperties": False}


def call(identifier="c1", name="Read", arguments='{"path":"demo.py"}'):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


def packet(text=None, calls=(), *, reason=None, usage=True):
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = list(calls)
    result = {"model": "served-model", "choices": [{
        "message": message, "finish_reason": reason or ("tool_calls" if calls else "stop"),
    }]}
    if usage:
        result["usage"] = {"prompt_tokens": 100, "completion_tokens": 10}
    return result


def scripted(*packets):
    requests, pending = [], iter(packets)

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=deepcopy(next(pending)))

    return httpx.MockTransport(respond), requests


async def read(arguments):
    return "source:" + arguments["path"]


def subject(*packets, limits=DEFAULT_LIMITS, tools=None, config=None):
    transport, requests = scripted(*packets)
    instance = MinimalAgent(CONFIG | (config or {}), tools if tools is not None else [
        Tool("Read", "Read static source.", SCHEMA, read),
    ], limits=limits, transport=transport)
    return instance, requests


@pytest_asyncio.fixture
async def events():
    result, bus = [], EventBus()

    async def collect(event):
        result.append(event)

    bus.add(collect)
    set_active_bus(bus)
    yield result
    bus.shutdown()
    set_active_bus(None)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_baseagent_loop_retains_tools_reasoning_and_observable_context(events):
    first = packet("Inspect the caller.", [call(), call("c2", arguments='{"path":"caller.py"}')])
    first["choices"][0]["message"]["reasoning_content"] = "Need both definitions."
    instance, requests = subject(first, packet("Static explanation with source citations."),
                                 packet("Follow-up answer."), config={"api_key": "test-secret"})
    assert isinstance(instance, BaseAgent)
    async with instance.session(session="v1/protocol"):
        assert await instance.result("traceback + help") == "Static explanation with source citations."
        assert instance.stats["steps"] == 2 and instance.stats["tool_calls"] == 2
        assert instance.stats["usage"] == [{"prompt_tokens": 100, "completion_tokens": 10}] * 2
        assert [part async for part in instance.stream("Why?")] == ["Follow-up answer."]
    await asyncio.sleep(0)
    assert instance._client.is_closed
    assert requests[1]["messages"][2] == first["choices"][0]["message"]
    assert [message["tool_call_id"] for message in requests[1]["messages"] if message["role"] == "tool"] == ["c1", "c2"]
    assert requests[2]["messages"][-2:] == [
        {"role": "assistant", "content": "Static explanation with source citations."},
        {"role": "user", "content": "Why?"},
    ]
    for request in requests:
        assert request["tools"] == [instance.tools["Read"].definition()]
        assert request["model"] == CONFIG["model"] and request["max_completion_tokens"] == Limits().output_tokens
    context = fold_state(events)["context"]
    assert [item.get("role", item["type"]) for item in context] == [
        "system", "user", "reasoning", "assistant", "function_call", "function_call",
        "function_call_output", "function_call_output", "assistant", "user", "assistant",
    ]
    recorded_outputs = [item["output"] for item in context if item["type"] == "function_call_output"]
    assert recorded_outputs == [message["content"] for message in requests[1]["messages"] if message["role"] == "tool"]
    config = next(event["data"]["config"] for event in events if event["type"] == "agent/set")
    assert config["api_key"] == "<redacted>" and "test-secret" not in json.dumps(events)
    assert [event["data"]["outcome"] for event in events if event["type"] == "session/end"] == ["completed"]
    assert next(event["data"]["usage"] for event in events if event["type"] == "session/end") == {
        "input": 300, "output": 30,
    }
    for index, event in enumerate(events):
        if event["type"] == "model/response":
            assert events[index + 1]["type"] == "context/append/assistant"
            assert events[index + 1]["data"]["items"] == event["data"]["output"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_call", [call(arguments="{"), call(arguments='{"wrong":1}'),
                                     call(arguments='{"path":8}'), call(name="Unknown")])
async def test_invalid_tool_calls_are_observed_and_the_model_can_revise(events, bad_call):
    instance, requests = subject(packet(calls=[bad_call]), packet(calls=[call("c2")]), packet("Revised answer."))
    async with instance.session(session="v1/revise"):
        assert await instance.result("q") == "Revised answer."
    assert instance.stats["tool_errors"] == 1 and instance.stats["tool_calls"] == 2
    error = json.loads(requests[1]["messages"][-1]["content"])
    assert error["ok"] is False
    assert json.loads(requests[2]["messages"][-1]["content"])["ok"] is True


@pytest.mark.asyncio
async def test_result_truncation_is_explicit_and_identical_in_context(events):
    instance, requests = subject(packet(calls=[call()]), packet("a"), limits=replace(Limits(), tool_chars=4))
    async with instance.session():
        await instance.result("q")
    await asyncio.sleep(0)
    content = requests[1]["messages"][-1]["content"]
    assert json.loads(content) == {"ok": True, "text": "sour", "characters": 14, "truncated": True}
    assert next(event["data"]["result"] for event in events if event["type"] == "tool/end") == content


@pytest.mark.asyncio
@pytest.mark.parametrize(("limits", "calls"), [(replace(Limits(), steps=1), [call()]),
                                         (replace(Limits(), tool_calls=1), [call(), call("c2")])])
async def test_budget_refuses_the_entire_unexecutable_batch(events, limits, calls):
    instance, requests = subject(packet("Unfinished thought.", calls), limits=limits)
    with pytest.raises(BudgetExceededError):
        async with instance.session():
            await instance.result("q")
    await asyncio.sleep(0)
    assert len(requests) == 1 and instance.stats["tool_calls"] == 0
    assert not any(event["type"] == "tool/start" for event in events)
    assert instance.stats["outcome"] == "failed" and instance._client.is_closed
    assert next(event["data"]["outcome"] for event in events if event["type"] == "session/end") == "failed"
    assert next(event["data"]["reasonCode"] for event in events if event["type"] == "session/end") == "budget_exhausted"


@pytest.mark.asyncio
async def test_cancellation_waits_for_tool_cleanup_and_closes_the_http_client(events):
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def wait_tool(arguments):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    instance, _ = subject(packet(calls=[call()]), tools=[Tool("Read", "Wait", SCHEMA, wait_tool)])

    async def run():
        async with instance.session():
            await instance.result("q")

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert cleaned.is_set() and instance._client.is_closed
    assert any(event["type"] == "tool/end" and "error" in event["data"] for event in events)
    assert next(event["data"]["reasonCode"] for event in events if event["type"] == "session/end") == "cancelled"


@pytest.mark.asyncio
async def test_deadline_and_interrupted_history_do_not_silently_restart(events):
    async def wait_tool(arguments):
        await asyncio.Event().wait()

    instance, requests = subject(packet(calls=[call()]), limits=replace(Limits(), seconds=0.02),
                                 tools=[Tool("Read", "Wait", SCHEMA, wait_tool)])
    async with instance.session():
        with pytest.raises(BudgetExceededError, match="deadline"):
            await instance.result("q")
        with pytest.raises(RuntimeError, match="interrupted"):
            await instance.result("retry")
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [packet("partial", reason="length"), packet(reason="content_filter"),
                                   packet(), packet(calls=[call(), call()])])
async def test_incomplete_or_invalid_provider_output_cannot_become_a_final_answer(events, reply):
    instance, _ = subject(reply)
    with pytest.raises(RequestFailedError):
        async with instance.session():
            await instance.result("q")
    assert instance.stats["tool_calls"] == 0 and instance._client.is_closed


@pytest.mark.asyncio
async def test_missing_usage_stays_unknown_and_empty_tool_collection_is_allowed(events):
    instance, requests = subject(packet("a", usage=False), tools=[])
    async with instance.session():
        assert await instance.result("q") == "a"
    assert instance.stats["usage"] == [None] and "tools" not in requests[0]


@pytest.mark.asyncio
async def test_session_usage_sums_nested_provider_details(events):
    first, final = packet(calls=[call()]), packet("a")
    for reply in (first, final):
        reply["usage"].update({"prompt_tokens_details": {"cached_tokens": 60},
                               "completion_tokens_details": {"reasoning_tokens": 5}})
    instance, _ = subject(first, final)
    async with instance.session():
        await instance.result("q")
    await asyncio.sleep(0)
    summary = next(event["data"] for event in events if event["type"] == "session/end")
    assert summary["usage"] == {"input": 200, "output": 20, "cacheRead": 120, "reasoning": 10}
    responses = [event["data"] for event in events if event["type"] == "model/response"]
    assert responses[0]["usage"] == {"input": 100, "output": 10, "cacheRead": 60, "reasoning": 5}
    assert responses[0]["rawUsage"] == first["usage"]
    assert instance.stats["usage"] == [first["usage"], final["usage"]]


@pytest.mark.asyncio
async def test_one_missing_usage_report_prevents_a_false_session_total(events):
    instance, _ = subject(packet(calls=[call()], usage=False), packet("a"))
    async with instance.session():
        await instance.result("q")
    await asyncio.sleep(0)
    assert "usage" not in next(event["data"] for event in events if event["type"] == "session/end")
    assert instance.stats["usage"][0] is None


@pytest.mark.asyncio
async def test_failed_http_request_remains_an_unknown_usage_measurement(events):
    first = packet(calls=[call()])
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=first) if len(requests) == 1 else httpx.Response(503)

    instance = MinimalAgent(CONFIG, [Tool("Read", "Read", SCHEMA, read)], transport=httpx.MockTransport(respond))
    with pytest.raises(httpx.HTTPStatusError):
        async with instance.session():
            await instance.result("q")
    await asyncio.sleep(0)
    assert instance.stats["steps"] == 2
    assert instance.stats["usage"] == [first["usage"], None]
    assert "usage" not in next(event["data"] for event in events if event["type"] == "session/end")


@pytest.mark.asyncio
async def test_provider_output_limit_field_is_explicit_and_recorded(events):
    instance, requests = subject(packet("a"), config={"max_tokens_field": "max_tokens"})
    async with instance.session():
        await instance.result("q")
    await asyncio.sleep(0)
    assert requests[0]["max_tokens"] == instance.limits.output_tokens
    assert "max_completion_tokens" not in requests[0]
    request = next(event["data"] for event in events if event["type"] == "model/request")
    assert request["parameters"]["max_tokens"] == instance.limits.output_tokens


@pytest.mark.asyncio
async def test_result_requires_a_baseagent_session(events):
    instance, requests = subject(packet("a"))
    with pytest.raises(RuntimeError):
        await instance.result("q")
    assert not requests


@pytest.mark.parametrize("parameters", [{"model": "other"}, {"messages": []}, {"max_tokens": 1}, {"n": 2}])
def test_provider_parameters_cannot_change_the_comparison_contract(parameters):
    with pytest.raises(ValueError, match="protocol"):
        MinimalAgent(CONFIG | {"parameters": parameters}, [])


def test_duplicate_tool_names_and_invalid_limits_are_rejected():
    tool = Tool("Read", "Read", SCHEMA, read)
    with pytest.raises(ValueError, match="unique"):
        MinimalAgent(CONFIG, [tool, tool])
    with pytest.raises(ValueError, match="positive"):
        Limits(steps=0)
