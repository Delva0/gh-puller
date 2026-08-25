"""LLM 调用适配器:SDK/HTTP 对象 → 事件溯源事件 dict 的归一化与调用包装。

外部只经本层函数调用(经 gh_puller.agent __init__ 再导出),不再直接触碰
ClaudeSDKClient / httpx(无感,对外语义不变):
- cc_stream / cc_text / cc_result:Claude Code(SDK)调用。文本增量 StreamEvent
  `text_delta` 优先、AssistantMessage 兜底(仅在未产出任何增量时)、
  ResultMessage.is_error → RuntimeError("agent 执行失败: ...") —— 与 deepwiki
  原 `_agent_stream` 漏斗逐字节一致;thinking/工具增量只进监控事件流,不改变产出。
- llm_complete / llm_stream:OpenAI 兼容端点(httpx);异常原样抛,重试留给调用方。

事件语义(对齐 deepseek-harness 事件溯源模型,规范见 gh_puller.agent.events):
单次运行一个 session(流式事件流内 seq 从 0 连续);进入即 session/start →
(context:* 说明事件)→ turn/start → step/start → user/message →
request/header(cc 路径 partial=true:SDK 不暴露请求体,system/tools 只能取调用方
options)→ 逐次 assistant/chunk → assistant/message(+ 工具则 tool/call /
tool/result)→ ... → step[end] → session/end。上下文每时每刻可恢复:折叠 surface
前缀(见 events.py 模块规范),任意请求平面 X = 该 step 首条 assistant/chunk 的 seq。

会话 id 默认 <ns>/<uuid4>(ns 归上层业务定:显式 session_ns → run_id →
session_name → "agent"),见 _session_id;文件侧只落非流式事件流
(NON_STREAM_TYPES,逐行跳过 assistant/chunk → 文件 seq 有洞,契约见 events.py)。

管线:适配器归一化 SDK/HTTP 对象 → 事件 dict → EventBus 扇出(sinks.EventBus,
publish 仅 put_nowait 到每 sink 的 asyncio.Queue,永不阻塞调用)→ sink worker 消费。
"""

import json
import time
import uuid

import httpx

from .events import new_event
from .sinks import ensure_bus

# ---------------------------------------------------------------------------
# 事件发布器(适配器共用):信封/turn/step/seq
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


