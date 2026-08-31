"""gh_puller.agent 监控核心的本地测试。

零 SDK / 零网络 / 零 token:SDK 以假模块注入 sys.modules,HTTP 不发起(禁用路径),
只驱动假事件 dict 与假消息对象。
- 覆盖:bus 扇出(顺序/一致性/有界丢最旧)、configure 停用短路、FileSink 三态目录
  布局与终态原子迁移(原始事件逐行)、适配器归一化(事件溯源模型:chunk/tool.call/
  tool.result 全量)、assistant 消息无重复兜底、WsSink 原样转发、OtelSink span 树、
  无副作用与错误语义。
"""

import asyncio
import json
import sys
import time
import types
from enum import StrEnum
from typing import ClassVar

import pytest
import pytest_asyncio
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from gh_puller import agent, envs
from gh_puller.agent import EventBus, FileSink, new_event, sinks
from gh_puller.agent.events import EventRecorder, _normalize_usage, _session_id, set_active_bus
from gh_puller.agent.generators.cc import _handle_assistant_message, _handle_stream_event
from gh_puller.agent.sinks import OtelSink


@pytest_asyncio.fixture(autouse=True)
async def _monitor_cleanup():
    yield
    agent.configure(ws_urls=[], otel_urls=[])  # 停用并取消 sink worker 任务
    await asyncio.sleep(0.01)  # 轮转一拍,让被取消的 worker 退场


def _recv(got):
    """消息收集协程。"""

    async def recv(evt):
        got.append(evt)

    return recv


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


@pytest.mark.asyncio
async def test_recorder_self_init_bus_on_first_event(monkeypatch, tmp_path):
    """录前自足:配置后未显式 ensure_bus —— 首次事件即自建总线(FileSink 恒开落配置目录),

    会话事件正常发布(不再"未 ensure 即零短路"),且同总线幂等复用。
    """
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    assert sinks._bus is None
    run = EventRecorder("s1", label="t")
    run.event("session/start", run_id=None, label="t", model="")  # 触发自建
    assert sinks._bus is not None
    assert len(sinks._bus._sinks) == 1  # 恒开 FileSink 已注册(落盘根即配置目录,无 sessions 子层)
    n_sinks = len(sinks._bus._sinks)
    run.event("turn/start", turn=1)  # 复用同一总线,不重复建
    assert sinks.ensure_bus() is sinks._bus and len(sinks._bus._sinks) == n_sinks


def test_recorder_event_no_loop_degrades_safe(tmp_path):
    """同步构造(无运行中事件循环):event 降级短路(返回 None),不因自建总线抛 RuntimeError。"""
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    run = EventRecorder("s2", label="t")
    assert run.event("session/start", run_id=None, label="t", model="") is None


@pytest.mark.asyncio
async def test_init_config_folds_sdk_objects():
    """config/init 遇不可序列化装配对象(mcp SDK Server 实例等)→ 折叠为 <类型名> 入流:

    new_event 的 JSON 校验在构造期即通过,不炸交付主路径;mcp_servers 装配形态
    (type/name)保留,凭证剥离沿用旧语义。
    """

    class Server:  # 模拟 mcp SDK 装配对象(不可 JSON 化)
        pass

    got = []
    bus = EventBus()
    bus.add(_recv(got))
    set_active_bus(bus)
    try:
        run = EventRecorder("s1", label="t")
        run.init_config({
            "system_prompt": "sp",
            "config_path": "/tmp/dw-settings.json",
            "api_key": "sk-secret",  # 凭证剥离:不入流
            "mcp_servers": {"graphify": {"type": "sdk", "name": "graphify",
                                         "instance": Server()}},
        })
        await asyncio.sleep(0.05)  # 轮转一拍,让 drain 消费
    finally:
        set_active_bus(None)
        bus.shutdown()
    assert got, "config/init 应正常发布(不抛 TypeError)"
    assert got[0]["type"] == "config/init"
    cfg = got[0]["data"]["config"]
    assert cfg["system_prompt"] == "sp"
    assert cfg["config_path"] == "/tmp/dw-settings.json"
    assert "api_key" not in cfg  # 凭证剥离
    assert cfg["mcp_servers"]["graphify"] == {"type": "sdk", "name": "graphify",
                                              "instance": "<Server>"}
    json.dumps(got[0])  # 事件整体可再序列化


# ---------------------------------------------------------------------------
# FileSink 扁平布局与非流式事件流投影(原始事件逐行)
# ---------------------------------------------------------------------------


def _evt(evt_type: str, session: str = "s1", seq: int = 0, **data) -> dict:
    """测试用具:built 信封事件(seq 显式,data 经 new_event 校验)。"""
    return {**new_event(evt_type, **data), "session": session, "label": "wiki:structure",
            "seq": seq}


@pytest.mark.asyncio
async def test_file_sink_flat_layout_nonstream_projection(tmp_path):
    sink = FileSink(str(tmp_path))
    # session/start → 扁平根即创建,append 实时可见,行即原始事件;
    # 会话 id 带 ns(judge:llm/uuid):文件名只取 "/" 后段
    await sink.consume(_evt("session/start", session="judge:llm/0460e1e9-5155-4014-9054-a39986462b20",
                            seq=0, run_id="r1", label="wiki:structure",
                            provider="anthropic", model=""))
    flat = tmp_path /"0460e1e9-5155-4014-9054-a39986462b20.jsonl"
    assert flat.exists()
    # assistant/chunk 不落盘(非流式事件流投影):文件 seq 出现洞(0 → 2)
    await sink.consume(_evt("assistant/chunk", session="judge:llm/0460e1e9-5155-4014-9054-a39986462b20",
                            seq=1, chunk={"type": "content", "index": 0, "text": "你好"}))
    await sink.consume(_evt("assistant/message", session="judge:llm/0460e1e9-5155-4014-9054-a39986462b20",
                            seq=2,
                            message={"role": "assistant", "content": [{"type": "text", "text": "你好"}]},
                            surfaceOp="append", sourceSeqs=[1]))
    lines = [json.loads(line) for line in flat.read_text().splitlines()]
    assert [ln["type"] for ln in lines] == ["session/start", "assistant/message"]
    assert [ln["seq"] for ln in lines] == [0, 2]  # 洞 = 被跳过的 chunk
    # session/end 留在文件内(不迁移、不分目录):隐式分类学,状态在事件里
    await sink.consume(_evt("session/end", session="judge:llm/0460e1e9-5155-4014-9054-a39986462b20",
                            seq=3, state="completed", ok=True,
                            duration_ms=10, text_chars=2, num_steps=1))
    assert flat.exists()
    assert not list(tmp_path.glob("completed"))
    assert not list(tmp_path.glob("running"))
    assert not list(tmp_path.glob("*.tmp"))
    final = [json.loads(line) for line in flat.read_text().splitlines()]
    assert final[-1]["type"] == "session/end" and final[-1]["data"]["state"] == "completed"
    # 无序文件:目录里只有一个扁平文件  # noqa: ERA001 - 中文说明注释,非被注释代码
    assert [p.name for p in tmp_path.glob("*.jsonl")] == [
        "0460e1e9-5155-4014-9054-a39986462b20.jsonl"]


@pytest.mark.asyncio
async def test_file_sink_aborted_by_error(tmp_path):
    sink = FileSink(str(tmp_path))
    await sink.consume(_evt("session/start", session="s2", seq=0, run_id="r2",
                            label="wiki:structure", provider="anthropic", model=""))
    await sink.consume(_evt("error", session="s2", seq=1, stage="run",
                            exc_type="RuntimeError", message="agent 执行失败"))
    await sink.consume(_evt("session/end", session="s2", seq=2, state="aborted", ok=False,
                            reason="RuntimeError: agent 执行失败", duration_ms=5,
                            text_chars=0, num_steps=1))
    aborted = tmp_path /"s2.jsonl"  # 扁平:无 state 目录
    assert aborted.exists()
    final = [json.loads(line) for line in aborted.read_text().splitlines()]
    assert final[-1]["type"] == "session/end" and final[-1]["data"]["state"] == "aborted"
    assert "RuntimeError" in final[-1]["data"]["reason"]


@pytest.mark.asyncio
async def test_file_sink_crash_residue_stays_flat(tmp_path):
    """崩溃残留(无 session/end)= 扁平目录里无终态行的文件,不迁移(排查素材)。"""
    sink = FileSink(str(tmp_path))
    await sink.consume(_evt("session/start", session="s3", seq=0, run_id="r3",
                            label="wiki:structure", provider="anthropic", model=""))
    path = tmp_path /"s3.jsonl"
    assert path.exists()
    final = [json.loads(line) for line in path.read_text().splitlines()]
    assert [ln["type"] for ln in final] == ["session/start"]
    assert "session/end" not in path.read_text()


@pytest.mark.asyncio
async def test_file_sink_raw_stream_writes_chunks(tmp_path):
    """原始事件流开关:raw=True 时 assistant/chunk 落盘,文件 seq 稠密(无洞)。"""
    sink = FileSink(str(tmp_path), raw=True)
    await sink.consume(_evt("session/start", session="raw1", seq=0, run_id="r1",
                            label="t", provider="anthropic", model=""))
    await sink.consume(_evt("assistant/chunk", session="raw1", seq=1,
                            chunk={"type": "content", "index": 0, "text": "你好"}))
    await sink.consume(_evt("assistant/message", session="raw1", seq=2,
                            message={"role": "assistant", "content": [{"type": "text", "text": "你好"}]},
                            surfaceOp="append", sourceSeqs=[1]))
    lines = [json.loads(line) for line in (tmp_path / "raw1.jsonl").read_text().splitlines()]
    assert [ln["type"] for ln in lines] == ["session/start", "assistant/chunk", "assistant/message"]
    assert [ln["seq"] for ln in lines] == [0, 1, 2]  # 稠密:chunk 不再被跳过


@pytest.mark.asyncio
async def test_configure_raw_flow_switch(monkeypatch, tmp_path):
    """configure(raw=...) → ensure_bus 的 FileSink 粒度切换;raw=None 重读 env 常量。"""
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[], raw=True)
    sinks.ensure_bus()
    assert sinks._file_sinks[0].raw is True  # 全量原始事件流(含 chunk)
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[], raw=None)
    assert not sinks._file_sinks  # configure 清空注册表(待重建)
    sinks.ensure_bus()
    assert sinks._file_sinks[0].raw is False  # None → env 常量缺省(非流式投影)
    monkeypatch.setattr(sinks.envs, "AGENT_MONITOR_FILE_RAW", True)
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[], raw=None)
    sinks.ensure_bus()
    assert sinks._file_sinks[0].raw is True  # None → 重读 env 常量


# ---------------------------------------------------------------------------
# 适配器归一化(喂形似 SDK 的纯 dict/对象)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_adapter_normalizes_events():
    """流事件 → 事件溯源:chunk 原始增量(含 thinking/tool_input)、tool/call 原始

    arguments 字符串、tool/result 全量、工具结果后 message_start → step 边界。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    bus = sinks.ensure_bus()
    bus.add(_recv(got))
    run = EventRecorder("s1", label="t")
    run.start()
    # 第 1 步:正文块 + 思考块
    _handle_stream_event(run, {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}})
    _handle_stream_event(run, {"type": "content_block_delta", "index": 0,
                               "delta": {"type": "text_delta", "text": "hi"}})
    _handle_stream_event(run, {"type": "content_block_stop", "index": 0})
    _handle_stream_event(run, {"type": "content_block_start", "index": 1, "content_block": {"type": "thinking"}})
    _handle_stream_event(run, {"type": "content_block_delta", "index": 1,
                               "delta": {"type": "thinking_delta", "thinking": "深"}})
    _handle_stream_event(run, {"type": "content_block_stop", "index": 1})
    # 工具调用:碎片 JSON 增量,收尾 raw arguments 字符串(不解析)
    _handle_stream_event(run, {"type": "content_block_start", "index": 2,
                               "content_block": {"type": "tool_use", "id": "t1", "name": "graphify_query"}})
    _handle_stream_event(run, {"type": "content_block_delta", "index": 2,
                               "delta": {"type": "input_json_delta", "partial_json": '{"q"'}})
    _handle_stream_event(run, {"type": "content_block_delta", "index": 2,
                               "delta": {"type": "input_json_delta", "partial_json": ': "x"}'}})
    _handle_stream_event(run, {"type": "content_block_stop", "index": 2})
    # 用户段工具结果(文本片段归属 tool/result,不入 text chunk)
    _handle_stream_event(run, {"type": "message_start", "message": {"role": "user"}})
    _handle_stream_event(run, {"type": "content_block_start", "index": 0,
                               "content_block": {"type": "tool_result", "tool_use_id": "t1", "is_error": False}})
    _handle_stream_event(run, {"type": "content_block_delta", "index": 0,
                               "delta": {"type": "text_delta", "text": "结果"}})
    _handle_stream_event(run, {"type": "content_block_delta", "index": 0,
                               "delta": {"type": "text_delta", "text": "片段"}})
    _handle_stream_event(run, {"type": "content_block_stop", "index": 0})
    # 下一段 assistant 消息 → step 边界(step/end + step/start)
    _handle_stream_event(run, {"type": "message_start", "message": {"role": "assistant"}})
    _handle_stream_event(run, {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}})
    _handle_stream_event(run, {"type": "content_block_delta", "index": 0,
                               "delta": {"type": "text_delta", "text": "答案是"}})
    await asyncio.sleep(0.05)

    types_ = [g["type"] for g in got]
    assert types_[:4] == ["session/start", "turn/start", "step/start", "assistant/chunk"]
    asst = next(g for g in got if g["type"] == "assistant/chunk")
    assert asst["data"]["chunk"]["text"] == "hi" and asst["seq"] == 3  # seq 从 0 连续
    thinking = next(g for g in got if g["type"] == "assistant/chunk"
                    and g["data"]["chunk"].get("type") == "thinking")
    assert thinking["data"]["chunk"]["text"] == "深"
    tool_c = next(g for g in got if g["type"] == "tool/call")
    assert tool_c["data"]["arguments"] == '{"q": "x"}'  # 原始字符串,JSON 解析交给 UI
    assert tool_c["data"]["callId"] == "t1" and tool_c["data"]["name"] == "graphify_query"
    tres = next(g for g in got if g["type"] == "tool/result")
    assert tres["data"]["message"]["content"][0]["content"] == "结果片段"  # 全量不截断
    assert tres["data"]["is_error"] is False and tres["data"]["step"] == 1
    assert tres["data"]["sourceSeqs"] == [tool_c["seq"]]
    # 工具结果后:step/end + step/start,新正文 chunk 归属 step 2
    last = got[-1]
    assert last["type"] == "assistant/chunk" and last["data"]["step"] == 2
    assert last["data"]["chunk"]["text"] == "答案是"
    # step 事件次序:run 入口开 step1,工具结果后收尾并开 step2
    assert [g["type"] for g in got if g["type"] in ("step/end", "step/start")] == \
        ["step/start", "step/end", "step/start"]


@pytest.mark.asyncio
async def test_assistant_message_monitors_but_never_duplicates():
    """整块消息:未产出增量时事件化一次(文本 chunk + 全量消息);已产出增量时

    只记元信息;无流事件的 tool_use 兜底补合成 tool/call。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    bus = sinks.ensure_bus()
    bus.add(_recv(got))
    msg = types.SimpleNamespace(
        content=[
            types.SimpleNamespace(type="text", text="hi"),
            types.SimpleNamespace(type="thinking", thinking="想", text=None),
            types.SimpleNamespace(type="tool_use", id="t2", name="read_file", input={"path": "a.py"}),
        ],
        stop_reason="end_turn",
        usage=types.SimpleNamespace(input_tokens=3, output_tokens=5, cache_read_input_tokens=1),
    )
    run = EventRecorder("s1", label="t")
    _handle_assistant_message(run, msg, already_yielded=False)
    assert run.text_chars == 2
    await asyncio.sleep(0.05)
    asst = next(g for g in got if g["type"] == "assistant/message")
    assert asst["data"]["message"]["content"][0] == {"type": "content", "text": "hi"}
    assert asst["data"]["usage"] == {
        "input_tokens": 3, "output_tokens": 5, "cache_read_input_tokens": 1,
    }
    assert asst["data"]["sourceSeqs"] == [g["seq"] for g in got if g["type"] == "assistant/chunk"]
    assert asst["data"]["stop_reason"] == "end_turn"
    synth = next(g for g in got if g["type"] == "tool/call")  # 兜底合成
    assert synth["data"]["callId"] == "t2" and synth["data"]["arguments"] == '{"path": "a.py"}'
    run2 = EventRecorder("s2", label="t")
    _handle_assistant_message(run2, msg, already_yielded=True)  # 已产出 → 不重复
    assert run2.text_chars == 0


