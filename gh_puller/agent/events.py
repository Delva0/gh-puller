"""agent 调用的可观测事件模型(事件溯源式;纯 dict 实现,零 SDK 依赖)。

对齐 deepseek-harness 的核心不变量:无损 append-only 事件日志,seq 每 session 从 0
连续单调(**流式事件流内稠密**;非流式投影侧允许洞,见下);LLM messages 上下文
是 surface 节点的派生(折叠)而非快照:

事件流按粒度分为两级:
- 流式事件流(agent 事件流)= TAXONOMY 全集(STREAM_TYPES):含 assistant/chunk
  原始增量,可还原实时(逐字)上下文;WS/OTel 通道承载;
- 非流式事件流 = TAXONOMY − {assistant/chunk}(NON_STREAM_TYPES):message 粒度,
  assistant/message 已是定型全量,可还原任意时刻的消息上下文;filesink 只落此级。

seq 序列是流式事件流的稠密序号;文件侧(非流式投影)按行跳过 chunk,因此
**文件内 seq 允许洞,洞 = 被跳过的 assistant/chunk**。折叠契约只做 seq 排序
与 seq < x 比较,不要求稠密 —— 读者(含前端)不得假定文件 seq 连续。
- surface 事件 user/message / assistant/message / tool/result 携带全量消息与
  surfaceOp(append 或 {op:'replace', start, end});
- config/init(ignorable 日志型)= **构造期初始配置快照**,在 session/start 之前打印
  (对应 self._client 的初始配置;api_key/token/base_url 等凭证与端点面剥离,不入流;
  SDK 装配对象如 mcp.server.Server 实例折叠为类型名,见 _jsonable);
- context/modify 是上下文修改的解释事件(日志型,不折动),折叠正确性与它无关 ——
  丢弃也只是少了解释,不会误解消息历史;
- **会话保活不再走事件**:session 活着但静默时的"保鲜"由 generators.base._guard 的
  keep-warm 定时器调 sinks.touch(session) 直触文件 mtime(只动时间、不加行),
  监测端(agent-monitor hub)借"无终态行且 mtime 静止超租约"判定会话已死 ——
  本文件不再有 session/heartbeat 事件(已从模型的静默补发事件里移除)。

折叠恢复规范(与 ui/src/monitor/surface.ts 同份语义,本模块不落 Python 实现,
契约测试见 tests/test_event_taxonomy.py):
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
# filesink 只落非流式事件流(NON_STREAM_TYPES);WS/OTel 通道承载完整流式事件流。
STREAM_TYPES = TAXONOMY
NON_STREAM_TYPES = TAXONOMY - {"assistant/chunk"}

# ignorable 日志型事件:读者可安全跳过(不影响消息派生/请求重建);缺失 ignorable
# 标记的未知类型 → 必须可解析(读者应报错,防静默丢消息)
LOG_TYPES = frozenset({"config/init", "context/modify"})


def type_of(evt: dict) -> str:
    """返回事件 type;未知类型 → ValueError。"""
    t = evt.get("type")
    if t not in TAXONOMY:
        raise ValueError(f"未知事件 type: {t!r}")
    return t


def new_event(evt_type: str, **data) -> dict:
    """构造事件信封:补 id/ts,type/data 分组,校验 type 与字段可 JSON 序列化。

    封套身份仅 session + seq(接收端按 session 归组、seq 排序);
    run_id/label/generator/model/retry/meta 是 session/start 的**快照**,不逐事件携带;
    surface 事件强制校验 message + surfaceOp(防适配器漏字段导致折叠不可复现)。
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
    """返回 (原长度, 预览):超长截断加省略号;None/空 → (0, "")。

    仅用于 OTel 等第三方通道的预览属性;监控事件本身全量不截断。
    """
    if not text:
        return (0, "")
    if len(text) <= n:
        return (len(text), text)
    return (len(text), text[:n] + "…")


def _jsonable(value):
    """事件载荷降级(递归):dict/list 透传,标量原样,其余对象折叠为 `<类型名>`。

    config/init 快照只作观测 —— SDK 装配对象(mcp_servers 里的 mcp.server.Server
    实例等)以类型名占位入流、不落实例,保证 new_event 的 JSON 校验在构造期即
    通过(而非主路径抛 "not JSON serializable" 打死交付)。
    """
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
    """会话 id:显式 session 原样;否则 <ns>/<uuid4>(ns 由上层业务决定分类命名空间)。

    ns 解析序:显式 session_ns 参数 → run_id → session_name → "agent";
    会话 id 形如 judge:llm/0460e1e9-5155-4014-9054-a39986462b20 —— grep
    session/start 的 session 字段即知来源;文件名只取 "/" 后段(见 FileSink)。
    """
    if session:
        return session
    ns = session_ns or run_id or session_name or "agent"
    return f"{ns}/{uuid.uuid4()}"


def _norm_token(u, keys: tuple[str, ...]):
    """从 SDK 对象/字典取值(映射 prompt/completion_tokens 命名)的通用取值器。"""
    for k in keys:
        v = getattr(u, k, None)
        if v is None and isinstance(u, dict):
            v = u.get(k)
        if v is not None:
            return v
    return None


