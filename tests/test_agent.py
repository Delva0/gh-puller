"""gh_puller.agent 监控核心的本地测试。

零 SDK / 零网络 / 零 token:SDK 以假模块注入 sys.modules,HTTP 不发起(禁用路径),
只驱动假事件 dict 与假消息对象。
- 覆盖:bus 扇出(顺序/一致性/有界丢最旧)、configure 停用短路、FileSink 三态目录
  布局与终态原子迁移、适配器归一化(流事件/整块消息/usage)、无副作用与错误语义。
"""

import asyncio
import json
import sys
import types

import pytest
import pytest_asyncio
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from gh_puller import agent
from gh_puller.agent import EventBus, FileSink, WsSink, new_event, sinks
from gh_puller.agent.adapters import _Run, _handle_assistant_message, _handle_stream_event, _normalize_usage
from gh_puller.agent.sinks import OtelSink


@pytest_asyncio.fixture(autouse=True)
async def _monitor_cleanup():
    yield
    agent.configure(file=False, ws_urls=[], otel_urls=[])  # 停用并取消 sink worker 任务
    await asyncio.sleep(0.01)  # 轮转一拍,让被取消的 worker 退场


# ---------------------------------------------------------------------------
# EventBus 扇出
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bus_fanout_ordered():
    a, b = [], []

    async def recv_a(evt):
        a.append(evt)

    async def recv_b(evt):
        b.append(evt)

    bus = EventBus()
    bus.add(recv_a)
    bus.add(recv_b)
    for i in range(5):
        bus.publish({"i": i})
    await asyncio.sleep(0.05)
    assert a == b == [{"i": i} for i in range(5)]
    bus.shutdown()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_bus_drop_oldest_when_full():
    """有界队列(5000)塞满后 publish 丢最旧,新到者优先。"""
    got = []

    async def recv(evt):
        got.append(evt)

    bus = EventBus()
    bus.add(recv)
    for i in range(5001):  # 不 await,单次循环内塞爆队列
        bus.publish({"i": i})
    await asyncio.sleep(0.1)
    assert len(got) == 5000
    assert got[0] == {"i": 1} and got[-1] == {"i": 5000}
    bus.shutdown()
    await asyncio.sleep(0)


def test_configure_disabled_short_circuits():
    """file/ws 全关:bus 无 sink,事件不构造(无 uuid/json 开销)、目录不建。"""
    agent.configure(file=False, ws_urls=[], otel_urls=[])
    run = _Run("s1", "claude", "", label="t")
    run.event("run.start", prompt_chars=1, prompt_preview="p", n_messages=1, system_chars=0, tool_names=[])
    bus = sinks._bus
    assert bus is not None and bus.enabled is False


# ---------------------------------------------------------------------------
# FileSink 布局与状态迁移
# ---------------------------------------------------------------------------


def _evt(kind: str, session: str = "s1", seq: int = 0, round: int = 0, **fields) -> dict:
    return new_event(kind, session=session, label="wiki:structure", provider="claude", model="",
                     seq=seq, round=round, **fields)


@pytest.mark.asyncio
async def test_file_sink_layout_and_finalized(tmp_path):
    sink = FileSink(str(tmp_path))
    # run.start → running/ 即创建,append 实时可见(行即 LLM 流)
    await sink.consume(_evt("run.start", prompt_chars=5, prompt_preview="hi", n_messages=1,
                            system_chars=0, tool_names=[]))
    running = tmp_path / "sessions" / "running" / "s1.jsonl"
    assert running.exists()
    await sink.consume(_evt("block.start", seq=1, block_type="content"))
    await sink.consume(_evt("text.delta", seq=2, text="你好"))
    await sink.consume(_evt("block.stop", seq=3, block_type="content"))
    lines = [json.loads(line) for line in running.read_text().splitlines()]
    assert [ln["type"] for ln in lines] == ["session.start", "round.start", "block.start",
                                            "block.delta", "block.end"]
    # completed:终态 os.replace 原子迁出,无 .tmp 残留
    await sink.consume(_evt("run.end", seq=4, ok=True))
    assert not running.exists()
    completed = tmp_path / "sessions" / "completed" / "s1.jsonl"
    assert completed.exists()
    assert not list((tmp_path / "sessions" / "completed").glob("*.tmp"))
    final = [json.loads(line) for line in completed.read_text().splitlines()]
    assert final[-1]["type"] == "session.end" and final[-1]["state"] == "completed"