@pytest.mark.asyncio
async def test_assistant_message_sdk_blocks_without_type():
    """SDK 0.2.142 起内容块为纯 dataclass(TextBlock/ThinkingBlock/ToolUseBlock 无 type 属性):

    按字段判别(`_block_kind`),文本/思考照常进 message;工具调用**不入消息块**,
    只以 tool/call 事件承载(兜底合成)。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    bus = sinks.ensure_bus()
    bus.add(_recv(got))
    msg = types.SimpleNamespace(
        content=[
            types.SimpleNamespace(text="hi"),  # TextBlock 形(无 type)
            types.SimpleNamespace(thinking="想", signature="s"),  # ThinkingBlock 形
            types.SimpleNamespace(id="t3", name="read_file"),  # ToolUseBlock 形(input 缺省)
        ],
        stop_reason=None,
        usage=None,
    )
    run = EventRecorder("s1", label="t")
    _handle_assistant_message(run, msg, already_yielded=False)
    assert run.text_chars == 2  # 文本已事件化(chunk 路径同享)
    await asyncio.sleep(0.05)
    asst = next(g for g in got if g["type"] == "assistant/message")
    assert asst["data"]["message"]["content"] == [
        {"type": "content", "text": "hi"},
        {"type": "thinking", "text": "想"},
    ]
    calls = [g for g in got if g["type"] == "tool/call"]
    assert [e["data"]["callId"] for e in calls] == ["t3"]  # 工具调用只经 tool/call 事件


@pytest.mark.asyncio
async def test_tool_call_single_emit_message_first_then_stream():
    """工具调用双发射点只发一次:消息路径先到(_call_seqs 记 seq),流 content_block_stop

    迟到时不再重发(真机 2.1.251:消息标记先于流事件,曾把每个真实 tool/call 双发)。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    bus = sinks.ensure_bus()
    bus.add(_recv(got))
    run = EventRecorder("s1", label="t")
    _handle_assistant_message(
        run,
        types.SimpleNamespace(
            content=[types.SimpleNamespace(type="tool_use", id="t1", name="read_file")],
            stop_reason=None,
            usage=None,
        ),
        already_yielded=False,
    )  # 消息路径:兜底补发 tool/call(记 _call_seqs)
    _handle_stream_event(run, {"type": "content_block_start", "index": 0,
                               "content_block": {"type": "tool_use", "id": "t1", "name": "read_file"}})
    _handle_stream_event(run, {"type": "content_block_stop", "index": 0})  # 流路径迟到:防重不发
    await asyncio.sleep(0.05)
    calls = [g for g in got if g["type"] == "tool/call"]
    assert [e["data"]["callId"] for e in calls] == ["t1"]  # 恰一次


