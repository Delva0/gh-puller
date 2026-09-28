"""Exercise package search agents without experiment recording or checkout-relative state."""

import asyncio
import base64
import copy
import json
import subprocess
import sys

import httpx
import pytest

from gh_puller.agent import sinks
from gh_puller.agent.events import EventBus, fold_state, set_active_bus
from gh_puller.agents import GitCodeAgent, GitHubAgent, WebAgent
from gh_puller.tools.storage import ToolStorage
from gh_puller.tools.tool_gitcode import GitCodeTool
from gh_puller.tools.tool_github import GitHubRESTTool

from ._support import capture


def completion(message):
    packet = {"choices": [{"delta": message,
                           "finish_reason": "tool_calls" if "tool_calls" in message else "stop"}]}
    body = f"data: {json.dumps(packet)}\n\ndata: [DONE]\n\n"
    return httpx.Response(200, stream=httpx.ByteStream(body.encode()))


def call(name, arguments, identity="read"):
    return {"tool_calls": [{"index": 0, "id": identity, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("owned_storage", [False, True])
@pytest.mark.parametrize(("agent_class", "tool", "arguments"), [
    (GitHubAgent, "github", {"requests": [{"path": "/repos/o/r"}]}),
    (GitCodeAgent, "gitcode", {"requests": [{"path": "/repos/o/r"}]}),
    (WebAgent, "web_fetch", {"requests": [{"url": "https://example.org/evidence"}]}),
])
async def test_search_loop_uses_runtime_storage_and_canonical_events(
    tmp_path, agent_class, tool, arguments, owned_storage,
):
    events = await capture(tmp_path / "events")
    requests, reads = [], []

    def model(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == "test-model" and body["reasoning_effort"] == "high"
        if len(requests) == 1:
            return completion(call(tool, arguments))
        assert any(m["role"] == "tool" and "evidence" in m["content"] for m in body["messages"])
        return completion({"content": "Answer from evidence"})

    def source(request):
        reads.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"id": 1, "name": "evidence"})

    transport = httpx.MockTransport(source)
    options = {"web_search_backend": "duckduckgo"}
    connection = {"web_transport": transport}
    if agent_class is not WebAgent:
        options["backend"] = "rest"
        options["tool_result_num_user_query"] = 2
        connection[agent_class.name + "_transport"] = transport
    config = {"model": "test-model", "base_url": "https://model.example/v1", "agent_options": options,
              "parameters": {"reasoning_effort": "high"}}
    storage = None if owned_storage else ToolStorage(tmp_path / "work")
    agent = agent_class(config, storage, api_key="test", model_transport=httpx.MockTransport(model), **connection)
    work = agent.storage.root
    async with agent.session(session="search"):
        assert await agent.result("Find evidence") == "Answer from evidence"
        assert len(reads) == 1
        files = [p for p in work.rglob("*") if p.is_file()]
        assert files and all(p.suffix in {".body", ".md"} or p.name.endswith("-tool-result.json") for p in files)
        assert await agent.result("Summarize the same evidence") == "Answer from evidence"
        assert len(reads) == 1 and len(requests) == 3
    await sinks.flush()
    assert {e["type"] for e in events} >= {"session/start", "model/request", "tool/start", "tool/end", "session/end"}
    assert events[-1]["data"]["outcome"] == "completed"
    assert work.exists() is not owned_storage
    assert not any(p.name in {"manifest.json", "timeline.jsonl", "session-state.json"} for p in tmp_path.rglob("*"))


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_class", "method"), [(GitHubRESTTool, "github_rest"), (GitCodeTool, "gitcode_api")])
async def test_saved_api_response_does_not_need_a_diagnostic_recorder(tmp_path, tool_class, method):
    reads = []

    def source(request):
        reads.append(request)
        return httpx.Response(200, json={"name": "exact evidence", "nested": {"value": 42}})

    storage = ToolStorage(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(source)) as client:
        tool = tool_class(client, storage, token="")
        read = getattr(tool, method)
        result = (await read("read", [{"path": "/repos/o/r"}]))["results"][0]
        saved = (await read("saved", [{"result_id": result["result_id"], "json_pointer": "/nested/value"}]))[
            "results"][0]
    assert saved["data"] == 42 and len(reads) == 1
    assert all(p.suffix == ".body" for p in tmp_path.iterdir())


@pytest.mark.asyncio
async def test_offload_retrieval_without_experiment_files(tmp_path):
    requests, reads = [], []

    def model(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return completion(call("github", {"requests": [{"path": "/repos/o/r"}]}))
        if len(requests) == 3:
            assert any("Tool result offloaded" in m.get("content", "")
                       for m in body["messages"] if m["role"] == "tool")
            return completion(call("get_tool_result", {"tool_call_ids": ["read"]}, "restore"))
        if len(requests) == 4:
            restored = json.loads(body["messages"][-1]["content"])
            assert "original evidence" in json.dumps(restored)
        return completion({"content": "Answer"})

    def source(request):
        reads.append(request)
        return httpx.Response(200, json={"name": "original evidence"})

    config = {"model": "test", "base_url": "https://model.example", "agent_options": {
        "backend": "rest", "web_search_backend": "duckduckgo", "tool_result_num_user_query": 1,
        "tool_result_preview_lines": 1, "tool_result_preview_chars": 10,
    }}
    agent = GitHubAgent(config, ToolStorage(tmp_path), api_key="test",
                        model_transport=httpx.MockTransport(model), github_transport=httpx.MockTransport(source))
    async with agent.session(session="offload"):
        assert await agent.result("Read") == "Answer"
        assert await agent.result("Retrieve") == "Answer"
    assert len(reads) == 1 and len(requests) == 4
    assert all(p.suffix == ".body" or p.name.endswith("-tool-result.json") for p in tmp_path.iterdir())


def test_observation_package_does_not_import_concrete_agents_or_tools():
    source = """
import importlib.abc
import sys

class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("gh_puller.agents", "gh_puller.tools", "graphub")):
            raise RuntimeError(fullname)

sys.meta_path.insert(0, Boundary())
import gh_puller.agent
from gh_puller.agent.events import fold_state
assert fold_state([]) == {"agent": None, "context": []}
"""
    subprocess.run([sys.executable, "-I", "-c", source], check=True, capture_output=True, text=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [GitHubAgent, GitCodeAgent, WebAgent])
async def test_event_prefixes_restore_own_memory_and_foreign_context_only(tmp_path, target):
    events = []
    count = 0
    source = None

    class Observations(EventBus):
        @property
        def enabled(self):
            return True

        def publish(self, event):
            if event["session"] != "original":
                return
            events.append(copy.deepcopy(event))

    set_active_bus(Observations())

    def model(request):
        nonlocal count
        count += 1
        if count == 1:
            return completion(call("github", {"requests": [{"path": "/repos/o/r"}]}))
        return completion({"content": "Evidence found"})

    config = {"model": "test", "base_url": "https://model.example", "agent_options": {
        "web_search_backend": "duckduckgo", "tool_result_preview_chars": 1,
    }}
    source = GitHubAgent(config, ToolStorage(tmp_path / "source"), api_key="test",
                         model_transport=httpx.MockTransport(model),
                         github_transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"name": "evidence"})))
    async with source.session(session="original"):
        await source.result("Read original evidence")
        await source.early_answers.early_answer("early", "An observed partial answer")
        final_prefix = len(events)
        source.clear_context()
    assert any(event["type"] == "artifact/saved" for event in events)
    assert not any(event["type"] in {"context/checkpoint", "search/state", "search/artifact"} for event in events)
    saved_at = next(i for i, e in enumerate(events) if e["type"] == "tool_result/saved")
    cleared_at = next(i for i, e in enumerate(events) if e["type"] == "tool_result/cleared")
    response_at = next(i for i, e in enumerate(events) if e["type"] == "github/response_saved")
    response_id = events[response_at]["data"]["metadata"]["result_id"]
    response_clear = next(i for i, e in enumerate(events) if e["type"] == "github/cleared")
    early_at = next(i for i, e in enumerate(events) if e["type"] == "agent/set/early_answers")
    early_clear = next(i for i, e in enumerate(events)
                       if e["type"] == "agent/set/early_answers" and not e["data"]["early_answers"])
    prefixes = range(1, len(events) + 1) if target is GitHubAgent else [final_prefix]
    for end in prefixes:
        options = {"web_search_backend": "duckduckgo", "web_search_interval": 7}
        resumed = target({**config, "agent_options": options}, ToolStorage(tmp_path / f"target-{end}"), api_key="test")
        async with resumed.session(session=f"resumed-{end}"):
            resumed.load_events(events[:end])
            assert resumed.web_tools.search_interval == 7
            assert len([m for m in resumed.messages if m["role"] == "system"]) == 1
            def conversation(items):
                return [{k: v for k, v in item.items() if k not in {"id", "metadata"}}
                        for item in items if item.get("role") != "system"]
            assert conversation(resumed._require_event_recorder().context()) == conversation(
                fold_state(events[:end])["context"]), (end, events[end - 1])
            if target is GitHubAgent:
                assert set(resumed.tool_results.saved) == ({"read"} if saved_at < end <= cleared_at else set())
                assert set(resumed.tools.responses) == ({response_id} if response_at < end <= response_clear else set())
                assert bool(resumed.early_answers.answers) == (early_at < end <= early_clear)
                if resumed.tool_results.saved:
                    result = await resumed.tool_results.get_tool_result("again", ["read"])
                    assert "evidence" in json.loads(result.content)["results"][0]["content"]
                if resumed.tools.responses:
                    assert b"evidence" in resumed.tools._body(resumed.tools._saved(response_id))
            else:
                assert not resumed.tool_results.saved and not resumed.early_answers.answers
                assert not list(resumed.storage.root.iterdir())
                if target is GitCodeAgent:
                    assert not resumed.tools.responses


