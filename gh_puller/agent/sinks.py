"""监控观测通道:sink 基础设施(事件总线/文件/WS/OTel)与运行时配置。

用户定义两种观测通道,无控制台通道:
- 文件 sink(默认恒开):AGENT_MONITOR_DIR/sessions/{running,completed,aborted}/<session>.jsonl,
  每行一条原始事件(事件溯源,全量无损 —— 折叠恢复规范见 gh_puller.agent.events;
  取代 v1 的 LLM 流聚合行,旧格式不兼容,历史数据需手动清理);
- Web/WS sink(AGENT_MONITOR_WEBUI_URL,默认 ws://localhost:8765/ws,逗号分隔多 hub):
  事件流推送给独立 hub(apps/agent-dashboard/server/,WS 端点 /ws),浏览器实时查看;
- OTel sink(AGENT_MONITOR_PHOENIX_URL,默认 http://localhost:6006/):事件流 → span 树 →
  OTLP HTTP(Phoenix 等后端;实现见本文件 OtelSink)。默认值经 ensure_bus 底层的
  _url_reachable TCP 探活:端点可达且 opentelemetry 可导入才注册(置空关闭)。

新增 OTel 后端(Langfuse 等)= envs.py 一个常量 + 本文件 _OTEL_BACKENDS 表一条;
多地址(逗号分隔)由 ensure_bus 逐 URL 注册同类 sink 实例,单 sink 类不改动。

管道:适配器(adapters)归一化 SDK/HTTP 对象 → 事件流 dict → EventBus 扇出(publish 仅
put_nowait 到每 sink 的 asyncio.Queue,永不阻塞调用)→ sink worker 消费(文件写盘)。
线程模型:v1 只有异步调用方,publish 为 loop-affine;若未来出现线程调用方,
须自行经 loop.call_soon_threadsafe 转发。
"""

import asyncio
import json
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

from .. import envs
from ..utils import _log as _utils_log
from .events import truncate

_STATE_DIRS = ("running", "completed", "aborted")


def _log(msg: str) -> None:
    _utils_log(msg, prefix="agent-monitor")


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
    """文件观测通道:每会话一个 JSONL,每行一条原始事件(事件溯源,全量无损)。

    目录结构即索引(sessions/{running,aborted,completed}/),行即自描述事件;
    Linux 查询友好:tail -f running/*.jsonl 实时看,jq 过滤并按折叠规范
    (gh_puller.agent.events)还原任意时刻消息上下文。终态经 os.replace 迁入
    对应状态目录;崩溃残留停留在 running/(即排查素材);hub 启动时按文件
    所在目录种子状态。无 session/start 起点的事件不可分组,丢弃(同 v1 语义)。
    """

    def __init__(self, root: str):
        self.root = Path(root)
        self._files: dict[str, Path] = {}
        for st in _STATE_DIRS:
            (self.root / "sessions" / st).mkdir(parents=True, exist_ok=True)

    async def consume(self, evt: dict) -> None:
        session = evt.get("session", "")
        if evt["type"] == "session/start":
            self._open(session)
        path = self._files.get(session)
        if path is None:
            return
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")
            f.flush()
        if evt["type"] == "session/end":
            self._close(session, (evt.get("data") or {}).get("state", "completed"))

    def _open(self, session: str) -> None:
        self._files[session] = self.root / "sessions" / "running" / f"{session}.jsonl"

    def _close(self, session: str, state: str) -> None:
        src = self._files.pop(session)
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
    """惰性导入 opentelemetry 各符号;缺失 → ImportError(可选依赖降级,由 ensure_bus 兜底)。"""
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    return trace, OTLPSpanExporter, Resource, TracerProvider, SimpleSpanProcessor


