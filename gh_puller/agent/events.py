"""agent 调用的可观测事件模型(事件溯源式;纯 dict 实现,零 SDK 依赖)。

对齐 deepseek-harness 的核心不变量:无损 append-only 事件日志,seq 每 session 从 0
连续单调(**流式事件流内稠密**;非流式投影侧允许洞,见下);LLM messages 上下文
是 surface 节点的派生(折叠)而非快照:

═══════════════════════════════════════════════════════════════════════
事件流全景(时间自下而上展开;一次 LLM 回合 = 一个 step)
═══════════════════════════════════════════════════════════════════════

    session/start ──► turn/start ──► step/start
                                        │            ┌─────────────────────┐
                                        │ thinking   │ assistant/chunk ×m │ 流式增量(逐条)
                                        │  批        └──────────┬──────────┘
                                        │                       ▼
                                        │            ┌─────────────────────┐
                                        │            │ assistant/message ×1│ m 个 chunk 拼接
                                        │            └─────────────────────┘
                                        │            ┌─────────────────────┐
                                        │ content    │ assistant/chunk ×n │ 流式增量(逐条)
                                        │  批        └──────────┬──────────┘
                                        │                       ▼
                                        │            ┌─────────────────────┐
                                        │            │ assistant/message ×1│ n 个 chunk 拼接
                                        │            └─────────────────────┘
                                        │            ┌─────────────────────┐
                                        │ 工具批     │ tool/call ×k …      │ 不进消息块
                                        │            │ tool/result ×k      │ (独立承载)
                                        │            └─────────────────────┘
    session/end ◄──── turn/end ◄──── step/end ◄──────┘

    · user/message 落在 step/start 之后(本回合输入)
    · thinking 批 → content 批 → 工具批:批序恒定(思考先行、消息后工具)
    · 工具结果后的下一个回合经 step_boundary 开新 step,批内契约逐 step 成立

消息块式契约(各生成器适配器统一遵守,cc/codex/opencode 同式):

    ┌───────────────────┬─────────────────────────────────────────────┐
    │ 块名              │ 序列                                         │
    ├───────────────────┼─────────────────────────────────────────────┤
    │ thinking 批       │ chunk(thinking) ×m ──► message(thinking) ×1 │
    │ content 批        │ chunk(content)  ×n ──► message(content)  ×1 │
    ├───────────────────┼─────────────────────────────────────────────┤
    │ 规则 1 消息单型块 │ message.content 内 thinking/content 不混装  │
    │ 规则 2 消息=拼接  │ 文本 = 该批 chunk 的全量顺序拼接             │
    │ 规则 3 sourceSeqs │ 只引本批 chunk seq(think→thinking,content→  │
    │                   │ content);tool/result 恒引对应 tool/call seq │
    └───────────────────┴─────────────────────────────────────────────┘

事件流两级粒度:流式事件流(TAXONOMY 全集,含 assistant/chunk——还原实时
逐字上下文;WS/OTel 通道承载)⊃ 非流式事件流(TAXONOMY − assistant/chunk,
message 粒度;filesink 缺省只落此级,AGENT_MONITOR_FILE_RAW=1 落全量)。

seq 序列是流式事件流的稠密序号;文件侧(非流式投影)按行跳过 chunk,因此
**文件内 seq 允许洞,洞 = 被跳过的 assistant/chunk**。折叠契约只做 seq 排序
与 seq < x 比较,不要求稠密 —— 读者(含前端)不得假定文件 seq 连续。

事件家族与载荷(每个 type 的 data 键):

    生命周期          session/start  run_id,label,generator,provider,model,retry,meta
                     session/end    state,ok,duration_ms,text_chars,num_steps,usage,…
                     turn/start|end turn 〔end 另有 reason〕
                     step/start|end turn,step(step_boundary 成对开合)
    ──────────────
    surface 层       user/message       message,source,surfaceOp
                     assistant/message message,sourceSeqs,surfaceOp
                                       〔cc SDK 路径另带 usage/stop_reason〕
                     tool/result       message,is_error,callId,name,sourceSeqs
    ──────────────
    流式层           assistant/chunk   chunk{type,index,text}
                                       · content|thinking = 块式契约的流式增量
                                       · tool_call|plan   = 增量段(各生成器语义)
    ──────────────
    工具调用         tool/call         callId,name,arguments(原始 arguments JSON)
    ──────────────
    日志型(ignorable) config/init    初始配置快照(凭证剥离+装配对象折叠为类型名)
                     context/modify  上下文修改解释面(不折动)
    错误             error            stage,exc_type,message(全量;session/end.reason ≤2000 字)

surface 事件携带全量消息与 surfaceOp(append 或 {op:'replace',start,end});
surface 事件 user/message / assistant/message / tool/result 是折叠集合
(messages(X) 派生见下);config/init 在 session/start 之前打印;context/modify
折叠正确性与它无关 —— 丢弃也只是少了解释,不会误解消息历史。
- **会话保活**:session 活着但静默时的"保鲜"由 generators.base.session 的
  keep-warm 定时器调 sinks.touch(session) 直触文件 mtime(只动时间、不加行),
  监测端(agent-monitor hub)借"无终态行且 mtime 静止超租约"判定会话已死 ——
  事件 taxonomy 不含 session/heartbeat。

折叠恢复规范(与 apps/agent-monitor/web/src/dashboard/monitor-data/surface.ts 同份语义,
本模块不落 Python 实现;实现测试归前端 surface.test.ts):
    messages(X) = [derive(evt) for node in surface_nodes if node.seq < X and derive 非空]
    derive: user/message→data.message; assistant/message→content 非空 ? message : None;
            tool/result→data.message; 其余→None

本模块只认普通 dict,严禁 import claude_agent_sdk / httpx —— 测试只喂假 dict。
seq/run_id 等会话属性由调用方(EventRecorder,见下)附加;new_event 只造 id/ts/type/data
信封(seq 不在此分配,防止双写)。

事件域全集 = 本文件(模型 TAXONOMY/new_event + 单次调用记录器 EventRecorder +
进程内总线 EventBus):**导入期**纯 stdlib、零 SDK/envs;sinks.py 是观测通道层。
运行期仅两处懒耦合(方法内 from .sinks import,零导入副作用):
- _ensure_maybe_bus(首次事件自足建总线,app/生成器不管理该机制);
- _keepwarm_loop(touch 是 FileSink 的职责,本层只做节奏)。
"""