class _Run:
    """单次运行的事件发布器:维护会话信封/turn/step/seq 计数,归一化后广播事件。"""

    def __init__(self, session: str, provider: str, model: str, *, label: str | None = None,
                 run_id: str | None = None, meta=None):
        self.session = session
        self.label = label or session
        self.provider = provider
        self.model = model
        self.run_id = run_id
        self.meta = meta
        self.seq = 0
        self.turn = 1  # 每 run 一个 dsh-style turn:单一用户消息 → 最终回答
        self.step = 1  # 一次 LLM 请求 = 一个 step;工具结果后的新请求 +1
        self.text_chars = 0
        self.t0 = time.monotonic()  # run 起点(与 start() 方法分名)
        self.tool_names: dict[str, str] = {}  # tool_use_id → 工具名(tool/result 归一化用)
        self._tool_pending = False  # 本轮工具结果已发 → 下个 assistant 消息段开新 step
        self._active_tool_use: dict[int, str] = {}  # 块 index → tool_use_id
        self._tool_use_pieces: dict[int, list[str]] = {}  # 块 index → input_json_delta 碎片
        self._tool_result: dict | None = None
        self._chunk_seqs: list[int] = []  # 本 step 的 assistant/chunk seq(消息 sourceSeqs)
        self._call_seqs: dict[str, int] = {}  # callId → tool/call 的 seq
        self._step_open = False
        self._ended = False
        self._reason: str | None = None  # error 事件后供 session/end.reason 使用
        self.result_usage: dict | None = None
        self.result_stop_reason: str | None = None
        self.result_cost_usd: float | None = None

    def event(self, evt_type: str, **data) -> dict | None:
        """造信封并发布;返回事件(seq 已分配);无 sink 时返回 None(零开销短路)。"""
        bus = ensure_bus()
        if not bus.enabled:
            return None
        evt = new_event(evt_type, **data)
        evt["session"] = self.session
        evt["run_id"] = self.run_id
        evt["label"] = self.label
        evt["provider"] = self.provider
        evt["model"] = self.model
        evt["seq"] = self.seq
        self.seq += 1
        bus.publish(evt)
        return evt

    def start(self, *, context: list[dict] | None = None, retry: dict | None = None) -> None:
        """运行进入:session/start(带 retry 元数据)→ context 说明事件 → turn/start → step/start。"""
        self.event("session/start", run_id=self.run_id, label=self.label, provider=self.provider,
                   model=self.model, retry=retry, meta=self.meta)
        for ctx in context or []:
            self.event(ctx["type"], **ctx["data"])  # 日志型说明事件:重放于 turn 之前
        self.event("turn/start", turn=self.turn)
        self.event("step/start", turn=self.turn, step=self.step)
        self._step_open = True

    def step_boundary(self) -> None:
        """上一步完成、新一步开始(工具结果后新一轮 LLM 请求);本 step 增量清空。"""
        if self._stepping():
            self.event("step/end", turn=self.turn, step=self.step)
        self.step += 1
        self._chunk_seqs = []
        self._step_open = True
        self.event("step/start", turn=self.turn, step=self.step)

    def user_message(self, message: dict, *, source: dict | None = None,
                     surface_op: str | dict = "append") -> None:
        """user/message surface 事件(source 缺省 human 用户)。"""
        if source is None:
            source = {"kind": "user"}
        self.event("user/message", turn=self.turn, step=self.step, message=message,
                   source=source, surfaceOp=surface_op)

    def chunk(self, chunk: dict) -> None:
        """assistant/chunk 原始增量;seq 记入本 step 的 sourceSeqs。"""
        evt = self.event("assistant/chunk", turn=self.turn, step=self.step, chunk=chunk)
        if evt is not None:
            self._chunk_seqs.append(evt["seq"])

    def text(self, text: str, *, index: int = 0) -> None:
        self.text_chars += len(text)
        self.chunk({"type": "text", "index": index, "text": text})

    def tool_call(self, call_id: str, name: str | None, arguments: str) -> None:
        evt = self.event("tool/call", turn=self.turn, step=self.step, callId=call_id,
                         name=name, arguments=arguments)
        if evt is not None:
            self._call_seqs[call_id] = evt["seq"]

    def tool_result(self, message: dict, *, call_id: str, name: str | None,
                    is_error: bool, src_seq: int | None = None) -> None:
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

    def finish(self, ok: bool) -> None:
        """finally 兜底:step/end → turn/end → session/end(幂等)。"""
        if self._ended:
            return
        self._ended = True
        state = "completed" if ok else "aborted"
        data = {"state": state, "ok": ok,
                "duration_ms": int((time.monotonic() - self.t0) * 1000),
                "text_chars": self.text_chars, "num_steps": self.step}
        for k, v in (("usage", self.result_usage), ("stop_reason", self.result_stop_reason),
                     ("total_cost_usd", self.result_cost_usd)):
            if v is not None:
                data[k] = v
        if not ok and self._reason:
            data["reason"] = self._reason
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


# ---------------------------------------------------------------------------
# 适配器通用:SDK/HTTP 对象 → 事件 dict(纯 dict 可单测)
# ---------------------------------------------------------------------------


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
        "input_tokens": _norm_token(u, ("input_tokens", "prompt_tokens")),
        "output_tokens": _norm_token(u, ("output_tokens", "completion_tokens")),
        "cache_read_input_tokens": _norm_token(u, ("cache_read_input_tokens",)),
    }


def _stage_of(exc: Exception) -> str:
    """error 事件 stage 分类:http(网络/状态码)/ parse(响应结构)/ run(其余)。"""
    if isinstance(exc, httpx.HTTPError):
        return "http"
    if isinstance(exc, (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError)):
        return "parse"
    return "run"


