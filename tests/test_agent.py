"""Local contract tests for Agent adapters and observation sinks."""

import asyncio
import json
import shlex
import sys
import types
from enum import StrEnum
from typing import ClassVar

import pytest
import pytest_asyncio
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from gh_puller import agent
from gh_puller.agent import sinks
from gh_puller.agent.events import EventBus, EventRecorder, fold_state, new_event, set_active_bus
from gh_puller.agent.sinks import FileSink, OtelSink


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_monitor():
    yield
    agent.configure(ws_urls=[], otel_urls=[])
    set_active_bus(None)
    await asyncio.sleep(0)


async def _capture(tmp_path) -> list[dict]:
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    events: list[dict] = []
    sinks.ensure_bus().add(_receiver(events))
    return events


def _receiver(target: list[dict]):
    async def receive(event: dict) -> None:
        target.append(event)

    return receive


async def _settle() -> None:
    await asyncio.sleep(0.03)


async def _collect(stream) -> str:
    return "".join([part async for part in stream])


def _event(event_type: str, seq: int, session: str = "s", **data) -> dict:
    return {**new_event(event_type, **data), "seq": seq, "session": session}


def _roles_at_requests(events: list[dict]) -> list[list[str]]:
    return [
        [message["role"] for message in fold_state(events[:index])["context"]]
        for index, event in enumerate(events) if event["type"] == "model/request"
    ]


@pytest.mark.asyncio
async def test_event_bus_preserves_order_for_every_sink() -> None:
    first: list[dict] = []
    second: list[dict] = []
    bus = EventBus()
    bus.add(_receiver(first))
    bus.add(_receiver(second))
    for index in range(5):
        bus.publish({"index": index})
    await _settle()
    assert first == second == [{"index": index} for index in range(5)]
    bus.shutdown()


@pytest.mark.asyncio
async def test_event_bus_backlog_never_drops_compact_events() -> None:
    received: list[dict] = []
    bus = EventBus()
    bus.add(_receiver(received))
    for index in range(5100):
        bus.publish({"type": "model/delta/text", "index": index})
    committed = {"type": "context/append/user", "content": []}
    bus.publish(committed)
    for _ in range(100):
        if committed in received:
            break
        await asyncio.sleep(0.01)
    assert received[-1] == committed
    assert len(received) < 5101
    bus.shutdown()


@pytest.mark.asyncio
async def test_recorder_supports_multiple_loose_turns_and_replay() -> None:
    events: list[dict] = []
    bus = EventBus()
    bus.add(_receiver(events))
    set_active_bus(bus)
    recorder = EventRecorder("s", agent="custom", config={"mode": "default"})
    recorder.start()
    for index in range(2):
        recorder.begin_turn()
        recorder.append_context({
            "role": "user", "content": [{"type": "text", "text": f"q{index}"}],
        })
        recorder.begin_step()
        request_id = recorder.model_request(model=f"m{index}")
        recorder.text(f"a{index}", request_id=request_id)
        message = {
            "role": "assistant", "content": [{"type": "text", "text": f"a{index}"}],
        }
        recorder.model_response(message, request_id=request_id)
        recorder.append_context(message)
        recorder.end_turn()
    recorder.finish(True)
    await _settle()
    state = fold_state(events)
    assert state["agent"] == {"agent": "custom", "config": {"mode": "default"}}
    assert [message["role"] for message in state["context"]] == [
        "user", "assistant", "user", "assistant",
    ]
    assert [event["data"]["requestId"] for event in events
            if event["type"] == "model/request"] == ["r1", "r2"]
    assert len([event for event in events if event["type"] == "turn/start"]) == 2
    bus.shutdown()


@pytest.mark.asyncio
async def test_recorder_correlates_interleaved_model_activity() -> None:
    events: list[dict] = []
    bus = EventBus()
    bus.add(_receiver(events))
    set_active_bus(bus)
    recorder = EventRecorder("s")
    left = recorder.model_request(request_id="left", model="planner")
    right = recorder.model_request(request_id="right", model="writer")
    recorder.text("R", request_id=right)
    recorder.text("L", request_id=left)
    recorder.model_response(
        {"role": "assistant", "content": [{"type": "text", "text": "L"}]},
        request_id=left,
    )
    recorder.model_response(
        {"role": "assistant", "content": [{"type": "text", "text": "R"}]},
        request_id=right,
    )
    await _settle()
    assert [event["data"]["requestId"] for event in events] == [
        "left", "right", "right", "left", "left", "right",
    ]
    bus.shutdown()


@pytest.mark.asyncio
async def test_base_agent_records_config_without_inferring_context(tmp_path) -> None:
    class BareAgent(agent.BaseAgent):
        agent = "bare"

        async def _enter(self) -> None:
            return None

        async def _exit(self, _exc) -> None:
            return None

    config = {
        "model": "configured",
        "mode": "plan",
        "system_prompt": "not applied by this adapter",
        "custom": {"value": 1},
    }
    events = await _capture(tmp_path)
    subject = BareAgent(config)
    async with subject.session(session="bare/s"):
        pass
    await _settle()
    assert next(event for event in events if event["type"] == "agent/set")["data"] == {
        "agent": "bare", "config": config,
    }
    assert not any(event["type"].startswith("context/") for event in events)


