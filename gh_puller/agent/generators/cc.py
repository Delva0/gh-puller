"""cc: Claude Code SDK wrapper — config world (ClaudeConfig + claude_options) + adapter.

Single-authority cc file: the adapter owns cc's event numbering (no second authority);
ClaudeConfig → ClaudeAgentOptions(**config) with SDK-native keys — config contract
sourced in the package docstring (see __init__.py). Credentials ride the chosen
settings file (SDK CLI-side login). SDK types lazily imported inside functions
(tests inject fake modules); the module import surface is SDK-free.
"""

import json
from typing import TypedDict

from ..events import EventRecorder, _normalize_usage
from .base import BaseGenerator
from .utils import RequestFailedError


class ClaudeConfig(TypedDict, total=False):
    """cc runtime config: keys are ClaudeAgentOptions field names (see __init__.py)."""

    model: str
    system_prompt: str
    allowed_tools: list[str]
    mcp_servers: dict
    cwd: str
    settings: str  # --settings load (only that file; credentials ride the file)
    add_dirs: list[str]
    permission_mode: str
    setting_sources: list[str]
    include_partial_messages: bool
    max_turns: int
    strict_mcp_config: bool  # setting_sources=[]不隔离mcp，需要该参数


def claude_options(config: dict):
    """ClaudeConfig → ClaudeAgentOptions instance (keys passthrough, see __init__.py).

    Non-obvious behavior: an empty settings value is dropped (SDK default
    isolation); strict_mcp_config defaults to True — headless cc recognizes only
    the injected tool desk (mcp_servers), ignoring machine-level MCP config.
    """
    from claude_agent_sdk import ClaudeAgentOptions  # lazy: fake module in tests

    sdk_options = dict(config)
    if not sdk_options.get("settings"):  # empty → SDK default isolation
        sdk_options.pop("settings", None)
    sdk_options.setdefault("strict_mcp_config", True)  # default isolation (see above)
    # Partial messages are the only source of monitor text/thinking increments.
    sdk_options.setdefault("include_partial_messages", True)
    return ClaudeAgentOptions(**sdk_options)


# ---------------------------------------------------------------------------
# 适配器:SDK 原始流事件/消息对象 → 事件 dict(纯 dict 可单测;cc 唯一权威合成)
# ---------------------------------------------------------------------------


def _block_kind(block) -> str:
    """内容块判型:按 type 属性/字段判别(离线测试假块同形)。

    SDK 0.2.142 起 TextBlock/ThinkingBlock/ToolUseBlock 为纯 dataclass(仅字段无
    type);ServerToolUseBlock(同 id/name/input 形)亦归 tool_use —— 服务器侧工具
    调用照常入事件。
    """
    t = getattr(block, "type", None)
    if t is not None:
        return t
    if hasattr(block, "thinking"):
        return "thinking"
    if hasattr(block, "text"):
        return "text"
    if hasattr(block, "id"):
        return "tool_use"
    return ""


def _handle_stream_event(event_recorder: EventRecorder, event: dict) -> None:
    """归一化 SDK 原始流事件(cursor 型)→ 监控事件;不改动文本产出路径(产出在 stream)。

    映射:content_block_delta → assistant/chunk(原始增量,含 thinking/tool_input);
    tool_use 收尾 → tool/call(原始 arguments JSON 字符串);tool_result 收尾 →
    tool/result(全量内容);message_start 且工具结果后 → step 边界。
    """
    typ = event.get("type")
    if typ == "message_start":
        if event_recorder._tool_pending and (event.get("message") or {}).get("role") == "assistant":
            event_recorder.step_boundary()
            event_recorder._tool_pending = False
        return
    if typ == "message_delta":
        stop = (event.get("delta") or {}).get("stop_reason")
        if stop:
            event_recorder._msg_stop_reason = stop  # 消息级 stop_reason(SDK 消息标记不带)
        return
    if typ == "content_block_start":
        cb = event.get("content_block") or {}
        idx = event.get("index", -1)
        btype = cb.get("type")
        if btype == "tool_use":
            tid = cb.get("id") or ""
            event_recorder.tool_names[tid] = cb.get("name") or ""
            event_recorder._active_tool_use[idx] = tid
            event_recorder._tool_use_pieces[idx] = []
        elif btype == "tool_result":
            event_recorder._tool_pending = True
            event_recorder._tool_result = {
                "id": cb.get("tool_use_id") or "", "pieces": [], "is_error": bool(cb.get("is_error")),
            }
        return
    if typ == "content_block_delta":
        delta = event.get("delta") or {}
        dtype = delta.get("type")
        idx = event.get("index", -1)
        if event_recorder._tool_result is not None and dtype == "text_delta":  # 工具结果内容归属 tool/result
            event_recorder._tool_result["pieces"].append(delta.get("text") or "")
            return
        if dtype == "text_delta":
            event_recorder.text(delta.get("text") or "", index=idx)
            return
        if dtype == "thinking_delta":
            event_recorder.chunk({"type": "thinking", "index": idx, "text": delta.get("thinking") or ""})
            return
        if dtype == "input_json_delta":
            piece = delta.get("partial_json") or ""
            event_recorder._tool_use_pieces.setdefault(idx, []).append(piece)
            event_recorder.chunk({"type": "tool_call", "index": idx, "partial_json": piece})
        return
    if typ == "content_block_stop":
        idx = event.get("index", -1)
        if event_recorder._tool_result is not None:
            text = "".join(event_recorder._tool_result["pieces"])
            tid = event_recorder._tool_result["id"]
            event_recorder.tool_result(
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": text,
                     "is_error": event_recorder._tool_result["is_error"]},
                ]},
                call_id=tid, name=event_recorder.tool_names.get(tid),
                is_error=event_recorder._tool_result["is_error"], src_seq=event_recorder._call_seqs.get(tid),
            )
            event_recorder._tool_results_seen.add(tid)  # 用户消息双路去重(见 _handle_user_message)
            event_recorder._tool_result = None
            return
        if idx in event_recorder._active_tool_use:
            tid = event_recorder._active_tool_use[idx]
            raw = "".join(event_recorder._tool_use_pieces.get(idx, []))
            if tid not in event_recorder._call_seqs:  # 消息路径已兜底发射先到先得,流停止不再重发
                event_recorder.tool_call(tid, event_recorder.tool_names.get(tid), raw)
            event_recorder._active_tool_use.pop(idx, None)
            event_recorder._tool_use_pieces.pop(idx, None)
            return
        return  # text/thinking 收尾:增量已逐条事件化(块型由 chunk.content 索引决定)


