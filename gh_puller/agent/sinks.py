"""监控观测通道:sink 基础设施(文件/WS/OTel 三通道 + 事件总线)与运行时配置;无控制台通道。

通道角色与细项(默认恒开/逗号分隔多地址/TCP 探活注册/多后端扩展 = env 常量 +
_OTEL_BACKENDS 一条)见实现与 envs.py;事件流语义(TAXONOMY 粒度、折叠恢复
、message 粒度防膨胀)与 publish/线程模型(有界队列、loop-affine)见 events.py EventBus。
"""

import asyncio
import json
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

from .. import envs
from ..utils import _log as _utils_log
from .events import NON_STREAM_TYPES, EventBus, set_active_bus, truncate


def _file_stem(session: str) -> str:
    """Map a session id to the filename stem (segment after the last "/", flat layout)."""
    return session.rsplit("/", 1)[-1]


def _log(msg: str) -> None:
    _utils_log(msg, prefix="agent-monitor")


# ---------------------------------------------------------------------------
# EventBus(进程内单例;publish 非阻塞)
# ---------------------------------------------------------------------------


class FileSink:
    """File channel: one flat JSONL per session (root = AGENT_MONITOR_DIR, no sessions sublayer).

    Granularity switch: raw=False (default) writes only the non-stream event flow
    (assistant/chunk skipped — file seqs may have holes; folding only compares seq);
    raw=True writes the full raw event flow incl. assistant/chunk (dense seqs, the same
    stream the WS/OTel channels carry; opt-in via AGENT_MONITOR_FILE_RAW=1 to avoid log
    inflation by default). One event per line. Taxonomy is implicit:
    state lives in the events (grep '"type":"session/end"' for the final state data.state,
    '"type":"error"' for errors, 'session/start' for the session origin). Linux-friendly
    monitoring: tail -f <root>/*.jsonl, jq with the folding spec (gh_puller.agent.events).
    session/end stays the dirty last line; keep-warm touches mtime via `touch`, never
    writes (lease judgment in events.py). Crash residue = files without a terminal line
    (running until the lease expires). Events without a session/start origin are dropped.
    """

    def __init__(self, root: str, *, raw: bool = False):
        self.root = Path(root)
        self.raw = raw
        self._files: dict[str, Path] = {}  # session → 当前文件(注册用;终态不移走)
        self.root.mkdir(parents=True, exist_ok=True)

    async def consume(self, evt: dict) -> None:
        """Append one event line to the session file; non-stream only unless raw."""
        if not self.raw and evt["type"] not in NON_STREAM_TYPES:
            return  # 流式事件(chunk):文件缺省只落非流式事件流,防日志膨胀;raw=True 落全量
        session = evt.get("session", "")
        if evt["type"] == "session/start":
            self._open(session)
        path = self._files.get(session)
        if path is None:
            return
        with open(path, "a", encoding="utf-8") as f:  # noqa: ASYNC230 - 文件 sink 本就同步落盘(逐事件追加即返回)
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")
            f.flush()

    def _open(self, session: str) -> None:
        """Register the session file (created on session/start)."""
        self._files[session] = self.root / f"{_file_stem(session)}.jsonl"

    async def touch(self, session: str) -> None:
        """Keep-warm primitive: refresh file mtime only (no writes); silent no-op on failure."""
        path = self._files.get(session)
        if path is None:
            return
        try:
            os.utime(path, None)
        except OSError as exc:
            _log(f"FileSink.touch 失败 {path.name}: {exc}")