def _handle_stream_event(run: _Run, event: dict) -> None:
    """归一化 SDK 原始流事件(cursor 型)→ 监控事件;不改动文本产出路径(产出在 cc_stream)。

    映射:content_block_delta → assistant/chunk(原始增量,含 thinking/tool_input);
    tool_use 收尾 → tool/call(原始 arguments JSON 字符串);tool_result 收尾 →
    tool/result(全量内容);message_start 且工具结果后 → step 边界。
    """
    typ = event.get("type")
    if typ == "message_start":
        if run._tool_pending and (event.get("message") or {}).get("role") == "assistant":
            run.step_boundary()
            run._tool_pending = False
        return
    if typ == "content_block_start":
        cb = event.get("content_block") or {}
        idx = event.get("index", -1)
        btype = cb.get("type")
        if btype == "tool_use":
            tid = cb.get("id") or ""
            run.tool_names[tid] = cb.get("name") or ""
            run._active_tool_use[idx] = tid
            run._tool_use_pieces[idx] = []
        elif btype == "tool_result":
            run._tool_pending = True
            run._tool_result = {
                "id": cb.get("tool_use_id") or "", "pieces": [], "is_error": bool(cb.get("is_error")),
            }
        return
    if typ == "content_block_delta":
        delta = event.get("delta") or {}
        dtype = delta.get("type")
        idx = event.get("index", -1)
        if run._tool_result is not None and dtype == "text_delta":  # 工具结果内容归属 tool/result
            run._tool_result["pieces"].append(delta.get("text") or "")
            return
        if dtype == "text_delta":
            run.text(delta.get("text") or "", index=idx)
            return
        if dtype == "thinking_delta":
            run.chunk({"type": "thinking", "index": idx, "text": delta.get("thinking") or ""})
            return
        if dtype == "input_json_delta":
            piece = delta.get("partial_json") or ""
            run._tool_use_pieces.setdefault(idx, []).append(piece)
            run.chunk({"type": "tool_input", "index": idx, "partial_json": piece})
        return
    if typ == "content_block_stop":
        idx = event.get("index", -1)
        if run._tool_result is not None:
            text = "".join(run._tool_result["pieces"])
            tid = run._tool_result["id"]
            run.tool_result(
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": text,
                     "is_error": run._tool_result["is_error"]},
                ]},
                call_id=tid, name=run.tool_names.get(tid),
                is_error=run._tool_result["is_error"], src_seq=run._call_seqs.get(tid),
            )
            run._tool_result = None
            return
        if idx in run._active_tool_use:
            tid = run._active_tool_use[idx]
            raw = "".join(run._tool_use_pieces.get(idx, []))
            run.tool_call(tid, run.tool_names.get(tid), raw)
            run._active_tool_use.pop(idx, None)
            run._tool_use_pieces.pop(idx, None)
            return
        return  # text/thinking 收尾:增量已逐条事件化(块型由 chunk.content 索引决定)


def _handle_assistant_message(run: _Run, msg, already_yielded: bool) -> None:
    """整块消息:未产出增量时 text 增量一次并事件化;此后发全量 assistant/message。

    sourceSeqs = 本 step 已发 chunk 的 seq;文本/思考/tool_use 块全量入 message;
    无流事件的 tool_use 兜底补合成 tool/call(流路径已由 content_block_stop 发射)。
    """
    content = []
    for b in msg.content:
        t = getattr(b, "type", None)
        if t == "text":
            text = getattr(b, "text", None) or ""
            if text and not already_yielded:
                run.text(text)
            content.append({"type": "text", "text": text})
        elif t == "thinking":
            content.append({"type": "thinking", "thinking": getattr(b, "thinking", None) or ""})
        elif t == "tool_use":
            entry = {"type": "tool_use", "id": getattr(b, "id", None) or "",
                     "name": getattr(b, "name", None) or ""}
            if getattr(b, "input", None) is not None:
                entry["input"] = b.input
            content.append(entry)
    run.event(
        "assistant/message", turn=run.turn, step=run.step,
        message={"role": "assistant", "content": content},
        usage=_normalize_usage(getattr(msg, "usage", None)),
        stop_reason=getattr(msg, "stop_reason", None),
        surfaceOp="append", sourceSeqs=list(run._chunk_seqs),
    )
    for block in msg.content:  # 兜底:无 input_json_delta 的 SDK 路径
        if getattr(block, "type", None) != "tool_use":
            continue
        tid = getattr(block, "id", None) or ""
        if tid and tid not in run._call_seqs:
            run.tool_call(tid, getattr(block, "name", None),
                          json.dumps(getattr(block, "input", None) or {}))