import asyncio
import json
import time
import uuid

from gh_puller.utils import _log

# 事件全量 taxonomy(点号→斜杠命名,与 dsh type 风格对齐)
TAXONOMY = frozenset(
    {
        "session/start",
        "session/end",
        "turn/start",
        "turn/end",
        "step/start",
        "step/end",
        "user/message",
        "assistant/chunk",  # 原始流增量(text/thinking/tool_input),非 surface
        "assistant/message",
        "tool/call",
        "tool/result",
        "config/init",
        "context/modify",
        "error",
    },
)

# surface 事件:消息上下文的可折叠集合(必带全量 message 与合法 surfaceOp)
SURFACE_TYPES = frozenset({"user/message", "assistant/message", "tool/result"})

# 事件流两级划分:流式事件流(agent 事件流,完整 taxonomy,可还原实时上下文)
# 与非流式事件流(逐行跳 chunk,message 粒度,可还原任意时刻消息上下文)。
# filesink 缺省只落非流式事件流(NON_STREAM_TYPES;AGENT_MONITOR_FILE_RAW=1 落全量);
# WS/OTel 通道承载完整流式事件流。
NON_STREAM_TYPES = TAXONOMY - {"assistant/chunk"}

# ignorable 日志型事件:读者可安全跳过(不影响消息派生/请求重建);缺失 ignorable
# 标记的未知类型 → 必须可解析(读者应报错,防静默丢消息)
LOG_TYPES = frozenset({"config/init", "context/modify"})