def _normalize_usage(u) -> dict | None:
    """SDK/HTTP usage → 统一结构 {input_tokens, output_tokens, cache_read_input_tokens}。"""
    if not u:
        return None
    return {
        "input_tokens": _norm_token(u, ("input_tokens", "prompt_tokens", "inputTokens")),
        "output_tokens": _norm_token(u, ("output_tokens", "completion_tokens", "outputTokens")),
        "cache_read_input_tokens": _norm_token(u, ("cache_read_input_tokens", "cacheReadTokens")),
    }


_active_bus: "EventBus | None" = None  # 由 sinks.configure/ensure_bus 单向设置(events 层零反向依赖)


def set_active_bus(bus: "EventBus | None") -> None:
    """设置当前总线(仅观测通道层调用);None = 无通道(事件发布零开销短路)。"""
    global _active_bus
    _active_bus = bus


def _ensure_maybe_bus() -> "EventBus | None":
    """录前自足:监控总线未建则懒建(任意会话的首次事件触达);返回当前总线或 None。

    机制归事件层 —— 宿主 app / 生成器层无需 ensure_bus,任何会话记录即自动挂接
    FileSink(恒开)+ 可达 WS/OTel。懒 import 保持本模块**导入期**纯 stdlib/零 sinks
    (ensure_bus 须在运行中的事件循环内调用,event() 恒在 loop 内)。幂等:建后即短路。
    """
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
    """单次运行的事件发布器:维护会话信封/turn/step/seq 计数,归一化后广播事件。"""

    def __init__(self, session: str, *, generator: str = "",
                 label: str | None = None,
                 run_id: str | None = None, meta=None):
        self.session = session
        self.label = label or session
        self.generator = generator
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
        """造信封并发布;返回事件(seq 已分配);无 sink 时返回 None(零开销短路)。"""
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
        """构造期初始配置快照(config/init;凭证/端点面剥离;须在 start() 之前打印)。

        对应 self._client 的初始配置 —— config 的观测面统一在此,不再附着
        session/start 与 turn/start(身份 = session id;元数据快照见本事件)。
        SDK 装配对象(如 mcp_servers 的 mcp.server.Server 实例)经 _jsonable 折叠
        为类型名 —— 快照只看装配形态,不落实例,不炸 JSON 校验。
        """
        creds = ("api_key", "token", "base_url")
        self.event("config/init",
                   config=_jsonable({k: v for k, v in config.items() if k not in creds}))

    # ------------------------------------------------------------------
    # 会话保活(fs touch 保鲜;会话记录器原生能力):agent 静默期不动事件流,只触文件
    # mtime —— 供监控端(hub)按"mtime 静止超租约"区分"活着但静默"与"进程已死"。
    # 不再发 heartbeat 事件;cadence 由调用方注入(零 env 依赖);触达经 sinks.touch
    # (touch 是 FileSink 的职责,见 sinks.py)。
    # ------------------------------------------------------------------

    def start_keepwarm(self, interval: float) -> None:
        """run 进行中启动保鲜定时器(cadence = interval 秒);幂等。

        仅当 interval>0 且有活跃监控总线时才建任务(否则无可保鲜对象,避免空转)。
        finish 前由 stop_keepwarm 取消(await 保证退场);_keepwarm_loop 只真睡
        `asyncio.sleep(interval)`,每拍对会话文件 os.utime,绝不 busy-loop。
        """
        if self._keepwarm_task is not None or interval <= 0 or _active_bus is None:
            return
        self._keepwarm_interval = interval
        self._keepwarm_task = asyncio.create_task(self._keepwarm_loop(interval))

    async def stop_keepwarm(self) -> None:
        """取消并等待保鲜任务退场(幂等);finish 前调用,保证 session/end 恒末行。

        取消在 run 生命周期(而非依赖 session/end 行送达)完成——即便该行被有界
        队列挤掉,保鲜也已停,mtime 才可冻结交 hub 租约判死。
        """
        task = self._keepwarm_task
        self._keepwarm_task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _keepwarm_loop(self, interval: float) -> None:
        """每 sleep(interval) 经 sinks.touch(session) 对会话文件触一次 mtime。

        touch 是文件 sink 的职责(见 sinks.FileSink.touch),本层只做节奏与自停;
        懒 import 避免 events→sinks 的导入期耦合(运行期才触达)。无静默判断 ——
        os.utime 便宜,会话开着就保鲜;事件密时 mtime 本就新鲜,多触一次无副作用。
        总线被关闭(configure)后 touch 扇出为空,自然 no-op。
        """
        from .sinks import touch

        while True:
            await asyncio.sleep(interval)
            await touch(self.session)

    def start(self, *, context: list[dict] | None = None, retry: dict | None = None,
              prologue: bool = True) -> None:
        """运行进入:session/start(带 retry 元数据)→ context 说明事件 → turn/start → step/start。

        prologue=False(dsh 投影路径专用):turn/step 生命周期由 dsh 原生事件自带,
        不合成 turn/start + step/start,且 _step_open 保持 False(dsh 路径不调 step_boundary)。
        """
        self.event("session/start", run_id=self.run_id, label=self.label, generator=self.generator,
                   retry=retry, meta=self.meta)
        for ctx in context or []:
            self.event(ctx["type"], **ctx["data"])  # 日志型说明事件:重放于 turn 之前
        if prologue:
            self.event("turn/start", turn=self.turn)
            self.event("step/start", turn=self.turn, step=self.step)
            self._step_open = True

    def step_boundary(self) -> None:
        """上一步完成、新一步开始(工具结果后新一轮 LLM 请求);本 step 增量清空。"""
        if self._stepping():
            self.event("step/end", turn=self.turn, step=self.step)
        self.step += 1
        self._chunk_seqs = []
        self._msg_reset()  # 新 LLM 轮:旧消息缓冲(若有残留)清空
        self._step_open = True
        self.event("step/start", turn=self.turn, step=self.step)

    def _msg_reset(self) -> None:
        """本 assistant 消息增量缓冲清零(消息标记/新消息起点处调用)。"""
        self._msg_text = ""
        self._msg_thinking = ""
        self._msg_tool_calls = []
        self._msg_stop_reason = None

    def user_message(self, message: dict, *, source: dict | None = None,
                     surface_op: str | dict = "append") -> None:
        """user/message surface 事件(source 缺省 human 用户)。"""
        if source is None:
            source = {"kind": "user"}
        self.event("user/message", turn=self.turn, step=self.step, message=message,
                   source=source, surfaceOp=surface_op)

    def chunk(self, chunk: dict) -> None:
        """assistant/chunk 原始增量;seq 记入本 step 的 sourceSeqs。

        段型 schema:{"type": "thinking"|"content"|"tool_call"(|"plan"), "index": 段序,
        "text": 文本增量}(type 已定段名,文本字段统一 text;tool_call 负载为 partial_json)。
        """
        evt = self.event("assistant/chunk", turn=self.turn, step=self.step, chunk=chunk)
        if evt is not None:
            self._chunk_seqs.append(evt["seq"])
        if chunk.get("type") == "thinking":
            self._msg_thinking += chunk.get("text", "")

    def text(self, text: str, *, index: int = 0) -> None:
        """content 段便捷发射:text_chars 只累计 content(thinking/tool_input 不计)。"""
        self.text_chars += len(text)
        self._msg_text += text
        self.chunk({"type": "content", "index": index, "text": text})

    def tool_call(self, call_id: str, name: str | None, arguments: str) -> None:
        """tool/call 事件:callId 为 wire 键;seq 记入 _call_seqs,供 tool_result 回填 sourceSeqs。"""
        evt = self.event("tool/call", turn=self.turn, step=self.step, callId=call_id,
                         name=name, arguments=arguments)
        if evt is not None:
            self._call_seqs[call_id] = evt["seq"]
        self._msg_tool_calls.append((call_id, name, arguments))

    def tool_result(self, message: dict, *, call_id: str, name: str | None,
                    is_error: bool, src_seq: int | None = None) -> None:
        """tool/result surface 事件:callId 关联对应 tool/call,sourceSeqs = 该 tool/call 的 seq。

        src_seq 由调用方传入(通常 _call_seqs.get(call_id));None → 省略 sourceSeqs 键
        (无可溯源的 tool/call seq)。
        """
        data = {"turn": self.turn, "step": self.step, "message": message, "is_error": is_error,
                "surfaceOp": "append", "callId": call_id}
        if name:
            data["name"] = name
        if src_seq is not None:
            data["sourceSeqs"] = [src_seq]
        self.event("tool/result", **data)

    def result_meta(self, msg) -> None:
        """ResultMessage → session/end 汇总字段(usage/stop_reason/cost)。"""
        self.result_usage = _normalize_usage(getattr(msg, "usage", None))
        self.result_stop_reason = getattr(msg, "stop_reason", None)
        self.result_cost_usd = getattr(msg, "total_cost_usd", None)

    def finish(self, ok: bool, *, epilogue: bool = True) -> None:
        """finally 兜底:step/end → turn/end → session/end(幂等)。

        epilogue=False(dsh 投影路径专用):turn/step 终局事件已由 dsh 原生事件转发,
        只收尾 session/end(状态汇总字段组装不变)。
        """
        if self._ended:
            return
        self._ended = True
        # 防御:不经 _guard 的 stop_keepwarm 直接结束的路径,也取消保鲜任务(非 await;
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
        """error 事件(全量 message,不截断);session/end.reason 取首 2000 字符。"""
        self.event("error", stage=stage, exc_type=type(exc).__name__, message=str(exc))
        self._reason = str(exc)[:2000]

    def _stepping(self) -> bool:
        return self._step_open


class EventBus:
    """进程内异步事件总线:publish 只 put_nowait 到每 sink 队列,永不阻塞调用方;慢 sink 只拖 sink 自己。

    有界队列(5000)满 → 丢最旧、新事件优先;publish 为 loop-affine(v1 仅异步
    调用方,线程调用方须经 loop.call_soon_threadsafe 转发)。进程内单例,由
    sinks.ensure_bus 惰性构建(configure 关闭重建)。
    """

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
