"""Replay tool-owned observations without a search Agent or an object snapshotter."""

import copy
import json
import time

import httpx
import pytest

from gh_puller.agent.base import BaseAgent
from gh_puller.agent.events import new_event, text_message
from gh_puller.agents import GitHubAgent, WebAgent
from gh_puller.tools.registry import ToolProvider, ToolRegistry, tool
from gh_puller.tools.storage import ToolStorage
from gh_puller.tools.tool_bash import DockerBashTools
from gh_puller.tools.tool_gitcode import GitCodeTool
from gh_puller.tools.tool_github import GitHubRESTTool
from gh_puller.tools.tool_offload import OffloadPolicy, ToolOutput, ToolResultStore


def observed_storage(path):
    events = []
    storage = ToolStorage(path, observer=lambda kind, **data: events.append(
        {"type": kind, "data": copy.deepcopy(data)}))
    return storage, events


@pytest.mark.asyncio
async def test_base_replays_only_context_and_preserves_arbitrary_items():
    class Adapter(BaseAgent):
        agent = "target"

        async def _enter(self):
            self._require_event_recorder().append_context(text_message("system", "target instructions"))

        async def _exit(self, exc):
            pass

        def _load_context(self, items):
            self.native = items

    unknown = {"type": "custom_item", "value": {"nested": [1, {"observation": "opaque"}]}}
    events = [new_event("context/set", items=[text_message("system", "foreign instructions"), unknown]),
              new_event("foreign/private", value="do not restore"),
              new_event("context/append/user", items=[text_message("user", "continue")])]
    target = Adapter({})
    async with target.session():
        target.load_events(events)
        assert target.native == [text_message("system", "foreign instructions"), unknown,
                                 text_message("user", "continue")]
        assert target.native == target._require_event_recorder().context()
        target.native[1]["value"]["nested"].clear()
        assert unknown["value"]["nested"]


def test_registry_delegates_once_to_an_independent_provider():
    class Library(ToolProvider):
        def __init__(self):
            self.restored = []

        @tool(description="Read", parameters={})
        async def read(self, call_id):
            pass

        @tool(description="List", parameters={})
        async def list(self, call_id):
            pass

        def load_events(self, events):
            self.restored.append(events)

    library = Library()
    registry = ToolRegistry((library, {"read": "read_alias"}))
    events = [new_event("library/selected", books=["one", "two"])]
    registry.load_events(events)
    assert library.restored == [events]


def test_private_replay_uses_execution_boundaries_not_config_observations():
    start = new_event("session/start", label="same")
    identity = new_event("agent/set", agent="search-github", config={"model": "a"})
    fact = new_event("github/cleared", tool="github_rest")
    changed = new_event("agent/set", agent="search-github", config={"model": "b"})
    events = [start, identity, fact, changed]
    assert GitHubAgent.own_events(events) == [fact, changed]
    assert not WebAgent.own_events(events)
    assert not GitHubAgent.own_events([*events, start, identity])
    foreign = new_event("agent/set", agent="search-web", config={})
    assert not GitHubAgent.own_events([*events, foreign, identity])


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "method"), [(GitHubRESTTool, "github_rest"), (GitCodeTool, "gitcode_api")])
@pytest.mark.parametrize("legacy", [False, True])
async def test_api_tools_restore_evidence_and_rate_limits_with_cold_caches(tmp_path, provider, method, legacy):
    reads = []

    def source(request):
        reads.append(request)
        return httpx.Response(200, json={"id": 42, "name": "original evidence"})

    storage, events = observed_storage(tmp_path / "original")
    async with httpx.AsyncClient(transport=httpx.MockTransport(source)) as client:
        original = provider(client, storage, token="")
        original.api.reuse_reads = True
        result = (await getattr(original, method)("read", [{"path": "/repos/o/r"}]))["results"][0]
        until = time.time() + 1000
        if provider is GitHubRESTTool:
            original.api.name_resource(("/repos/o/r", "lexical"), "core")
            original.api.cooldown("all", until)
        else:
            original.api.cooldown_until(until)
        if legacy:
            for event in events:
                if event["type"].endswith("/response_saved"):
                    event["data"] = {"tool": original.api.scope, "metadata": copy.deepcopy(
                        original.responses[result["result_id"]])}
        before = copy.deepcopy(events)
        original_events = events
        for generation in range(2):
            restored_storage, replayed = observed_storage(tmp_path / f"restored-{generation}")
            restored_storage.load_events(events, resolve_artifact=storage.read_artifact)
            restored = provider(client, restored_storage, token="")
            restored.api.reuse_reads = True
            restored.load_events(events)
            saved = (await getattr(restored, method)("saved", [{"result_id": result["result_id"]}]))["results"][0]
            assert saved["data"]["id"] == 42
            assert not restored.api._reads.responses and len(reads) == 1
            if provider is GitHubRESTTool:
                assert restored.api.cooldowns["all"] == until
                assert not restored.api.resource_names
            else:
                assert restored.api.cooldown == until
            events = list(replayed)
            assert all("metadata" not in e["data"] for e in events if e["type"].endswith("/response_saved"))
        restored.begin_query()
        blocked = (await getattr(restored, method)("fresh", [{"path": "/repos/o/r"}]))["results"][0]
        assert "error" in blocked and len(reads) == 1
        if provider is GitHubRESTTool:
            restored.api.cooldowns.clear()
        else:
            restored.api.cooldown = 0
        fresh = (await getattr(restored, method)("new-read", [{"path": "/repos/o/r"}]))["results"][0]
        assert fresh["result_id"] != result["result_id"] and len(reads) == 2
        assert not any(e["type"].endswith(("/read_cached", "/query_started", "/resource_named")) for e in events)
        assert before[0] == {"type": "artifact/allocated", "data": {"sequence": 1}}
        start = len(original_events)
        original.clear_context()
        for end in range(start + 1, len(original_events) + 1):
            cleared_storage = ToolStorage(tmp_path / f"clear-{end}")
            cleared_storage.load_events(original_events[:end], resolve_artifact=storage.read_artifact)
            cleared = provider(client, cleared_storage, token="")
            cleared.load_events(original_events[:end])
            assert not cleared.responses and not cleared.api._reads.responses