@pytest.mark.asyncio
async def test_file_sink_compact_and_raw_have_identical_state(tmp_path) -> None:
    compact = FileSink(str(tmp_path / "compact"))
    raw = FileSink(str(tmp_path / "raw"), raw=True)
    events = [
        _event("session/start", 0, label="x"),
        _event("agent/set", 1, agent="x", config={"model": "m"}),
        _event("context/append/user", 2, content=[{"type": "text", "text": "q"}]),
        _event("model/request", 3, requestId="r1"),
        _event("model/delta/text", 4, requestId="r1", index=0, text="a"),
        _event("context/append/assistant", 5, content=[{"type": "text", "text": "a"}]),
        _event("session/end", 6, outcome="completed", durationMs=1),
    ]
    for event in events:
        await compact.consume(event)
        await raw.consume(event)
    compact_events = [json.loads(line) for line in (tmp_path / "compact" / "s.jsonl")
                      .read_text(encoding="utf-8").splitlines()]
    raw_events = [json.loads(line) for line in (tmp_path / "raw" / "s.jsonl")
                  .read_text(encoding="utf-8").splitlines()]
    assert [event["type"] for event in compact_events] == [
        "session/start", "agent/set", "context/append/user", "model/request",
        "context/append/assistant", "session/end",
    ]
    assert fold_state(compact_events) == fold_state(raw_events)


@pytest.mark.asyncio
async def test_otel_uses_request_and_call_correlations() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    sink = OtelSink("", tracer=provider.get_tracer("test"))
    events = [
        _event("session/start", 0, label="run"),
        _event("agent/set", 1, agent="x", config={"model": "configured"}),
        _event("model/request", 2, requestId="r1", model="m", provider="p"),
        _event("model/delta/text", 3, requestId="r1", index=0, text="ok"),
        _event("model/response", 4, requestId="r1",
               message={"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
               usage={"input": 2, "output": 1}),
        _event("tool/start", 5, callId="c1", name="read", arguments={"path": "a"}),
        _event("tool/end", 6, callId="c1", error={"type": "IOError", "message": "bad"}),
        _event("session/end", 7, outcome="failed", durationMs=2),
    ]
    for event in events:
        await sink.consume(event)
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["run"].attributes["gh_puller.agent"] == "x"
    assert spans["model:r1"].attributes["gen_ai.request.model"] == "m"
    assert spans["model:r1"].attributes["gh_puller.text_preview"] == "ok"
    assert spans["tool:read"].status.status_code is StatusCode.ERROR
    assert spans["run"].status.status_code is StatusCode.ERROR


# --- Claude Code ---------------------------------------------------------


class _ClaudeOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _ClaudeStream:
    def __init__(self, event):
        self.event = event


class _ClaudeAssistant:
    def __init__(self, content, *, stop_reason="end_turn", usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage


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
        "allowed_tools": ["Read"],
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
    assert len([event for event in events if event["type"] == "context/append/system"]) == 1
    assert [message["role"] for message in fold_state(events)["context"]] == [
        "system", "user", "assistant", "user", "assistant", "user", "assistant",
    ]
    assert _roles_at_requests(events) == [
        ["system", "user"],
        ["system", "user", "assistant", "user"],
        ["system", "user", "assistant", "user", "assistant", "user"],
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
    assert _roles_at_requests(events) == [
        ["user"], ["user", "assistant", "tool"],
    ]


# --- OpenAI-compatible ---------------------------------------------------


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

    def __init__(self, **_kwargs):
        self.index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def stream(self, *_args, **_kwargs):
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
        [{"choices": [{"delta": {"content": "a1"}, "finish_reason": "stop"}]}],
        [{"choices": [{"delta": {"content": "a2"}, "finish_reason": "stop"}]}],
    ]
    monkeypatch.setattr(openai.httpx, "AsyncClient", _HttpClient)
    events = await _capture(tmp_path)
    subject = agent.OpenAI({"model": "m", "base_url": "http://fake", "provider": "p"})
    async with subject.session(session="openai/s"):
        assert await subject.result({
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "q1"},
            ],
            "tools": [{"type": "function", "function": {
                "name": "read", "description": "Read a file",
                "parameters": {"type": "object"},
            }}],
        }) == "a1"
        assert await subject.result({"messages": [{"role": "user", "content": "q2"}]}) == "a2"
    await _settle()
    contexts = [event["data"]["messages"] for event in events
                if event["type"] == "context/set"]
    assert len(contexts) == 2
    assert next(event for event in events if event["type"] == "agent/set")["data"][
        "config"] == {"model": "m", "base_url": "http://fake", "provider": "p"}
    assert [
        {key: value for key, value in event["data"].items() if key != "requestId"}
        for event in events if event["type"] == "model/request"
    ] == [
        {"model": "m", "parameters": {}, "provider": "p"},
        {"model": "m", "parameters": {}, "provider": "p"},
    ]
    assert contexts[0][0] == {
        "role": "system", "content": [{"type": "text", "text": "system"}],
    }
    assert contexts[0][1] == {
        "role": "system", "content": [
            {"type": "tool_definition", "name": "read", "description": "Read a file",
             "inputSchema": {"type": "object"}},
        ],
    }
    assert [message["role"] for message in contexts[0]] == ["system", "system", "user"]
    assert [message["role"] for message in contexts[1]] == ["user"]
    assert [event["data"]["requestId"] for event in events
            if event["type"] == "model/request"] == ["r1", "r2"]
    assert _roles_at_requests(events) == [
        ["system", "system", "user"], ["user"],
    ]