def _handle_assistant_message(event_recorder: EventRecorder, msg, already_yielded: bool) -> None:
    """整块消息:未产出增量时 text 增量一次并事件化;此后发全量 assistant/message。

    sourceSeqs = 本 step 已发 chunk 的 seq;文本/思考块入 message(content/thinking),
    工具调用**不入消息块** —— 只经独立 tool/call 事件承载(_call_seqs 先到先得防重,
    流路径由 content_block_stop 发射,无流事件由本函数兜底合成)。
    partial 模式(真机 CLI 2.1.237):SDK 消息标记 content 为空,全量 message
    从本消息增量缓冲(_msg_text/_msg_thinking)重建 ——
    事件序 = 增量 → 标记,标记即该消息收尾。
    """
    content = []
    for b in msg.content:
        t = _block_kind(b)
        if t == "text":
            text = getattr(b, "text", None) or ""
            if text and not already_yielded:
                event_recorder.text(text)
            content.append({"type": "content", "text": text})
        elif t == "thinking":
            content.append({"type": "thinking", "text": getattr(b, "thinking", None) or ""})
        # tool_use 不装消息块:工具调用只以 tool/call 事件承载
    if not content:
        # 重建:[thinking, text];工具调用经独立 tool/call 事件承载,不入消息块
        if event_recorder._msg_thinking:
            content.append({"type": "thinking", "text": event_recorder._msg_thinking})
        if event_recorder._msg_text:
            content.append({"type": "content", "text": event_recorder._msg_text})
    stop_reason = getattr(msg, "stop_reason", None) or event_recorder._msg_stop_reason
    event_recorder._msg_reset()
    # 块式契约:sourceSeqs 按消息内容块类型分组(think 消息只引 thinking chunk seqs,
    # content 消息只引 content chunk seqs)——不得用到达时刻累计的 _chunk_seqs
    # (content 消息晚到时会把 thinking 批 seqs 一并带进)。
    src_seqs = [s for t in {b["type"] for b in content}
                for s in event_recorder._chunk_type_seqs.get(t, [])]
    event_recorder.event(
        "assistant/message", turn=event_recorder.turn, step=event_recorder.step,
        message={"role": "assistant", "content": content},
        usage=_normalize_usage(getattr(msg, "usage", None)),
        stop_reason=stop_reason,
        surfaceOp="append", sourceSeqs=src_seqs,
    )
    for block in msg.content:  # 兜底:无 input_json_delta 的 SDK 路径
        if _block_kind(block) != "tool_use":
            continue
        tid = getattr(block, "id", None) or ""
        if tid and tid not in event_recorder._call_seqs:
            event_recorder.tool_call(tid, getattr(block, "name", None),
                          json.dumps(getattr(block, "input", None) or {}))


