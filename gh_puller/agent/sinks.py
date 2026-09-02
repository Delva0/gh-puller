"""Deliver canonical agent events to files, WebSockets, and OpenTelemetry."""

import asyncio
import json
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

from .. import envs
from ..utils import _log as _utils_log
from .events import EventBus, _put_event, is_compact_event, set_active_bus, truncate


def _file_stem(session: str) -> str:
    """Map a session id to the filename stem (segment after the last "/", flat layout)."""
    return session.rsplit("/", 1)[-1]


def _log(msg: str) -> None:
    _utils_log(msg, prefix="agent-monitor")


class FileSink:
    """Write one flat JSONL log per session.

    Compact logs omit only model deltas, so replayed request state is identical to the
    raw stream. Events before ``session/start`` are ignored.
    """

    def __init__(self, root: str, *, raw: bool = False):
        self.root = Path(root)
        self.raw = raw
        self._files: dict[str, Path] = {}
        self.root.mkdir(parents=True, exist_ok=True)

    async def consume(self, evt: dict) -> None:
        """Append one event line to the session file; non-stream only unless raw."""
        if not self.raw and not is_compact_event(evt["type"]):
            return
        session = evt.get("session", "")
        if evt["type"] == "session/start":
            self._open(session)
        path = self._files.get(session)
        if path is None:
            return
        with open(path, "a", encoding="utf-8") as f:  # noqa: ASYNC230 - Ordered append is synchronous.
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

    Its queue retains every compact event and bounds only model-delta backlog during
    disconnect. Push failures never bubble (monitoring must not drag callers). Events
    only deliver after connect (reconnect resumes from the break point). Raw events
    keep their own envelopes and seqs but travel in short batches to amortize
    token-stream framing.
    """

    def __init__(self, url: str):
        self.url = url
        self._q: asyncio.Queue[dict] = asyncio.Queue()
        self._task = asyncio.create_task(self._run())

    async def consume(self, evt: dict) -> None:
        """Queue one event for delivery, shedding only excess model deltas."""
        _put_event(self._q, evt)

    async def _run(self) -> None:
        import websockets  # Lazy optional sink dependency.

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
            except Exception as exc:  # Preserve the queue while reconnecting.
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


def _response_text(output: list[dict], item_type: str, part_type: str) -> str:
    """Extract displayed text from one canonical response Item kind."""
    return "".join(
        str(part.get("text") or "")
        for item in output if item.get("type") == item_type
        for part in item.get("content") or [] if part.get("type") == part_type
    )


def _otel():
    """Lazy import of opentelemetry symbols; missing → ImportError (optional dependency, downgraded by ensure_bus)."""
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    return trace, OTLPSpanExporter, Resource, TracerProvider, SimpleSpanProcessor


class OtelSink:
    """Project activity events into an OTel span tree.

    A session is the root, each ``model/request`` opens a model span correlated by
    ``requestId``, and each ``tool/start`` opens a tool span correlated by ``callId``.
    Semantic markers are annotations only. Sink failures never reach the agent.
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
        self._spans: dict[str, dict] = {}
        self._failed: set[str] = set()

    async def consume(self, evt: dict) -> None:
        """Dispatch one event against the span tree of its session; per-session failures logged once."""
        session = evt.get("session", "")
        t = evt.get("type")
        try:
            state = self._spans.get(session) if session else None
            if t == "session/start":
                self._on_session_start(session, evt)
            elif state is None:
                return
            elif t == "session/end":
                self._on_session_end(session, evt)
            elif t == "agent/set" or t.startswith("agent/set/"):
                self._on_agent_set(state, evt)
            elif t == "model/request":
                self._on_model_request(state, evt)
            elif t.startswith("model/delta/"):
                self._on_model_delta(state, evt)
            elif t == "model/response":
                self._on_model_response(state, evt)
            elif t == "tool/start":
                self._on_tool_start(state, evt)
            elif t == "tool/end":
                self._on_tool_end(state, evt)
            elif t == "session/error":
                self._on_error(state, evt)
            else:  # lifecycle/context information stays on the root span
                self._on_info(state, evt)
        except Exception as exc:  # Observation failures must not reach the agent.
            if session not in self._failed:
                self._failed.add(session)
                _log(f"otel sink 消费失败({session}): {type(exc).__name__}: {exc}")

    def _child(self, state: dict, name: str, ts=None) -> object:
        ctx = self._trace.set_span_in_context(state["root"])
        return self.tracer.start_span(name, context=ctx, start_time=_ns(ts))

    def _on_session_start(self, session: str, evt: dict) -> None:
        stale = self._spans.pop(session, None)
        if stale is not None:
            stale["root"].end(end_time=_ns(evt.get("ts")))
        d = evt.get("data") or {}
        label = d.get("label") or session
        root = self.tracer.start_span(label, start_time=_ns(evt.get("ts")))
        self._spans[session] = {
            "root": root,
            "requests": {},
            "tools": {},
            "error": None,
            "turns": 0,
            "steps": 0,
        }
        _attrs(root, {
            "gh_puller.session": session,
            "gh_puller.label": label,
            "gh_puller.run_id": d.get("runId"),
        })

    def _on_agent_set(self, state: dict, evt: dict) -> None:
        d = evt["data"]
        if evt["type"] == "agent/set":
            _attrs(state["root"], {
                "gh_puller.agent": d.get("agent"),
                "gh_puller.agent.config": d.get("config"),
            })
            return
        facet = evt["type"].removeprefix("agent/set/")
        _attrs(state["root"], {f"gh_puller.agent.{facet}": d.get(facet)})

    def _on_model_request(self, state: dict, evt: dict) -> None:
        request_id = evt["data"]["requestId"]
        stale = state["requests"].pop(request_id, None)
        if stale is not None:
            stale["span"].end(end_time=_ns(evt.get("ts")))
        span = self._child(state, f"model:{request_id}", evt.get("ts"))
        d = evt["data"]
        _attrs(span, {
            "gen_ai.provider.name": d.get("provider"),
            "gen_ai.request.model": d.get("model"),
            "gen_ai.request.parameters": d.get("parameters"),
        })
        state["requests"][request_id] = {
            "span": span,
            "text": "",
            "reasoning": "",
        }

    def _on_model_delta(self, state: dict, evt: dict) -> None:
        request = state["requests"].get(evt["data"]["requestId"])
        if request is None:
            return
        text = evt["data"].get("text") or ""
        if evt["type"] == "model/delta/text":
            request["text"] += text
        elif evt["type"] == "model/delta/reasoning":
            request["reasoning"] += text

    def _on_model_response(self, state: dict, evt: dict) -> None:
        d = evt["data"]
        request = state["requests"].pop(d["requestId"], None)
        span = request["span"] if request else self._child(
            state, f"model:{d['requestId']}", evt.get("ts"))
        usage = d.get("usage") or {}
        text = request["text"] if request else ""
        reasoning = request["reasoning"] if request else ""
        output = d.get("output") or []
        text = text or _response_text(output, "message", "output_text")
        reasoning = reasoning or _response_text(output, "reasoning", "reasoning_text")
        _attrs(span, {
            "gen_ai.usage.input_tokens": usage.get("input"),
            "gen_ai.usage.output_tokens": usage.get("output"),
            "gh_puller.cache_read_tokens": usage.get("cacheRead"),
            "gh_puller.cache_write_tokens": usage.get("cacheWrite"),
            "gh_puller.stop_reason": d.get("stopReason"),
            "gh_puller.text_chars": len(text),
            "gh_puller.text_preview": truncate(text, 300)[1],
            "gh_puller.reasoning_chars": len(reasoning),
            "gh_puller.reasoning_preview": truncate(reasoning, 300)[1],
        })
        span.end(end_time=_ns(evt.get("ts")))

    def _on_tool_start(self, state: dict, evt: dict) -> None:
        d = evt["data"]
        call_id = d["callId"]
        span = self._child(state, f"tool:{d.get('name') or call_id}", evt.get("ts"))
        _attrs(span, {
            "gh_puller.call_id": call_id,
            "gh_puller.tool_name": d.get("name"),
            "gh_puller.arguments": d.get("arguments"),
        })
        stale = state["tools"].pop(call_id, None)
        if stale is not None:
            stale.end(end_time=_ns(evt.get("ts")))
        state["tools"][call_id] = span

    def _on_tool_end(self, state: dict, evt: dict) -> None:
        d = evt["data"]
        call_id = d["callId"]
        span = state["tools"].pop(call_id, None)
        if span is None:
            span = self._child(state, f"tool:{call_id}", evt.get("ts"))
        _attrs(span, {
            "gh_puller.result": d.get("result"),
            "gh_puller.error": d.get("error"),
        })
        if d.get("error") is not None:
            span.set_status(self._trace.Status(self._trace.StatusCode.ERROR))
        span.end(end_time=_ns(evt.get("ts")))

    def _on_info(self, state: dict, evt: dict) -> None:
        t = evt.get("type")
        d = evt.get("data") or {}
        if t == "turn/start":
            state["turns"] += 1
        elif t == "step/start":
            state["steps"] += 1
        elif t == "turn/end":
            state["root"].set_attribute("gh_puller.turn_reason", d.get("reason"))

    def _on_error(self, state: dict, evt: dict) -> None:
        d = evt["data"]
        error = d.get("error") or {}
        detail = f"{error.get('type', '')}: {error.get('message', '')}".strip(": ")
        state["error"] = detail
        _attrs(state["root"], {"gh_puller.error": detail, "gh_puller.error_scope": d.get("scope")})
        state["root"].set_status(self._trace.Status(self._trace.StatusCode.ERROR, detail[:300]))

    def _on_session_end(self, session: str, evt: dict) -> None:
        state = self._spans.pop(session, None)
        if state is None:
            return
        d = evt.get("data") or {}
        root = state["root"]
        for request in state["requests"].values():
            request["span"].end(end_time=_ns(evt.get("ts")))
        for span in state["tools"].values():
            span.end(end_time=_ns(evt.get("ts")))
        _attrs(root, {
            "gh_puller.duration_ms": d.get("durationMs"),
            "gh_puller.outcome": d.get("outcome"),
            "gh_puller.turns": state["turns"],
            "gh_puller.steps": state["steps"],
        })
        if d.get("outcome") == "completed":
            root.set_status(self._trace.Status(self._trace.StatusCode.OK))
        else:
            reason = state["error"] or d.get("reason") or "agent did not complete"
            root.set_status(self._trace.Status(self._trace.StatusCode.ERROR, reason[:300]))
        root.end(end_time=_ns(evt.get("ts")))


_REACH_TIMEOUT_SECONDS = 1.0
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
        raw: True writes model deltas; None re-reads AGENT_MONITOR_FILE_RAW;
            False writes the compact replay-equivalent stream.
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
        _file_sinks.append(fs)
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
            except ImportError as exc:
                _log(f"otel sink 未启用(缺依赖): {exc}")
                continue
            _log(f"otel sink 已启用: {traces_url}")
        _bus = b
        set_active_bus(b)
    return _bus