@pytest.mark.asyncio
async def test_offload_restores_retention_and_attachments_without_reexecuting(tmp_path):
    storage, events = observed_storage(tmp_path / "source")
    storage.write("image.body", b"image bytes")
    parts = [{"type": "input_image", "image_url": "image.body", "media_type": "image/png"}]
    images = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1hZ2UgYnl0ZXM=", "detail": "auto"}}]
    message = {"role": "tool", "tool_call_id": "read", "content": "Complete original evidence"}
    image = {"role": "user", "content": images}
    source = ToolResultStore(storage)
    source.begin_user_query()
    source.bind(ToolOutput(message["content"], images=images, observations=parts), message, image,
                name="read", tool_query=next(iter(source.begin_tool_batch(1))),
                policy=OffloadPolicy(num_user_query=1, preview_chars=1))
    source.prepare_messages([message, image])
    target_storage = ToolStorage(tmp_path / "target")
    target_storage.load_events(events, resolve_artifact=storage.read_artifact)
    target = ToolResultStore(target_storage)
    target.load_events(events)
    messages = copy.deepcopy([message, image])
    target.bind_messages(messages)
    assert target.prepare_messages(messages) == messages
    target.begin_user_query()
    offloaded = target.prepare_messages(messages)
    assert len(offloaded) == 1 and "Tool result offloaded" in offloaded[0]["content"]
    restored = await target.get_tool_result("retrieve", ["read"])
    assert json.loads(restored.content)["results"][0]["content"] == message["content"]
    assert restored.images == images

    source.begin_user_query()
    later, later_image = {**message, "tool_call_id": "later"}, copy.deepcopy(image)
    source.bind(ToolOutput(later["content"], images=images, observations=parts), later, later_image,
                name="retrieve", tool_query=next(iter(source.begin_tool_batch(1))),
                policy=OffloadPolicy(num_user_query=1, preview_chars=1))
    duplicate = ToolStorage(tmp_path / "duplicate-image")
    duplicate.load_events(events, resolve_artifact=storage.read_artifact)
    target = ToolResultStore(duplicate)
    target.load_events(events)
    messages = copy.deepcopy([message, image, later, later_image])
    target.bind_messages(messages, images={"read": messages[1], "later": messages[3]})
    prepared = target.prepare_messages(messages)
    assert all(m is not messages[1] for m in prepared)
    assert any(m is messages[3] for m in prepared)


@pytest.mark.asyncio
async def test_shell_restores_recorded_prefixes_only_for_the_same_container(tmp_path):
    class Sandbox:
        def __init__(self):
            self.identity = {"container_id": "same-container", "real_workdir": "/repo"}
            self.requests = []

        async def request(self, args):
            self.requests.append(args)
            return {"task_id": args["task_id"], "status": "completed", "exit_code": 0,
                    "output": "done", "background": False, "cwd": "/repo/subdir"}

    storage, events = observed_storage(tmp_path / "source")
    sandbox = Sandbox()
    source = DockerBashTools(storage, sandbox)
    storage.event("sandbox/connected", connection=sandbox.identity)
    await source.bash("call", "cd subdir")
    expected = source.save()
    for end in range(1, len(events) + 1):
        target = DockerBashTools(ToolStorage(tmp_path / str(end)), sandbox)
        target.load_events(events[:end])
        if any(e["type"] == "sandbox/task_request" for e in events[:end]):
            assert target.calls == source.calls
        if any(e["type"] == "sandbox/task_observed" for e in events[:end]):
            assert target.observed == source.observed
        if any(e["type"] == "sandbox/shell_state" for e in events[:end]):
            assert target.save() == expected
        await target.aclose()
    assert len(sandbox.requests) == 1
    foreign = Sandbox()
    foreign.identity = {**sandbox.identity, "container_id": "another-container"}
    target = DockerBashTools(ToolStorage(tmp_path / "foreign"), foreign)
    target.load_events(events)
    assert not target.calls and not target.observed and target.cwd == "/repo"