def _options_header(options) -> dict:
    """ClaudeAgentOptions → request/header 的 header 快照(partial 语义见调用方)。

    SDK 不暴露 rendered system / resolved 工具 schema,只取调用方 options:
    system_prompt 全量、工具名清单(allowed_tools + mcp 前缀),tools 无 schema。
    """
    system = getattr(options, "system_prompt", None) or ""
    names = list(getattr(options, "allowed_tools", None) or [])
    names.extend(f"mcp__{name}__" for name in (getattr(options, "mcp_servers", None) or {}))
    return {"config": {"provider": "claude", "model": getattr(options, "model", None) or ""},
            "system": system or None, "tools": [{"name": n} for n in names] or None}


def _llm_header(payload: dict) -> dict:
    """OpenAI payload → request/header 的 header 快照(请求体全可见 → 精确)。

    system 消息并入 system 字符串;tools 归一 {name, description?, input_schema?};
    config 透传 model 与常用标量(JSON 可序列化)。
    """
    system = "\n\n".join(str(m.get("content") or "")
                         for m in payload.get("messages", []) if m.get("role") == "system")
    tools: list[dict] = []
    for t in payload.get("tools") or []:
        f = t.get("function") or t
        tools.append({"name": f.get("name") or "", "description": f.get("description"),
                      "input_schema": f.get("input_schema") or f.get("parameters")})
    config: dict = {"provider": "openai", "model": payload.get("model")}
    for k in ("temperature", "max_tokens", "top_p", "response_format"):
        if payload.get(k) is not None:
            config[k] = payload[k]
    return {"config": config, "system": system or None, "tools": tools or None}


def _llm_emit_messages(run: _Run, payload: dict) -> None:
    """payload messages → surface 事件(仅 user/assistant 折叠;system 已入 header)。"""
    for m in payload.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant"):
            continue
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
        message = {"role": role, "content": blocks}
        if role == "user":
            run.user_message(message)
        else:  # 历史 assistant 消息:无 usage/停止原因,仅内容折叠
            run.event("assistant/message", turn=run.turn, step=run.step, message=message,
                      surfaceOp="append")


# ---------------------------------------------------------------------------
# Claude Code(SDK)包装:cc_stream / cc_text / cc_result
# ---------------------------------------------------------------------------


