"""LLM 调用适配器:SDK/HTTP 对象 → 事件流 dict 的归一化与调用包装。

外部只经本层函数调用(经 gh_puller.agent __init__ 再导出),不再直接触碰
ClaudeSDKClient / httpx(无感,对外语义不变):
- cc_stream / cc_text / cc_result:Claude Code(SDK)调用。文本增量 StreamEvent
  `text_delta` 优先、AssistantMessage 兜底(仅在未产出任何增量时)、
  ResultMessage.is_error → RuntimeError("agent 执行失败: ...") —— 与 deepwiki
  原 `_agent_stream` 漏斗逐字节一致;thinking/工具增量只进监控事件流,不改变产出。
- llm_complete / llm_stream:OpenAI 兼容端点(httpx);异常原样抛,重试留给调用方。

管线:适配器归一化 SDK/HTTP 对象 → 事件流 dict → EventBus 扇出(sinks.EventBus,
publish 仅 put_nowait 到每 sink 的 asyncio.Queue,永不阻塞调用)→ sink worker 消费。
事件语义见 gh_puller.agent.events(_Run 发布信封/seq/round,归一化见下方函数)。
"""

import json
import time
import uuid

import httpx

from .events import new_event, truncate
from .sinks import ensure_bus


# ---------------------------------------------------------------------------
# 事件发布器(适配器共用):信封/seq/round
# ---------------------------------------------------------------------------


class _Run:
    """单次运行的事件发布器:维护会话信封/seq/round 计数,归一化后广播事件。"""

    def __init__(self, session: str, provider: str, model: str, *, label: str | None = None, meta=None):
        self.session = session
        self.label = label or session
        self.provider = provider
        self.model = model
        self.meta = meta
        self.seq = 0
        self.round = 0  # 由适配器按 SDK 消息边界计:llm 单轮恒 0,agent 每段 assistant 产出 +1
        self.text_chars = 0
        self.start = time.monotonic()
        self.tool_names: dict[str, str] = {}  # tool_use_id → 工具名(tool.result 归一化用)
        self._tool_pending = False  # 本轮工具结果已发 → 下个 assistant 消息段开新 round
        self._active_tool_use: dict[int, str] = {}  # 块 index → tool_use_id
        self._tool_use_pieces: dict[int, list[str]] = {}  # 块 index → input_json_delta 碎片
        self._tool_result: dict | None = None

    def event(self, kind: str, **fields) -> None:
        bus = ensure_bus()
        if not bus.enabled:  # 零 sink 短路:不构造事件(无 uuid/json 开销)
            return
        evt = new_event(
            kind, session=self.session, label=self.label, provider=self.provider,
            model=self.model, seq=self.seq, round=self.round, **fields,
        )
        if self.meta is not None:
            evt["meta"] = self.meta
        self.seq += 1
        bus.publish(evt)

    def text(self, text: str) -> None:
        self.text_chars += len(text)
        self.event("text.delta", text=text)

    def finish(self, ok: bool) -> None:
        """finally 兜底的 run.end:ok 决定聚合器终态 completed/aborted。"""
        self.event(
            "run.end", ok=ok, text_chars=self.text_chars,
            duration_ms=int((time.monotonic() - self.start) * 1000), num_rounds=self.round + 1,
        )