class OtelSink:
    """事件流消费端:逐会话构建 OTel span 树并经 OTLP 导出(契约同 FileSink)。

    只消费 gh_puller.agent.events 的事件 dict(无 session/start 起点则忽略),零 SDK 依赖;
    对外粒度落在标准 OTel 上:
    - 根 span 一次 agent 运行(session/start → session/end),按会话维护;
    - step/start→end 对应一次 LLM 请求,原文/思考增量只累加进 step span 属性
      (OTLP 协议无 token 级增量语义,预览截断 300 字);
    - tool/call / tool/result 子 span(调用参数与结果预览;is_error → ERROR);
    - usage/停止原因/错误/上下文事件挂在根 span 属性上,gen_ai.* 兼容 Phoenix 等
      后端的自动识别。

    opentelemetry 为可选依赖(惰性导入于 __init__:缺失 → ImportError,由 ensure_bus
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
        self._spans: dict[str, dict] = {}  # session → {root, step, buf…}
        self._failed: set[str] = set()  # 已报过错的 session(每会话只报一次)

    # ---- 事件分发 ----

    async def consume(self, evt: dict) -> None:
        session = evt.get("session", "")
        t = evt.get("type")
        try:
            state = self._spans.get(session) if session else None
            if t == "session/start":
                self._on_session_start(session, evt)
            elif state is None:
                return  # 无 session/start 起点:事件不可分组,丢弃(同 FileSink)
            elif t == "session/end":
                self._on_session_end(session, evt)
            elif t == "step/start":
                self._on_step_start(state, evt)
            elif t == "step/end":
                self._on_step_end(state, evt)
            elif t == "assistant/chunk":
                self._on_chunk(state, evt)
            elif t == "assistant/message":
                self._on_message(state, evt)
            elif t == "tool/call":
                self._on_tool_call(state, evt)
            elif t == "tool/result":
                self._on_tool_result(state, evt)
            elif t == "error":
                self._on_error(state, evt)
            else:  # turn/user/request/context 信息事件:根 span 属性
                self._on_info(state, evt)
        except Exception as exc:  # 隔离:监控失败绝不冒泡(每次会话只报一次)
            if session not in self._failed:
                self._failed.add(session)
                _log(f"otel sink 消费失败({session}): {type(exc).__name__}: {exc}")

    # ---- span 树构建 ----

    def _child(self, state: dict, name: str, ts=None) -> object:
        ctx = self._trace.set_span_in_context(state["root"])
        return self.tracer.start_span(name, context=ctx, start_time=_ns(ts))

    def _on_session_start(self, session: str, evt: dict) -> None:
        stale = self._spans.pop(session, None)  # 同会话重入:旧根强制收尾
        if stale is not None:
            stale["root"].end(end_time=_ns(evt.get("ts")))
        d = evt.get("data") or {}
        label = d.get("label") or session
        root = self.tracer.start_span(
            f"{label} · {d.get('provider', '')}".strip(" ·"),
            start_time=_ns(evt.get("ts")),
        )
        self._spans[session] = {
            "root": root,
            "step": None,
            "step_n": 0,
            "buf": "",
            "thinking": "",
            "error": None,
            "context_chars": 0,
            "modifies": [],
        }
        _attrs(root, {
            "gen_ai.provider.name": d.get("provider"),
            "gen_ai.request.model": d.get("model"),
            "gh_puller.session": session,
            "gh_puller.label": label,
            "gh_puller.run_id": d.get("run_id"),
            "gh_puller.retry": d.get("retry"),
            "gh_puller.meta": d.get("meta"),
        })

    def _on_step_start(self, state: dict, evt: dict) -> None:
        state["step_n"] += 1
        state["buf"] = ""  # 每步独立累加:一步 = 一次 LLM 请求
        state["thinking"] = ""
        span = self._child(state, f"step.{state['step_n']}", evt.get("ts"))
        _attrs(span, {"gh_puller.turn": evt["data"].get("turn"), "gh_puller.step": evt["data"].get("step")})
        state["step"] = span

    def _on_step_end(self, state: dict, evt: dict) -> None:
        span = state["step"]
        if span is None:
            return
        if state["thinking"]:
            _, prev = truncate(state["thinking"], 300)
            _attrs(span, {"gh_puller.thinking_chars": len(state["thinking"]), "gh_puller.thinking_preview": prev})
        if state["buf"]:
            chars, preview = truncate(state["buf"], 300)
            _attrs(span, {"gh_puller.text_chars": chars, "gh_puller.text_preview": preview})
        span.end(end_time=_ns(evt.get("ts")))
        state["step"] = None

    def _on_chunk(self, state: dict, evt: dict) -> None:
        c = evt["data"]["chunk"]
        if c.get("type") == "text":
            state["buf"] += c.get("text") or ""
        elif c.get("type") == "thinking":
            state["thinking"] += c.get("text") or ""

    def _on_message(self, state: dict, evt: dict) -> None:
        d = evt["data"]
        usage = d.get("usage") or {}
        _attrs(state["root"], {
            "gen_ai.usage.input_tokens": usage.get("input_tokens"),
            "gen_ai.usage.output_tokens": usage.get("output_tokens"),
            "gh_puller.cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "gh_puller.stop_reason": d.get("stop_reason"),
        })
        if d.get("interrupted"):
            state["root"].set_attribute("gh_puller.interrupted", True)

    def _on_tool_call(self, state: dict, evt: dict) -> None:
        d = evt["data"]
        args = d.get("arguments") or ""
        span = self._child(state, f"tool.call:{d.get('name') or d.get('callId') or ''}", evt.get("ts"))
        _attrs(span, {
            "gh_puller.call_id": d.get("callId"),
            "gh_puller.tool_name": d.get("name"),
            "gh_puller.arguments_chars": len(args),
            "gh_puller.arguments_preview": truncate(args, 300)[1],
            "gh_puller.step": d.get("step"),
        })
        span.end(end_time=_ns(evt.get("ts")))

    def _on_tool_result(self, state: dict, evt: dict) -> None:
        d = evt["data"]
        blocks = (d.get("message") or {}).get("content") or []
        first = blocks[0] if blocks else {}
        tid = d.get("callId") or first.get("tool_use_id")
        content = _tool_result_text(d.get("message") or {})
        span = self._child(state, f"tool.result:{d.get('name') or tid or ''}", evt.get("ts"))
        chars, preview = truncate(content, 300)
        _attrs(span, {
            "gh_puller.call_id": tid,
            "gh_puller.tool_name": d.get("name"),
            "gh_puller.is_error": d.get("is_error") or bool(first.get("is_error")),
            "gh_puller.content_chars": chars,
            "gh_puller.content_preview": preview,
            "gh_puller.step": d.get("step"),
        })
        if d.get("is_error"):
            span.set_status(self._trace.Status(self._trace.StatusCode.ERROR))
        span.end(end_time=_ns(evt.get("ts")))

    def _on_info(self, state: dict, evt: dict) -> None:
        t = evt.get("type")
        d = evt.get("data") or {}
        if t == "request/header":
            h = d.get("header") or {}
            _attrs(state["root"], {
                "gh_puller.request_reason": d.get("reason"),
                "gh_puller.request_partial": d.get("partial"),
                "gh_puller.system_chars": len(str(h.get("system") or "")),
                "gh_puller.tool_count": len(h.get("tools") or []),
            })
        elif t == "context/inject":
            state["context_chars"] += len(str(d.get("text") or ""))
        elif t == "context/modify":
            state["modifies"].append(d.get("kind"))
        elif t == "turn/end":
            state["root"].set_attribute("gh_puller.turn_reason", d.get("reason"))

    def _on_error(self, state: dict, evt: dict) -> None:
        d = evt["data"]
        detail = f"{d.get('exc_type', '')}: {d.get('message', '')}"
        state["error"] = detail
        _attrs(state["root"], {"gh_puller.error": detail, "gh_puller.error_stage": d.get("stage")})
        state["root"].set_status(self._trace.Status(self._trace.StatusCode.ERROR, detail[:300]))

    def _on_session_end(self, session: str, evt: dict) -> None:
        state = self._spans.pop(session, None)
        if state is None:
            return
        d = evt.get("data") or {}
        root = state["root"]
        _attrs(root, {
            "gh_puller.duration_ms": d.get("duration_ms"),
            "gh_puller.text_chars": d.get("text_chars"),
            "gh_puller.num_steps": d.get("num_steps"),
            "gh_puller.state": d.get("state"),
            "gh_puller.context_chars": state["context_chars"],
            "gh_puller.context_modifies": state["modifies"],
        })
        if d.get("usage"):
            _attrs(root, {
                "gh_puller.final_usage": d["usage"],
                "gh_puller.stop_reason": d.get("stop_reason"),
            })
        if d.get("ok"):
            root.set_status(self._trace.Status(self._trace.StatusCode.OK))
        else:
            reason = state["error"] or "agent 执行未成功完成"
            root.set_status(self._trace.Status(self._trace.StatusCode.ERROR, reason[:300]))
        root.end(end_time=_ns(evt.get("ts")))


def _tool_result_text(message: dict) -> str:
    """tool/result 消息 → 结果文本(preview 用):拼接各块 content。"""
    parts = []
    for block in message.get("content") or []:
        c = block.get("content")
        if isinstance(c, list):
            parts.extend(str(x.get("text") or "") for x in c if isinstance(x, dict))
        elif c is not None:
            parts.append(str(c))
    return "".join(parts)


# ---------------------------------------------------------------------------
# 运行时配置(缺省取 envs 导入常量;运行时改走 configure)
# ---------------------------------------------------------------------------

_REACH_TIMEOUT_SECONDS = 1.0  # OTel/WS 端点可达性 TCP 探测超时(每进程 bus 构建时每 URL 一次)

# OTel 后端表:新增后端 = envs.py 一个常量 + 此处一条 (env 常量名, 日志标签)
_OTEL_BACKENDS = (("AGENT_MONITOR_PHOENIX_URL", "phoenix"),)

_DEF_SCHEME_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _split_urls(raw) -> list[str]:
    """str|序列|None → URL 列表:逗号分隔/逐条 strip/保序去重;空(None/""/空白/[]) → []。"""
    out: list[str] = []
    if raw is None:
        return out
    items = raw.split(",") if isinstance(raw, str) else raw
    for item in items:
        item = str(item).strip()
        if item and item not in out:
            out.append(item)
    return out


def _otel_traces_url(base: str) -> str:
    """OTLP 便捷归一:path 为空或 "/" → 补 /v1/traces(默认 http://localhost:6006/ 即生效);
    完整 OTLP URL(如 langfuse 的 /api/public/otel/v1/traces)原样通过。

    归一放在封装层(ensure_bus)而非 OtelSink 内部:OtelSink 契约保持"完整 OTLP
    traces URL"(其 docstring 已声明);基底补路径是配置层便利,未来后端如需
    不同路径规则,由 _OTEL_BACKENDS 条目扩展。
    """
    p = urlsplit(base)
    if p.path in ("", "/"):
        base = f"{p.scheme}://{p.netloc}/v1/traces"
    return base


def _url_reachable(url: str, timeout: float = _REACH_TIMEOUT_SECONDS) -> bool:
    """同步 TCP 探活(bus 构建时对每 URL 执行一次;localhost 拒连即时返回,不含请求)。"""
    p = urlsplit(url)
    try:
        host, port = p.hostname, p.port or _DEF_SCHEME_PORTS.get(p.scheme, 80)
    except ValueError:
        return False
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _default_otel_urls() -> list[str]:
    """按 _OTEL_BACKENDS 表聚合各后端 env 常量(各自动做逗号分隔)→ URL 列表。"""
    urls: list[str] = []
    for attr, _ in _OTEL_BACKENDS:
        urls.extend(_split_urls(getattr(envs, attr)))
    return urls


_cfg = {
    "file": True,  # 文件 sink 默认恒开(env 已无 AGENT_MONITOR_FILE;运行时 configure(file=...) 可关/重定向)
    "file_dir": envs.AGENT_MONITOR_DIR,
    "ws_urls": _split_urls(envs.AGENT_MONITOR_WEBUI_URL),
    "otel_urls": _default_otel_urls(),
}
_bus: EventBus | None = None


def configure(*, file=None, file_dir=None, ws_urls=None, otel_urls=None) -> None:
    """重配监控(测试/嵌入用);缺省取 envs 常量,生效于下一次事件发布。

    ws_urls / otel_urls:URL 列表或逗号分隔字符串;None → 重读对应 env 常量
    (otel 为 _OTEL_BACKENDS 全表);空 → 不部署该类 sink(每 URL 一个 sink 实例)。
    file 运行时开关保留(默认 True):测试需停用/重定向文件观测,避免写真实 monitor 目录。
    关闭旧 bus(取消 sink 任务),新配置惰性重建 —— 幂等,可反复调用。
    """
    global _bus
    _cfg["file"] = True if file is None else bool(file)
    _cfg["file_dir"] = envs.AGENT_MONITOR_DIR if file_dir is None else file_dir
    _cfg["ws_urls"] = _split_urls(envs.AGENT_MONITOR_WEBUI_URL if ws_urls is None else ws_urls)
    _cfg["otel_urls"] = _default_otel_urls() if otel_urls is None else _split_urls(otel_urls)
    if _bus is not None:
        _bus.shutdown()
        _bus = None


def ensure_bus() -> EventBus:
    """惰性构建单例(带已启用 sink);须在运行中的事件循环内调用(create_task)。

    每 URL 注册条件:TCP 可达(_url_reachable);OTel 还要求 opentelemetry 可导入
    (OtelSink 构造,缺失 → ImportError 降级);任一不满足 → 日志并跳过该实例。
    """
    global _bus
    if _bus is None:
        b = EventBus()
        if _cfg["file"]:
            b.add(FileSink(_cfg["file_dir"]).consume)
        for url in _cfg["ws_urls"]:
            if not _url_reachable(url):
                _log(f"ws sink 未启用: 端口不可达 {url}")
                continue
            b.add(WsSink(url).consume)
        for url in _cfg["otel_urls"]:
            traces_url = _otel_traces_url(url)
            if not _url_reachable(url):
                _log(f"otel sink 未启用: 端口不可达 {url}")
                continue
            try:
                b.add(OtelSink(traces_url).consume)
            except ImportError as exc:  # opentelemetry 缺失只降级,不拖垮调用
                _log(f"otel sink 未启用(缺依赖): {exc}")
                continue
            _log(f"otel sink 已启用: {traces_url}")
        _bus = b
    return _bus