# --- Codex ---------------------------------------------------------------


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
    })
    async with subject.session(session="codex/s"):
        assert await _collect(subject.stream("q1")) == "a1"
        assert await _collect(subject.stream("q2")) == "a2"
    assert _CodexClient.starts == 1
    await _settle()
    assert [event["data"]["requestId"] for event in events
            if event["type"] == "model/request"] == ["r1", "r2"]
    assert next(event for event in events if event["type"] == "agent/set")["data"][
        "config"]["model"] == "m"
    assert len([event for event in events if event["type"] == "context/append/system"]) == 1
    assert len([event for event in events if event["type"] == "context/append"]) == 1
    assert [message["role"] for message in fold_state(events)["context"]] == [
        "system", "developer", "user", "assistant", "user", "assistant",
    ]
    assert _roles_at_requests(events) == [
        ["system", "developer", "user"],
        ["system", "developer", "user", "assistant", "user"],
    ]


@pytest.mark.asyncio
async def test_codex_batches_model_tool_calls_before_execution(monkeypatch, tmp_path) -> None:
    tool = types.SimpleNamespace(
        type="commandExecution", id="c1", command="ls", cwd="/tmp",
        aggregated_output="ok", exit_code=0, status="completed")
    script = [
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
    block = events[assistant]["data"]["content"][0]
    assert block == {
        "type": "tool_call", "callId": "c1", "name": "shell",
        "arguments": {"command": "ls", "cwd": "/tmp"},
    }
    assert _roles_at_requests(events) == [
        ["user"], ["user", "assistant", "tool"],
    ]


# --- OpenCode ------------------------------------------------------------


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
    })
    async with subject.session(session="opencode/s"):
        assert await _collect(subject.stream("q1")) == "answer"
        assert await _collect(subject.stream("q2")) == "answer"
    argv = args_path.read_text(encoding="utf-8").splitlines()
    assert argv.count("--session") == 1
    assert "native-session" in argv
    await _settle()
    assert [event["data"]["requestId"] for event in events
            if event["type"] == "model/request"] == ["r1", "r2"]
    assert next(event for event in events if event["type"] == "agent/set")["data"][
        "config"]["model"] == "m"
    assert len([event for event in events if event["type"] == "context/append/system"]) == 1
    assert len([event for event in events if event["type"] == "context/append/assistant"]) == 2
    assert [message["role"] for message in fold_state(events)["context"]] == [
        "system", "user", "assistant", "user", "assistant",
    ]
    assert _roles_at_requests(events) == [
        ["system", "user"], ["system", "user", "assistant", "user"],
    ]


# --- DSH -----------------------------------------------------------------


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
        assert await _collect(subject.stream("q2")) == "a2"
    await _settle()
    assert [event["data"]["requestId"] for event in events
            if event["type"] == "model/request"] == ["r1", "r2"]
    assert next(event for event in events if event["type"] == "agent/set")["data"][
        "config"]["model"] == "m"
    assert len([event for event in events if event["type"] == "context/append/system"]) == 1
    assert len([event for event in events if event["type"] == "context/append/assistant"]) == 2
    assert [message["role"] for message in fold_state(events)["context"]] == [
        "system", "user", "assistant", "user", "assistant",
    ]
    assert _roles_at_requests(events) == [
        ["system", "user"], ["system", "user", "assistant", "user"],
    ]


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
    assert events[assistant]["data"]["content"][0] == {
        "type": "tool_call", "callId": "c1", "name": "read",
        "arguments": {"path": "a.py"},
    }
    request = next(event for event in events if event["type"] == "model/request")
    assert request["data"] == {
        "requestId": "r1", "model": "m2", "provider": "p2",
        "parameters": {"temperature": 0},
    }
    state = fold_state(events)
    assert state["context"][0] == {
        "role": "system", "content": [
            {"type": "text", "text": "system"},
            {"type": "tool_definition", "name": "read", "description": "Read a file",
             "inputSchema": {"type": "object"}},
        ],
    }


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
        {"role": "user", "content": [{"type": "text", "text": "summary"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "final"}]},
    ]