# ---------------------------------------------------------------------------
# 适配器通用:SDK/HTTP 对象 → 事件流 dict(纯 dict 可单测)
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
    """归一化 SDK 原始流事件(cursor 型)→ 监控事件;不改动文本产出路径(产出在 cc_stream)。"""
    typ = event.get("type")
    if typ == "message_start":
        if run._tool_pending and (event.get("message") or {}).get("role") == "assistant":
            run.round += 1
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
            run.event("block.start", block_type="tool_use", tool_id=tid or None, tool_name=cb.get("name"))
        elif btype == "tool_result":
            run._tool_pending = True
            run._tool_result = {
                "id": cb.get("tool_use_id") or "", "pieces": [], "is_error": bool(cb.get("is_error")),
            }
        elif btype in ("text", "thinking"):
            run.event("block.start", block_type="content" if btype == "text" else "thinking")
        return
    if typ == "content_block_delta":
        delta = event.get("delta") or {}
        dtype = delta.get("type")
        if run._tool_result is not None and dtype == "text_delta":  # 工具结果内容归属 tool.result
            run._tool_result["pieces"].append(delta.get("text") or "")
            return
        if dtype == "text_delta":
            run.text(delta.get("text") or "")
            return
        if dtype == "thinking_delta":
            run.event("thinking.delta", text=delta.get("thinking") or "")
            return
        if dtype == "input_json_delta":
            run._tool_use_pieces.setdefault(event.get("index", -1), []).append(delta.get("partial_json") or "")
        return
    if typ == "content_block_stop":
        idx = event.get("index", -1)
        if run._tool_result is not None:
            text = "".join(run._tool_result["pieces"])
            tid = run._tool_result["id"]
            run.event(
                "tool.result", tool_id=tid or None, tool_name=run.tool_names.get(tid),
                is_error=run._tool_result["is_error"],
                content_chars=len(text), content_preview=truncate(text, 300)[1],
            )
            run._tool_result = None
            return
        if idx in run._active_tool_use:
            tid = run._active_tool_use[idx]
            raw = "".join(run._tool_use_pieces.get(idx, []))
            try:
                tool_input = json.loads(raw) if raw else None
            except json.JSONDecodeError:  # 碎片不完整(截流/异常):原样存文本
                tool_input = raw
            run.event(
                "block.stop", block_type="tool_use", tool_id=tid or None,
                tool_name=run.tool_names.get(tid), tool_input=tool_input,
            )
            run._active_tool_use.pop(idx, None)
            run._tool_use_pieces.pop(idx, None)
            return
        run.event("block.stop")  # text/thinking 收尾;块类型聚合器自持
        return


def _handle_assistant_message(run: _Run, msg, already_yielded: bool) -> None:
    """整块消息(无 partial 事件的兜底路径):未产出增量时 text.delta 一次并事件化。"""
    text = "".join(getattr(b, "text", None) or "" for b in msg.content)
    if text and not already_yielded:
        run.text(text)
    run.event(
        "message.assistant", text_chars=len(text),
        block_types=[getattr(b, "type", None) for b in msg.content],
        stop_reason=getattr(msg, "stop_reason", None),
        usage=_normalize_usage(getattr(msg, "usage", None)),
    )


def _handle_result_message(run: _Run, msg) -> None:
    run.event(
        "result", text_chars=len(msg.result or ""),
        duration_ms=int((time.monotonic() - run.start) * 1000),
        stop_reason=getattr(msg, "stop_reason", None),
        usage=_normalize_usage(getattr(msg, "usage", None)),
        total_cost_usd=getattr(msg, "total_cost_usd", None),
    )


def _options_meta(options) -> tuple[int, list[str]]:
    """ClaudeAgentOptions → (system_chars, 工具清单);monitoring 用,可容忍缺省。"""
    system = getattr(options, "system_prompt", None)
    tool_names = list(getattr(options, "allowed_tools", None) or [])
    tool_names.extend(f"mcp__{name}__" for name in (getattr(options, "mcp_servers", None) or {}))
    return (len(str(system or "")), tool_names)


def _llm_run_start(run: _Run, payload: dict) -> None:
    """OpenAI 兼容端点 run.start 归一化(payload 已含 model/messages,其余键透传)。"""
    messages = payload["messages"]
    last = messages[-1] if messages else {}
    run.event(
        "run.start",
        prompt_chars=sum(len(str(m.get("content") or "")) for m in messages),
        prompt_preview=truncate(last.get("content"), 500)[1],
        n_messages=len(messages),
        system_chars=sum(len(str(m.get("content") or "")) for m in messages if m.get("role") == "system"),
        tool_names=[],
    )


# ---------------------------------------------------------------------------
# Claude Code(SDK)包装:cc_stream / cc_text / cc_result
# ---------------------------------------------------------------------------


async def cc_stream(
    options, prompt: str, *, session: str | None = None,
    session_name: str | None = None, meta: dict | None = None,
):
    """Claude Code 流式应答(监控 + 执行)。

    对外产出:文本增量(StreamEvent text_delta 优先,AssistantMessage 兜底,
    ResultMessage.is_error → RuntimeError("agent 执行失败: ..."))—— 与 deepwiki
    原漏斗语义一致;thinking/工具增量仅进事件流。options 整体透传(调用方自组装,
    如 deepwiki 的进程内 MCP 闭包)。
    """
    from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, StreamEvent

    run = _Run(
        session or uuid.uuid4().hex[:12], "claude", getattr(options, "model", None) or "",
        label=session_name, meta=meta,
    )
    system_chars, tool_names = _options_meta(options)
    run.event(
        "run.start", prompt_chars=len(prompt), prompt_preview=truncate(prompt, 500)[1],
        n_messages=1, system_chars=system_chars, tool_names=tool_names,
    )
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
                    _handle_result_message(run, msg)
            ok = True
    except Exception as exc:
        run.event("error", exc_type=type(exc).__name__, message=str(exc)[:500], stage="run")
        raise
    finally:
        run.finish(ok)