def _handle_user_message(event_recorder: EventRecorder, msg) -> None:
    """SDK user 段消息(partial 模式)→ 合成 tool/result + 置 _tool_pending。

    真机 CLI 2.1.237:工具结果经 UserMessage 透传(流里无 tool_result 内容块)。
    与流路径(content_block_stop tool_result)互斥去重:同 callId 先到先得
    (_tool_results_seen);元信息(tool_use_result/origin)v1 不落事件。
    """
    from claude_agent_sdk import ToolResultBlock  # lazy:测试可喂假模块

    blocks = getattr(msg, "content", None)
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if not isinstance(block, ToolResultBlock):
            continue
        tid = block.tool_use_id or ""
        if tid in event_recorder._tool_results_seen:
            continue
        is_error = bool(block.is_error)
        raw = block.content
        if isinstance(raw, list):
            text = "".join(str(c.get("text") or "")
                           for c in raw if isinstance(c, dict) and c.get("type") == "text")
        else:
            text = raw or ""
        event_recorder.tool_result(
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                          "content": text, "is_error": is_error}]},
            call_id=tid, name=event_recorder.tool_names.get(tid),
            is_error=is_error, src_seq=event_recorder._call_seqs.get(tid),
        )
        event_recorder._tool_results_seen.add(tid)
        event_recorder._tool_pending = True  # 工具结果后 → 下条 assistant 消息(message_start)开 step 边界


# ---------------------------------------------------------------------------
# Claude Code(SDK)包装
# ---------------------------------------------------------------------------


class ClaudeCode(BaseGenerator):
    """cc: Claude Code command. Config shape: file-class (see __init__.py).

    ClaudeConfig → ClaudeAgentOptions(**config); credentials ride the chosen
    settings file (SDK CLI-side login). Client binding is built at construction
    (claude_options binding + ClaudeSDKClient object): one instance = one client
    wrapper; connection enter (child spawn) at session startup
    (`async with cc.session(...)`), exit reaps.
    """

    generator = "cc"
    provider = "anthropic"

    def __init__(self, config: dict):
        super().__init__(config)
        from claude_agent_sdk import ClaudeSDKClient  # lazy:测试可喂假模块

        self._client = ClaudeSDKClient(options=claude_options(config))

    async def _enter(self):
        await self._client.__aenter__()  # 子进程 spawn(连接进入)

    async def _exit(self, exc):
        await self._client.__aexit__(*exc)  # 子进程收殓

    async def stream(self, prompt: str):
        """Stream assistant deltas from the Claude Code SDK query (payload-only; metadata via session()).

        Yielded text: StreamEvent text_delta first, AssistantMessage whole-text fallback,
        ResultMessage.is_error → RequestFailedError(detail); thinking/tool increments
        go to the event stream only. config passes through as ClaudeAgentOptions.
        """
        from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, UserMessage

        event_recorder = self._require_event_recorder()
        event_recorder.user_message({"role": "user", "content": [{"type": "text", "text": prompt}]})
        yielded = False
        await self._client.query(prompt)
        async for msg in self._client.receive_response():
            if isinstance(msg, StreamEvent):
                _handle_stream_event(event_recorder, msg.event)
                event = msg.event
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta") or {}
                    # 工具结果文本增量已归属 tool/result(_handle_stream_event),
                    # 不构成 assistant 产出 → 不得 yield(与 text_chars 同口径)
                    if delta.get("type") == "text_delta" and delta.get("text") \
                            and event_recorder._tool_result is None:
                        yielded = True
                        yield delta["text"]
            elif isinstance(msg, AssistantMessage):
                _handle_assistant_message(event_recorder, msg, yielded)
                if not yielded:
                    # 兜底:无 partial 事件时整块取文本(ThinkingBlock 无 text 属性,天然跳过)
                    for block in msg.content:
                        text = getattr(block, "text", None)
                        if text:
                            yield text
            elif isinstance(msg, UserMessage):
                _handle_user_message(event_recorder, msg)  # 工具结果(partial 模式经 user 消息透传)
            elif isinstance(msg, ResultMessage):
                if msg.is_error:
                    detail = (msg.errors or [])[-1] if msg.errors else msg.result
                    raise RequestFailedError(detail or msg.subtype)
                event_recorder.result_meta(msg)

    async def result(self, prompt: str) -> str:
        """Return the final round's output: ResultMessage.result directly; failure or no output → RequestFailedError."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, UserMessage

        event_recorder = self._require_event_recorder()
        event_recorder.user_message({"role": "user", "content": [{"type": "text", "text": prompt}]})
        await self._client.query(prompt)
        async for msg in self._client.receive_response():
            if isinstance(msg, StreamEvent):
                _handle_stream_event(event_recorder, msg.event)
            elif isinstance(msg, AssistantMessage):
                _handle_assistant_message(event_recorder, msg,
                                          already_yielded=event_recorder.text_chars > 0)
            elif isinstance(msg, UserMessage):
                _handle_user_message(event_recorder, msg)  # 工具结果(partial 模式经 user 消息透传)
            elif isinstance(msg, ResultMessage):
                if msg.is_error:
                    detail = (msg.errors or [])[-1] if msg.errors else msg.result
                    raise RequestFailedError(detail or msg.subtype)
                event_recorder.result_meta(msg)
                result = msg.result
                if not result:
                    raise RequestFailedError("未产出最终结果")
                return result
        raise RequestFailedError("未产出最终结果")