async def cc_stream(
    options, prompt: str, *, session: str | None = None,
    session_name: str | None = None, run_id: str | None = None,
    session_ns: str | None = None,
    context: list[dict] | None = None, retry: dict | None = None,
    meta: dict | None = None,
):
    """Claude Code 流式应答(监控 + 执行)。

    对外产出:文本增量(StreamEvent text_delta 优先,AssistantMessage 兜底,
    ResultMessage.is_error → RuntimeError("agent 执行失败: ..."))—— 与 deepwiki
    原漏斗语义一致;thinking/工具增量仅进事件流。options 整体透传(调用方自组装,
    如 deepwiki 的进程内 MCP 闭包)。context = 上下文说明事件列表
    (context/inject|modify,{type,data} 形),重放于 session/start 之后。
    """
    from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, StreamEvent

    run = _Run(
        _session_id(session, session_ns, run_id, session_name), "claude",
        getattr(options, "model", None) or "",
        label=session_name, run_id=run_id, meta=meta,
    )
    run.start(context=context, retry=retry)
    run.user_message({"role": "user", "content": [{"type": "text", "text": prompt}]})
    run.event("request/header", header=_options_header(options), reason="initial", partial=True)
    yielded = False
    ok = False
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    _handle_stream_event(run, msg.event)
                    event = msg.event
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yielded = True
                            yield delta["text"]
                elif isinstance(msg, AssistantMessage):
                    _handle_assistant_message(run, msg, yielded)
                    if not yielded:
                        # 兜底:无 partial 事件时整块取文本(ThinkingBlock 无 text 属性,天然跳过)
                        for block in msg.content:
                            text = getattr(block, "text", None)
                            if text:
                                yield text
                elif isinstance(msg, ResultMessage):
                    if msg.is_error:
                        detail = (msg.errors or [])[-1] if msg.errors else msg.result
                        raise RuntimeError(f"agent 执行失败: {detail or msg.subtype}")
                    run.result_meta(msg)
            ok = True
    except Exception as exc:
        run.error(exc, "run")  # error 事件 + session/end.reason 一并落
        raise
    finally:
        run.finish(ok)


async def cc_text(options, prompt: str, *, session: str | None = None,
                  session_name: str | None = None, run_id: str | None = None,
                  session_ns: str | None = None,
                  context: list[dict] | None = None, retry: dict | None = None,
                  meta: dict | None = None) -> str:
    """agent 整收应答(流式转整收,监控走与 cc_stream 同一条路径)。"""
    parts: list[str] = []
    async for chunk in cc_stream(options, prompt, session=session, session_name=session_name,
                                 run_id=run_id, session_ns=session_ns,
                                 context=context, retry=retry, meta=meta):
        parts.append(chunk)
    return "".join(parts)


async def cc_result(options, prompt: str, *, session: str | None = None,
                    session_name: str | None = None, run_id: str | None = None,
                    session_ns: str | None = None,
                    context: list[dict] | None = None, retry: dict | None = None,
                    meta: dict | None = None) -> str:
    """非流式取最终结果(judge 用):失败或无结果 → RuntimeError(调用方降级)。"""
    from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, StreamEvent

    run = _Run(
        _session_id(session, session_ns, run_id, session_name), "claude",
        getattr(options, "model", None) or "",
        label=session_name, run_id=run_id, meta=meta,
    )
    run.start(context=context, retry=retry)
    run.user_message({"role": "user", "content": [{"type": "text", "text": prompt}]})
    run.event("request/header", header=_options_header(options), reason="initial", partial=True)
    ok = False
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    _handle_stream_event(run, msg.event)
                elif isinstance(msg, AssistantMessage):
                    _handle_assistant_message(run, msg, already_yielded=run.text_chars > 0)
                elif isinstance(msg, ResultMessage):
                    if msg.is_error:
                        detail = (msg.errors or [])[-1] if msg.errors else msg.result
                        raise RuntimeError(f"agent 执行失败: {detail or msg.subtype}")
                    run.result_meta(msg)
                    result = msg.result
                    if not result:
                        raise RuntimeError("agent 未产出最终结果")
                    ok = True
                    return result
            raise RuntimeError("agent 未产出最终结果")
    except Exception as exc:
        run.error(exc, "run")
        raise
    finally:
        run.finish(ok)


# ---------------------------------------------------------------------------
# OpenAI 兼容(httpx)包装:llm_complete / llm_stream
# ---------------------------------------------------------------------------


def _llm_headers(headers: dict | None, api_key: str | None) -> dict:
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    if api_key:
        hdrs.setdefault("Authorization", f"Bearer {api_key}")
    return hdrs