async def cc_text(options, prompt: str, *, session: str | None = None,
                  session_name: str | None = None, meta: dict | None = None) -> str:
    """agent 整收应答(流式转整收,监控走与 cc_stream 同一条路径)。"""
    parts: list[str] = []
    async for chunk in cc_stream(options, prompt, session=session, session_name=session_name, meta=meta):
        parts.append(chunk)
    return "".join(parts)


async def cc_result(options, prompt: str, *, session: str | None = None,
                    session_name: str | None = None, meta: dict | None = None) -> str:
    """非流式取最终结果(judge 用):失败或无结果 → RuntimeError(调用方降级)。"""
    from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, StreamEvent

    run = _Run(
        session or uuid.uuid4().hex[:12], "claude", getattr(options, "model", None) or "",
        label=session_name, meta=meta,
    )
    system_chars, tool_names = _options_meta(options)
    run.event(
        "run.start", prompt_chars=len(prompt), prompt_preview=truncate(prompt, 500)[1],
        n_messages=1, system_chars=system_chars, tool_names=tool_names,
    )
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
                    _handle_result_message(run, msg)
                    result = msg.result
                    if not result:
                        raise RuntimeError("agent 未产出最终结果")
                    ok = True
                    return result
            raise RuntimeError("agent 未产出最终结果")
    except Exception as exc:
        run.event("error", exc_type=type(exc).__name__, message=str(exc)[:500], stage="run")
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
    session: str | None = None, session_name: str | None = None, meta: dict | None = None,
) -> str:
    """OpenAI 兼容非流式补全(异常原样抛,重试留给调用方)。

    payload 为 chat/completions 请求体(须含 model/messages;其余键原样透传,
    如 response_format/temperature/max_tokens —— 兼容题库扩展点),HTTP body 与直连一致。
    事件流单轮单块:run.start → block.start(content) → text.delta(终值一次) →
    block.stop → result → run.end(三方 LLM 工具调用无流式支持:终值一次入 tool.use 块)。
    """
    model = payload["model"]
    run = _Run(session or uuid.uuid4().hex[:12], "openai", model, label=session_name, meta=meta)
    _llm_run_start(run, payload)
    ok = False
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{url}/chat/completions", json=payload, headers=_llm_headers(headers, api_key))
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"] or {}
        content = msg.get("content") or ""
        usage = data.get("usage") or {}
        duration_ms = int((time.monotonic() - run.start) * 1000)
        run.event("block.start", block_type="content")
        if content:
            run.text(content)
        run.event("block.stop", block_type="content")
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments") or ""
            try:
                tool_input = json.loads(args) if args else None
            except json.JSONDecodeError:
                tool_input = args
            run.event(
                "block.start", block_type="tool_use",
                tool_id=tc.get("id"), tool_name=fn.get("name"),
            )
            run.event("block.stop", block_type="tool_use", tool_input=tool_input)
        run.event(
            "result", text_chars=len(content), duration_ms=duration_ms,
            usage={"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")},
        )
        ok = True
        return content
    except Exception as exc:
        run.event("error", exc_type=type(exc).__name__, message=str(exc)[:500], stage=_stage_of(exc))
        raise
    finally:
        run.finish(ok)


async def llm_stream(
    *, url: str, payload: dict, api_key: str | None = None,
    timeout: httpx.Timeout | None = None, headers: dict | None = None,
    session: str | None = None, session_name: str | None = None, meta: dict | None = None,
):
    """OpenAI 兼容流式补全(SSE 逐 delta,预留接口):payload 语义同 llm_complete,附加 stream=True。"""
    run = _Run(session or uuid.uuid4().hex[:12], "openai", payload["model"], label=session_name, meta=meta)
    _llm_run_start(run, payload)
    ok = False
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
                        run.text(text)
                        yield text
        run.event(
            "result", text_chars=run.text_chars,
            duration_ms=int((time.monotonic() - run.start) * 1000),
        )
        ok = True
    except Exception as exc:
        run.event("error", exc_type=type(exc).__name__, message=str(exc)[:500], stage=_stage_of(exc))
        raise
    finally:
        run.finish(ok)