@pytest.mark.asyncio
async def test_synchronous_provider_artifacts_join_the_same_event_stream(tmp_path):
    events = await capture(tmp_path / "events")
    config = {"model": "test", "base_url": "https://model.example", "agent_options": {
        "web_search_backend": "duckduckgo",
    }}
    source = WebAgent(config, ToolStorage(tmp_path / "source"), api_key="test")
    async with source.session(session="original"):
        await asyncio.to_thread(source.storage.write, "worker.body", b"worker evidence")
        await asyncio.to_thread(source.web_tools.remember_resource, {"ref": "worker", "body_file": "worker.body"})
    await sinks.flush()
    restored = WebAgent(config, ToolStorage(tmp_path / "restored"), api_key="test")
    async with restored.session(session="restored"):
        restored.load_events(list(events))
        assert restored.web_tools.resources == {"worker": {"ref": "worker", "body_file": "worker.body"}}
        assert restored.storage.read("worker.body") == b"worker evidence"


@pytest.mark.asyncio
async def test_own_image_context_survives_repeated_reconstruction(tmp_path):
    events = await capture(tmp_path / "events")
    requests, downloads = [], []
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0eoAAAAASUVORK5CYII=")

    def model(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return completion(call("web_fetch", {"requests": [{"url": "https://example.org/image.png"}]}))
        return completion({"content": "An image"})

    def download(request):
        downloads.append(request)
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    config = {"model": "test", "base_url": "https://model.example", "agent_options": {
        "web_search_backend": "duckduckgo",
    }}
    source = WebAgent(config, ToolStorage(tmp_path / "source"), api_key="test",
                      model_transport=httpx.MockTransport(model), web_transport=httpx.MockTransport(download))
    async with source.session(session="source"):
        await source.result("See this image")
        messages = copy.deepcopy(source.messages[1:])
    assert any(isinstance(m.get("content"), list) for m in messages)
    await sinks.flush()
    history = [e for e in events if e["session"] == "source"]
    for generation in range(2):
        name = f"restored-{generation}"
        target = WebAgent(config, ToolStorage(tmp_path / name), api_key="test",
                          model_transport=httpx.MockTransport(model))
        async with target.session(session=name):
            target.load_events(history)
            assert target.messages[1:] == messages
            if generation == 1:
                await target.result("Continue")
                assert requests[-1]["messages"][1:-1] == messages
        await sinks.flush()
        history = [e for e in events if e["session"] == name]
    assert len(downloads) == 1


@pytest.mark.asyncio
async def test_foreign_function_calls_keep_pairing_without_shared_item_ids(tmp_path):
    from gh_puller.agent.events import function_call_item, function_output_item, new_event

    config = {"model": "test", "base_url": "https://model.example", "agent_options": {
        "web_search_backend": "duckduckgo",
    }}
    requests = []

    def model(request):
        requests.append(json.loads(request.content))
        return completion({"content": "Continued"})

    items = [{**function_call_item(call_id, "foreign_tool", "{}"), "id": f"item-{index}"}
             for index, call_id in enumerate(("a", "b"))]
    items.extend(function_output_item(call_id, result) for call_id, result in (("a", "first"), ("b", "second")))
    agent = WebAgent(config, ToolStorage(tmp_path), api_key="test", model_transport=httpx.MockTransport(model))
    async with agent.session():
        agent.load_events([new_event("context/set", items=items)])
        await agent.result("Continue")
        assert [m["content"] for m in requests[0]["messages"] if m["role"] == "tool"] == ["first", "second"]
        assert not agent.web_tools.resources
