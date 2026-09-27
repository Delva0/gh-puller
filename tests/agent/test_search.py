"""Exercise package search agents without experiment recording or checkout-relative state."""

import json
import subprocess
import sys

import httpx
import pytest

from gh_puller.agent import sinks
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