def new_event(evt_type: str, **data) -> dict:
    """Build an event envelope (id/ts, type/data split); validate type and JSON serializability.

    Envelope identity is session + seq only (receivers group by session, order by seq);
    run_id/label/generator/model/retry/meta live in the session/start snapshot, never
    per event; surface events enforce message + surfaceOp (protects fold reproducibility).

    Args:
        evt_type: Event type in TAXONOMY.
        data: Event payload matching the type semantics; surface events require
            `message` and a valid `surfaceOp`.
    """
    if evt_type not in TAXONOMY:
        raise ValueError(f"未知事件 type: {evt_type!r}")
    evt = {"id": f"e-{uuid.uuid4().hex[:7]}", "ts": time.time(), "type": evt_type, "data": data}
    if evt_type in SURFACE_TYPES:
        if "message" not in data:
            raise ValueError(f"surface 事件缺 message 字段: {evt_type!r}")
        op = data.get("surfaceOp")
        if op != "append" and not (isinstance(op, dict) and op.get("op") == "replace"):
            raise ValueError(f"surface 事件缺合法 surfaceOp: {evt_type!r}")
    if evt_type in LOG_TYPES:
        evt["ignorable"] = True
    json.dumps(evt)  # 校验可 JSON 化(如 set 字段 → TypeError)
    return evt


def truncate(text: str | None, n: int) -> tuple[int, str]:
    """Return (original length, preview); long text is truncated with an ellipsis.

    Only feeds preview attributes of third-party channels (OTel); monitor events are
    never truncated.

    Args:
        text: Text to preview; None/empty yields (0, "").
        n: Max preview length in characters.

    Returns:
        (original length, preview text) tuple.
    """
    if not text:
        return (0, "")
    if len(text) <= n:
        return (len(text), text)
    return (len(text), text[:n] + "…")