@pytest.mark.asyncio
async def test_file_sink_aborted_by_error(tmp_path):
    sink = FileSink(str(tmp_path))
    await sink.consume(_evt("run.start", session="s2", prompt_chars=5, prompt_preview="hi",
                            n_messages=1, system_chars=0, tool_names=[]))
    await sink.consume(_evt("error", session="s2", exc_type="RuntimeError", message="agent 执行失败", stage="run"))
    aborted = tmp_path / "sessions" / "aborted" / "s2.jsonl"
    assert aborted.exists()
    final = [json.loads(line) for line in aborted.read_text().splitlines()]
    assert final[-1]["type"] == "session.end" and final[-1]["state"] == "aborted"
    assert "RuntimeError" in final[-1]["reason"]


# ---------------------------------------------------------------------------
# 适配器归一化(喂形似 SDK 的纯 dict/对象)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_adapter_normalizes_events():
    agent.configure(file=False, ws_urls=[], otel_urls=[])
    got = []
    bus = sinks.ensure_bus()
    assert bus.enabled is False

    async def recv(evt):
        got.append(evt)

    bus.add(recv)
    run = _Run("s1", "claude", "", label="t")
    # 第 0 轮:正文块 + 思考块
    _handle_stream_event(run, {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}})
    _handle_stream_event(run, {"type": "content_block_delta", "index": 0,
                               "delta": {"type": "text_delta", "text": "hi"}})
    _handle_stream_event(run, {"type": "content_block_stop", "index": 0})
    _handle_stream_event(run, {"type": "content_block_start", "index": 1, "content_block": {"type": "thinking"}})
    _handle_stream_event(run, {"type": "content_block_delta", "index": 1,
                               "delta": {"type": "thinking_delta", "thinking": "深"}})
    _handle_stream_event(run, {"type": "content_block_stop", "index": 1})
    # 工具调用:碎片 JSON 装配为终值
    _handle_stream_event(run, {"type": "content_block_start", "index": 2,
                               "content_block": {"type": "tool_use", "id": "t1", "name": "graphify_query"}})
    _handle_stream_event(run, {"type": "content_block_delta", "index": 2,
                               "delta": {"type": "input_json_delta", "partial_json": '{"q"'}})
    _handle_stream_event(run, {"type": "content_block_delta", "index": 2,
                               "delta": {"type": "input_json_delta", "partial_json": ': "x"}'}})
    _handle_stream_event(run, {"type": "content_block_stop", "index": 2})
    # 用户段工具结果(文本片段归属 tool.result,不入 text.delta)
    _handle_stream_event(run, {"type": "message_start", "message": {"role": "user"}})
    _handle_stream_event(run, {"type": "content_block_start", "index": 0,
                               "content_block": {"type": "tool_result", "tool_use_id": "t1", "is_error": False}})
    _handle_stream_event(run, {"type": "content_block_delta", "index": 0,
                               "delta": {"type": "text_delta", "text": "结果片段"}})
    _handle_stream_event(run, {"type": "content_block_stop", "index": 0})
    # 下一段 assistant 消息 → round+1
    _handle_stream_event(run, {"type": "message_start", "message": {"role": "assistant"}})
    _handle_stream_event(run, {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}})
    _handle_stream_event(run, {"type": "content_block_delta", "index": 0,
                               "delta": {"type": "text_delta", "text": "答案是"}})
    await asyncio.sleep(0.05)

    assert [(g["kind"], g.get("round")) for g in got][:3] == \
        [("block.start", 0), ("text.delta", 0), ("block.stop", 0)]
    tool_stop = next(g for g in got if g["kind"] == "block.stop" and g.get("tool_input"))
    assert tool_stop["tool_input"] == {"q": "x"} and tool_stop["tool_id"] == "t1"
    tres = next(g for g in got if g["kind"] == "tool.result")
    assert tres["tool_id"] == "t1" and tres["tool_name"] == "graphify_query"
    assert tres["content_chars"] == 4 and tres["round"] == 0
    assert tres["content_preview"] == "结果片段"
    assert got[-1]["kind"] == "text.delta" and got[-1]["round"] == 1 and got[-1]["text"] == "答案是"