async def llm_complete(
    *, url: str, payload: dict, api_key: str | None = None,
    timeout: httpx.Timeout | None = None, headers: dict | None = None,
    session: str | None = None, session_name: str | None = None, run_id: str | None = None,
    session_ns: str | None = None,
    context: list[dict] | None = None, retry: dict | None = None, meta: dict | None = None,
) -> str:
    """OpenAI 兼容非流式补全(异常原样抛,重试留给调用方)。

    payload 为 chat/completions 请求体(须含 model/messages;其余键原样透传,
    如 response_format/temperature/max_tokens —— 兼容题库扩展点),HTTP body 与直连一致。
    事件:payload 全量消息 → surface(可折叠恢复该请求输入);响应当次
    text 增量 + assistant/message + 每 tool_call 一个 tool/call(原始 arguments 字符串)。
    """
    model = payload["model"]
    run = _Run(_session_id(session, session_ns, run_id, session_name), "openai", model,
               label=session_name, run_id=run_id, meta=meta)
    run.start(context=context, retry=retry)
    _llm_emit_messages(run, payload)
    run.event("request/header", header=_llm_header(payload), reason="initial")
    run.event("request/context", provider="openai", model=model)
    ok = False
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{url}/chat/completions", json=payload, headers=_llm_headers(headers, api_key))
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"] or {}
        content = msg.get("content") or ""
        usage = data.get("usage") or {}
        if content:
            run.text(content)
        blocks = [{"type": "text", "text": content}] if content else []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments") or ""
            try:
                parsed = json.loads(args) if args else None
            except json.JSONDecodeError:
                parsed = args
            blocks.append({"type": "tool_use", "id": tc.get("id"),
                           "name": fn.get("name") or "", "input": parsed})
            run.tool_call(tc.get("id") or "", fn.get("name"), args)
        norm_usage = _normalize_usage(usage)
        run.event(
            "assistant/message", turn=run.turn, step=run.step,
            message={"role": "assistant", "content": blocks},
            usage=norm_usage, stop_reason=msg.get("stop_reason"),
            surfaceOp="append", sourceSeqs=list(run._chunk_seqs),
        )
        run.result_usage = norm_usage
        run.result_stop_reason = msg.get("stop_reason")
        ok = True
        return content
    except Exception as exc:
        run.error(exc, _stage_of(exc))
        raise
    finally:
        run.finish(ok)


async def llm_stream(
    *, url: str, payload: dict, api_key: str | None = None,
    timeout: httpx.Timeout | None = None, headers: dict | None = None,
    session: str | None = None, session_name: str | None = None, run_id: str | None = None,
    session_ns: str | None = None,
    context: list[dict] | None = None, retry: dict | None = None, meta: dict | None = None,
):
    """OpenAI 兼容流式补全(SSE 逐 delta,预留接口):payload 语义同 llm_complete,附加 stream=True。"""
    run = _Run(_session_id(session, session_ns, run_id, session_name), "openai",
               payload["model"], label=session_name, run_id=run_id, meta=meta)
    run.start(context=context, retry=retry)
    _llm_emit_messages(run, payload)
    run.event("request/header", header=_llm_header(payload), reason="initial")
    run.event("request/context", provider="openai", model=payload["model"])
    ok = False
    full = ""
    try:
        body: dict = {**payload, "stream": True}
        async with httpx.AsyncClient(timeout=timeout) as client, client.stream(
            "POST", f"{url}/chat/completions", json=body, headers=_llm_headers(headers, api_key)
        ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if not payload or payload == "[DONE]":
                        break
                    choices = json.loads(payload).get("choices") or []
                    text = (choices[0].get("delta") or {}).get("content") or ""
                    if text:
                        full += text
                        run.text(text)
                        yield text
        run.event(
            "assistant/message", turn=run.turn, step=run.step,
            message={"role": "assistant", "content": [{"type": "text", "text": full}]},
            surfaceOp="append", sourceSeqs=list(run._chunk_seqs),
        )
        ok = True
    except Exception as exc:
        run.error(exc, _stage_of(exc))
        raise
    finally:
        run.finish(ok)