class WsSink:
    """WS channel (producer side): background task pushes events to the hub, silent 1→2→…→30s backoff on disconnect.

    Internal bounded queue (5000, drop-oldest) absorbs backlog during disconnect;
    push failures never bubble (monitoring must not drag callers). Events only deliver
    after connect (reconnect resumes from the break point). Raw events keep their own
    envelopes and seqs but travel in short batches to amortize token-stream framing.
    """

    def __init__(self, url: str):
        self.url = url
        self._q: asyncio.Queue[dict] = asyncio.Queue(maxsize=5000)
        self._task = asyncio.create_task(self._run())

    async def consume(self, evt: dict) -> None:
        """Queue one event for delivery; drops the oldest when the bounded queue is full."""
        try:
            self._q.put_nowait(evt)
        except asyncio.QueueFull:
            self._q.get_nowait()  # 先丢旧:最近的进度优先
            self._q.put_nowait(evt)

    async def _run(self) -> None:
        import websockets  # 惰性导入:仅实际部署 WS sink 时才要求 websockets 依赖

        wait = 1
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=20) as ws:
                    wait = 1
                    while True:
                        first = await self._q.get()
                        await asyncio.sleep(0.016)
                        events = [first]
                        while len(events) < 256 and not self._q.empty():
                            events.append(self._q.get_nowait())
                        await ws.send(json.dumps({"type": "evts", "events": events}, ensure_ascii=False))
            except Exception as exc:  # 握手/断连:保留缓冲,退避重连
                _log(f"ws sink 未连接({wait}s 后重试): {exc}")
                await asyncio.sleep(wait)
                wait = min(wait * 2, 30)


def _ns(ts) -> int | None:
    """Convert an event ts (float seconds) to an OTel nanosecond timestamp; None keeps None."""
    return int(ts * 1e9) if ts else None


def _attrs(span, mapping: dict) -> None:
    """Set span attributes in batch; None skipped, list/dict serialized as JSON."""
    for key, value in mapping.items():
        if value is None:
            continue
        span.set_attribute(
            key, json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value)


def _otel():
    """Lazy import of opentelemetry symbols; missing → ImportError (optional dependency, downgraded by ensure_bus)."""
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    return trace, OTLPSpanExporter, Resource, TracerProvider, SimpleSpanProcessor


class OtelSink:
    """Event-stream consumer: OTel span tree per session, OTLP export (event contract same as FileSink).

    Consumes only event dicts from gh_puller.agent.events (ignored without a
    session/start origin), zero SDK dependency. Granularity: one root span per agent
    run (session/start → session/end); one step span per LLM request (deltas accumulate
    into step attributes, previews truncated to 300 chars); tool/call–tool/result child
    spans (is_error → ERROR); usage/stop_reason/error/context attributes hang on the
    root span — gen_ai.* so Phoenix-typed backends auto-detect.

    opentelemetry is optional (lazy import in __init__; missing → ImportError, downgraded
    by ensure_bus). Tracer injection targets tests (InMemorySpanExporter); the default
    lazily builds TracerProvider (SimpleSpanProcessor + OTLPSpanExporter), endpoint must
    be a full OTLP traces URL (e.g. http://localhost:6006/v1/traces).

    Robustness: parent context passes explicitly (set_span_in_context; consume crosses
    awaits in the EventBus worker so active-span nesting would misnest); failures log
    to stderr once per session, never bubble.
    """

    def __init__(self, endpoint: str, *, tracer=None):
        self._trace, otel_exporter, Resource, TracerProvider, SimpleSpanProcessor = _otel()
        self.tracer = tracer
        if self.tracer is None:
            tp = TracerProvider(
                resource=Resource({"service.name": envs.OTEL_SERVICE_NAME}),
            )
            tp.add_span_processor(SimpleSpanProcessor(otel_exporter(endpoint=endpoint)))
            self.tracer = tp.get_tracer("gh-puller.agent-monitor")
        self._spans: dict[str, dict] = {}  # session → {root, step, buf…}
        self._failed: set[str] = set()  # 已报过错的 session(每会话只报一次)

    # ---- 事件分发 ----

    async def consume(self, evt: dict) -> None:
        """Dispatch one event against the span tree of its session; per-session failures logged once."""
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
            else:  # lifecycle/context information stays on the root span
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
            "gh_puller.generator": d.get("generator") or "",
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
        if c.get("type") == "content":
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
        if t == "context/modify":
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
    """Flatten a tool/result message into result text (previews): concat block content."""
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
    """Normalize str|sequence|None into an ordered, deduped URL list; empty inputs → []."""
    out: list[str] = []
    if raw is None:
        return out
    items = raw.split(",") if isinstance(raw, str) else raw
    for raw_item in items:
        item = str(raw_item).strip()
        if item and item not in out:
            out.append(item)
    return out