def _jsonable(value):
    """Recursively downgrade a payload; non-scalar objects collapse to a `<TypeName>` placeholder."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


# ---------------------------------------------------------------------------
# 运行事件发布器(单次调用的 EventRecorder)+ 进程内总线(EventBus):
# 纯 stdlib(事件域全集),依旧零 SDK/envs 依赖。
# ---------------------------------------------------------------------------

def _session_id(session: str | None, session_ns: str | None, run_id: str | None,
                session_name: str | None) -> str:
    """Session id: explicit value, else `<ns>/<uuid4>`; ns = session_ns → run_id → session_name → "agent"."""
    if session:
        return session
    ns = session_ns or run_id or session_name or "agent"
    return f"{ns}/{uuid.uuid4()}"


def _norm_token(u, keys: tuple[str, ...]):
    """Fetch the first non-None value among the given attribute/keys (att/attr mapping helper)."""
    for k in keys:
        v = getattr(u, k, None)
        if v is None and isinstance(u, dict):
            v = u.get(k)
        if v is not None:
            return v
    return None


def _normalize_usage(u) -> dict | None:
    """Map SDK/HTTP usage fields to {input_tokens, output_tokens, cache_read_input_tokens}; None keeps None."""
    if not u:
        return None
    return {
        "input_tokens": _norm_token(u, ("input_tokens", "prompt_tokens", "inputTokens")),
        "output_tokens": _norm_token(u, ("output_tokens", "completion_tokens", "outputTokens")),
        "cache_read_input_tokens": _norm_token(u, ("cache_read_input_tokens", "cacheReadTokens")),
    }


_active_bus: "EventBus | None" = None  # 由 sinks.configure/ensure_bus 单向设置(events 层零反向依赖)


def set_active_bus(bus: "EventBus | None") -> None:
    """Set the active bus (called only by the observation-channel layer).

    Args:
        bus: Bus to receive events, or None to disable channels (zero-cost short-circuit).
    """
    global _active_bus
    _active_bus = bus


def _ensure_maybe_bus() -> "EventBus | None":
    """Lazily build the bus on the first event of any session; None outside a running loop."""
    bus = _active_bus
    if bus is None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return None  # 无运行中事件循环(同步构造/离线测试):降级短路(不建裸任务)
        from .sinks import ensure_bus

        ensure_bus()
        bus = _active_bus
    return bus


class EventRecorder:
    """Event publisher for one run: session envelope, turn/step/seq counters, normalized broadcast."""

    def __init__(self, session: str, *, generator: str = "",
                 provider: str | None = None, model: str | None = None,
                 label: str | None = None,
                 run_id: str | None = None, meta=None):
        """Create the recorder bound to one session.

        Args:
            session: Session id (derivation rules in `_session_id`).
            generator: Pipeline id of the producing generator.
            provider: Backend domain recorded in the session snapshot.
            model: Model name recorded in the session snapshot.
            label: Human-readable session label (defaults to the session id).
            run_id: Run id recorded in the session snapshot.
            meta: Arbitrary metadata recorded in the session snapshot.
        """
        self.session = session
        self.label = label or session
        self.generator = generator
        self.provider = provider
        self.model = model
        self.run_id = run_id
        self.meta = meta
        self.seq = 0
        self.turn = 1  # 每 run 一个 dsh-style turn:单一用户消息 → 最终回答
        self.step = 1  # 一次 LLM 请求 = 一个 step;工具结果后的新请求 +1
        self.text_chars = 0
        self.t0 = time.monotonic()  # run 起点(与 start() 方法分名)
        self._keepwarm_task: asyncio.Task | None = None  # 会话保鲜定时器(会话记录器原生)
        self._keepwarm_interval: float | None = None
        self.tool_names: dict[str, str] = {}  # tool_use_id → 工具名(tool/result 归一化用)
        self._tool_pending = False  # 本轮工具结果已发 → 下个 assistant 消息段开新 step
        self._active_tool_use: dict[int, str] = {}  # 块 index → tool_use_id
        self._tool_use_pieces: dict[int, list[str]] = {}  # 块 index → input_json_delta 碎片
        self._tool_result: dict | None = None
        self._chunk_seqs: list[int] = []  # 本 step 的 assistant/chunk seq(消息 sourceSeqs)
        self._chunk_type_seqs: dict[str, list[int]] = {}  # chunk type → 本 step 的 seq(消息 sourceSeqs 按批分组)
        self._call_seqs: dict[str, int] = {}  # callId → tool/call 的 seq
        self._tool_results_seen: set[str] = set()  # 已发射 tool/result 的 callId(流/用户消息双路去重)
        # partial 模式重建缓冲(cc 真机 CLI:SDK 消息标记 content 为空,全量从本消息增量重建)
        self._msg_text = ""  # 本 assistant 消息的文本增量累计
        self._msg_thinking = ""  # 本 assistant 消息的思考增量累计
        self._msg_tool_calls: list[tuple[str, str, str]] = []  # (callId, name, arguments)
        self._msg_stop_reason: str | None = None  # message_delta.stop_reason(消息级)
        self._step_open = False
        self._ended = False
        self._reason: str | None = None  # error 事件后供 session/end.reason 使用
        self.result_usage: dict | None = None
        self.result_stop_reason: str | None = None
        self.result_cost_usd: float | None = None

    def event(self, evt_type: str, **data) -> dict | None:
        """Build and publish one event; returns it with seq assigned.

        Args:
            evt_type: Event type in TAXONOMY.
            data: Event payload (see `new_event`).

        Returns:
            Published event with seq assigned, or None when no channel is active.
        """
        bus = _ensure_maybe_bus()  # 录前自足:首次事件即建监控总线(幂等;随 event 恒在 loop 内)
        if bus is None or not bus.enabled:
            return None  # 无通道:零开销短路(publish 语义不变)
        evt = new_event(evt_type, **data)
        evt["session"] = self.session  # 身份仅 session;元数据快照见 session/start
        evt["seq"] = self.seq
        self.seq += 1
        bus.publish(evt)
        return evt

    def init_config(self, config: dict) -> None:
        """Snapshot the construction-time config (config/init; creds/endpoints stripped).

        Printed before start(): the single observation surface for the client's initial
        config; SDK assembly objects (e.g. mcp.server.Server in mcp_servers) collapse to
        type names — only the assembly shape is observed, never instances.

        Args:
            config: Config dict as assembled at construction time.
        """
        creds = ("api_key", "token", "base_url")
        self.event("config/init",
                   config=_jsonable({k: v for k, v in config.items() if k not in creds}))

    # ------------------------------------------------------------------
    # 会话保活(fs touch 保鲜;会话记录器原生能力):agent 静默期不动事件流,只触文件
    # mtime —— 供监控端(hub)按"mtime 静止超租约"区分"活着但静默"与"进程已死"。
    # cadence 由调用方注入(零 env 依赖);触达经 sinks.touch,事件流保持不变
    # (touch 是 FileSink 的职责,见 sinks.py)。
    # ------------------------------------------------------------------

    def start_keepwarm(self, interval: float) -> None:
        """Start the keep-warm timer (cadence = interval seconds) while the run is active; idempotent.

        Only creates a task when interval > 0 and a live bus exists; `stop_keepwarm`
        cancels it before finish; the loop just sleeps and touches the session file
        mtime, never busy-loops.

        Args:
            interval: Seconds between mtime touches; ≤0 disables keep-warm.
        """
        if self._keepwarm_task is not None or interval <= 0 or _active_bus is None:
            return
        self._keepwarm_interval = interval
        self._keepwarm_task = asyncio.create_task(self._keepwarm_loop(interval))

    async def stop_keepwarm(self) -> None:
        """Cancel and await the keep-warm task (idempotent); call before finish so session/end stays the last line.

        Cancellation completes within the run lifecycle — even if the end line is
        dropped by the bounded queue, keep-warm is already stopped so mtime freezes
        for the hub lease to judge.
        """
        task = self._keepwarm_task
        self._keepwarm_task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _keepwarm_loop(self, interval: float) -> None:
        """Sleep `interval`, then touch the session file mtime; cheap, no quiet-detection."""
        from .sinks import touch

        while True:
            await asyncio.sleep(interval)
            await touch(self.session)

    def start(self, *, context: list[dict] | None = None, retry: dict | None = None,
              prologue: bool = True) -> None:
        """Enter the run: session/start (with retry metadata) → context events → turn/start → step/start.

        prologue=False (dsh projection path only): turn/step lifecycle is carried by
        dsh's own events, no turn/start + step/start synthesized and `_step_open` stays False.

        Args:
            context: Context-modify explanation events replayed before the run.
            retry: Retry metadata recorded in session/start.
            prologue: Whether to synthesize turn/start and step/start.
        """
        self.event("session/start", run_id=self.run_id, label=self.label, generator=self.generator,
                   provider=self.provider, model=self.model, retry=retry, meta=self.meta)
        for ctx in context or []:
            self.event(ctx["type"], **ctx["data"])  # 日志型说明事件:重放于 turn 之前
        if prologue:
            self.event("turn/start", turn=self.turn)
            self.event("step/start", turn=self.turn, step=self.step)
            self._step_open = True

    def step_boundary(self) -> None:
        """Close the previous step and open the next (new LLM round after a tool result); step buffers reset."""
        if self._stepping():
            self.event("step/end", turn=self.turn, step=self.step)
        self.step += 1
        self._chunk_seqs = []
        self._chunk_type_seqs = {}
        self._msg_reset()  # 新 LLM 轮:旧消息缓冲(若有残留)清空
        self._step_open = True
        self.event("step/start", turn=self.turn, step=self.step)

    def _msg_reset(self) -> None:
        """Reset the current assistant-message delta buffers."""
        self._msg_text = ""
        self._msg_thinking = ""
        self._msg_tool_calls = []
        self._msg_stop_reason = None

    def user_message(self, message: dict, *, source: dict | None = None,
                     surface_op: str | dict = "append") -> None:
        """Emit a user/message surface event.

        Args:
            message: User message dict (folds into the message surface).
            source: Source attribution dict; defaults to {"kind": "user"}.
            surface_op: Surface marker — "append" or a dict {"op": "replace", ...}.
        """
        if source is None:
            source = {"kind": "user"}
        self.event("user/message", turn=self.turn, step=self.step, message=message,
                   source=source, surfaceOp=surface_op)

    def chunk(self, chunk: dict) -> None:
        """Emit an assistant/chunk raw delta; its seq joins this step's tiered sourceSeqs.

        块式契约(见模块 docstring):type=content|thinking 的增量按批与
        assistant/message 对偶(批内 message 定型 = 本批 chunk 拼接,sourceSeqs =
        本批 chunk seq);type=tool_call|plan 为增量段(未定型为独立消息)。

        Args:
            chunk: Segment dict {"type": "thinking"|"content"|"tool_call"|"plan",
                "index": segment order, "text": delta} (tool_call payload is partial JSON).
        """
        evt = self.event("assistant/chunk", turn=self.turn, step=self.step, chunk=chunk)
        if evt is not None:
            self._chunk_seqs.append(evt["seq"])
            self._chunk_type_seqs.setdefault(str(chunk.get("type", "")), []).append(evt["seq"])
        if chunk.get("type") == "thinking":
            self._msg_thinking += chunk.get("text", "")

    def text(self, text: str, *, index: int = 0) -> None:
        """Convenience emitter for a content segment.

        Args:
            text: Content delta text.
            index: Segment order within the message.
        """
        self.text_chars += len(text)
        self._msg_text += text
        self.chunk({"type": "content", "index": index, "text": text})

    def tool_call(self, call_id: str, name: str | None, arguments: str) -> None:
        """Emit a tool/call event; its seq is kept for the matching tool/result sourceSeqs.

        Args:
            call_id: Tool call id (wire key `callId`).
            name: Tool name, or None when unknown.
            arguments: Raw arguments (JSON string or partial JSON).
        """
        evt = self.event("tool/call", turn=self.turn, step=self.step, callId=call_id,
                         name=name, arguments=arguments)
        if evt is not None:
            self._call_seqs[call_id] = evt["seq"]
        self._msg_tool_calls.append((call_id, name, arguments))

    def tool_result(self, message: dict, *, call_id: str, name: str | None,
                    is_error: bool, src_seq: int | None = None) -> None:
        """Emit a tool/result surface event; sourceSeqs = the matching tool/call seq.

        Args:
            message: Tool result message dict (folds into the message surface).
            call_id: Tool call id linking to the matching tool/call event.
            name: Tool name, or None when unknown.
            is_error: Whether the tool invocation failed.
            src_seq: Seq of the matching tool/call; None omits `sourceSeqs`
                (no traceable call seq).
        """
        data = {"turn": self.turn, "step": self.step, "message": message, "is_error": is_error,
                "surfaceOp": "append", "callId": call_id}
        if name:
            data["name"] = name
        if src_seq is not None:
            data["sourceSeqs"] = [src_seq]
        self.event("tool/result", **data)

    def result_meta(self, msg) -> None:
        """Fold a ResultMessage into the session/end summary (usage/stop_reason/cost).

        Args:
            msg: Result message carrying usage, stop_reason and total_cost_usd.
        """
        self.result_usage = _normalize_usage(getattr(msg, "usage", None))
        self.result_stop_reason = getattr(msg, "stop_reason", None)
        self.result_cost_usd = getattr(msg, "total_cost_usd", None)

    def finish(self, ok: bool, *, epilogue: bool = True) -> None:
        """Idempotent teardown: step/end → turn/end → session/end.

        epilogue=False (dsh projection path only): turn/step finals were already
        forwarded from dsh native events; only session/end is assembled.

        Args:
            ok: Whether the run completed (state becomes "completed", else "aborted").
            epilogue: Whether to emit turn/step finals before session/end.
        """
        if self._ended:
            return
        self._ended = True
        # 防御:不经 session 的 stop_keepwarm 直接结束的路径,也取消保鲜任务(非 await;
        # _guard 路径已 await,此处兜底防对侧任务在 session/end 之后仍 touch)。
        if self._keepwarm_task is not None:
            self._keepwarm_task.cancel()
            self._keepwarm_task = None
        state = "completed" if ok else "aborted"
        data = {"state": state, "ok": ok,
                "duration_ms": int((time.monotonic() - self.t0) * 1000),
                "text_chars": self.text_chars, "num_steps": self.step}
        data |= {
            k: v
            for k, v in (("usage", self.result_usage), ("stop_reason", self.result_stop_reason),
                         ("total_cost_usd", self.result_cost_usd))
            if v is not None
        }
        if not ok and self._reason:
            data["reason"] = self._reason
        if epilogue:
            if self._step_open:
                self.event("step/end", turn=self.turn, step=self.step)
                self._step_open = False
            self.event("turn/end", turn=self.turn, reason="completed" if ok else "error")
        self.event("session/end", **data)

    def error(self, exc: Exception, stage: str) -> None:
        """Emit an error event (full message, untruncated); session/end.reason keeps the first 2000 chars.

        Args:
            exc: The exception that failed the run.
            stage: Failure stage label (e.g. "run", "extended", ...).
        """
        self.event("error", stage=stage, exc_type=type(exc).__name__, message=str(exc))
        self._reason = str(exc)[:2000]

    def _stepping(self) -> bool:
        """Whether enter/step lifecycle events are open."""
        return self._step_open


class EventBus:
    """In-process async bus: publish put_nowait to each sink queue, never blocks; a slow sink drags only itself.

    Bounded queue (5000): full → drop oldest, newest wins; publish is loop-affine
    (async callers only; thread callers must forward via loop.call_soon_threadsafe).
    Process singleton built lazily by sinks.ensure_bus (configure rebuilds it).
    """

    def __init__(self):
        self._sinks: list[asyncio.Queue[dict]] = []
        self._tasks: list[asyncio.Task] = []

    @property
    def enabled(self) -> bool:
        return bool(self._sinks)

    def add(self, consume) -> None:
        """Register a sink consumer coroutine.

        Args:
            consume: Coroutine `async def consume(evt: dict) -> None`.
        """
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=5000)
        self._sinks.append(q)
        self._tasks.append(asyncio.create_task(self._drain(consume, q)))

    async def _drain(self, consume, q) -> None:
        """Send-queue drain coroutine; sink failures only log to stderr, never bubble to callers."""
        while True:
            evt = await q.get()
            try:
                await consume(evt)
            except Exception as exc:  # sink 失败只报 stderr,绝不冒泡到调用方
                _log(f"sink 消费失败: {type(exc).__name__}: {exc}")

    def publish(self, evt: dict) -> None:
        """Fan out one event to every sink queue (never blocks the caller).

        Args:
            evt: Event dict to deliver to all sinks.
        """
        if not self._sinks:
            return
        for q in self._sinks:
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                q.get_nowait()  # 有界队列:丢最旧,新事件优先
                q.put_nowait(evt)

    def shutdown(self) -> None:
        """Cancel all sink tasks (bounded-queue drains); no await, callers own reaping."""
        for task in self._tasks:
            task.cancel()