def test_assistant_message_monitors_but_never_duplicates():
    """整块消息:未产出增量时事件化一次;已产出增量时只记元信息。"""
    agent.configure(file=False, ws_urls=[], otel_urls=[])
    msg = types.SimpleNamespace(
        content=[
            types.SimpleNamespace(type="text", text="hi"),
            types.SimpleNamespace(type="thinking", text=None),
        ],
        stop_reason="end_turn",
        usage=types.SimpleNamespace(input_tokens=3, output_tokens=5, cache_read_input_tokens=1),
    )
    run = _Run("s1", "claude", "", label="t")
    _handle_assistant_message(run, msg, already_yielded=False)
    assert run.text_chars == 2
    run2 = _Run("s2", "claude", "", label="t")
    _handle_assistant_message(run2, msg, already_yielded=True)
    assert run2.text_chars == 0  # 兜底不重复流式增量


def test_normalize_usage_maps_sdk_and_http():
    u = types.SimpleNamespace(input_tokens=1, output_tokens=2, cache_read_input_tokens=None)
    assert _normalize_usage(u) == {"input_tokens": 1, "output_tokens": 2,
                                         "cache_read_input_tokens": None}
    assert _normalize_usage({"prompt_tokens": 3, "completion_tokens": 4}) == \
        {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": None}
    assert _normalize_usage(None) is None


# ---------------------------------------------------------------------------
# 无副作用路径与错误语义(假 SDK 模块注入)
# ---------------------------------------------------------------------------


class _AsyncIter:
    def __init__(self, items):
        self._it = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeStreamEvent:
    def __init__(self, event):
        self.event = event


class _FakeAssistantMessage:
    def __init__(self, content=None, stop_reason=None, usage=None):
        self.content = content or []
        self.stop_reason = stop_reason
        self.usage = usage


class _FakeResultMessage:
    def __init__(self, result=None, is_error=False, errors=None, subtype=None,
                 stop_reason=None, usage=None, total_cost_usd=None):
        self.result = result
        self.is_error = is_error
        self.errors = errors or []
        self.subtype = subtype
        self.stop_reason = stop_reason
        self.usage = usage
        self.total_cost_usd = total_cost_usd


class _FakeClient:
    """形似 ClaudeSDKClient 的假客户端(消息列表经类属性注入)。"""

    msgs: list = []

    def __init__(self, options=None):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        pass

    def receive_response(self):
        return _AsyncIter(type(self).msgs)


def _fake_sdk(msgs):
    """构造假 claude_agent_sdk 模块(仅 cc_* 需要的成员),monkeypatch 注入 sys.modules。"""
    mod = types.ModuleType("claude_agent_sdk")
    _FakeClient.msgs = msgs
    mod.StreamEvent = _FakeStreamEvent
    mod.AssistantMessage = _FakeAssistantMessage
    mod.ResultMessage = _FakeResultMessage
    mod.ClaudeSDKClient = _FakeClient
    return mod


def _options():
    return types.SimpleNamespace(model="", system_prompt="sys", allowed_tools=None, mcp_servers=None)


@pytest.mark.asyncio
async def test_cc_stream_exact_output_no_duplicates(monkeypatch, tmp_path):
    """禁用监控 + 假 SDK:产出逐字节一致(content 增量优先,AssistantMessage 不重复兜底),
    monitor 目录不建(零副作用)。"""
    agent.configure(file=False, file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    sdk = _fake_sdk([
        _FakeStreamEvent({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
        _FakeStreamEvent({"type": "content_block_delta", "index": 0,
                          "delta": {"type": "text_delta", "text": "hi"}}),
        _FakeStreamEvent({"type": "content_block_delta", "index": 0,
                          "delta": {"type": "text_delta", "text": "好"}}),
        _FakeAssistantMessage(content=[types.SimpleNamespace(type="text", text="hi好")]),
        _FakeResultMessage(result="hi好"),
    ])
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    chunks = [c async for c in agent.cc_stream(_options(), "prompt", session_name="x")]
    assert "".join(chunks) == "hi好"
    assert not (tmp_path / "sessions").exists()  # 未构建 FileSink,无写盘


@pytest.mark.asyncio
async def test_cc_text_fallback_without_partials(monkeypatch, tmp_path):
    """无 partial 事件:AssistantMessage 整块兜底一次,输出与原漏斗一致。"""
    agent.configure(file=False, file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    sdk = _fake_sdk([
        _FakeAssistantMessage(content=[types.SimpleNamespace(type="text", text="hi好world")]),
        _FakeResultMessage(result="hi好world"),
    ])
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    assert await agent.cc_text(_options(), "prompt", session_name="x") == "hi好world"
    assert not (tmp_path / "sessions").exists()


@pytest.mark.asyncio
async def test_cc_stream_error_semantics(monkeypatch, tmp_path):
    """is_error 的 ResultMessage → RuntimeError"agent 执行失败: ..."(与旧漏斗同一文案)。"""
    agent.configure(file=False, file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    sdk = _fake_sdk([_FakeResultMessage(result="", is_error=True, errors=["boom"])])
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    with pytest.raises(RuntimeError, match="agent 执行失败"):
        async for _ in agent.cc_stream(_options(), "p", session_name="x"):
            pass
    assert not (tmp_path / "sessions").exists()


# ---------------------------------------------------------------------------
# WsSink 跨进程契约(仅转发原始事件;聚合只发生在消费端)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_sink_forwards_raw_events_only(monkeypatch):
    """WsSink 只发原始事件帧(不聚合、无 llm 流字段);跨进程契约:聚合发生在消费端。"""
    sent = []
    connects = []

    class _FakeWs:
        async def send(self, payload):
            sent.append(json.loads(payload))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def _connect(url, **kw):
        connects.append(url)
        return _FakeWs()

    # WsSink._run 内 import websockets → 打 sys.modules 注入假实现(零网络)
    mod = types.ModuleType("websockets")
    mod.connect = _connect
    monkeypatch.setitem(sys.modules, "websockets", mod)

    sink = agent.WsSink("ws://preview/ws")
    evts = [
        _evt("run.start", seq=0, prompt_chars=5, prompt_preview="hi", n_messages=1,
             system_chars=0, tool_names=[]),
        _evt("block.start", seq=1, block_type="content"),
        _evt("text.delta", seq=2, text="你好"),
        _evt("text.delta", seq=3, text="世界"),
    ]
    for e in evts:
        await sink.consume(e)
    await asyncio.sleep(0.05)  # 排空 ws worker
    try:
        # 原始事件原样转发:类型/全字段/顺序逐帧精确一致
        assert sent == [{"type": "evt", "event": e} for e in evts]
        # 无聚合产物泄漏(无 llm 行帧),delta 只携本块文本
        assert not any(f.get("line") for f in sent)
        delta_texts = [f["event"]["text"] for f in sent if f["event"]["kind"] == "text.delta"]
        assert delta_texts == ["你好", "世界"]
        assert "你好世界" not in [f["event"].get("text", "") for f in sent]
        assert connects == ["ws://preview/ws"]  # 单连接,无重连抖动
    finally:
        sink._task.cancel()
        await asyncio.wait([sink._task])  # CancelledError 不被 except Exception 吞,正常退场
    assert sink._task.cancelled()


# ---------------------------------------------------------------------------
# OtelSink(事件流 → OTel span;注入 InMemorySpanExporter,零网络)
# ---------------------------------------------------------------------------


def _otel_sink():
    """构造注入 InMemorySpanExporter 的 OtelSink(不触网)。"""
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return OtelSink("http://preview/v1/traces", tracer=tp.get_tracer("test")), exporter


@pytest.mark.asyncio
async def test_otel_span_tree():
    sink, exporter = _otel_sink()
    for evt in [
        _evt("run.start", seq=0, prompt_chars=6, prompt_preview="prompt", n_messages=1,
             system_chars=10, tool_names=["graphify_query"]),
        _evt("block.start", seq=1, block_type="content"),
        _evt("text.delta", seq=2, text="你好"),
        _evt("text.delta", seq=3, text="世界"),
        _evt("block.stop", seq=4),
        _evt("block.start", seq=5, block_type="tool_use", tool_id="t1", tool_name="graphify_query"),
        _evt("block.stop", seq=6, block_type="tool_use", tool_id="t1", tool_input={"q": "x"}),
        _evt("tool.result", seq=7, tool_id="t1", tool_name="graphify_query", is_error=False,
             content_chars=4, content_preview="结果"),
        _evt("result", seq=8, usage={"input_tokens": 3, "output_tokens": 5,
                                     "cache_read_input_tokens": 1}, total_cost_usd=0.001,
             duration_ms=123),
        _evt("run.end", seq=9, ok=True, text_chars=4, duration_ms=123, num_rounds=1),
    ]:
        await sink.consume(evt)
    root = next(s for s in exporter.get_finished_spans() if s.name == "wiki:structure · claude")
    content = next(s for s in exporter.get_finished_spans() if s.name == "block.content")
    tool = next(s for s in exporter.get_finished_spans() if s.name == "block.tool_use")
    tres = next(s for s in exporter.get_finished_spans() if s.name.startswith("tool.result"))
    # 父子关系:块/工具结果都挂在根 span 下
    assert content.parent.span_id == root.context.span_id
    assert tool.parent.span_id == root.context.span_id
    assert tres.parent.span_id == root.context.span_id
    assert root.attributes["gen_ai.provider.name"] == "claude"
    assert root.attributes["gh_puller.prompt_preview"] == "prompt"
    assert root.attributes["gh_puller.tool_names"] == '["graphify_query"]'
    assert root.attributes["gen_ai.usage.input_tokens"] == 3
    assert root.attributes["gh_puller.total_cost_usd"] == 0.001
    assert root.attributes["gh_puller.duration_ms"] == 123
    assert root.status.status_code == StatusCode.OK
    assert content.attributes["gh_puller.text_chars"] == 4
    assert content.attributes["gh_puller.text_preview"] == "你好世界"
    assert tool.attributes["gh_puller.tool_input"] == '{"q": "x"}'
    assert tres.attributes["gh_puller.is_error"] is False
    assert sink._spans == {}  # run.end 清场


@pytest.mark.asyncio
async def test_otel_error_status_and_cleanup():
    sink, exporter = _otel_sink()
    await sink.consume(_evt("run.start", seq=0, prompt_chars=5, prompt_preview="p", n_messages=1,
                            system_chars=0, tool_names=[]))
    await sink.consume(_evt("error", seq=1, exc_type="RuntimeError", message="agent 执行失败: boom",
                            stage="run"))
    await sink.consume(_evt("run.end", seq=2, ok=False, text_chars=0, duration_ms=10, num_rounds=1))
    root = exporter.get_finished_spans()[0]
    assert root.status.status_code == StatusCode.ERROR
    assert "RuntimeError" in root.attributes["gh_puller.error"]
    assert root.attributes["gh_puller.error_stage"] == "run"
    assert root.attributes["gh_puller.duration_ms"] == 10
    assert sink._spans == {}


@pytest.mark.asyncio
async def test_otel_usage_attrs_skip_none():
    sink, exporter = _otel_sink()
    await sink.consume(_evt("run.start", seq=0, prompt_chars=5, prompt_preview="p", n_messages=1,
                            system_chars=0, tool_names=[]))
    await sink.consume(_evt("message.assistant", seq=1,
                            usage={"input_tokens": 8, "output_tokens": 2, "cache_read_input_tokens": None},
                            stop_reason="end_turn"))
    await sink.consume(_evt("run.end", seq=2, ok=True, text_chars=0, duration_ms=9, num_rounds=1))
    root = exporter.get_finished_spans()[0]
    assert root.attributes["gen_ai.usage.input_tokens"] == 8
    assert root.attributes["gen_ai.usage.output_tokens"] == 2
    assert "gh_puller.cache_read_input_tokens" not in root.attributes  # None 跳过
    assert root.attributes["gh_puller.stop_reason"] == "end_turn"


def test_otel_off_by_default():
    """otel_urls / ws_urls 空:与 file 全关一致,bus 无 sink(零构造开销)。"""
    agent.configure(file=False, ws_urls=[], otel_urls=[])
    assert sinks.ensure_bus().enabled is False


@pytest.mark.asyncio
async def test_otel_failure_isolated(monkeypatch):
    """tracer 异常:consume 不抛、每会话只报一次日志。"""
    logged = []
    monkeypatch.setattr("gh_puller.agent.sinks._log", logged.append)

    class _BoomTracer:
        def start_span(self, *a, **kw):
            raise RuntimeError("boom")

    sink = OtelSink("http://preview/v1/traces", tracer=_BoomTracer())
    await sink.consume(_evt("run.start", seq=0, prompt_chars=5, prompt_preview="p", n_messages=1,
                            system_chars=0, tool_names=[]))  # start_span 抛 → 吞掉不抛
    await sink.consume(_evt("text.delta", seq=1, text="x"))  # 同一会话已报过 → 静默
    assert len(logged) == 1
    assert "otel sink 消费失败" in logged[0]


# ---------------------------------------------------------------------------
# 封套层:URL 归一 / 多实例注册 / 可达性门控(configure/ensure_bus;零网络)
# ---------------------------------------------------------------------------


def test_split_urls_parsing():
    """str|序列|None → 逗号分隔/逐条去空白/保序去重;空 → []。"""
    assert sinks._split_urls("") == []
    assert sinks._split_urls(None) == []
    assert sinks._split_urls("  ") == []
    assert sinks._split_urls("ws://a/ws") == ["ws://a/ws"]
    assert sinks._split_urls("ws://a/ws, ws://b/ws") == ["ws://a/ws", "ws://b/ws"]
    assert sinks._split_urls("ws://a/ws,,ws://a/ws") == ["ws://a/ws"]  # 去空项 + 去重
    assert sinks._split_urls(["ws://a/ws", "ws://b/ws"]) == ["ws://a/ws", "ws://b/ws"]


def test_otel_traces_url_normalization():
    """基底地址(无路径)自动补 /v1/traces;完整 OTLP URL 原样通过。"""
    assert sinks._otel_traces_url("http://localhost:6006/") == "http://localhost:6006/v1/traces"
    assert sinks._otel_traces_url("http://localhost:6006") == "http://localhost:6006/v1/traces"
    assert sinks._otel_traces_url("http://localhost:6006/v1/traces") == "http://localhost:6006/v1/traces"
    assert sinks._otel_traces_url("http://h:3000/api/public/otel/v1/traces") == \
        "http://h:3000/api/public/otel/v1/traces"


class _RecSink:
    """记录构造 URL 的假 sink(封套层测试用;consume 为异步空操作)。"""

    def __init__(self, urls):
        self.urls = urls

    async def consume(self, evt):
        pass


def _rec_sink(urls):
    """monkeypatch 辅助:替换 sinks.WsSink/OtelSink 为记录构造 URL 的假类。"""
    def patch(monkeypatch, target):
        def _make(url, *a, **kw):
            urls.append(url)
            return _RecSink(urls)

        monkeypatch.setattr(f"gh_puller.agent.sinks.{target}", _make)

    return patch


@pytest.mark.asyncio
async def test_ensure_bus_multi_ws_sinks(monkeypatch):
    """多 URL 通过实例副本数展开:两个目标 → 两个 WsSink 实例,各持一 URL。"""
    urls = []
    patch = _rec_sink(urls)
    patch(monkeypatch, "WsSink")
    monkeypatch.setattr("gh_puller.agent.sinks._url_reachable", lambda url: True)
    agent.configure(file=False, ws_urls="ws://a/ws, ws://b/ws", otel_urls=[])
    assert sinks.ensure_bus().enabled is True
    assert urls == ["ws://a/ws", "ws://b/ws"]


@pytest.mark.asyncio
async def test_ensure_bus_skips_unreachable_otel(monkeypatch):
    """端点不可达:不注册该 OTel 实例(仅一条日志);与空列表同效(bus 空)。"""
    logged = []
    monkeypatch.setattr("gh_puller.agent.sinks._url_reachable", lambda url: False)
    monkeypatch.setattr("gh_puller.agent.sinks._log", logged.append)
    agent.configure(file=False, ws_urls=[], otel_urls="http://localhost:6006/")
    assert sinks.ensure_bus().enabled is False
    assert any("端口不可达" in m for m in logged)


@pytest.mark.asyncio
async def test_ensure_bus_otel_missing_dependency(monkeypatch):
    """opentelemetry 缺失(OtelSink 构造抛 ImportError):降级跳过,不拖垮调用。"""
    logged = []

    def _boom(url, *a, **kw):
        raise ImportError("no opentelemetry")

    monkeypatch.setattr("gh_puller.agent.sinks._url_reachable", lambda url: True)
    monkeypatch.setattr("gh_puller.agent.sinks.OtelSink", _boom)
    monkeypatch.setattr("gh_puller.agent.sinks._log", logged.append)
    agent.configure(file=False, ws_urls=[], otel_urls="http://localhost:6006/")
    assert sinks.ensure_bus().enabled is False
    assert any("缺依赖" in m for m in logged)


@pytest.mark.asyncio
async def test_ensure_bus_one_otel_sink_per_url(monkeypatch):
    """每个可达 OTel 地址一个 sink 实例;构造 URL 已归一为完整 OTLP 路径。"""
    urls = []
    patch = _rec_sink(urls)
    patch(monkeypatch, "OtelSink")
    monkeypatch.setattr("gh_puller.agent.sinks._url_reachable", lambda url: True)
    monkeypatch.setattr("gh_puller.agent.sinks._log", lambda msg: None)
    agent.configure(file=False, ws_urls=[], otel_urls="http://p1:6006/,http://p2:6006/v1/traces")
    assert sinks.ensure_bus().enabled is True
    assert urls == ["http://p1:6006/v1/traces", "http://p2:6006/v1/traces"]


@pytest.mark.asyncio
async def test_file_sink_on_by_default(monkeypatch, tmp_path):
    """AGENT_MONITOR_FILE 已移除:file 参数缺省(None)即恒开,目录即建。"""
    monkeypatch.setattr("gh_puller.agent.sinks._url_reachable", lambda url: False)
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    bus = sinks.ensure_bus()  # file=None → True(无 env 可读)
    assert bus.enabled is True
    assert (tmp_path / "sessions").exists()


@pytest.mark.asyncio
async def test_configure_none_reseeds_env_constant(monkeypatch):
    """configure 参数 None → 重读 env 常量(经逗号分隔归一);覆盖新 env 名。"""
    # otel 半:AGENT_MONITOR_PHOENIX_URL
    otel_urls = []
    patch = _rec_sink(otel_urls)
    patch(monkeypatch, "OtelSink")
    monkeypatch.setattr("gh_puller.agent.sinks._url_reachable", lambda url: True)
    monkeypatch.setattr("gh_puller.agent.sinks._log", lambda msg: None)
    monkeypatch.setattr(sinks.envs, "AGENT_MONITOR_PHOENIX_URL", "http://envp:6006/")
    agent.configure(file=False, ws_urls=[], otel_urls=None)  # None → 重读 envs 常量
    assert sinks.ensure_bus().enabled is True
    assert otel_urls == ["http://envp:6006/v1/traces"]
    # ws 半:AGENT_MONITOR_WEBUI_URL
    ws_urls = []
    patch = _rec_sink(ws_urls)
    patch(monkeypatch, "WsSink")
    monkeypatch.setattr(sinks.envs, "AGENT_MONITOR_WEBUI_URL", "ws://env/ws")
    agent.configure(file=False, ws_urls=None, otel_urls=[])
    assert sinks.ensure_bus().enabled is True
    assert ws_urls == ["ws://env/ws"]