def test_normalize_usage_maps_sdk_and_http():
    u = types.SimpleNamespace(input_tokens=1, output_tokens=2, cache_read_input_tokens=None)
    assert _normalize_usage(u) == {"input_tokens": 1, "output_tokens": 2,
                                         "cache_read_input_tokens": None}
    assert _normalize_usage({"prompt_tokens": 3, "completion_tokens": 4}) == \
        {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": None}
    assert _normalize_usage(None) is None


# ---------------------------------------------------------------------------
# 会话 id 归属(ns 解析序:显式 session_ns → run_id → session_name → "agent")
# ---------------------------------------------------------------------------


def test_session_id_ns_resolution():
    """ns 解析序;显式 session 原样;默认 <ns>/<uuid4> 且 ns 不含 "/"。"""
    assert _session_id("explicit", "ns", "run", "name") == "explicit"
    sid = _session_id(None, "ns-x", "run", "name")
    assert sid.startswith("ns-x/")
    assert len(sid.rsplit("/", 1)[1]) == 36  # 全量 uuid4
    assert _session_id(None, None, "run", "name").startswith("run/")
    assert _session_id(None, None, None, "name").startswith("name/")
    assert _session_id(None, None, None, None).startswith("agent/")  # 全兜底
    assert _session_id(None, "deepwiki:wiki", None, None).startswith("deepwiki:wiki/")


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


class _FakeClaudeAgentOptions:
    """形似 ClaudeAgentOptions:记录构造 kwargs(cc 经 claude_options 装配)。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


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

    msgs: ClassVar[list] = []  # 类级共享注入点(测试按类整体替换)
    enters: ClassVar[int] = 0  # 进入计数(生成器 with 语义断言用)
    exits: ClassVar[int] = 0
    entered: ClassVar[int] = 0  # 在飞进入数(生产层引用计数断言用)

    def __init__(self, options=None):
        self.options = options

    async def __aenter__(self):
        type(self).enters += 1
        type(self).entered += 1
        return self

    async def __aexit__(self, *exc):
        type(self).exits += 1
        type(self).entered -= 1
        return False

    async def query(self, prompt):
        pass

    def receive_response(self):
        return _AsyncIter(type(self).msgs)


class _FakeToolResultBlock:
    """形似 SDK ToolResultBlock(partial 模式工具结果经 UserMessage 透传)。"""

    def __init__(self, tool_use_id, content=None, is_error=None):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class _FakeUserMessage:
    """形似 SDK UserMessage。"""

    def __init__(self, content, parent_tool_use_id=None):
        self.content = content
        self.parent_tool_use_id = parent_tool_use_id
        self.tool_use_result = None
        self.origin = None
        self.uuid = None


def _fake_sdk(msgs):
    """构造假 claude_agent_sdk 模块(仅 cc_* 需要的成员),monkeypatch 注入 sys.modules。"""
    mod = types.ModuleType("claude_agent_sdk")
    _FakeClient.msgs = msgs
    _FakeClient.enters = _FakeClient.exits = 0
    _FakeClient.entered = 0
    mod.StreamEvent = _FakeStreamEvent
    mod.AssistantMessage = _FakeAssistantMessage
    mod.ResultMessage = _FakeResultMessage
    mod.UserMessage = _FakeUserMessage
    mod.ToolResultBlock = _FakeToolResultBlock
    mod.ClaudeSDKClient = _FakeClient
    mod.ClaudeAgentOptions = _FakeClaudeAgentOptions
    return mod


def _options():
    return {"model": "", "system_prompt": "sys"}


def test_cc_strict_mcp_default_isolation(monkeypatch):
    """默认隔离:未显式给 strict_mcp_config 时装配层注入 True;显式 False 保留。"""
    sdk = _fake_sdk([])
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    gen = agent.ClaudeCode(_options())
    assert gen._client.options.kwargs["strict_mcp_config"] is True
    gen2 = agent.ClaudeCode({"model": "", "strict_mcp_config": False})
    assert gen2._client.options.kwargs["strict_mcp_config"] is False


async def _fold(chunks):
    """旧 text 语义适配(text 已废):断言值取流增量折叠。"""
    return "".join([c async for c in chunks])


async def _call(gen_cls, config, prompt, *, method="stream", **session_kwargs):
    """适配器一次调用(测试统一入口):生成器 with 契约 —— 会话元数据在 session(),

    stream/result 只收载荷;session 块内直呼(一次会话一次调用)。
    """
    gen = gen_cls(config)
    async with gen.session(**session_kwargs):
        if method == "stream":
            return await _fold(gen.stream(prompt))
        return await gen.result(prompt)


@pytest.mark.asyncio
async def test_cc_stream_exact_output_no_duplicates(monkeypatch, tmp_path):
    """假 SDK:产出逐字节一致(content 增量优先,AssistantMessage 不重复兜底);

    监控自足(agent/ 首跑建 FileSink),会话落到配置的 tmp 目录(不写真实 monitor)。
    """
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
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
    gen = agent.ClaudeCode(_options())
    async with gen.session(session_name="x"):
        chunks = [c async for c in gen.stream("prompt")]
    assert "".join(chunks) == "hi好"
    await asyncio.sleep(0.05)  # 等 bus 末拍落盘
    files = list(tmp_path.glob("*.jsonl"))
    assert any("session/end" in f.read_text(encoding="utf-8") for f in files)  # 会话已落盘(tmp 隔离)


@pytest.mark.asyncio
async def test_cc_stream_captures_event_sequence(monkeypatch):
    """监控开启 + 假 SDK:全事件序列(含全量 prompt user/message、partial header)可折叠。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    sdk = _fake_sdk([
        _FakeStreamEvent({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
        _FakeStreamEvent({"type": "content_block_delta", "index": 0,
                          "delta": {"type": "text_delta", "text": "hi"}}),
        _FakeStreamEvent({"type": "content_block_stop", "index": 0}),
        _FakeAssistantMessage(content=[types.SimpleNamespace(type="text", text="hi")]),
        _FakeResultMessage(result="hi", usage=types.SimpleNamespace(input_tokens=2, output_tokens=1)),
    ])
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    await _call(agent.ClaudeCode, _options(), "整段 prompt ⚙", session_name="x", run_id="r1")
    await asyncio.sleep(0.05)
    types_ = [g["type"] for g in got]
    assert types_[0] == "config/init" and got[1]["data"]["run_id"] == "r1"  # 配置快照先于 session/start
    assert "user/message" in types_ and "assistant/message" in types_
    assert types_[-2:] == ["turn/end", "session/end"] and got[-1]["data"]["state"] == "completed"
    um = next(g for g in got if g["type"] == "user/message")
    assert um["data"]["message"]["content"][0]["text"] == "整段 prompt ⚙"  # 全量不截断
    rs = next(g for g in got if g["type"] == "session/end")
    assert rs["data"]["usage"] == {"input_tokens": 2, "output_tokens": 1, "cache_read_input_tokens": None}


@pytest.mark.asyncio
async def test_cc_stream_partial_markers_rebuild_and_boundary(monkeypatch):
    """partial 模式(真机 CLI 形状):AssistantMessage=空内容标记 → 缓冲重建全量;

    工具结果经 UserMessage(tool_result 块)合成 tool/result + 置 _tool_pending
    → 下条 assistant 消息(message_start)开 step 边界(修复前两者皆无)。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    sdk = _fake_sdk([
        # 消息 1:thinking 段(增量 → 空标记)
        _FakeStreamEvent({"type": "content_block_start", "index": 0,
                          "content_block": {"type": "thinking"}}),
        _FakeStreamEvent({"type": "content_block_delta", "index": 0,
                          "delta": {"type": "thinking_delta", "thinking": "深"}}),
        _FakeStreamEvent({"type": "content_block_stop", "index": 0}),
        _FakeAssistantMessage(content=[]),
        # 消息 2:工具调用(流路径合成 tool/call)→ 空标记
        _FakeStreamEvent({"type": "content_block_start", "index": 0,
                          "content_block": {"type": "tool_use", "id": "t1", "name": "WebSearch"}}),
        _FakeStreamEvent({"type": "content_block_delta", "index": 0,
                          "delta": {"type": "input_json_delta", "partial_json": '{"q": "x"}'}}),
        _FakeStreamEvent({"type": "content_block_stop", "index": 0}),
        _FakeAssistantMessage(content=[]),
        # 工具结果:UserMessage(tool_result 块)→ tool/result + _tool_pending
        _FakeUserMessage([_FakeToolResultBlock(
            "t1", [{"type": "text", "text": "R 结果"}], is_error=False)]),
        # 消息 3:新一轮 assistant(message_start → 开边界)→ text 段 → 空标记
        _FakeStreamEvent({"type": "message_start", "message": {"role": "assistant"}}),
        _FakeStreamEvent({"type": "content_block_start", "index": 0,
                          "content_block": {"type": "text"}}),
        _FakeStreamEvent({"type": "content_block_delta", "index": 0,
                          "delta": {"type": "text_delta", "text": "答案"}}),
        _FakeStreamEvent({"type": "content_block_stop", "index": 0}),
        _FakeAssistantMessage(content=[]),
        _FakeResultMessage(result="答案"),
    ])
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    assert await _call(agent.ClaudeCode, _options(), "prompt", session_name="x") == "答案"
    await asyncio.sleep(0.05)
    msgs = [g for g in got if g["type"] == "assistant/message"]
    # 空标记重建:msg1 = thinking;末条 = text(增量 → 标记)
    assert len(msgs) == 3
    assert msgs[0]["data"]["message"]["content"] == [{"type": "thinking", "text": "深"}]
    assert msgs[-1]["data"]["message"]["content"] == [{"type": "content", "text": "答案"}]
    tr = [g for g in got if g["type"] == "tool/result"]
    assert len(tr) == 1 and tr[0]["data"]["is_error"] is False
    assert tr[0]["data"]["message"]["content"][0]["content"] == "R 结果"
    assert tr[0]["data"]["name"] == "WebSearch"
    assert tr[0]["data"]["sourceSeqs"] == [g["seq"] for g in got if g["type"] == "tool/call"]
    # 工具结果后开步边界:step/end + step/start;末条消息归属 step 2
    assert [g["type"] for g in got if g["type"] in ("step/end", "step/start")] == \
        ["step/start", "step/end", "step/start", "step/end"]
    assert msgs[-1]["data"]["step"] == 2


@pytest.mark.asyncio
async def test_cc_text_fallback_without_partials(monkeypatch, tmp_path):
    """无 partial 事件:AssistantMessage 整块兜底一次,输出与原漏斗一致;

    监控自足落盘 tmp(会话/止行齐备)。
    """
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    sdk = _fake_sdk([
        _FakeAssistantMessage(content=[types.SimpleNamespace(type="text", text="hi好world")]),
        _FakeResultMessage(result="hi好world"),
    ])
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    assert await _call(agent.ClaudeCode, _options(), "prompt", session_name="x") == "hi好world"
    await asyncio.sleep(0.05)
    files = list(tmp_path.glob("*.jsonl"))
    assert any("session/end" in f.read_text(encoding="utf-8") for f in files)


@pytest.mark.asyncio
async def test_cc_stream_error_semantics(monkeypatch, tmp_path):
    """is_error 的 ResultMessage → RuntimeError"agent 执行失败: ..."(与旧漏斗同一文案);

    会话仍落盘(tmp,含 aborted 终态)。
    """
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    sdk = _fake_sdk([_FakeResultMessage(result="", is_error=True, errors=["boom"])])
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    with pytest.raises(agent.RequestFailedError, match="boom"):
        await _call(agent.ClaudeCode, _options(), "p", session_name="x")
    await asyncio.sleep(0.05)
    files = list(tmp_path.glob("*.jsonl"))
    assert any("session/end" in f.read_text(encoding="utf-8") for f in files)


@pytest.mark.asyncio
async def test_generator_session_lifecycle(monkeypatch):
    """会话 with 语义:一次 session = 客户端进入一次/回收一次(监控与客户端同寿);

    stream 只收载荷(session 元数据在 session());session 外调用 stream → 契约错误。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    sdk = _fake_sdk([
        _FakeAssistantMessage(content=[types.SimpleNamespace(type="text", text="hi")]),
        _FakeResultMessage(result="hi"),
    ])
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    gen = agent.ClaudeCode(_options())
    with pytest.raises(RuntimeError, match="session"):
        await _fold(gen.stream("prompt"))  # 会话外调用 → 契约错误
    async with gen.session(session_name="x"):  # 一次会话(子进程 spawn)
        assert _FakeClient.entered == 1
        assert await _fold(gen.stream("prompt")) == "hi"
        assert _FakeClient.entered == 1  # stream 不再进客户端(循环只在会话内)
    assert _FakeClient.entered == 0
    assert _FakeClient.enters == 1 and _FakeClient.exits == 1


# ---------------------------------------------------------------------------
# dsh 适配器(DeepSeek Harness SDK):假模块注入 + 同步 to_thread(零 SDK/网络/token)
# ---------------------------------------------------------------------------


class _DshSdkProtocolError(Exception):
    pass


class _DshJsonRpcError(Exception):
    pass


class _FakeNotification:
    """形似 dsh Notification 的假通知(dataclass:method/payload)。"""

    def __init__(self, method, payload):
        self.method = method
        self.payload = payload


class _FakeHarness:
    """形似 DeepSeekHarness 的假 harness:run() 同步重放通知脚本后返回注入结果。"""

    notifs: ClassVar[list] = []
    result: ClassVar[types.SimpleNamespace | None] = None
    raise_on_run: Exception | None = None
    calls: ClassVar[list] = []  # (kwargs, prompt, session_id)
    enters: ClassVar[int] = 0  # 进入/退出计数(生成器 with 语义断言用;同步 CM 经线程桥)
    exits: ClassVar[int] = 0

    def __init__(self, config=None, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        type(self).enters += 1
        return self

    def __exit__(self, *exc):
        type(self).exits += 1
        return False

    def run(self, prompt, *, session_id=None, on_notification=None):
        type(self).calls.append((self.kwargs, prompt, session_id))
        if type(self).raise_on_run is not None:
            raise type(self).raise_on_run
        for n in type(self).notifs:
            on_notification(n)
        return type(self).result


def _dsh_note(evt_type, *, data=None, seq=0, surface_op=None, source_seqs=None,
              session_id="sid"):
    """dsh session.event 通知构造(fixture 形状:surfaceOp/sourceEventSeqs 在信封层)。"""
    evt = {"type": evt_type, "seq": seq, "time": 0, "data": data or {}}
    if surface_op is not None:
        evt["surfaceOp"] = surface_op
    if source_seqs is not None:
        evt["sourceEventSeqs"] = source_seqs
    return _FakeNotification("session.event", {"sessionId": session_id, "event": evt})


def _dsh_chunk(ctype, **fields):
    return {"type": ctype, **fields}


def _dsh_options(**kw):
    """dsh config dict(provider 缺省 deepseek-official)。"""
    return {"provider": "deepseek-official", "model": "m1",
            "session_root": "/tmp/x", **kw}


def _fake_dsh(monkeypatch, notifs, *, finish_reason="completed", final_response="hi好",
              session_id="sid", sdk_raise=None, sync_to_thread=True, harness_cls=None):
    """注入假 deepseek_harness(errors 子模块一并);缺省把 to_thread 同步化。

    to_thread 同步化后泵路径原样执行(call_soon_threadsafe 按序落地):
    确定性、零网络、零 SDK、零 token。sync_to_thread=False 保持真实线程桥
    (测 run 在飞时的消费者提前退场);harness_cls 换假 harness(如慢 run)。
    """
    mod = types.ModuleType("deepseek_harness")
    errors = types.ModuleType("deepseek_harness.errors")
    errors.SdkProtocolError = _DshSdkProtocolError
    errors.JsonRpcError = _DshJsonRpcError
    mod.errors = errors
    mod.DeepSeekHarness = harness_cls or _FakeHarness
    mod.RunResult = types.SimpleNamespace
    monkeypatch.setitem(sys.modules, "deepseek_harness", mod)
    monkeypatch.setitem(sys.modules, "deepseek_harness.errors", errors)
    _FakeHarness.notifs = notifs
    _FakeHarness.result = types.SimpleNamespace(
        session_id=session_id, final_response=final_response, finish_reason=finish_reason,
        events=[], notifications=list(notifs), session_root=None)
    _FakeHarness.raise_on_run = sdk_raise
    _FakeHarness.calls = []
    _FakeHarness.enters = _FakeHarness.exits = 0
    if not sync_to_thread:
        return

    async def _to_thread_sync(fn, *a, **kw):
        return fn(*a, **kw)

    monkeypatch.setattr(asyncio, "to_thread", _to_thread_sync)


@pytest.mark.asyncio
async def test_dsh_stream_yields_and_projects_taxonomy(monkeypatch):
    """dsh 流 → 文本增量逐字节一致 + 全事件投影(seq 稠密/usage 归一化/step 追踪)。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    sid = "0460e1e9-5155-4014-9054-a39986462b20"
    notes = [
        _dsh_note("turn/start", seq=0, session_id=sid, data={"turn": 1}),
        _dsh_note("step/start", seq=1, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("user/message", seq=2, session_id=sid, surface_op="append",
                  data={"content": [{"type": "text", "text": "prompt"}],
                        "source": {"kind": "user"}, "role": "user", "id": sid}),
        _dsh_note("request/header", seq=3, session_id=sid,
                  data={"header": {"config": {"provider": "deepseek-official", "model": "m1"},
                                   "system": "sys", "tools": []},
                        "reason": "initial"}),
        _dsh_note("request/context", seq=4, session_id=sid,
                  data={"provider": "deepseek-official", "model": "m1"}),
        # 第 1 步:正文 + 思考增量
        _dsh_note("assistant/chunk", seq=5, session_id=sid,
                  data={"turn": 1, "step": 1, "chunk": _dsh_chunk("text-delta", index=0, text="hi")}),
        _dsh_note("assistant/chunk", seq=6, session_id=sid,
                  data={"turn": 1, "step": 1, "chunk": _dsh_chunk("reasoning-delta", index=1, text="想")}),
        _dsh_note("assistant/chunk", seq=7, session_id=sid,
                  data={"turn": 1, "step": 1, "chunk": _dsh_chunk("text-delta", index=0, text="好")}),
        # 工具块:碎片增量 + block-end 完整 arguments → 块端合成 tool/call
        _dsh_note("assistant/chunk", seq=8, session_id=sid,
                  data={"turn": 1, "step": 1,
                        "chunk": _dsh_chunk("tool-call-delta", index=2, id="t1",
                                            name="graphify_query", argumentsDelta='{"q"')}),
        _dsh_note("assistant/chunk", seq=9, session_id=sid,
                  data={"turn": 1, "step": 1,
                        "chunk": _dsh_chunk("tool-call-delta", index=2, id="t1",
                                            argumentsDelta=': "x"}')}),
        _dsh_note("assistant/chunk", seq=10, session_id=sid,
                  data={"turn": 1, "step": 1,
                        "chunk": _dsh_chunk("block-end", index=2,
                                            block={"type": "tool-call", "id": "t1",
                                                   "name": "graphify_query",
                                                   "arguments": '{"q": "x"}'})}),
        _dsh_note("assistant/chunk", seq=11, session_id=sid,
                  data={"turn": 1, "step": 1,
                        "chunk": _dsh_chunk("usage", usage={"inputTokens": 11, "outputTokens": 3,
                                                            "cacheReadTokens": 2})}),
        _dsh_note("assistant/chunk", seq=12, session_id=sid,
                  data={"turn": 1, "step": 1, "chunk": _dsh_chunk("finish", reason={"kind": "tool-calls"})}),
        # 整块消息 → 显式 tool/call(同 id,块端已合成 → 去重映射)→ tool/result
        _dsh_note("assistant/message", seq=13, session_id=sid, surface_op="append",
                  source_seqs=[5, 7],
                  data={"turn": 1, "step": 1,
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "hi好"}]},
                        "usage": {"inputTokens": 11, "outputTokens": 3, "cacheReadTokens": 2}}),
        _dsh_note("tool/call", seq=14, session_id=sid,
                  data={"turn": 1, "step": 1, "callId": "t1", "name": "graphify_query",
                        "arguments": '{"q": "x"}'}),
        _dsh_note("tool/result", seq=15, session_id=sid, source_seqs=[14],
                  data={"turn": 1, "step": 1,
                        "message": {"source": {"kind": "tool", "callId": "t1"}, "role": "user",
                                    "content": [{"type": "tool-result", "toolCallId": "t1",
                                                 "isError": False,
                                                 "content": [{"type": "text", "text": "结果"}]}]}}),
        _dsh_note("step/end", seq=16, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("step/start", seq=17, session_id=sid, data={"turn": 1, "step": 2}),
        _dsh_note("assistant/chunk", seq=18, session_id=sid,
                  data={"turn": 1, "step": 2, "chunk": _dsh_chunk("text-delta", index=0, text="!")}),
        _dsh_note("assistant/chunk", seq=19, session_id=sid,
                  data={"turn": 1, "step": 2, "chunk": _dsh_chunk("finish", reason={"kind": "stop"})}),
        _dsh_note("assistant/message", seq=20, session_id=sid, surface_op="append",
                  source_seqs=[18],
                  data={"turn": 1, "step": 2,
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "hi好!"}]}}),
        _dsh_note("step/end", seq=21, session_id=sid, data={"turn": 1, "step": 2}),
        _dsh_note("turn/end", seq=22, session_id=sid, data={"turn": 1, "reason": {"kind": "completed"}}),
    ]
    _fake_dsh(monkeypatch, notes, finish_reason="completed", final_response="hi好!", session_id=sid)
    gen = agent.Dsh(_dsh_options())
    async with gen.session(session=f"deepwiki:wiki/{sid}", session_name="wiki:structure"):
        chunks = [c async for c in gen.stream("prompt")]
    assert "".join(chunks) == "hi好!"
    await asyncio.sleep(0.05)
    types_ = [g["type"] for g in got]
    assert types_[0] == "config/init" and types_[1] == "session/start"
    assert types_[:5] == ["config/init", "session/start", "turn/start", "step/start", "user/message"]
    assert [g["seq"] for g in got] == list(range(len(got)))  # 投影流 seq 稠密
    # user/message 重塑:扁平化 → gh 嵌套 message + source
    um = next(g for g in got if g["type"] == "user/message")
    assert um["data"]["message"] == {"role": "user", "content": [{"type": "text", "text": "prompt"}]}
    assert um["data"]["source"] == {"kind": "user"}
    # 工具:块端 tool_call 增量与合成 tool/call(显式同 id 事件去重 → 恰一条)
    tool_inputs = [g for g in got if g["type"] == "assistant/chunk"
                   and g["data"]["chunk"].get("type") == "tool_call"]
    assert [t["data"]["chunk"]["partial_json"] for t in tool_inputs] == ['{"q"', ': "x"}']
    tc = [g for g in got if g["type"] == "tool/call"]
    assert len(tc) == 1 and tc[0]["data"]["arguments"] == '{"q": "x"}'
    assert tc[0]["data"]["name"] == "graphify_query" and tc[0]["data"]["step"] == 1
    # tool/result:卡片改名 + sourceSeqs 溯源到合成事件
    tr = next(g for g in got if g["type"] == "tool/result")
    assert tr["data"]["message"]["content"][0] == {"type": "tool_result", "tool_use_id": "t1",
                                                   "content": "结果", "is_error": False}
    assert tr["data"]["sourceSeqs"] == [tc[0]["seq"]]
    # assistant/message:step1 usage 归一化 + stop_reason(finish chunk)
    am1 = next(g for g in got if g["type"] == "assistant/message" and g["data"]["step"] == 1)
    assert am1["data"]["usage"] == {"input_tokens": 11, "output_tokens": 3,
                                    "cache_read_input_tokens": 2}
    assert am1["data"]["stop_reason"] == "tool-calls"
    am2 = next(g for g in got if g["type"] == "assistant/message" and g["data"]["step"] == 2)
    assert am2["data"]["stop_reason"] == "stop" and "usage" not in am2["data"]
    # step 追踪 dsh 值:第 2 步增量归属 step 2
    assert [g["data"]["step"] for g in got if g["type"] == "assistant/chunk"][-1] == 2
    # session/end 汇总:completed + usage 归一到末条
    end = got[-1]
    assert end["type"] == "session/end" and end["data"]["state"] == "completed"
    assert end["data"]["usage"] == {"input_tokens": 11, "output_tokens": 3,
                                    "cache_read_input_tokens": 2}
    assert end["data"]["stop_reason"] == "stop"


@pytest.mark.asyncio
async def test_dsh_stream_source_seqs_mapped(monkeypatch):
    """跳过的插件事件使 dsh seq 稀疏:sourceSeqs 映射为 gh seq 而非裸 dsh seq。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    sid = "sparse"
    notes = [
        _dsh_note("agent/inbox/spliced", seq=0, session_id=sid, data={"x": 1}),  # 跳过
        _dsh_note("turn/start", seq=1, session_id=sid, data={"turn": 1}),
        _dsh_note("step/start", seq=2, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("permission/preset", seq=3, session_id=sid, data={"mode": "deny"}),  # 跳过
        _dsh_note("user/message", seq=4, session_id=sid, surface_op="append",
                  data={"content": [{"type": "text", "text": "q"}], "source": {"kind": "user"},
                        "role": "user", "id": sid}),
        _dsh_note("assistant/chunk", seq=5, session_id=sid,
                  data={"turn": 1, "step": 1, "chunk": _dsh_chunk("text-delta", index=0, text="A")}),
        _dsh_note("session/title", seq=6, session_id=sid, data={"title": "x"}),  # 跳过
        _dsh_note("assistant/chunk", seq=7, session_id=sid,
                  data={"turn": 1, "step": 1, "chunk": _dsh_chunk("text-delta", index=0, text="B")}),
        _dsh_note("assistant/message", seq=8, session_id=sid, surface_op="append",
                  source_seqs=[5, 7],
                  data={"turn": 1, "step": 1,
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "AB"}]}}),
        _dsh_note("step/end", seq=9, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("turn/end", seq=10, session_id=sid, data={"turn": 1, "reason": {"kind": "completed"}}),
    ]
    _fake_dsh(monkeypatch, notes, session_id=sid)
    assert await _call(agent.Dsh, _dsh_options(), "q", session=f"deepwiki:wiki/{sid}") == "AB"
    await asyncio.sleep(0.05)
    chunks = [g for g in got if g["type"] == "assistant/chunk"]
    assert [g["seq"] for g in chunks] == [5, 6]  # gh seq(dsh 5/7 → 投影后 5/6;config/init 占 0)
    am = next(g for g in got if g["type"] == "assistant/message")
    assert am["data"]["sourceSeqs"] == [5, 6]
    assert 7 not in am["data"]["sourceSeqs"]  # 无裸 dsh seq


@pytest.mark.asyncio
async def test_dsh_stream_skips_non_taxonomy_events(monkeypatch):
    """插件/生命周期事件不投影不报错;异 sessionId 的子代理通知丢弃。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    sid = "skips"
    notes = [
        _dsh_note("session/end-seed", seq=0, session_id=sid, data={}),
        _dsh_note("approval/asked", seq=1, session_id=sid, data={"a": 1}),
        _dsh_note("sandbox/mode", seq=2, session_id=sid, data={"mode": "a"}),
        _dsh_note("todo/write", seq=3, session_id=sid, data={"todos": []}),
        _dsh_note("turn/start", seq=4, session_id=sid, data={"turn": 1}),
        _dsh_note("step/start", seq=5, session_id=sid, data={"turn": 1, "step": 1}),
        # 子代理会话泄漏(SDK 在过滤前回调全部会话通知):sessionId 不同 → 丢弃
        _dsh_note("assistant/chunk", seq=6, session_id="other",
                  data={"turn": 1, "step": 1, "chunk": _dsh_chunk("text-delta", index=0, text="leak")}),
        _dsh_note("user/message", seq=7, session_id=sid, surface_op="append",
                  data={"content": [{"type": "text", "text": "q"}], "source": {"kind": "subagent"},
                        "role": "user", "id": sid}),
        _dsh_note("assistant/chunk", seq=8, session_id=sid,
                  data={"turn": 1, "step": 1, "chunk": _dsh_chunk("text-delta", index=0, text="ok")}),
        _dsh_note("assistant/message", seq=9, session_id=sid, surface_op="append",
                  data={"turn": 1, "step": 1,
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}),
        _dsh_note("step/end", seq=10, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("turn/end", seq=11, session_id=sid, data={"turn": 1, "reason": {"kind": "completed"}}),
    ]
    _fake_dsh(monkeypatch, notes, session_id=sid)
    assert await _call(agent.Dsh, _dsh_options(), "q", session=f"deepwiki:wiki/{sid}") == "ok"
    await asyncio.sleep(0.05)
    assert {g["type"] for g in got} <= set(agent.TAXONOMY)  # 无 ValueError,只出 TAXONOMY
    texts = [g["data"]["chunk"].get("text") for g in got
             if g["type"] == "assistant/chunk" and g["data"]["chunk"].get("type") == "content"]
    assert texts == ["ok"]


@pytest.mark.asyncio
async def test_dsh_stream_tool_result_reshaped(monkeypatch):
    """tool/result:卡片改名(文本块拼接全量)、data.error 透传、name 来自块端工具名。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    sid = "toolr"
    notes = [
        _dsh_note("turn/start", seq=0, session_id=sid, data={"turn": 1}),
        _dsh_note("step/start", seq=1, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("user/message", seq=2, session_id=sid, surface_op="append",
                  data={"content": [{"type": "text", "text": "q"}], "source": {"kind": "user"},
                        "role": "user", "id": sid}),
        _dsh_note("assistant/chunk", seq=3, session_id=sid,
                  data={"turn": 1, "step": 1,
                        "chunk": _dsh_chunk("block-end", index=0,
                                            block={"type": "tool-call", "id": "t9", "name": "lookup",
                                                   "arguments": '{"k": "v"}'})}),
        _dsh_note("tool/result", seq=4, session_id=sid, source_seqs=[3],
                  data={"turn": 1, "step": 1,
                        "error": {"name": "GoalError", "code": "GOAL_NOT_FOUND"},
                        "message": {"source": {"kind": "tool", "callId": "t9"}, "role": "user",
                                    "content": [{"type": "tool-result", "toolCallId": "t9",
                                                 "isError": True,
                                                 "content": [{"type": "text", "text": "第一段"},
                                                             {"type": "text", "text": "第二段"}]}]}}),
        _dsh_note("step/end", seq=5, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("turn/end", seq=6, session_id=sid, data={"turn": 1, "reason": {"kind": "completed"}}),
    ]
    _fake_dsh(monkeypatch, notes, session_id=sid, final_response="")
    await _call(agent.Dsh, _dsh_options(), "q", session=f"deepwiki:wiki/{sid}")
    await asyncio.sleep(0.05)
    tr = next(g for g in got if g["type"] == "tool/result")
    assert tr["data"]["message"]["content"][0] == {"type": "tool_result", "tool_use_id": "t9",
                                                   "content": "第一段第二段", "is_error": True}
    assert tr["data"]["error"] == {"name": "GoalError", "code": "GOAL_NOT_FOUND"}
    assert tr["data"]["name"] == "lookup"  # 无独立 tool/call 事件 → 块端工具名
    assert tr["data"]["is_error"] is True


@pytest.mark.asyncio
async def test_dsh_stream_reasoning_delta_is_thinking_chunk(monkeypatch):
    """reasoning-delta:不产出文本,只发 thinking chunk(与 cc 漏斗一致)。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    sid = "think"
    notes = [
        _dsh_note("turn/start", seq=0, session_id=sid, data={"turn": 1}),
        _dsh_note("step/start", seq=1, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("user/message", seq=2, session_id=sid, surface_op="append",
                  data={"content": [{"type": "text", "text": "q"}], "source": {"kind": "user"},
                        "role": "user", "id": sid}),
        _dsh_note("assistant/chunk", seq=3, session_id=sid,
                  data={"turn": 1, "step": 1, "chunk": _dsh_chunk("reasoning-delta", index=0, text="深")}),
        _dsh_note("assistant/chunk", seq=4, session_id=sid,
                  data={"turn": 1, "step": 1, "chunk": _dsh_chunk("text-delta", index=1, text="回答")}),
        _dsh_note("assistant/message", seq=5, session_id=sid, surface_op="append",
                  data={"turn": 1, "step": 1,
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "回答"}]}}),
        _dsh_note("step/end", seq=6, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("turn/end", seq=7, session_id=sid, data={"turn": 1, "reason": {"kind": "completed"}}),
    ]
    _fake_dsh(monkeypatch, notes, session_id=sid)
    assert await _call(agent.Dsh, _dsh_options(), "q", session=f"deepwiki:wiki/{sid}") == "回答"
    await asyncio.sleep(0.05)
    th = next(g for g in got if g["type"] == "assistant/chunk"
              and g["data"]["chunk"].get("type") == "thinking")
    assert th["data"]["chunk"]["text"] == "深"


@pytest.mark.asyncio
async def test_dsh_stream_non_completed_reason_raises(monkeypatch):
    """finish_reason 非 completed → RuntimeError(cc 同文案);turn/end 恰一条(不双发)。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    sid = "maxt"
    notes = [
        _dsh_note("turn/start", seq=0, session_id=sid, data={"turn": 1}),
        _dsh_note("step/start", seq=1, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("user/message", seq=2, session_id=sid, surface_op="append",
                  data={"content": [{"type": "text", "text": "q"}], "source": {"kind": "user"},
                        "role": "user", "id": sid}),
        _dsh_note("assistant/chunk", seq=3, session_id=sid,
                  data={"turn": 1, "step": 1, "chunk": _dsh_chunk("text-delta", index=0, text="部分")}),
        _dsh_note("step/end", seq=4, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("turn/end", seq=5, session_id=sid,
                  data={"turn": 1, "reason": {"kind": "max-tokens"}}),
    ]
    _fake_dsh(monkeypatch, notes, finish_reason="max-tokens", final_response="部分", session_id=sid)
    with pytest.raises(agent.RequestFailedError, match="max-tokens"):
        await _call(agent.Dsh, _dsh_options(), "q", session=f"deepwiki:wiki/{sid}")
    await asyncio.sleep(0.05)
    assert [g["type"] for g in got].count("turn/end") == 1  # dsh 已转发 → 不合成(epilogue=False)
    end = got[-1]
    assert end["type"] == "session/end" and end["data"]["state"] == "aborted"
    assert end["data"]["reason"] == "max-tokens"  # 监控事件记原始 detail(文案组合层包装)
    assert any(g["type"] == "error" and g["data"]["stage"] == "run" for g in got)


@pytest.mark.asyncio
async def test_dsh_stream_sdk_protocol_error_stage_parse(monkeypatch):
    """SDK 协议错误:原样重抛、error 事件 stage=parse;无 dsh turn/end → 合成终局。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    sid = "proto"
    notes = [
        _dsh_note("turn/start", seq=0, session_id=sid, data={"turn": 1}),
        _dsh_note("step/start", seq=1, session_id=sid, data={"turn": 1, "step": 1}),
    ]
    _fake_dsh(monkeypatch, notes, session_id=sid, sdk_raise=_DshSdkProtocolError("boom"))
    gen = agent.Dsh(_dsh_options())
    with pytest.raises(_DshSdkProtocolError, match="boom"):
        async with gen.session(session=f"deepwiki:wiki/{sid}"):
            await _fold(gen.stream("q"))
    await asyncio.sleep(0.05)
    err = next(g for g in got if g["type"] == "error")
    assert err["data"]["stage"] == "parse"
    assert err["data"]["exc_type"] == "_DshSdkProtocolError"
    assert [g["type"] for g in got].count("turn/end") == 1  # 崩溃路径 epilogue 合成
    assert got[-1]["type"] == "session/end" and got[-1]["data"]["state"] == "aborted"


@pytest.mark.asyncio
async def test_dsh_result_empty_final_response_raises(monkeypatch):
    """completed 但无最终文本 → dsh_result RuntimeError(cc_result 同文案)。"""
    agent.configure(ws_urls=[], otel_urls=[])
    sid = "empty"
    notes = [
        _dsh_note("turn/start", seq=0, session_id=sid, data={"turn": 1}),
        _dsh_note("step/start", seq=1, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("user/message", seq=2, session_id=sid, surface_op="append",
                  data={"content": [{"type": "text", "text": "q"}], "source": {"kind": "user"},
                        "role": "user", "id": sid}),
        _dsh_note("step/end", seq=3, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("turn/end", seq=4, session_id=sid, data={"turn": 1, "reason": {"kind": "completed"}}),
    ]
    _fake_dsh(monkeypatch, notes, finish_reason="completed", final_response="", session_id=sid)
    gen = agent.Dsh(_dsh_options())
    with pytest.raises(agent.RequestFailedError, match="未产出最终结果"):
        async with gen.session(session=f"deepwiki:wiki/{sid}"):
            await gen.result("q")


@pytest.mark.asyncio
async def test_dsh_harness_fields_and_session_id(monkeypatch):
    """options → DeepSeekHarness kwargs(None 跳过 → SDK 缺省);gh session → dsh id 取尾段。"""
    agent.configure(ws_urls=[], otel_urls=[])
    sid = "0460e1e9-5155-4014-9054-a39986462b20"
    notes = [
        _dsh_note("turn/start", seq=0, session_id=sid, data={"turn": 1}),
        _dsh_note("step/start", seq=1, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("user/message", seq=2, session_id=sid, surface_op="append",
                  data={"content": [{"type": "text", "text": "job"}], "source": {"kind": "user"},
                        "role": "user", "id": sid}),
        _dsh_note("assistant/chunk", seq=3, session_id=sid,
                  data={"turn": 1, "step": 1, "chunk": _dsh_chunk("text-delta", index=0, text="hi好")}),
        _dsh_note("turn/end", seq=4, session_id=sid, data={"turn": 1, "reason": {"kind": "completed"}}),
    ]
    _fake_dsh(monkeypatch, notes, session_id=sid)
    options = {"provider": "deepseek-official", "model": "m1", "cwd": "/repo",
               "session_root": "/dsh-sessions", "env": {"A": "1"},
               "shutdown_timeout_seconds": None}
    assert await _call(agent.Dsh, options, "job", session=f"judge:llm/{sid}") == "hi好"
    kwargs, prompt, session_id = _FakeHarness.calls[0]
    assert kwargs == {"provider": "deepseek-official", "model": "m1", "cwd": "/repo",
                      "session_root": "/dsh-sessions", "env": {"A": "1"},
                      "cordis": agent.dsh_cordis_path()}  # None 跳过;cordis 未设 → 缺省隔离组合
    assert session_id == sid and prompt == "job"


# ---------------------------------------------------------------------------
# 生成器 with 语义(dsh 路):同步 harness CM 经线程桥进入/回收
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generator_with_dsh_bridges_sync_harness(monkeypatch):
    """dsh 生成器 with:harness(同步 CM)经 to_thread 桥进入/回收(_enter/_exit);

    stream 隐式 with 引用计数叠合 → 恰进一次、出一次(与底层 client 同形 wrapper)。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    sid = "withdsh"
    notes = [
        _dsh_note("turn/start", seq=0, session_id=sid, data={"turn": 1}),
        _dsh_note("step/start", seq=1, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("user/message", seq=2, session_id=sid, surface_op="append",
                  data={"content": [{"type": "text", "text": "q"}], "source": {"kind": "user"},
                        "role": "user", "id": sid}),
        _dsh_note("assistant/chunk", seq=3, session_id=sid,
                  data={"turn": 1, "step": 1, "chunk": _dsh_chunk("text-delta", index=0, text="好")}),
        _dsh_note("step/end", seq=4, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("turn/end", seq=5, session_id=sid, data={"turn": 1, "reason": {"kind": "completed"}}),
    ]
    _fake_dsh(monkeypatch, notes, final_response="好", session_id=sid)
    gen = agent.Dsh(_dsh_options())
    async with gen.session(session=f"deepwiki:wiki/{sid}"):  # 一次会话(harness 进入;测试 to_thread 同步化)
        assert _FakeHarness.enters == 1
        assert await _fold(gen.stream("q")) == "好"
        assert _FakeHarness.enters == 1  # stream 只驱动循环:不再进 harness
    assert _FakeHarness.enters == 1 and _FakeHarness.exits == 1


class _SlowHarness(_FakeHarness):
    """慢 run:回放通知后再滞留 0.25s —— 给"消费者提前退场时 run 在飞"留时间窗。"""

    def run(self, prompt, *, session_id=None, on_notification=None):
        result = super().run(prompt, session_id=session_id, on_notification=on_notification)
        time.sleep(0.25)
        return result


@pytest.mark.asyncio
async def test_dsh_stream_consumer_close_teardown_detached(monkeypatch):
    """消费者提前退场(run 在飞,真线程桥):_exit 不绊住消费者 —— 回收 detach

    后台(等线程自然跑完再退出 harness):不早收(不与 run 竞态)、不泄漏。
    旧语义:线程 with 块负责回收;新语义:生成器 with 收尸,提前退场走后台。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    sid = "detach"
    notes = [
        _dsh_note("turn/start", seq=0, session_id=sid, data={"turn": 1}),
        _dsh_note("step/start", seq=1, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("user/message", seq=2, session_id=sid, surface_op="append",
                  data={"content": [{"type": "text", "text": "q"}], "source": {"kind": "user"},
                        "role": "user", "id": sid}),
        _dsh_note("assistant/chunk", seq=3, session_id=sid,
                  data={"turn": 1, "step": 1, "chunk": _dsh_chunk("text-delta", index=0, text="好")}),
    ]
    _fake_dsh(monkeypatch, notes, sync_to_thread=False, harness_cls=_SlowHarness, session_id=sid)
    t0 = time.monotonic()
    gen = agent.Dsh(_dsh_options())
    async with gen.session(session=f"deepwiki:wiki/{sid}"):
        async for _ in gen.stream("q"):
            break  # 首个产出即退场:run 线程仍滞留(slow harness 尾部 0.25s)
    assert time.monotonic() - t0 < 0.2  # 会话退场不阻塞等 run(回收走后台收尸)
    await asyncio.sleep(0.4)  # 等后台收尸(线程跑完 → harness.__exit__)
    # 计数在 _SlowHarness(自身类的类属性遮蔽 _FakeHarness 同名计数)
    assert _SlowHarness.enters == 1 and _SlowHarness.exits == 1


@pytest.mark.asyncio
async def test_run_epilogue_and_prologue_preserve_cc_defaults():
    """回归:start/finish 缺省行为(合成 turn/step 生命周期)零变化;双 False 只留封套端点。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    run = EventRecorder("s1", label="t")
    run.start()
    run.finish(True)
    await asyncio.sleep(0.05)
    assert [g["type"] for g in got] == ["session/start", "turn/start", "step/start",
                                        "step/end", "turn/end", "session/end"]
    got.clear()
    run2 = EventRecorder("s2", label="t")
    run2.start(prologue=False)
    run2.finish(True, epilogue=False)
    await asyncio.sleep(0.05)
    assert [g["type"] for g in got] == ["session/start", "session/end"]


def test_normalize_usage_accepts_camel_case():
    """dsh TokenUsage(camelCase)→ gh 键;reasoningTokens 等额外键忽略。"""
    u = {"inputTokens": 1, "outputTokens": 2, "cacheReadTokens": 3, "reasoningTokens": 9}
    assert _normalize_usage(u) == {"input_tokens": 1, "output_tokens": 2,
                                   "cache_read_input_tokens": 3}


@pytest.mark.asyncio
async def test_dsh_stream_user_message_fallback(monkeypatch):
    """流缺 user/message:首个 assistant 事件前合成 prompt 消息(可折叠、不预发重复)。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    sid = "nouser"
    notes = [
        _dsh_note("turn/start", seq=0, session_id=sid, data={"turn": 1}),
        _dsh_note("step/start", seq=1, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("assistant/chunk", seq=2, session_id=sid,
                  data={"turn": 1, "step": 1, "chunk": _dsh_chunk("text-delta", index=0, text="a")}),
        _dsh_note("assistant/message", seq=3, session_id=sid, surface_op="append",
                  data={"turn": 1, "step": 1,
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "a"}]}}),
        _dsh_note("step/end", seq=4, session_id=sid, data={"turn": 1, "step": 1}),
        _dsh_note("turn/end", seq=5, session_id=sid, data={"turn": 1, "reason": {"kind": "completed"}}),
    ]
    _fake_dsh(monkeypatch, notes, session_id=sid)
    assert await _call(agent.Dsh, _dsh_options(), "prompt", session=f"deepwiki:wiki/{sid}") == "a"
    await asyncio.sleep(0.05)
    ums = [g for g in got if g["type"] == "user/message"]
    assert len(ums) == 1
    assert ums[0]["data"]["message"]["content"][0]["text"] == "prompt"
    chunk_seq = next(g["seq"] for g in got if g["type"] == "assistant/chunk")
    assert ums[0]["seq"] < chunk_seq  # 合成于首个 assistant 增量之前


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
        _evt("session/start", seq=0, run_id=None, label="wiki:structure", provider="anthropic", model=""),
        _evt("assistant/chunk", seq=1, chunk={"type": "content", "index": 0, "text": "你好"}),
        _evt("assistant/chunk", seq=2, chunk={"type": "content", "index": 0, "text": "世界"}),
    ]
    for e in evts:
        await sink.consume(e)
    await asyncio.sleep(0.05)  # 排空 ws worker
    try:
        # 原始事件原样转发:类型/全字段/顺序逐帧精确一致  # noqa: ERA001 - 中文说明注释,非被注释代码
        assert sent == [{"type": "evt", "event": e} for e in evts]
        # 无聚合产物泄漏(无 llm 行帧),delta 只携本块文本
        assert not any(f.get("line") for f in sent)
        delta_texts = [f["event"]["data"]["chunk"]["text"] for f in sent
                       if f["event"]["type"] == "assistant/chunk"]
        assert delta_texts == ["你好", "世界"]
        assert "你好世界" not in delta_texts
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
        _evt("session/start", seq=0, run_id="r1", label="wiki:structure",
             provider="anthropic", model=""),
        _evt("step/start", seq=1, turn=1, step=1),
        _evt("assistant/chunk", seq=2, chunk={"type": "content", "index": 0, "text": "你好"}),
        _evt("assistant/chunk", seq=3, chunk={"type": "content", "index": 0, "text": "世界"}),
        _evt("assistant/message", seq=4, message={"role": "assistant", "content": []},
             usage={"input_tokens": 3, "output_tokens": 5, "cache_read_input_tokens": 1},
             stop_reason="end_turn", surfaceOp="append"),
        _evt("tool/call", seq=5, callId="t1", name="graphify_query", arguments='{"q": "x"}'),
        _evt("tool/result", seq=6, callId="t1", name="graphify_query", is_error=False,
             message={"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "t1", "content": "结果", "is_error": False}]},
             surfaceOp="append", sourceSeqs=[5]),
        _evt("step/end", seq=7, turn=1, step=1),
        _evt("session/end", seq=8, state="completed", ok=True, duration_ms=123,
             text_chars=4, num_steps=1),
    ]:
        await sink.consume(evt)
    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "wiki:structure · anthropic")
    step = next(s for s in spans if s.name == "step.1")
    tool_call = next(s for s in spans if s.name == "tool.call:graphify_query")
    tres = next(s for s in spans if s.name == "tool.result:graphify_query")
    # 父子关系:step/工具都挂在根 span 下
    assert step.parent.span_id == root.context.span_id
    assert tool_call.parent.span_id == root.context.span_id
    assert tres.parent.span_id == root.context.span_id
    assert root.attributes["gen_ai.provider.name"] == "anthropic"
    assert root.attributes["gh_puller.run_id"] == "r1"
    assert root.attributes["gen_ai.usage.input_tokens"] == 3
    assert root.attributes["gh_puller.stop_reason"] == "end_turn"
    assert root.attributes["gh_puller.duration_ms"] == 123
    assert root.attributes["gh_puller.num_steps"] == 1
    assert root.status.status_code == StatusCode.OK
    assert step.attributes["gh_puller.text_chars"] == 4
    assert step.attributes["gh_puller.text_preview"] == "你好世界"
    assert tool_call.attributes["gh_puller.arguments_preview"] == '{"q": "x"}'
    assert tres.attributes["gh_puller.content_preview"] == "结果"
    assert tres.attributes["gh_puller.is_error"] is False
    assert sink._spans == {}  # session/end 清场


@pytest.mark.asyncio
async def test_otel_error_status_and_cleanup():
    sink, exporter = _otel_sink()
    await sink.consume(_evt("session/start", seq=0, run_id=None, label="wiki:structure",
                            provider="anthropic", model=""))
    await sink.consume(_evt("error", seq=1, stage="run", exc_type="RuntimeError",
                            message="agent 执行失败: boom"))
    await sink.consume(_evt("session/end", seq=2, state="aborted", ok=False,
                            duration_ms=10, text_chars=0, num_steps=1,
                            reason="RuntimeError: agent 执行失败: boom"))
    root = exporter.get_finished_spans()[0]
    assert root.status.status_code == StatusCode.ERROR
    assert "RuntimeError" in root.attributes["gh_puller.error"]
    assert root.attributes["gh_puller.error_stage"] == "run"
    assert root.attributes["gh_puller.duration_ms"] == 10
    assert sinks  # 防未用 import 误删(见下)
    assert sink._spans == {}


@pytest.mark.asyncio
async def test_otel_usage_attrs_skip_none():
    sink, exporter = _otel_sink()
    await sink.consume(_evt("session/start", seq=0, run_id=None, label="wiki:structure",
                            provider="anthropic", model=""))
    await sink.consume(_evt("assistant/message", seq=1,
                            message={"role": "assistant", "content": [{"type": "text", "text": "x"}]},
                            usage={"input_tokens": 8, "output_tokens": 2, "cache_read_input_tokens": None},
                            stop_reason="end_turn", surfaceOp="append"))
    await sink.consume(_evt("session/end", seq=2, state="completed", ok=True,
                            duration_ms=9, text_chars=1, num_steps=1))
    root = exporter.get_finished_spans()[0]
    assert root.attributes["gen_ai.usage.input_tokens"] == 8
    assert root.attributes["gen_ai.usage.output_tokens"] == 2
    assert "gh_puller.cache_read_input_tokens" not in root.attributes  # None 跳过
    assert root.attributes["gh_puller.stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_otel_off_by_default():
    """otel_urls / ws_urls 空:总线恒挂文件 sink(恒开约定)→ enabled;默认无 ws/otel。"""
    agent.configure(ws_urls=[], otel_urls=[])
    bus = sinks.ensure_bus()
    assert bus.enabled is True  # 文件 sink 恒在
    assert len(bus._sinks) == 1  # 仅文件 sink(ws/otel 空,无额外注册)
    bus.shutdown()


@pytest.mark.asyncio
async def test_otel_failure_isolated(monkeypatch):
    """tracer 异常:consume 不抛、每会话只报一次日志。"""
    logged = []
    monkeypatch.setattr("gh_puller.agent.sinks._log", logged.append)

    class _BoomTracer:
        def start_span(self, *a, **kw):
            raise RuntimeError("boom")

    sink = OtelSink("http://preview/v1/traces", tracer=_BoomTracer())
    await sink.consume(_evt("session/start", seq=0, run_id=None, label="wiki:structure",
                            provider="anthropic", model=""))  # start_span 抛 → 吞掉不抛
    # 已报过 → 静默
    await sink.consume(
        _evt("assistant/chunk", seq=1, chunk={"type": "content", "index": 0, "text": "x"}))
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
    agent.configure(ws_urls="ws://a/ws, ws://b/ws", otel_urls=[])
    assert sinks.ensure_bus().enabled is True
    assert urls == ["ws://a/ws", "ws://b/ws"]


@pytest.mark.asyncio
async def test_ensure_bus_skips_unreachable_otel(monkeypatch):
    """端点不可达:不注册该 OTel 实例(仅一条日志);总线只剩恒在的文件 sink。"""
    logged = []
    monkeypatch.setattr("gh_puller.agent.sinks._url_reachable", lambda url: False)
    monkeypatch.setattr("gh_puller.agent.sinks._log", logged.append)
    agent.configure(ws_urls=[], otel_urls="http://localhost:6006/")
    bus = sinks.ensure_bus()
    assert bus.enabled is True  # 文件 sink 恒在(系统约定)
    assert len(bus._sinks) == 1  # 仅文件 sink:不可达 OTel 未注册
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
    agent.configure(ws_urls=[], otel_urls="http://localhost:6006/")
    bus = sinks.ensure_bus()
    assert bus.enabled is True  # 文件 sink 恒在
    assert len(bus._sinks) == 1  # 仅文件 sink:缺依赖 OTel 未注册
    assert any("缺依赖" in m for m in logged)


@pytest.mark.asyncio
async def test_ensure_bus_one_otel_sink_per_url(monkeypatch):
    """每个可达 OTel 地址一个 sink 实例;构造 URL 已归一为完整 OTLP 路径。"""
    urls = []
    patch = _rec_sink(urls)
    patch(monkeypatch, "OtelSink")
    monkeypatch.setattr("gh_puller.agent.sinks._url_reachable", lambda url: True)
    monkeypatch.setattr("gh_puller.agent.sinks._log", lambda msg: None)
    agent.configure(ws_urls=[], otel_urls="http://p1:6006/,http://p2:6006/v1/traces")
    assert sinks.ensure_bus().enabled is True
    assert urls == ["http://p1:6006/v1/traces", "http://p2:6006/v1/traces"]


@pytest.mark.asyncio
async def test_file_sink_on_by_default(monkeypatch, tmp_path):
    """AGENT_MONITOR_FILE 已移除:file 参数缺省(None)即恒开,目录即建。"""
    monkeypatch.setattr("gh_puller.agent.sinks._url_reachable", lambda url: False)
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    bus = sinks.ensure_bus()  # file=None → True(无 env 可读)
    assert bus.enabled is True  # 恒开:无 env 可关


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
    agent.configure(ws_urls=[], otel_urls=None)  # None → 重读 envs 常量
    assert sinks.ensure_bus().enabled is True
    assert otel_urls == ["http://envp:6006/v1/traces"]
    # ws 半:AGENT_MONITOR_WEBUI_URL
    ws_urls = []
    patch = _rec_sink(ws_urls)
    patch(monkeypatch, "WsSink")
    monkeypatch.setattr(sinks.envs, "AGENT_MONITOR_WEBUI_URL", "ws://env/ws")
    agent.configure(ws_urls=None, otel_urls=[])
    assert sinks.ensure_bus().enabled is True
    assert ws_urls == ["ws://env/ws"]


# ---------------------------------------------------------------------------
# codex 适配器(OpenAI Codex SDK):假模块注入(AsyncCodex/CodexConfig/Sandbox)零 SDK/网络/token
# ---------------------------------------------------------------------------


class _CodexJsonRpcError(Exception):
    pass


class _FakeCodexSandbox(StrEnum):
    read_only = "read-only"
    workspace_write = "workspace-write"
    full_access = "full-access"


class _FakeCodexApprovalMode(StrEnum):
    deny_all = "deny_all"
    auto_review = "auto_review"


class _FakeNotify:
    """形似 codex Notification 的假通知(method/payload;payload 用 SimpleNamespace)。"""

    def __init__(self, method, payload):
        self.method = method
        self.payload = types.SimpleNamespace(**payload)


class _FakeCodexConfig:
    """形似 CodexConfig 的假 dataclass:记录构造 kwargs。"""

    instances: ClassVar[list] = []

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        type(self).instances.append(self)


class _FakeTurnHandle:
    """形似 AsyncTurnHandle:stream() 重放注入通知后自然结束;run() 返回注入的 turn_result。"""

    notifs: ClassVar[list] = []
    turn_result: object = None

    def __init__(self, thread_id, turn_id):
        self.thread_id = thread_id
        self.id = turn_id

    async def stream(self):
        for n in type(self).notifs:
            yield n

    async def run(self):
        return type(self).turn_result


class _FakeThread:
    """形似 AsyncThread:记录 turn(prompt, kwargs),返回假 handle。"""

    calls: ClassVar[list] = []

    def __init__(self, thread_id):
        self.id = thread_id

    async def turn(self, prompt, **kwargs):
        type(self).calls.append((prompt, kwargs))
        return _FakeTurnHandle(self.id, "t1")


class _FakeCodex:
    """形似 AsyncCodex:拦截 config/login_api_key/thread_start,退出计数(防子进程泄漏)。"""

    instances: ClassVar[list] = []
    closed = 0
    thread_raise: Exception | None = None

    def __init__(self, config=None):
        self.config = config
        self.logged_keys: list[str] = []
        self.thread_start_kwargs = None
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        type(self).closed += 1
        return False

    async def login_api_key(self, api_key):
        self.logged_keys.append(api_key)

    async def thread_start(self, **kwargs):
        if type(self).thread_raise is not None:
            raise type(self).thread_raise
        self.thread_start_kwargs = kwargs
        return _FakeThread("thr_1")


def _fake_codex(monkeypatch, notifs, *, thread_raise=None, user_home):
    """注入假 openai_codex(errors 一并)并复位全部静态;Path.home 指到假 HOME。

    user_home 是假"用户主目录"(含 ~/.codex/auth.json 等):auth 引导复制从此取,
    防读到机器真实 ~/.codex/{auth.json,config.toml} 且防向真实 ~/.gh-puller 写。
    """
    import pathlib

    mod = types.ModuleType("openai_codex")
    mod.AsyncCodex = _FakeCodex
    mod.CodexConfig = _FakeCodexConfig
    mod.Sandbox = _FakeCodexSandbox
    mod.ApprovalMode = _FakeCodexApprovalMode
    mod.JsonRpcError = _CodexJsonRpcError
    monkeypatch.setitem(sys.modules, "openai_codex", mod)
    _FakeTurnHandle.notifs = list(notifs)
    _FakeThread.calls = []
    _FakeCodex.instances = []
    _FakeCodex.closed = 0
    _FakeCodex.thread_raise = thread_raise
    _FakeCodexConfig.instances = []
    monkeypatch.setattr(pathlib.Path, "home",
                        classmethod(lambda cls: pathlib.Path(user_home)))


def _codex_nt(method, **payload):
    """codex 通知构造(payload 蛇形属性,与 pydantic populate_by_name 属性面一致)。"""
    return _FakeNotify(method, payload)


def _codex_it(**fields):
    """codex item 构造(ThreadItem 实际项,蛇形字段)。"""
    return types.SimpleNamespace(**fields)


def _codex_options(**kw):
    return {"model": "m1", "system_prompt": "sys", **kw}


def _codex_user_home(tmp_path, *, with_auth=False):
    """假用户主目录(with_auth → 放置 ~/.codex/auth.json 供引导复制)。"""
    home = tmp_path / "home"
    if with_auth:
        (home / ".codex").mkdir(parents=True, exist_ok=True)
        (home / ".codex" / "auth.json").write_text('{"tok": "u"}', encoding="utf-8")
    return str(home)


@pytest.mark.asyncio
async def test_codex_stream_text_deltas_deduped(monkeypatch, tmp_path):
    """文本增量优先、assistant/message 全量 + phase;delta 与 completed 恰一次(不双发)。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    _fake_codex(monkeypatch, [
        _codex_nt("turn/started", turn=_codex_it(id="t1")),
        _codex_nt("item/started", item=_codex_it(type="agentMessage", id="m1")),
        _codex_nt("item/agentMessage/delta", delta="hi", item_id="m1"),
        _codex_nt("item/agentMessage/delta", delta="好", item_id="m1"),
        _codex_nt("item/completed",
                  item=_codex_it(type="agentMessage", id="m1", text="hi好", phase="final_answer")),
        _codex_nt("turn/completed", turn=_codex_it(id="t1", status="completed", error=None)),
    ], user_home=_codex_user_home(tmp_path))
    gen = agent.Codex(_codex_options(codex_home=str(tmp_path)))
    async with gen.session(session_name="x"):
        chunks = [c async for c in gen.stream("prompt")]
    assert "".join(chunks) == "hi好"
    await asyncio.sleep(0.05)
    asst = next(g for g in got if g["type"] == "assistant/message")
    assert asst["data"]["message"]["content"] == [
        {"type": "content", "text": "hi好", "phase": "final_answer"}]
    assert asst["data"]["sourceSeqs"] == \
        [g["seq"] for g in got if g["type"] == "assistant/chunk"]  # 增量 seq 引用
    assert len([g for g in got if g["type"] == "assistant/chunk"]) == 2


@pytest.mark.asyncio
async def test_codex_stream_event_sequence(monkeypatch, tmp_path):
    """全 TAXONOMY 事件序列:session/turn/step 合成,partial header,终局 completed。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    _fake_codex(monkeypatch, [
        _codex_nt("turn/started", turn=_codex_it(id="t1")),
        _codex_nt("item/started", item=_codex_it(type="agentMessage", id="m1")),
        _codex_nt("item/agentMessage/delta", delta="a", item_id="m1"),
        _codex_nt("item/completed",
                  item=_codex_it(type="agentMessage", id="m1", text="a", phase=None)),
        _codex_nt("thread/tokenUsage/updated",
                  token_usage=_codex_it(total=_codex_it(input_tokens=2, output_tokens=1,
                                                        cache_read_input_tokens=None),
                                        last=None)),
        _codex_nt("turn/completed", turn=_codex_it(id="t1", status="completed", error=None)),
    ], user_home=_codex_user_home(tmp_path))
    await _call(agent.Codex, _codex_options(codex_home=str(tmp_path)), "整段 prompt ⚙",
                session_name="x", run_id="r1")
    await asyncio.sleep(0.05)
    types_ = [g["type"] for g in got]
    assert types_[:5] == ["config/init", "session/start", "turn/start", "step/start", "user/message"]
    assert got[1]["data"]["run_id"] == "r1"  # session/start(索引 1)携带身份
    assert [g["seq"] for g in got] == list(range(len(got)))  # 合成流 seq 稠密
    um = next(g for g in got if g["type"] == "user/message")
    assert um["data"]["message"]["content"][0]["text"] == "整段 prompt ⚙"
    assert types_[-2:] == ["turn/end", "session/end"] and got[-1]["data"]["state"] == "completed"
    assert got[-1]["data"]["usage"] == {"input_tokens": 2, "output_tokens": 1,
                                        "cache_read_input_tokens": None}


@pytest.mark.asyncio
async def test_codex_stream_thinking_and_plan_chunks(monkeypatch, tmp_path):
    """reasoning 增量 → thinking chunk(不产出文本);reasoning/plan completed 无重复双投。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    _fake_codex(monkeypatch, [
        _codex_nt("turn/started", turn=_codex_it(id="t1")),
        _codex_nt("item/started", item=_codex_it(type="reasoning", id="r1")),
        _codex_nt("item/reasoning/textDelta", delta="深", item_id="r1", content_index=0),
        _codex_nt("item/completed", item=_codex_it(type="reasoning", id="r1",
                                                   content=["深"], summary=["…"])),
        _codex_nt("item/started", item=_codex_it(type="plan", id="p1")),
        _codex_nt("item/completed", item=_codex_it(type="plan", id="p1", text="计划")),
        _codex_nt("item/started", item=_codex_it(type="agentMessage", id="m1")),
        _codex_nt("item/agentMessage/delta", delta="回答", item_id="m1"),
        _codex_nt("item/completed",
                  item=_codex_it(type="agentMessage", id="m1", text="回答", phase=None)),
        _codex_nt("turn/completed", turn=_codex_it(id="t1", status="completed", error=None)),
    ], user_home=_codex_user_home(tmp_path))
    assert await _call(agent.Codex, _codex_options(codex_home=str(tmp_path)), "q",
                       session_name="x") == "回答"
    await asyncio.sleep(0.05)
    kinds = [g["data"]["chunk"].get("type") for g in got
             if g["type"] == "assistant/chunk"]
    assert kinds == ["thinking", "plan", "content"]  # reasoning completed 未重复;plan 恰一次
    assert kinds.count("plan") == 1
    # 块式契约:reasoning completed 定型 message(thinking);content 消息分型 sourceSeqs
    msgs = [g for g in got if g["type"] == "assistant/message"]
    think_msg = next(m for m in msgs
                     if m["data"]["message"]["content"][0].get("type") == "thinking")
    assert think_msg["data"]["message"]["content"] == [{"type": "thinking", "text": "深"}]
    assert think_msg["data"]["sourceSeqs"] == \
        [g["seq"] for g in got if g["type"] == "assistant/chunk"
         and g["data"]["chunk"].get("type") == "thinking"]
    content_msg = next(m for m in msgs
                       if m["data"]["message"]["content"][0].get("type") == "content")
    assert content_msg["data"]["sourceSeqs"] == \
        [g["seq"] for g in got if g["type"] == "assistant/chunk"
         and g["data"]["chunk"].get("type") == "content"]


@pytest.mark.asyncio
async def test_codex_stream_summary_and_plan_deltas(monkeypatch, tmp_path):
    """摘要增量(summaryTextDelta)与 plan 增量(plan/delta)按真实段位/逐条输出。

    CoT 加密模型的可见推理只有摘要:summaryPartAdded 标记段位起点(无文本不产事件),
    段序由 summaryTextDelta 的 summary_index 承载(index 跳变=新段);plan/delta 逐条
    plan chunk,completed 兜底经 st.plan_items 防双投;有 summary 无 content 的 reasoning
    completed(无增量流)→ summary 兜底。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    _fake_codex(monkeypatch, [
        _codex_nt("turn/started", turn=_codex_it(id="t1")),
        _codex_nt("item/started", item=_codex_it(type="reasoning", id="r1")),
        _codex_nt("item/reasoning/summaryPartAdded", item_id="r1", summary_index=0),
        _codex_nt("item/reasoning/summaryTextDelta", delta="深", item_id="r1", summary_index=0),
        _codex_nt("item/reasoning/summaryPartAdded", item_id="r1", summary_index=1),
        _codex_nt("item/reasoning/summaryTextDelta", delta="思", item_id="r1", summary_index=1),
        _codex_nt("item/completed", item=_codex_it(type="reasoning", id="r1",
                                                   content=[], summary=["深", "思"])),
        _codex_nt("item/started", item=_codex_it(type="plan", id="p1")),
        _codex_nt("item/plan/delta", delta="先查", item_id="p1"),
        _codex_nt("item/plan/delta", delta="再写", item_id="p1"),
        _codex_nt("item/completed", item=_codex_it(type="plan", id="p1", text="先查再写")),
        _codex_nt("item/completed", item=_codex_it(type="reasoning", id="r2",
                                                   content=[], summary=["兜底"])),
        _codex_nt("item/started", item=_codex_it(type="agentMessage", id="m1")),
        _codex_nt("item/agentMessage/delta", delta="回答", item_id="m1"),
        _codex_nt("item/completed",
                  item=_codex_it(type="agentMessage", id="m1", text="回答", phase=None)),
        _codex_nt("turn/completed", turn=_codex_it(id="t1", status="completed", error=None)),
    ], user_home=_codex_user_home(tmp_path))
    assert await _call(agent.Codex, _codex_options(codex_home=str(tmp_path)), "q",
                       session_name="x") == "回答"
    await asyncio.sleep(0.05)
    chunks = [g["data"]["chunk"] for g in got if g["type"] == "assistant/chunk"]
    assert [c["type"] for c in chunks] == \
        ["thinking", "thinking", "plan", "plan", "thinking", "content"]
    assert [c["index"] for c in chunks[:2]] == [0, 1]  # 摘要段位真实承载(index 跳变=新段)
    assert [c["text"] for c in chunks] == ["深", "思", "先查", "再写", "兜底", "回答"]
    assert [c["index"] for c in chunks[2:4]] == [0, 0]  # plan 无段位字段,恒定单文档
    # 块式契约:每个 reasoning 项定型 message(thinking)(全文拼接),content 消息分型
    msgs = [g for g in got if g["type"] == "assistant/message"]
    think_msgs = [m for m in msgs
                  if m["data"]["message"]["content"][0].get("type") == "thinking"]
    assert [m["data"]["message"]["content"][0]["text"] for m in think_msgs] == ["深思", "兜底"]
    assert think_msgs[0]["data"]["sourceSeqs"] == \
        [g["seq"] for g in got if g["type"] == "assistant/chunk"
         and g["data"]["chunk"].get("type") == "thinking"][:2]
    content_msg = next(m for m in msgs
                       if m["data"]["message"]["content"][0].get("type") == "content")
    assert content_msg["data"]["sourceSeqs"] == \
        [g["seq"] for g in got if g["type"] == "assistant/chunk"
         and g["data"]["chunk"].get("type") == "content"]


@pytest.mark.asyncio
async def test_codex_stream_tool_round_single_step_boundary(monkeypatch, tmp_path):
    """并行两个 mcp 工具:各 1 组 tool/call|result;随后下一 LLM item 恰开一次 step 边界。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    _fake_codex(monkeypatch, [
        _codex_nt("turn/started", turn=_codex_it(id="t1")),
        _codex_nt("item/started", item=_codex_it(type="reasoning", id="r1")),
        _codex_nt("item/reasoning/textDelta", delta="想", item_id="r1", content_index=0),
        _codex_nt("item/completed", item=_codex_it(type="reasoning", id="r1", content=["想"])),
        _codex_nt("item/completed",
                  item=_codex_it(type="mcpToolCall", id="c1", server="graphify",
                                 tool="query_graph", arguments={"q": "x"}, status="completed",
                                 result=_codex_it(content=[_codex_it(type="text", text="结果1")],
                                                  structured_content=None),
                                 error=None)),
        _codex_nt("item/completed",
                  item=_codex_it(type="mcpToolCall", id="c2", server="graphify",
                                 tool="query_graph", arguments='{"q": "y"}', status="completed",
                                 result=_codex_it(content=[_codex_it(type="text", text="结果2")],
                                                  structured_content=None),
                                 error=None)),
        _codex_nt("item/started", item=_codex_it(type="agentMessage", id="m2")),
        _codex_nt("item/agentMessage/delta", delta="final", item_id="m2"),
        _codex_nt("item/completed",
                  item=_codex_it(type="agentMessage", id="m2", text="final", phase=None)),
        _codex_nt("turn/completed", turn=_codex_it(id="t1", status="completed", error=None)),
    ], user_home=_codex_user_home(tmp_path))
    assert await _call(agent.Codex, _codex_options(codex_home=str(tmp_path)), "q",
                       session_name="x") == "final"
    await asyncio.sleep(0.05)
    calls = [g for g in got if g["type"] == "tool/call"]
    assert len(calls) == 2
    assert [c["data"]["name"] for c in calls] == ["mcp__graphify__query_graph"] * 2
    assert calls[0]["data"]["arguments"] == '{"q": "x"}'  # dict → json.dumps
    assert calls[1]["data"]["arguments"] == '{"q": "y"}'  # str 原样
    results = [g for g in got if g["type"] == "tool/result"]
    assert len(results) == 2
    assert [r["data"]["is_error"] for r in results] == [False, False]
    assert results[0]["data"]["message"]["content"][0]["content"] == "结果1"
    assert results[0]["data"]["sourceSeqs"] == [calls[0]["seq"]]
    # 工具结果后:恰一组 step/end + step/start(tool_round_open 聚合并行工具,单次翻转);
    # 末位 step/end = epilogue 收尾第二步(与 cc 同规)
    boundaries = [g["type"] for g in got if g["type"] in ("step/end", "step/start")]
    assert boundaries == ["step/start", "step/end", "step/start", "step/end"]


@pytest.mark.asyncio
async def test_codex_stream_web_search_tool(monkeypatch, tmp_path):
    """webSearch item(web_search 特性):completed 全字段 → tool/call|result + 单步边界。

    修复前该 item 被静默跳过(真机实测零工具事件、全程 step=1);started 是空壳
    占位(无 query)→ 不产事件;结果 content = results 原样 JSON(黑盒不透字段)。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    _fake_codex(monkeypatch, [
        _codex_nt("turn/started", turn=_codex_it(id="t1")),
        _codex_nt("item/started", item=_codex_it(type="agentMessage", id="m1")),
        _codex_nt("item/agentMessage/delta", delta="先查", item_id="m1"),
        _codex_nt("item/started",
                  item=_codex_it(type="webSearch", id="w1", query="", action=None, results=None)),
        _codex_nt("item/completed",
                  item=_codex_it(type="webSearch", id="w1", query="gh-puller README",
                                 action=_codex_it(type="search", query="gh-puller README",
                                                  queries=None),
                                 results=[{"url": "https://x/g", "title": "t", "snippet": "s"}])),
        _codex_nt("item/started", item=_codex_it(type="agentMessage", id="m2")),
        _codex_nt("item/agentMessage/delta", delta="答案", item_id="m2"),
        _codex_nt("item/completed",
                  item=_codex_it(type="agentMessage", id="m2", text="答案", phase=None)),
        _codex_nt("turn/completed", turn=_codex_it(id="t1", status="completed", error=None)),
    ], user_home=_codex_user_home(tmp_path))
    assert await _call(agent.Codex, _codex_options(codex_home=str(tmp_path)), "q",
                       session_name="x") == "先查答案"
    await asyncio.sleep(0.05)
    calls = [g for g in got if g["type"] == "tool/call"]
    assert len(calls) == 1
    assert calls[0]["data"]["name"] == "web_search"
    assert json.loads(calls[0]["data"]["arguments"]) == {
        "query": "gh-puller README", "action": {"type": "search", "query": "gh-puller README"}}
    res = next(g for g in got if g["type"] == "tool/result")
    assert res["data"]["is_error"] is False
    assert res["data"]["message"]["content"][0]["content"] == \
        json.dumps([{"url": "https://x/g", "title": "t", "snippet": "s"}], ensure_ascii=False)
    assert res["data"]["sourceSeqs"] == [calls[0]["seq"]]
    # webSearch 后:单次 step 边界(与其它工具同类);末位 step/end = epilogue
    assert [g["type"] for g in got if g["type"] in ("step/start", "step/end")] == \
        ["step/start", "step/end", "step/start", "step/end"]


@pytest.mark.asyncio
async def test_codex_stream_command_execution_failed_is_error(monkeypatch, tmp_path):
    """commandExecution:归 shell 工具;exit_code≠0 → tool/result is_error + 文本聚合。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    _fake_codex(monkeypatch, [
        _codex_nt("turn/started", turn=_codex_it(id="t1")),
        _codex_nt("item/completed",
                  item=_codex_it(type="commandExecution", id="sh1", command="false",
                                 cwd="/repo", exit_code=1, aggregated_output="boom! 1",
                                 status="failed")),
        _codex_nt("turn/completed", turn=_codex_it(id="t1", status="failed",
                                                   error=_codex_it(message="boom"))),
    ], user_home=_codex_user_home(tmp_path))
    with pytest.raises(agent.RequestFailedError, match="boom"):
        await _call(agent.Codex, _codex_options(codex_home=str(tmp_path)), "q", session_name="x")
    await asyncio.sleep(0.05)
    call = next(g for g in got if g["type"] == "tool/call")
    assert call["data"]["name"] == "shell"
    assert json.loads(call["data"]["arguments"]) == {"command": "false", "cwd": "/repo"}
    result = next(g for g in got if g["type"] == "tool/result")
    assert result["data"]["is_error"] is True
    assert result["data"]["message"]["content"][0]["content"] == "boom! 1"


@pytest.mark.asyncio
async def test_codex_stream_failed_turn_raises(monkeypatch, tmp_path):
    """turn status=failed → RuntimeError("agent 执行失败: ..."),error stage=run,abort 终局。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    _fake_codex(monkeypatch, [
        _codex_nt("turn/started", turn=_codex_it(id="t1")),
        _codex_nt("turn/completed", turn=_codex_it(id="t1", status="failed",
                                                   error=_codex_it(message="boom"))),
    ], user_home=_codex_user_home(tmp_path))
    with pytest.raises(agent.RequestFailedError, match="boom"):
        await _call(agent.Codex, _codex_options(codex_home=str(tmp_path)), "q", session_name="x")
    await asyncio.sleep(0.05)
    err = next(g for g in got if g["type"] == "error")
    assert err["data"]["stage"] == "run" and err["data"]["exc_type"] == "RequestFailedError"
    assert got[-1]["type"] == "session/end" and got[-1]["data"]["state"] == "aborted"
    assert got[-1]["data"]["reason"] == "boom"


@pytest.mark.asyncio
async def test_codex_stream_missing_completed_raises(monkeypatch, tmp_path):
    """流自然终止而未见 turn/completed → RuntimeError(传输中断兜底)。"""
    agent.configure(ws_urls=[], otel_urls=[])
    _fake_codex(monkeypatch, [
        _codex_nt("turn/started", turn=_codex_it(id="t1")),
    ], user_home=_codex_user_home(tmp_path))
    gen = agent.Codex(_codex_options(codex_home=str(tmp_path)))
    with pytest.raises(agent.RequestFailedError, match="turn 未收到完成事件"):
        async with gen.session(session_name="x"):
            await _fold(gen.stream("q"))


@pytest.mark.asyncio
async def test_codex_stream_sdk_error_stage_parse(monkeypatch, tmp_path):
    """SDK JSON-RPC 协议错误:原样重抛、error 事件 stage=parse。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    _fake_codex(monkeypatch, [], thread_raise=_CodexJsonRpcError("boom"),
                user_home=_codex_user_home(tmp_path))
    gen = agent.Codex(_codex_options(codex_home=str(tmp_path)))
    with pytest.raises(_CodexJsonRpcError, match="boom"):
        async with gen.session(session_name="x"):
            await _fold(gen.stream("q"))
    await asyncio.sleep(0.05)
    err = next(g for g in got if g["type"] == "error")
    assert err["data"]["stage"] == "parse" and err["data"]["exc_type"] == "_CodexJsonRpcError"
    assert got[-1]["type"] == "session/end" and got[-1]["data"]["state"] == "aborted"


@pytest.mark.asyncio
async def test_codex_stream_config_isolation_and_auth(monkeypatch, tmp_path):
    """config 装配:CODEX_HOME 进 env(cwd/codex_bin/overrides/launch 透传)、token →

    login_api_key、sandbox/approval 字符串 → SDK 枚举、系统提示 → base_instructions。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    _fake_codex(monkeypatch, [
        _codex_nt("turn/started", turn=_codex_it(id="t1")),
        _codex_nt("turn/completed", turn=_codex_it(id="t1", status="completed", error=None)),
    ], user_home=_codex_user_home(tmp_path, with_auth=True))
    options = {
        "model": "m1", "system_prompt": "sys", "codex_home": str(tmp_path / "codex-home"),
        "token": "sk-x", "sandbox": "full_access", "approval_mode": "auto_review",
        "cwd": "/repo", "codex_bin": "/bin/codex", "config_overrides": ("k=v",),
        "launch_args_override": ("l", "a"), "env": {"GRAPHIFY_OUT": "/g"}, "effort": "high",
        "base_instructions": "SYSTEM",
    }
    await _call(agent.Codex, options, "prompt", session_name="x")
    cfg = _FakeCodexConfig.instances[0]
    assert cfg.cwd == "/repo" and cfg.codex_bin == "/bin/codex"
    assert cfg.config_overrides == ("k=v",) and cfg.launch_args_override == ("l", "a")
    assert cfg.env == {"GRAPHIFY_OUT": "/g", "CODEX_HOME": str(tmp_path / "codex-home")}
    codex = _FakeCodex.instances[0]
    assert codex.logged_keys == ["sk-x"]  # token → login_api_key
    assert codex.thread_start_kwargs["sandbox"] == _FakeCodexSandbox.full_access
    assert codex.thread_start_kwargs["approval_mode"] == _FakeCodexApprovalMode.auto_review
    assert codex.thread_start_kwargs["base_instructions"] == "SYSTEM"  # 显式优先于 system_prompt
    assert codex.thread_start_kwargs["cwd"] == "/repo"  # thread cwd(工作区根)
    assert _FakeThread.calls[0] == ("prompt", {"cwd": "/repo", "effort": "high",
                                               "summary": "detailed"})  # turn 级透传 + 摘要缺省打开
    # 隔离 home:config.toml 仅 graphify;token → 先断 auth 符号链接再 login
    # (防 login_api_key 穿透写穿用户真实 ~/.codex/auth.json —— 凭证进本隔离 home)
    assert (tmp_path / "codex-home" / "config.toml").read_text() == ""  # 工具桌默认不注入
    assert not (tmp_path / "codex-home" / "auth.json").exists()


def test_codex_turn_summary_default_and_override():
    """codex_turn 装配:缺省打开可见推理摘要(detailed);显式 none/auto 尊重。"""
    from gh_puller.agent.generators.codex import codex_turn

    assert codex_turn({})["summary"] == "detailed"
    assert codex_turn({"summary": "none"})["summary"] == "none"
    assert codex_turn({"summary": "auto"})["summary"] == "auto"
    assert codex_turn({"summary": "concise"})["summary"] == "concise"


@pytest.mark.asyncio
async def test_codex_stream_consumer_close_closes_codex(monkeypatch, tmp_path):
    """消费者提前退场:async with 语义回收 AsyncCodex(__aexit__ 计数);终局 aborted。"""
    agent.configure(ws_urls=[], otel_urls=[])
    _fake_codex(monkeypatch, [
        _codex_nt("turn/started", turn=_codex_it(id="t1")),
        _codex_nt("item/started", item=_codex_it(type="agentMessage", id="m1")),
        _codex_nt("item/agentMessage/delta", delta="x", item_id="m1"),
        _codex_nt("item/completed",
                  item=_codex_it(type="agentMessage", id="m1", text="x", phase=None)),
    ], user_home=_codex_user_home(tmp_path))
    gen = agent.Codex(_codex_options(codex_home=str(tmp_path)))
    async with gen.session(session_name="x"):
        async for c in gen.stream("q"):
            first = c
            break  # 消费者提前退场:流关闭
    assert first == "x"
    assert _FakeCodex.closed == 1  # 会话退出 → AsyncCodex.__aexit__ 已执行(app-server 回收)


@pytest.mark.asyncio
async def test_codex_result_empty_final_response_raises(monkeypatch, tmp_path):
    """completed 但无产出文本 → codex_result RuntimeError(与 cc_result 同文案)。

    result 与 stream 同构消费通知流(_codex_drain):turn/completed 后无
    agentMessage 即无终局文本(st.final_response 空)。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    _fake_codex(monkeypatch, [
        _codex_nt("turn/started", turn=_codex_it(id="t1")),
        _codex_nt("turn/completed", turn=_codex_it(id="t1", status="completed", error=None)),
    ], user_home=_codex_user_home(tmp_path))
    gen = agent.Codex(_codex_options(codex_home=str(tmp_path)))
    with pytest.raises(agent.RequestFailedError, match="未产出最终结果"):
        async with gen.session(session_name="x"):
            await gen.result("q")


def test_codex_home_setup_config_toml_and_auth(tmp_path, monkeypatch):
    """隔离 home:config.toml 仅 graphify 单服务器(无用户配置面);auth 符号链接引用

    本地凭证(cc 的 CLI 自持凭证同形:重新登录即跟随,无副本陈旧)且幂等。
    """
    from pathlib import Path

    from gh_puller.agent.generators.codex import codex_home_setup as _codex_home_setup

    user_home = tmp_path / "home"
    (user_home / ".codex").mkdir(parents=True)
    (user_home / ".codex" / "auth.json").write_text('{"tok": "u"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(user_home)))
    home = tmp_path / "codex-home"
    _codex_home_setup(str(home), mcp_servers=[{"id": "graphify", "command": "python3",
                                                "args": ["-m", "graphify.serve"],
                                                "env_vars": ["GRAPHIFY_OUT"]}])
    cfg = (home / "config.toml").read_text(encoding="utf-8")
    assert cfg.startswith("[mcp_servers.graphify]")
    assert 'command = "python3"' in cfg
    assert 'args = ["-m", "graphify.serve"]' in cfg
    assert 'env_vars = ["GRAPHIFY_OUT"]' in cfg
    assert "required = true" in cfg
    assert cfg.count("[mcp_servers.") == 1  # 无用户配置面(不含第三方服务器/设置)
    auth = home / "auth.json"
    assert auth.is_symlink()  # 实时引用用户凭证,非复制
    assert auth.resolve() == (user_home / ".codex" / "auth.json")
    # 幂等:内容不变不重写(config.toml mtime 不变;auth 仍为同一条链接)
    before = (home / "config.toml").stat().st_mtime_ns
    _codex_home_setup(str(home), mcp_servers=[{"id": "graphify", "command": "python3",
                                                "args": ["-m", "graphify.serve"],
                                                "env_vars": ["GRAPHIFY_OUT"]}])
    assert (home / "config.toml").stat().st_mtime_ns == before
    assert auth.is_symlink() and auth.resolve() == (user_home / ".codex" / "auth.json")


# ---------------------------------------------------------------------------
# opencode 适配器(CLI run --format json):假可执行脚本喂 JSONL(零网络/token)
# ---------------------------------------------------------------------------


def _oc_part(kind: str, **fields) -> dict:
    """opencode 事件 part 构造(默认信封字段 + 追加字段)。"""
    return {"id": "prt_x", "sessionID": "ses_x", "messageID": "msg_x", "type": kind, **fields}


def _oc_line(kind: str, *, part=None, error=None) -> dict:
    """opencode JSONL 行构造(信封只取 type/part/error 三个被解析的键)。"""
    evt = {"type": kind}
    if part is not None:
        evt["part"] = part
    if error is not None:
        evt["error"] = error
    return evt


def _opencode_bin(tmp_path, events, *, exit_code=0, stderr=""):
    """假 opencode CLI 可执行脚本:stdout 逐行打印注入事件 JSON,stderr 写文本,exit_code 退出。

    events = 预构造的事件 dict 列表(真机捕获 schema 见 tests/test_agent_real.py 注记);
    config["opencode_bin"] 指向它 —— 单测驱动完整子进程管线(虚假命令零网络)。
    """
    payload = tmp_path / "fake-opencode-events.jsonl"
    payload.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
                       encoding="utf-8")
    path = tmp_path / "fake-opencode"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"with open({str(payload)!r}, encoding='utf-8') as fh:\n"
        "    lines = fh.read().splitlines()\n"
        "for line in lines:\n"
        "    print(line)\n"
        f"if {stderr!r}:\n"
        f"    sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def test_opencode_argv_and_config_content_assembly(tmp_path):
    """纯函数装配:argv(--pure/--auto/旗标拼装)、env(OPENCODE_CONFIG)、注入段(instructions+mcp)。"""
    from gh_puller.agent.generators.opencode import (
        _opencode_argv, _opencode_config_content, _opencode_env,
    )

    cfg = {"model": "deepseek/deepseek-chat", "agent": "build", "variant": "high",
           "auto": True, "session": "ses_1",
           "config_path": str(tmp_path / "opencode.json"),
           "system_prompt": "sys", "env": {"GRAPHIFY_OUT": "/g"},
           "mcp_servers": [{"id": "graphify", "command": "python3",
                            "args": ["-m", "graphify.serve"], "env_vars": ["GRAPHIFY_OUT"]}]}
    argv = _opencode_argv(cfg, "你好")
    assert argv == ["opencode", "--pure", "run", "--model", "deepseek/deepseek-chat",
                    "--agent", "build", "--variant", "high", "--session", "ses_1",
                    "--auto", "--format", "json", "你好"]
    assert "--thinking" not in argv  # 缺省关(不恒传;经 config.thinking 显式打开)
    assert "--thinking" in _opencode_argv({**cfg, "thinking": True}, "x")
    env = _opencode_env(cfg)
    assert env["OPENCODE_CONFIG"] == str(tmp_path / "opencode.json")
    assert env["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"] == "1"  # 缺省隔离本机 claude 技能面
    content = _opencode_config_content(cfg, "/tmp/i.md", env)
    assert content["instructions"] == ["/tmp/i.md"]
    assert content["mcp"] == {"graphify": {"type": "local",
                                           "command": ["python3", "-m", "graphify.serve"],
                                           "enabled": True,
                                           "environment": {"GRAPHIFY_OUT": "/g"}}}
    # 无注入面:system_prompt/mcp_servers 均缺省 → 零注入段
    assert _opencode_config_content({}, None, {}) == {}
    # auto 缺省 True;显式 False 不落 --auto
    assert "--auto" in _opencode_argv({}, "x")
    assert "--auto" not in _opencode_argv({"auto": False}, "x")


@pytest.mark.asyncio
async def test_opencode_stream_synthesizes_taxonomy(monkeypatch, tmp_path):
    """假 bin:JSONL → TAXONOMY 全序列(text 累积快照差分/工具合成/step 边界/tokens 归一/终局)。

    事件序列镜像真机捕获(见 tests/test_agent_real.py 注记):step_start → text(累积) →
    tool_use(completed)→ step_finish(tool-calls)→ step_start → text → step_finish(stop)。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    binpath = _opencode_bin(tmp_path, [
        _oc_line("step_start", part=_oc_part("step-start")),
        _oc_line("text", part=_oc_part("text", id="p1", text="你好")),
        _oc_line("text", part=_oc_part("text", id="p1", text="你好世界")),  # 同 part.id 累积快照 → 差分
        _oc_line("tool_use", part=_oc_part("tool",
                                           callID="c1", tool="mcp__graphify__query_graph",
                                           state={"status": "completed", "input": {"q": "x"},
                                                  "output": "结果", "title": "t",
                                                  "metadata": {"exit": 0}})),
        _oc_line("step_finish", part=_oc_part("step-finish", reason="tool-calls",
                                              tokens={"total": 10, "input": 11, "output": 3,
                                                      "reasoning": 1,
                                                      "cache": {"read": 2, "write": 0}},
                                              cost=0.01)),
        _oc_line("step_start", part=_oc_part("step-start")),
        _oc_line("text", part=_oc_part("text", id="p3", text="答案")),
        _oc_line("step_finish", part=_oc_part("step-finish", reason="stop",
                                              tokens={"total": 20, "input": 30, "output": 4,
                                                      "reasoning": 0,
                                                      "cache": {"read": 5, "write": 0}},
                                              cost=0.02)),
    ])
    gen = agent.OpenCode({"opencode_bin": binpath})
    async with gen.session(session_name="x", run_id="r1"):
        chunks = [c async for c in gen.stream("prompt")]
    assert "".join(chunks) == "你好世界答案"
    await asyncio.sleep(0.05)
    types_ = [g["type"] for g in got]
    assert types_[:5] == ["config/init", "session/start", "turn/start", "step/start", "user/message"]
    assert got[1]["data"]["run_id"] == "r1"  # session/start(索引 1)携带身份
    assert [g["seq"] for g in got] == list(range(len(got)))  # 合成流 seq 稠密
    steps = [g["data"]["step"] for g in got if g["type"] in ("step/start", "step/end")]
    assert steps == [1, 1, 2, 2]  # 首 step_start 与 prologue 重合;第二次 step_start 开新 step
    call = next(g for g in got if g["type"] == "tool/call")
    assert call["data"] == {"turn": 1, "step": 1, "callId": "c1",
                            "name": "mcp__graphify__query_graph", "arguments": '{"q": "x"}'}
    res = next(g for g in got if g["type"] == "tool/result")
    assert res["data"]["is_error"] is False
    assert res["data"]["name"] == "mcp__graphify__query_graph"
    assert res["data"]["sourceSeqs"] == [call["seq"]]
    assert res["data"]["message"]["content"][0]["content"] == "结果"
    msgs = [g for g in got if g["type"] == "assistant/message"]
    assert [m["data"]["message"]["content"] for m in msgs] == [
        [{"type": "content", "text": "你好世界"}],  # step1 文字(工具轮开头)→ 本步全量
        [{"type": "content", "text": "答案"}],
    ]
    # 文件面顺序:每步文字 surface 先于同 step 的工具(修复前 msg 锚 step_finish,
    # 整份 jsonl 表现为 text 落后于工具调用 —— 本轮在首个 tool_use 前发射)
    assert msgs[0]["seq"] < next(g for g in got if g["type"] == "tool/call")["seq"] < msgs[1]["seq"]
    assert msgs[0]["data"]["sourceSeqs"] == \
        [g["seq"] for g in got if g["type"] == "assistant/chunk"][:2]  # 本步文本 chunk seq
    end = got[-1]
    assert end["type"] == "session/end" and end["data"]["state"] == "completed"
    assert end["data"]["usage"] == {"input_tokens": 30, "output_tokens": 4,
                                    "cache_read_input_tokens": 5}  # 末条 step_finish 为准
    assert end["data"]["total_cost_usd"] == 0.02
    assert end["data"]["stop_reason"] == "stop"


@pytest.mark.asyncio
async def test_opencode_stream_text_after_tools_still_head_of_step(monkeypatch, tmp_path):
    """真机回合形态:text 事件晚于本回合已完成工具到达(CLI 工具回调穿插)——

    按到达序直发将得"工具结果在前、语言在后"(初始实现缺陷);回合缓冲后
    assistant/message 恒置顶于同 step 工具,工具按 CLI 到达序成对回补。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    binpath = _opencode_bin(tmp_path, [
        _oc_line("step_start", part=_oc_part("step-start")),
        _oc_line("tool_use", part=_oc_part("tool",
                                           callID="c1", tool="bash",
                                           state={"status": "completed",
                                                  "input": {"command": "ls"}, "output": "out1",
                                                  "title": "t", "metadata": {"exit": 0}})),
        _oc_line("text", part=_oc_part("text", id="p1", text="我先查一下")),  # 文本后到
        _oc_line("tool_use", part=_oc_part("tool",
                                           callID="c2", tool="grep",
                                           state={"status": "completed",
                                                  "input": {"command": "grep x"}, "output": "out2",
                                                  "title": "t", "metadata": {"exit": 0}})),
        _oc_line("step_finish", part=_oc_part("step-finish", reason="tool-calls", tokens={}, cost=0)),
        _oc_line("step_start", part=_oc_part("step-start")),
        _oc_line("text", part=_oc_part("text", id="p2", text="答案")),
        _oc_line("step_finish", part=_oc_part("step-finish", reason="stop", tokens={}, cost=0)),
    ])
    gen = agent.OpenCode({"opencode_bin": binpath})
    async with gen.session(session_name="x"):
        chunks = [c async for c in gen.stream("q")]
    assert "".join(chunks) == "我先查一下答案"  # 流式产出不受缓冲影响(增量实时)
    await asyncio.sleep(0.05)
    msg = next(g for g in got if g["type"] == "assistant/message")
    calls = [g for g in got if g["type"] == "tool/call"]
    results = [g for g in got if g["type"] == "tool/result"]
    # 回合语义序:文本置顶 → 工具成对(call+result)按 CLI 到达序
    assert msg["seq"] < calls[0]["seq"] < results[0]["seq"] < calls[1]["seq"] < results[1]["seq"]
    assert [c["data"]["name"] for c in calls] == ["bash", "grep"]
    assert msg["data"]["message"]["content"] == [{"type": "content", "text": "我先查一下"}]
    assert results[0]["data"]["sourceSeqs"] == [calls[0]["seq"]]  # 延迟回补不影响 sourceSeqs
    assert [g["data"]["step"] for g in got if g["type"] == "step/start"] == [1, 2]  # step 边界不变


@pytest.mark.asyncio
async def test_opencode_stream_tool_exit_failure_is_error(monkeypatch, tmp_path):
    """工具 metadata.exit 非 0 → tool/result is_error(不投文本增量)。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    binpath = _opencode_bin(tmp_path, [
        _oc_line("step_start", part=_oc_part("step-start")),
        _oc_line("tool_use", part=_oc_part("tool",
                                           callID="c1", tool="bash",
                                           state={"status": "completed",
                                                  "input": {"command": "false"},
                                                  "output": "", "title": "t",
                                                  "metadata": {"exit": 1}})),
        _oc_line("step_finish", part=_oc_part("step-finish", reason="stop", tokens={}, cost=0)),
    ])
    gen = agent.OpenCode({"opencode_bin": binpath})
    async with gen.session(session_name="x"):
        async for _ in gen.stream("q"):
            pass
    await asyncio.sleep(0.05)
    res = next(g for g in got if g["type"] == "tool/result")
    assert res["data"]["is_error"] is True


@pytest.mark.asyncio
async def test_opencode_stream_reasoning_to_thinking_chunk(monkeypatch, tmp_path):
    """reasoning 事件(config.thinking 打开后入流)→ 块式设计(think 批先于 content 批)。

    按 part.id 整段快照差分(与 text 同规则):
    chunk(thinking)× m → message(thinking)× 1(m 个 chunk 拼接,think 批完成即定型);
    chunk(content)× n → message(content)× 1(n 个 chunk 拼接);不产面向用户的文本。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    binpath = _opencode_bin(tmp_path, [
        _oc_line("step_start", part=_oc_part("step-start")),
        _oc_line("reasoning", part=_oc_part("reasoning", id="r1", text="先思考")),
        _oc_line("reasoning", part=_oc_part("reasoning", id="r1", text="先思考再作答")),  # 累积快照 → 差分
        _oc_line("text", part=_oc_part("text", id="p1", text="回答")),
        _oc_line("step_finish", part=_oc_part("step-finish", reason="stop", tokens={}, cost=0)),
    ])
    gen = agent.OpenCode({"opencode_bin": binpath, "thinking": True})  # config 字段显式打开
    async with gen.session(session_name="x"):
        chunks = [c async for c in gen.stream("q")]
    assert "".join(chunks) == "回答"  # 思考不构成产出
    await asyncio.sleep(0.05)
    kinds = [g["data"]["chunk"].get("type") for g in got if g["type"] == "assistant/chunk"]
    assert kinds == ["thinking", "thinking", "content"]
    think_texts = [g["data"]["chunk"]["text"] for g in got
                   if g["type"] == "assistant/chunk" and g["data"]["chunk"].get("type") == "thinking"]
    assert think_texts == ["先思考", "再作答"]  # 累积快照差分:第二条仅增量
    msgs = [g for g in got if g["type"] == "assistant/message"]
    # 块式设计:thinking 与 content 各成一条 assistant/message,批序 think 先、content 后;
    # message 文本 = 对应 chunk 拼接(全文),sourceSeqs = 对应 chunk 的 seqs
    assert len(msgs) == 2
    assert msgs[0]["data"]["message"]["content"] == [{"type": "thinking", "text": "先思考再作答"}]
    assert msgs[1]["data"]["message"]["content"] == [{"type": "content", "text": "回答"}]
    assert msgs[0]["seq"] < msgs[1]["seq"]
    think_chunk_seqs = [g["seq"] for g in got if g["type"] == "assistant/chunk"
                        and g["data"]["chunk"].get("type") == "thinking"]
    content_chunk_seqs = [g["seq"] for g in got if g["type"] == "assistant/chunk"
                          and g["data"]["chunk"].get("type") == "content"]
    assert msgs[0]["data"]["sourceSeqs"] == think_chunk_seqs
    assert msgs[1]["data"]["sourceSeqs"] == content_chunk_seqs


@pytest.mark.asyncio
async def test_opencode_stream_invalid_tool_intercepted_is_error(monkeypatch, tmp_path):
    """不可用工具被拦截:opencode 合成 tool="invalid" 的 completed 事件,input.error 含错误全文。

    实测(run7):模型先调裸名 list_projects → 被拦,status=completed、input={"tool",
    "error": "Model tried to call unavailable tool..."} —— is_error 必须 True(信息在
    input.error 面,不靠 output 文本匹配)。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    binpath = _opencode_bin(tmp_path, [
        _oc_line("step_start", part=_oc_part("step-start")),
        _oc_line("tool_use", part=_oc_part("tool",
                                           callID="c1", tool="invalid",
                                           state={"status": "completed",
                                                  "input": {"tool": "list_projects",
                                                            "error": "Model tried to call unavailable tool 'list_projects'"},
                                                  "output": "The arguments provided to the tool are invalid: Model tried to call unavailable tool 'list_projects'."})),
        _oc_line("step_finish", part=_oc_part("step-finish", reason="stop", tokens={}, cost=0)),
    ])
    gen = agent.OpenCode({"opencode_bin": binpath})
    async with gen.session(session_name="x"):
        async for _ in gen.stream("q"):
            pass
    await asyncio.sleep(0.05)
    calls = [g for g in got if g["type"] == "tool/call"]
    res = next(g for g in got if g["type"] == "tool/result")
    assert len(calls) == 1 and calls[0]["data"]["name"] == "invalid"
    assert res["data"]["is_error"] is True
    assert "unavailable tool" in res["data"]["message"]["content"][0]["content"]


@pytest.mark.asyncio
async def test_opencode_stream_tool_error_status_completes(monkeypatch, tmp_path):
    """工具 error 态(status=error,state.error 文本,无 output)→ tool/call+tool/result 合成。

    实测(gh-puller-mcp 工具失败)会发 status="error" 的 tool_use —— 与 completed 同权,
    不得跳过丢弃(否则监控缺该调用且 is_error 信息丢失)。
    """
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    binpath = _opencode_bin(tmp_path, [
        _oc_line("step_start", part=_oc_part("step-start")),
        _oc_line("tool_use", part=_oc_part("tool",
                                           callID="c1", tool="gh_puller_search_code",
                                           state={"status": "error",
                                                  "input": {"pattern": "x"},
                                                  "error": "path or file_pattern contains invalid characters"})),
        _oc_line("step_finish", part=_oc_part("step-finish", reason="stop", tokens={}, cost=0)),
    ])
    gen = agent.OpenCode({"opencode_bin": binpath})
    async with gen.session(session_name="x"):
        async for _ in gen.stream("q"):
            pass
    await asyncio.sleep(0.05)
    calls = [g for g in got if g["type"] == "tool/call"]
    res = next(g for g in got if g["type"] == "tool/result")
    assert len(calls) == 1 and calls[0]["data"]["name"] == "gh_puller_search_code"
    assert res["data"]["is_error"] is True
    assert "invalid characters" in res["data"]["message"]["content"][0]["content"]


@pytest.mark.asyncio
async def test_opencode_stream_error_event_raises(monkeypatch, tmp_path):
    """error 事件(APIError 家族)→ RequestFailedError(data.message);stage=run。"""
    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    binpath = _opencode_bin(tmp_path, [
        _oc_line("error", error={"name": "APIError", "data": {"message": "boom"}}),
    ])
    gen = agent.OpenCode({"opencode_bin": binpath})
    with pytest.raises(agent.RequestFailedError, match="boom"):
        async with gen.session(session_name="x"):
            async for _ in gen.stream("q"):
                pass
    await asyncio.sleep(0.05)
    err = next(g for g in got if g["type"] == "error")
    assert err["data"]["stage"] == "run"


@pytest.mark.asyncio
async def test_opencode_stream_nonzero_exit_raises(monkeypatch, tmp_path):
    """进程退出码非 0 → RequestFailedError(含 stderr 尾部诊断)。"""
    agent.configure(ws_urls=[], otel_urls=[])
    binpath = _opencode_bin(tmp_path, [
        _oc_line("text", part=_oc_part("text", text="hi")),
    ], exit_code=1, stderr="bad creds\n")
    gen = agent.OpenCode({"opencode_bin": binpath})
    with pytest.raises(agent.RequestFailedError, match="退出码 1"):
        async with gen.session(session_name="x"):
            async for _ in gen.stream("q"):
                pass


@pytest.mark.asyncio
async def test_opencode_stream_missing_stop_raises(monkeypatch, tmp_path):
    """无 step_finish(stop)且退出 0 → 未收到完成事件(不静默返回)。"""
    agent.configure(ws_urls=[], otel_urls=[])
    binpath = _opencode_bin(tmp_path, [
        _oc_line("step_start", part=_oc_part("step-start")),
        _oc_line("text", part=_oc_part("text", text="hi")),
    ])
    gen = agent.OpenCode({"opencode_bin": binpath})
    with pytest.raises(agent.RequestFailedError, match="turn 未收到完成事件"):
        async with gen.session(session_name="x"):
            async for _ in gen.stream("q"):
                pass


@pytest.mark.asyncio
async def test_opencode_result_semantics(monkeypatch, tmp_path):
    """result 与 stream 同构:末条 text 整段;无产出 → 未产出最终结果。"""
    agent.configure(ws_urls=[], otel_urls=[])
    binpath = _opencode_bin(tmp_path, [
        _oc_line("step_start", part=_oc_part("step-start")),
        _oc_line("text", part=_oc_part("text", id="p1", text="你好")),
        _oc_line("text", part=_oc_part("text", id="p1", text="你好世界")),
        _oc_line("step_finish", part=_oc_part("step-finish", reason="stop", tokens={}, cost=0)),
    ])
    gen = agent.OpenCode({"opencode_bin": binpath})
    async with gen.session(session_name="x"):
        assert await gen.result("q") == "你好世界"
    empty = _opencode_bin(tmp_path, [
        _oc_line("step_start", part=_oc_part("step-start")),
        _oc_line("step_finish", part=_oc_part("step-finish", reason="stop", tokens={}, cost=0)),
    ])
    gen = agent.OpenCode({"opencode_bin": empty})
    with pytest.raises(agent.RequestFailedError, match="未产出最终结果"):
        async with gen.session(session_name="x"):
            await gen.result("q")


# ---------------------------------------------------------------------------
# llm 适配器(OpenAI 兼容 httpx):假客户端注入(零网络/token)
# ---------------------------------------------------------------------------


class _FakeLLMResp:
    """形似 httpx 响应:桶装 json。"""

    def __init__(self, body: dict):
        self._json = body

    def raise_for_status(self):
        pass

    def json(self) -> dict:
        return self._json


class _FakeLLMStreamRes:
    """形似流式响应:aiter_lines 逐行吐 "data: ..." 后 [DONE]。"""

    def __init__(self, chunks: list[dict]):
        self.chunks = chunks

    def raise_for_status(self):
        pass

    def aiter_lines(self):
        async def gen():
            for c in self.chunks:
                yield "data: " + json.dumps(c)
            yield "data: [DONE]"

        return gen()


class _FakeLLMClient:
    """形似 httpx.AsyncClient:记录调用;post 桶装 json,stream 桶装 SSE。

    带 with 语义(生成器 __aenter__/__aexit__ 直接转发到 client,与真 httpx 同形)。
    """

    post_body: ClassVar[dict] = {}
    stream_chunks: ClassVar[list[dict]] = []
    calls: ClassVar[list[tuple]] = []
    enters: ClassVar[int] = 0
    exits: ClassVar[int] = 0

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        type(self).enters += 1
        return self

    async def __aexit__(self, *exc):
        type(self).exits += 1
        return False

    async def post(self, url, **kwargs):
        type(self).calls.append(("post", url))
        return _FakeLLMResp(type(self).post_body)

    def stream(self, method, url, **kwargs):
        type(self).calls.append(("stream", url))
        chunks = type(self).stream_chunks

        class _CM:
            async def __aenter__(self):
                return _FakeLLMStreamRes(chunks)

            async def __aexit__(self, *exc):
                return False

        return _CM()


def _llm_options():
    return {"model": "m1", "base_url": "http://fake", "api_key": "sk-fake", "provider": "p"}


@pytest.mark.asyncio
async def test_llm_result_drains_stream_events(monkeypatch):
    """llm result 经流式端点抽取:事件与 stream 同构(逐 delta chunk,非整段 1 条);

    末块 usage/finish_reason 落入 session/end。
    """
    from gh_puller.agent.generators import openai as gen_mod

    agent.configure(ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    _FakeLLMClient.post_body = {"choices": [{"message": {"role": "assistant",
                                                         "content": "整段"}}]}
    _FakeLLMClient.stream_chunks = [
        {"choices": [{"delta": {"content": "好"}}]},
        {"choices": [{"delta": {"content": "了"}, "finish_reason": "stop"}],
         "usage": {"input_tokens": 2, "output_tokens": 1, "cache_read_input_tokens": 0}},
    ]
    _FakeLLMClient.calls = []
    monkeypatch.setattr(gen_mod.httpx, "AsyncClient", _FakeLLMClient)
    payload = {"messages": [{"role": "user", "content": "问题"}], "max_tokens": 16}
    out = await _call(agent.OpenAI, _llm_options(), payload, method="result", session_name="x")
    assert out == "好了"
    await asyncio.sleep(0.05)
    # result 走流式端点,不再单发 POST
    assert ("stream", "http://fake/chat/completions") in [c[:2] for c in _FakeLLMClient.calls]
    assert ("post", "http://fake/chat/completions") not in [c[:2] for c in _FakeLLMClient.calls]
    chunks = [g for g in got if g["type"] == "assistant/chunk"]
    assert [c["data"]["chunk"]["text"] for c in chunks] == ["好", "了"]  # 逐 delta
    end = got[-1]
    assert end["type"] == "session/end" and end["data"]["state"] == "completed"
    assert end["data"]["usage"] == {"input_tokens": 2, "output_tokens": 1,
                                    "cache_read_input_tokens": 0}
    assert end["data"]["stop_reason"] == "stop"


# ---------------------------------------------------------------------------
# 会话保鲜(keep-warm):_guard 内启停,每 interval 触一次文件 mtime(只动时间戳、
# 不落行、无 session/heartbeat 事件);session/end 恒为末行;run 收尾即取消(无泄漏)
# ---------------------------------------------------------------------------


async def _slow_stream(self):
    """每批通知后停 50ms(制造 > interval 的静默缺口)。"""
    for n in type(self).notifs:
        yield n
        await asyncio.sleep(0.05)


async def _busy_stream(self):
    """通知以 8ms 节奏流动(< interval 10ms:活动期;总时长 > interval,保鲜仍触)。"""
    for n in type(self).notifs:
        yield n
        await asyncio.sleep(0.008)


@pytest.mark.asyncio
async def test_keepwarm_touches_during_quiet_gap(monkeypatch, tmp_path):
    """静默保鲜:interval 0.01s + 事件间 50ms 静默 → keep-warm 每 interval 触一次

    sinks.touch(只动 mtime、不发 session/heartbeat 事件);session/end 恒为末条;
    run 结束后保鲜任务已取消,不再触(无泄漏)。
    """
    monkeypatch.setattr(envs, "AGENT_MONITOR_HEARTBEAT_SECS", 0.01)
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    touched: list[str] = []

    async def _fake_touch(session: str) -> None:
        touched.append(session)

    monkeypatch.setattr(sinks, "touch", _fake_touch)
    monkeypatch.setattr(_FakeTurnHandle, "stream", _slow_stream)
    _fake_codex(monkeypatch, [
        _codex_nt("turn/started", turn=_codex_it(id="t1")),
        _codex_nt("item/started", item=_codex_it(type="agentMessage", id="m1")),
        _codex_nt("item/agentMessage/delta", delta="hi", item_id="m1"),
        _codex_nt("item/completed",
                  item=_codex_it(type="agentMessage", id="m1", text="hi", phase=None)),
        _codex_nt("turn/completed", turn=_codex_it(id="t1", status="completed", error=None)),
    ], user_home=_codex_user_home(tmp_path))
    await _call(agent.Codex, _codex_options(codex_home=str(tmp_path)), "prompt",
                session_name="x")
    await asyncio.sleep(0.05)  # 等 bus 末拍(终局事件)到达 got
    assert touched  # 静默缺口 ≥1 拍:保鲜触发过(事件模型不再有心跳行)
    assert "session/heartbeat" not in [g["type"] for g in got]
    assert [g["type"] for g in got][-2:] == ["turn/end", "session/end"]  # cancel 先于 finish
    n = len(touched)
    await asyncio.sleep(0.1)  # 数拍后:保鲜任务已取消,不再触
    assert len(touched) == n


@pytest.mark.asyncio
async def test_keepwarm_unconditional_during_busy(monkeypatch, tmp_path):
    """保鲜无静默判断:活动期(1ms 节奏)也逐拍触发 —— 事件密时 mtime 本就新鲜,

    多触一次无副作用;任何时刻都不再出现 session/heartbeat(防 spam 结构消除)。
    """
    monkeypatch.setattr(envs, "AGENT_MONITOR_HEARTBEAT_SECS", 0.01)
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    touched: list[str] = []

    async def _fake_touch(session: str) -> None:
        touched.append(session)

    monkeypatch.setattr(sinks, "touch", _fake_touch)
    monkeypatch.setattr(_FakeTurnHandle, "stream", _busy_stream)
    _fake_codex(monkeypatch, [
        _codex_nt("turn/started", turn=_codex_it(id="t1")),
        _codex_nt("item/started", item=_codex_it(type="agentMessage", id="m1")),
        _codex_nt("item/agentMessage/delta", delta="hi", item_id="m1"),
        _codex_nt("item/completed",
                  item=_codex_it(type="agentMessage", id="m1", text="hi", phase=None)),
        _codex_nt("turn/completed", turn=_codex_it(id="t1", status="completed", error=None)),
    ], user_home=_codex_user_home(tmp_path))
    await _call(agent.Codex, _codex_options(codex_home=str(tmp_path)), "prompt",
                session_name="x")
    await asyncio.sleep(0.05)
    assert touched  # 活动期同按 cadence 保鲜(无条件)
    assert "session/heartbeat" not in [g["type"] for g in got]
    assert got[-1]["type"] == "session/end"


@pytest.mark.asyncio
async def test_keepwarm_stops_on_aborted_run(monkeypatch, tmp_path):
    """异常路径(thread_start 抛):error + finish(False);保鲜任务一并取消,末行 aborted,零泄漏。"""
    monkeypatch.setattr(envs, "AGENT_MONITOR_HEARTBEAT_SECS", 0.01)
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    got = []
    sinks.ensure_bus().add(_recv(got))
    touched: list[str] = []

    async def _fake_touch(session: str) -> None:
        touched.append(session)

    monkeypatch.setattr(sinks, "touch", _fake_touch)
    _fake_codex(monkeypatch, [], thread_raise=RuntimeError("boom"),
                user_home=_codex_user_home(tmp_path))
    gen = agent.Codex(_codex_options(codex_home=str(tmp_path)))
    with pytest.raises(RuntimeError, match="boom"):
        async with gen.session(session_name="x"):
            await _fold(gen.stream("q"))
    await asyncio.sleep(0.05)
    assert got[-1]["type"] == "session/end" and got[-1]["data"]["state"] == "aborted"
    n = len(touched)
    await asyncio.sleep(0.1)
    assert len(touched) == n  # 保鲜任务已取消(无泄漏)


@pytest.mark.asyncio
async def test_file_sink_touch_updates_mtime_no_row(tmp_path):
    """FileSink.touch:只 os.utime 更新 mtime、不写行;未知 session no-op,失败静默。"""
    sink = FileSink(str(tmp_path))
    await sink.consume(_evt("session/start", session="s4", seq=0, run_id="r4",
                            label="wiki:structure", provider="anthropic", model=""))
    path = tmp_path /"s4.jsonl"
    before = path.stat().st_mtime_ns
    await asyncio.sleep(0.01)  # 时钟推进,确保 utime 目标时间戳不同
    await sink.touch("s4")
    assert path.stat().st_mtime_ns > before  # mtime 前进
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [ln["type"] for ln in lines] == ["session/start"]  # 未写入任何新行
    await sink.touch("unknown")  # 未知会话:no-op 不炸
    await sink.touch("s4")  # 再触幂等
    assert [json.loads(line)["type"] for line in path.read_text().splitlines()] == ["session/start"]
