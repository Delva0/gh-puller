"""cc:Claude Code(SDK)包装 —— 配置世界(ClaudeConfig + claude_options)+ 适配器本体。

本文件 = cc 的独立扩展点(唯一权威合成器,cc 是唯一权威 → 适配器只维护一套自造编号);
config 契约/字段映射与适配器同文件:ClaudeConfig → ClaudeAgentOptions(**config),
config_path → settings(SDK 传 --settings,只装载所选文件);凭证随所选 settings
文件(SDK CLI 侧登录)。SDK 类型仅函数内懒导入(测试经假模块注入),模块 import
面零 SDK。
"""

import json
from typing import TypedDict

from ..events import EventRecorder, _normalize_usage
from .base import BaseGenerator
from .utils import RequestFailedError


class ClaudeConfig(TypedDict, total=False):
    """cc 运行时 config:整体作为 ClaudeAgentOptions(**config);config_path → settings。"""

    model: str
    system_prompt: str
    allowed_tools: list[str]
    mcp_servers: dict
    cwd: str
    config_path: str
    add_dirs: list[str]
    permission_mode: str
    setting_sources: list[str]
    include_partial_messages: bool
    max_turns: int
    strict_mcp_config: bool


def claude_options(config: dict):
    """ClaudeConfig → ClaudeAgentOptions 实例(config_path → settings 装载)。

    本层只做键映射(config 原样透传 + config_path → settings),不关心 SDK 运行细节。
    默认隔离:strict_mcp_config 未显式给定时取 True —— 无头 cc 只认 mcp_servers
    注入的工具桌,忽略本机用户级 MCP 配置(防本机全局服务器与装配工具桌混用)。
    """
    from claude_agent_sdk import ClaudeAgentOptions  # lazy:测试可喂假模块

    sdk_options = {k: v for k, v in config.items() if k != "config_path"}  # 概念键不得透传
    if config.get("config_path"):  # 统一概念键 → SDK settings(--settings 装载)
        sdk_options["settings"] = config["config_path"]
    sdk_options.setdefault("strict_mcp_config", True)  # 本层默认隔离
    return ClaudeAgentOptions(**sdk_options)


# ---------------------------------------------------------------------------
# 适配器:SDK 原始流事件/消息对象 → 事件 dict(纯 dict 可单测;cc 唯一权威合成)
# ---------------------------------------------------------------------------


def _block_kind(block) -> str:
    """内容块判型:块多带 type 属性(离线测试假块同形);SDK 0.2.142 起 TextBlock/
    ThinkingBlock/ToolUseBlock 为纯 dataclass(仅字段无 type),按字段判别。

    ServerToolUseBlock(同 id/name/input 形)亦归 tool_use —— 服务器侧工具调用照常入事件。
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
    event_recorder.event(
        "assistant/message", turn=event_recorder.turn, step=event_recorder.step,
        message={"role": "assistant", "content": content},
        usage=_normalize_usage(getattr(msg, "usage", None)),
        stop_reason=stop_reason,
        surfaceOp="append", sourceSeqs=list(event_recorder._chunk_seqs),
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
    """cc:Claude Code 命令。配置形态:file 类 —— config_path 指向 settings JSON。

    ClaudeConfig → ClaudeAgentOptions(**config),config_path → settings(SDK 传
    --settings,只装载所选文件);凭证随所选 settings 文件(SDK CLI 侧登录)。
    构造期即建立客户端绑定(claude_options 绑定 + ClaudeSDKClient 对象):一个实例
    = 一个客户端包装;连接进入(子进程 spawn)在一次会话开头 ——
    `async with cc.session(...)`(session 进入)、退出回收。
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
        """Claude Code 流式驱动(纯载荷;会话元数据经 session())。

        对外产出:assistant 文本增量(StreamEvent text_delta 优先,AssistantMessage
        兜底,ResultMessage.is_error → RequestFailedError(detail));thinking/工具
        增量仅进事件流。config 整体透传为 ClaudeAgentOptions(config_path →
        settings)。
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
        """非流式最终结果(纯载荷):只拿最后一轮 —— 直接取 ResultMessage.result。

        保留「agent 未产出最终结果」语义;失败或无结果 → RequestFailedError。
        """
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
