"""监控观测通道:sink 基础设施(事件总线/文件/WS/OTel)与运行时配置。

用户定义两种观测通道,无控制台通道:
- 文件 sink(默认开):AGENT_MONITOR_DIR/sessions/{running,completed,aborted}/<session>.jsonl,
  每行是 LLM 流 —— 事件流的增量聚合产物(thinking/content 各并 1 块/轮、工具块 0..n),
  聚合器见 gh_puller.agent.events.LlmAggregator,纯 dict 一处实现,FS/hub 共用;
- Web/WS sink(AGENT_MONITOR_WS_URL 非空启用):事件流推送给独立 hub
  (apps/agent-dashboard/server/,WS 端点 /ws),浏览器实时查看;
- OTel sink(AGENT_MONITOR_OTEL_ENDPOINT 非空启用):事件流 → span 树 → OTLP HTTP
  (本地 Phoenix 等后端;实现见本文件 OtelSink)。

管道:适配器(adapters)归一化 SDK/HTTP 对象 → 事件流 dict → EventBus 扇出(publish 仅
put_nowait 到每 sink 的 asyncio.Queue,永不阻塞调用)→ sink worker 消费(文件写盘)。
线程模型:v1 只有异步调用方,publish 为 loop-affine;若未来出现线程调用方,
须自行经 loop.call_soon_threadsafe 转发。
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from .. import envs
from .events import LlmAggregator, truncate

_STATE_DIRS = ("running", "completed", "aborted")


def _log(msg: str) -> None:
    print(f"[agent-monitor] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# EventBus(进程内单例;publish 非阻塞)
# ---------------------------------------------------------------------------


class EventBus:
    """进程内异步事件总线:publish 只 put_nowait 到每 sink 队列,慢 sink 只拖 sink,不拖调用。"""

    def __init__(self):
        self._sinks: list[asyncio.Queue[dict]] = []
        self._tasks: list[asyncio.Task] = []

    @property
    def enabled(self) -> bool:
        return bool(self._sinks)

    def add(self, consume) -> None:
        """注册 sink 消费协程:async def consume(evt: dict) -> None。"""
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=5000)
        self._sinks.append(q)
        self._tasks.append(asyncio.create_task(self._drain(consume, q)))

    async def _drain(self, consume, q) -> None:
        while True:
            evt = await q.get()
            try:
                await consume(evt)
            except Exception as exc:  # sink 失败只报 stderr,绝不冒泡到调用方
                _log(f"sink 消费失败: {type(exc).__name__}: {exc}")

    def publish(self, evt: dict) -> None:
        if not self._sinks:
            return
        for q in self._sinks:
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                q.get_nowait()  # 有界队列:丢最旧,新事件优先
                q.put_nowait(evt)

    def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()


class FileSink:
    """文件观测通道:每会话一个 JSONL(LLM 流行),终态经 os.replace 迁入对应状态目录。

    目录结构即索引(sessions/{running,aborted,completed}/),行即自描述 LLM 流,
    Linux 查询友好:tail -f running/*.jsonl 实时看,jq 过滤块文本还原全文。
    崩溃残留停留在 running/(即排查素材);hub 启动时按文件所在目录种子状态。
    """

    def __init__(self, root: str):
        self.root = Path(root)
        self._agg: dict[str, LlmAggregator] = {}
        self._files: dict[str, Path] = {}
        for st in _STATE_DIRS:
            (self.root / "sessions" / st).mkdir(parents=True, exist_ok=True)

    async def consume(self, evt: dict) -> None:
        session = evt.get("session", "")
        if evt["kind"] == "run.start":
            self._open(session, evt)
        agg = self._agg.get(session)
        if agg is None:
            return  # 无 run.start 起点:事件不可聚合,丢弃
        lines = agg.feed(evt)
        if not lines:
            return
        with open(self._files[session], "a", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
            f.flush()
        if lines[-1]["type"] == "session.end":
            self._close(session, lines[-1]["state"])

    def _open(self, session: str, evt: dict) -> None:
        agg = LlmAggregator(session, evt.get("label", ""), evt.get("provider", ""), evt.get("model", ""))
        self._agg[session] = agg
        self._files[session] = self.root / "sessions" / "running" / f"{session}.jsonl"

    def _close(self, session: str, state: str) -> None:
        src = self._files.pop(session)
        self._agg.pop(session)
        os.replace(src, self.root / "sessions" / state / src.name)


class WsSink:
    """WS 观测通道(生产端):后台任务连 hub,逐事件推送;断连静默,1→2→…→30s 指数退避。

    内部有界队列(5000, drop-oldest)与 bus 队列各自兜底:断连期间积压先丢旧,
    推送失败绝不冒泡(监控不拖累调用)。事件在 connect 后才投递:重连后从断点继续。

    契约:仅转发原始事件帧(测试见 tests/test_agent.py);
    聚合(LlmAggregator)只发生在消费端(FileSink / hub),本通道从不发送聚合/llm 行。
    """

    def __init__(self, url: str):
        self.url = url
        self._q: asyncio.Queue[dict] = asyncio.Queue(maxsize=5000)
        self._task = asyncio.create_task(self._run())

    async def consume(self, evt: dict) -> None:
        try:
            self._q.put_nowait(evt)
        except asyncio.QueueFull:
            self._q.get_nowait()  # 先丢旧:最近的进度优先
            self._q.put_nowait(evt)

    async def _run(self) -> None:
        import websockets

        wait = 1
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=20) as ws:
                    wait = 1
                    while True:
                        if not self._q.empty():
                            evt = self._q.get_nowait()
                        else:
                            evt = await self._q.get()
                        await ws.send(json.dumps({"type": "evt", "event": evt}, ensure_ascii=False))
            except Exception as exc:  # 握手/断连:保留缓冲,退避重连
                _log(f"ws sink 未连接({wait}s 后重试): {exc}")
                await asyncio.sleep(wait)
                wait = min(wait * 2, 30)


def _ns(ts) -> int | None:
    """事件 ts(float 秒,events.new_event)→ OTel 纳秒时间戳;缺失 → None。"""
    return int(ts * 1e9) if ts else None


def _attrs(span, mapping: dict) -> None:
    """批量 set_attribute:None 跳过,list/dict 序列化为 JSON 字符串。"""
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        span.set_attribute(key, value)


def _otel():
    """惰性导入 opentelemetry 各符号;缺失 → ImportError(可选依赖降级,由 _ensure_bus 兜底)。"""
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    return trace, OTLPSpanExporter, Resource, TracerProvider, SimpleSpanProcessor


class OtelSink:
    """事件流消费端:逐会话构建 OTel span 树并经 OTLP 导出(契约同 FileSink)。

    只消费 gh_puller.agent.events 的事件 dict(无 run.start 起点则忽略),零 SDK 依赖;
    对外的信息粒度与 FileSink 的 LLM 流一致但落在标准 OTel 上:
    - 根 span 一次 agent 运行(run.start → run.end),按会话维护 span 树;
    - 块级粒度:每个 content/thinking/tool_use 块一个子 span,block.stop 即 end——
      SimpleSpanProcessor 同步导出(块级实时);OTLP 协议无 token 级增量语义,
      text.delta/thinking.delta 只累加进当前块的属性(不逐字传输);
    - 工具结果/usage/错误/成本挂在细粒度 span 或根 span 属性上,gen_ai.* 兼容
      Phoenix 等后端的自动识别。

    opentelemetry 为可选依赖(惰性导入于 __init__:缺失 → ImportError,由 _ensure_bus
    降级)。tracer 注入为测试用(InMemorySpanExporter);缺省懒建
    TracerProvider(SimpleSpanProcessor + OTLPSpanExporter),endpoint 须为
    完整 OTLP traces URL(如 http://localhost:6006/v1/traces)。

    健壮性:span 全部显式传递父子(set_span_in_context),不使用 start_as_current_span
    (consume 在 EventBus worker 协程中跨 await,活动 span 会错巢);失败只记 stderr
    且每会话仅一次,绝不冒泡(监控不拖累调用)。
    """

    def __init__(self, endpoint: str, *, tracer=None):
        self._trace, otel_exporter, Resource, TracerProvider, SimpleSpanProcessor = _otel()
        self.tracer = tracer
        if self.tracer is None:
            tp = TracerProvider(
                resource=Resource({"service.name": os.environ.get("OTEL_SERVICE_NAME", "gh-puller")})
            )
            tp.add_span_processor(SimpleSpanProcessor(otel_exporter(endpoint=endpoint)))
            self.tracer = tp.get_tracer("gh-puller.agent-monitor")
        self._spans: dict[str, dict] = {}  # session → {root, stack, error, logged…}
        self._failed: set[str] = set()  # 已报过错的 session(每会话只报一次)

    # ---- 事件分发 ----

    async def consume(self, evt: dict) -> None:
        session = evt.get("session", "")
        kind = evt.get("kind")
        try:
            state = self._spans.get(session) if session else None
            if kind == "run.start":
                self._on_run_start(session, evt)
            elif state is None:
                return  # 无 run.start 起点:事件不可分组,丢弃(同 FileSink)
            elif kind == "block.start":
                self._on_block_start(state, evt)
            elif kind in ("text.delta", "thinking.delta"):
                self._on_delta(state, evt, kind)
            elif kind == "block.stop":
                self._on_block_stop(state, evt)
            elif kind == "tool.result":
                self._on_tool_result(state, evt)
            elif kind in ("message.assistant", "result"):
                self._on_result_like(state, evt)
            elif kind == "error":
                self._on_error(state, evt)
            elif kind == "run.end":
                self._on_run_end(session, evt)
        except Exception as exc:  # 隔离:监控失败绝不冒泡(每次会话只报一次)
            if session not in self._failed:
                self._failed.add(session)
                _log(f"otel sink 消费失败({session}): {type(exc).__name__}: {exc}")

    # ---- span 树构建 ----

    def _child(self, state: dict, name: str, ts=None) -> object:
        ctx = self._trace.set_span_in_context(state["root"])
        return self.tracer.start_span(name, context=ctx, start_time=_ns(ts))

    def _on_run_start(self, session: str, evt: dict) -> None:
        stale = self._spans.pop(session, None)  # 同会话重入:旧根强制收尾
        if stale is not None:
            stale["root"].end(end_time=_ns(evt.get("ts")))
        label = evt.get("label") or session
        root = self.tracer.start_span(
            f"{label} · {evt.get('provider', '')}".strip(" ·"),
            start_time=_ns(evt.get("ts")),
        )
        self._spans[session] = {
            "root": root,
            "stack": [],
            "error": None,
            "tool_names": evt.get("tool_names"),
        }
        _attrs(root, {
            "gen_ai.provider.name": evt.get("provider"),
            "gen_ai.request.model": evt.get("model"),
            "gh_puller.session": session,
            "gh_puller.label": label,
            "gh_puller.prompt_preview": evt.get("prompt_preview"),
            "gh_puller.prompt_chars": evt.get("prompt_chars"),
            "gh_puller.n_messages": evt.get("n_messages"),
            "gh_puller.system_chars": evt.get("system_chars"),
            "gh_puller.tool_names": evt.get("tool_names"),
            "gh_puller.meta": evt.get("meta"),
        })

    def _open_block(self, state: dict, kind: str, evt: dict, *, tool_id=None, tool_name=None) -> dict:
        span = self._child(state, f"block.{kind}", evt.get("ts"))
        _attrs(span, {"gh_puller.block_type": kind, "gh_puller.round": evt.get("round"),
                      "gh_puller.tool_id": tool_id, "gh_puller.tool_name": tool_name})
        entry = {"kind": kind, "span": span, "buf": "", "tool_id": tool_id, "tool_name": tool_name}
        state["stack"].append(entry)
        return entry

    def _on_block_start(self, state: dict, evt: dict) -> None:
        kind = evt.get("block_type")
        if kind == "tool_use":
            self._open_block(state, "tool_use", evt, tool_id=evt.get("tool_id"),
                             tool_name=evt.get("tool_name"))
        elif kind in ("content", "thinking"):
            self._open_block(state, kind, evt)

    def _on_delta(self, state: dict, evt: dict, kind: str) -> None:
        """delta 累加进当前块:类型不匹配(兜底路径无 block.start)则自动开块。"""
        btype = "content" if kind == "text.delta" else "thinking"
        top = state["stack"][-1] if state["stack"] else None
        if top is None or top["kind"] != btype:
            top = self._open_block(state, btype, evt)
        top["buf"] += evt.get("text") or evt.get("thinking", "") or ""

    def _pop_block(self, state: dict, *, tool_id=None) -> dict | None:
        """按 tool_id(工具块可交错)或最近非工具块弹出;无匹配 → None。"""
        stack = state["stack"]
        if tool_id is not None:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i]["tool_id"] == tool_id:
                    return stack.pop(i)
        for i in range(len(stack) - 1, -1, -1):
            if stack[i]["kind"] in ("content", "thinking"):
                return stack.pop(i)
        return None

    def _on_block_stop(self, state: dict, evt: dict) -> None:
        entry = self._pop_block(state, tool_id=evt.get("tool_id"))
        if entry is None:
            return
        span = entry["span"]
        if entry["kind"] == "tool_use":
            _attrs(span, {"gh_puller.tool_id": evt.get("tool_id"), "gh_puller.tool_name": evt.get("tool_name"),
                          "gh_puller.tool_input": evt.get("tool_input")})
        else:
            preview_len, preview = truncate(entry["buf"], 300)
            prefix = "gh_puller.thinking" if entry["kind"] == "thinking" else "gh_puller.text"
            _attrs(span, {f"{prefix}_chars": preview_len, f"{prefix}_preview": preview})
        span.end(end_time=_ns(evt.get("ts")))

    def _on_tool_result(self, state: dict, evt: dict) -> None:
        span = self._child(state, f"tool.result:{evt.get('tool_name') or evt.get('tool_id') or ''}",
                           evt.get("ts"))
        _attrs(span, {
            "gh_puller.tool_id": evt.get("tool_id"),
            "gh_puller.tool_name": evt.get("tool_name"),
            "gh_puller.is_error": evt.get("is_error"),
            "gh_puller.content_chars": evt.get("content_chars"),
            "gh_puller.content_preview": evt.get("content_preview"),
            "gh_puller.round": evt.get("round"),
        })
        if evt.get("is_error"):
            span.set_status(self._trace.Status(self._trace.StatusCode.ERROR))
        span.end(end_time=_ns(evt.get("ts")))

    def _set_usage(self, root, evt: dict) -> None:
        usage = evt.get("usage") or {}
        _attrs(root, {
            "gen_ai.usage.input_tokens": usage.get("input_tokens"),
            "gen_ai.usage.output_tokens": usage.get("output_tokens"),
            "gh_puller.cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "gh_puller.total_cost_usd": evt.get("total_cost_usd"),
        })

    def _on_result_like(self, state: dict, evt: dict) -> None:
        self._set_usage(state["root"], evt)
        if evt.get("stop_reason"):
            state["root"].set_attribute("gh_puller.stop_reason", evt["stop_reason"])
        if evt.get("duration_ms") is not None:
            state["root"].set_attribute("gh_puller.duration_ms", evt["duration_ms"])

    def _on_error(self, state: dict, evt: dict) -> None:
        detail = f"{evt.get('exc_type', '')}: {evt.get('message', '')}"
        state["error"] = detail
        _attrs(state["root"], {"gh_puller.error": detail, "gh_puller.error_stage": evt.get("stage")})
        state["root"].set_status(self._trace.Status(self._trace.StatusCode.ERROR, detail[:300]))

    def _on_run_end(self, session: str, evt: dict) -> None:
        state = self._spans.pop(session, None)
        if state is None:
            return
        root = state["root"]
        _attrs(root, {
            "gh_puller.duration_ms": evt.get("duration_ms"),
            "gh_puller.text_chars": evt.get("text_chars"),
            "gh_puller.num_rounds": evt.get("num_rounds"),
        })
        if evt.get("ok"):
            root.set_status(self._trace.Status(self._trace.StatusCode.OK))
        else:
            reason = state["error"] or "agent 执行未成功完成"
            root.set_status(self._trace.Status(self._trace.StatusCode.ERROR, reason[:300]))
        root.end(end_time=_ns(evt.get("ts")))


# ---------------------------------------------------------------------------
# 运行时配置(缺省取 envs 导入常量;运行时改走 configure)
# ---------------------------------------------------------------------------

_cfg = {
    "file": envs.AGENT_MONITOR_FILE,
    "file_dir": envs.AGENT_MONITOR_DIR,
    "ws_url": envs.AGENT_MONITOR_WS_URL,
    "otel_endpoint": envs.AGENT_MONITOR_OTEL_ENDPOINT,
}
_bus: EventBus | None = None


def configure(*, file=None, file_dir=None, ws_url=None, otel_endpoint=None) -> None:
    """重配监控(测试/嵌入用);缺省取 envs 常量,生效于下一次事件发布。

    关闭旧 bus(取消 sink 任务),新配置惰性重建 —— 幂等,可反复调用。
    """
    global _bus
    _cfg["file"] = envs.AGENT_MONITOR_FILE if file is None else bool(file)
    _cfg["file_dir"] = envs.AGENT_MONITOR_DIR if file_dir is None else file_dir
    _cfg["ws_url"] = envs.AGENT_MONITOR_WS_URL if ws_url is None else ws_url
    _cfg["otel_endpoint"] = envs.AGENT_MONITOR_OTEL_ENDPOINT if otel_endpoint is None else otel_endpoint
    if _bus is not None:
        _bus.shutdown()
        _bus = None


def _ensure_bus() -> EventBus:
    """惰性构建单例(带已启用 sink);须在运行中的事件循环内调用(create_task)。"""
    global _bus
    if _bus is None:
        b = EventBus()
        if _cfg["file"]:
            b.add(FileSink(_cfg["file_dir"]).consume)
        if _cfg["ws_url"]:
            b.add(WsSink(_cfg["ws_url"]).consume)
        if _cfg["otel_endpoint"]:
            try:
                b.add(OtelSink(_cfg["otel_endpoint"]).consume)
            except ImportError as exc:  # opentelemetry 缺失只降级,不拖垮调用
                _log(f"otel sink 未启用(缺依赖): {exc}")
        _bus = b
    return _bus