def _otel_traces_url(base: str) -> str:
    """Normalize an OTLP base URL: empty path → /v1/traces; full URLs pass through."""
    p = urlsplit(base)
    if p.path in ("", "/"):
        base = f"{p.scheme}://{p.netloc}/v1/traces"
    return base


def _url_reachable(url: str, timeout: float = _REACH_TIMEOUT_SECONDS) -> bool:
    """Sync TCP liveness probe (once per URL at bus build; localhost refusal returns immediately)."""
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
    """Aggregate the _OTEL_BACKENDS env constants into the default OTel URL list."""
    urls: list[str] = []
    for attr, _ in _OTEL_BACKENDS:
        urls.extend(_split_urls(getattr(envs, attr)))
    return urls


_cfg = {
    # FileSink is always on; file_dir redirects its output for isolation and embedding.
    "file_dir": envs.AGENT_MONITOR_DIR,
    "raw": envs.AGENT_MONITOR_FILE_RAW,
    "ws_urls": _split_urls(envs.AGENT_MONITOR_WEBUI_URL),
    "otel_urls": _default_otel_urls(),
}
_bus: EventBus | None = None
# 文件 sink 实例注册表(keep-warm 的 touch 直达;ensure_bus 建、configure 清)
_file_sinks: list[FileSink] = []


def configure(*, file_dir=None, ws_urls=None, otel_urls=None, raw=None) -> None:
    """Reconfigure monitoring (tests/embedding); defaults re-read env constants, effective on the next publish.

    Closes the old bus (cancels sink tasks); the new config rebuilds lazily — idempotent.

    Args:
        file_dir: Directory for the always-on file sink (cannot be disabled); tests/embedding
            redirect here instead of the real monitor dir.
        ws_urls: URL list or comma-separated string; None re-reads the env constant;
            empty deploys none (one sink instance per URL).
        otel_urls: URL list or comma-separated string; None re-reads the whole
            _OTEL_BACKENDS table; empty disables OTel sinks.
        raw: True → FileSink writes the full raw event flow (incl. assistant/chunk);
            None re-reads AGENT_MONITOR_FILE_RAW; False → non-stream projection (default).
    """
    global _bus, _file_sinks
    _cfg["file_dir"] = envs.AGENT_MONITOR_DIR if file_dir is None else file_dir
    _cfg["raw"] = envs.AGENT_MONITOR_FILE_RAW if raw is None else bool(raw)
    _cfg["ws_urls"] = _split_urls(envs.AGENT_MONITOR_WEBUI_URL if ws_urls is None else ws_urls)
    _cfg["otel_urls"] = _default_otel_urls() if otel_urls is None else _split_urls(otel_urls)
    if _bus is not None:
        _bus.shutdown()
        _bus = None
    _file_sinks = []
    set_active_bus(None)


async def touch(session: str) -> None:
    """Keep-warm fan-out: forward to every registered FileSink.touch; no sink → no-op."""
    for fs in _file_sinks:
        await fs.touch(session)


def ensure_bus() -> EventBus:
    """Lazily build the singleton bus with enabled sinks; call inside a running loop (create_task).

    Per-URL registration condition: TCP reachable (_url_reachable); OTel also requires
    opentelemetry importable (OtelSink construction defect → ImportError downgrade).
    Failing conditions log and skip the instance.
    """
    global _bus, _file_sinks
    if _bus is None:
        b = EventBus()
        fs = FileSink(_cfg["file_dir"], raw=_cfg["raw"])
        _file_sinks.append(fs)  # touch 扇出门(与事件通道同一实例)
        b.add(fs.consume)
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
        set_active_bus(b)
    return _bus
