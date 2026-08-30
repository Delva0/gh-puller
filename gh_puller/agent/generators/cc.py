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


def claude_options(config: dict):
    """ClaudeConfig → ClaudeAgentOptions 实例(config_path → settings 装载)。

    本层只做键映射(config 原样透传 + config_path → settings),不关心 SDK 运行细节。
    """
    from claude_agent_sdk import ClaudeAgentOptions  # lazy:测试可喂假模块

    sdk_options = {k: v for k, v in config.items() if k != "config_path"}  # 概念键不得透传
    if config.get("config_path"):  # 统一概念键 → SDK settings(--settings 装载)
        sdk_options["settings"] = config["config_path"]
    return ClaudeAgentOptions(**sdk_options)


# ---------------------------------------------------------------------------
# 适配器:SDK 原始流事件/消息对象 → 事件 dict(纯 dict 可单测;cc 唯一权威合成)
# ---------------------------------------------------------------------------


def _handle_stream_event(run: EventRecorder, event: dict) -> None:
    """归一化 SDK 原始流事件(cursor 型)→ 监控事件;不改动文本产出路径(产出在 stream)。

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
    if typ == "message_delta":
        stop = (event.get("delta") or {}).get("stop_reason")
        if stop:
            run._msg_stop_reason = stop  # 消息级 stop_reason(SDK 消息标记不带)
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
            run.chunk({"type": "tool_call", "index": idx, "partial_json": piece})
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
            run._tool_results_seen.add(tid)  # 用户消息双路去重(见 _handle_user_message)
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


def _handle_assistant_message(run: EventRecorder, msg, already_yielded: bool) -> None:
    """整块消息:未产出增量时 text 增量一次并事件化;此后发全量 assistant/message。

    sourceSeqs = 本 step 已发 chunk 的 seq;文本/思考/tool_use 块全量入 message;
    无流事件的 tool_use 兜底补合成 tool/call(流路径已由 content_block_stop 发射)。
    partial 模式(真机 CLI 2.1.237):SDK 消息标记 content 为空,全量 message
    从本消息增量缓冲(_msg_text/_msg_thinking/_msg_tool_calls)重建 ——
    事件序 = 增量 → 标记,标记即该消息收尾。
    """
    content = []
    for b in msg.content:
        t = getattr(b, "type", None)
        if t == "text":
            text = getattr(b, "text", None) or ""
            if text and not already_yielded:
                run.text(text)
            content.append({"type": "content", "text": text})
        elif t == "thinking":
            content.append({"type": "thinking", "text": getattr(b, "thinking", None) or ""})
        elif t == "tool_use":
            entry = {"type": "tool_call", "id": getattr(b, "id", None) or "",
                     "name": getattr(b, "name", None) or ""}
            if getattr(b, "input", None) is not None:
                entry["input"] = b.input
            content.append(entry)
    if not content:
        # 重建:[thinking, text, tool_use…];工具调用经独立 tool/call 事件承载,
        # 此处仅入消息块(顺序对齐 SDK 内容块惯例)
        if run._msg_thinking:
            content.append({"type": "thinking", "text": run._msg_thinking})
        if run._msg_text:
            content.append({"type": "content", "text": run._msg_text})
        for tid, name, arguments in run._msg_tool_calls:
            entry = {"type": "tool_call", "id": tid, "name": name}
            if arguments:
                try:
                    entry["input"] = json.loads(arguments)
                except json.JSONDecodeError:
                    entry["input"] = arguments
            content.append(entry)
    stop_reason = getattr(msg, "stop_reason", None) or run._msg_stop_reason
    run._msg_reset()
    run.event(
        "assistant/message", turn=run.turn, step=run.step,
        message={"role": "assistant", "content": content},
        usage=_normalize_usage(getattr(msg, "usage", None)),
        stop_reason=stop_reason,
        surfaceOp="append", sourceSeqs=list(run._chunk_seqs),
    )
    for block in msg.content:  # 兜底:无 input_json_delta 的 SDK 路径
        if getattr(block, "type", None) != "tool_use":
            continue
        tid = getattr(block, "id", None) or ""
        if tid and tid not in run._call_seqs:
            run.tool_call(tid, getattr(block, "name", None),
                          json.dumps(getattr(block, "input", None) or {}))


def _handle_user_message(run: EventRecorder, msg) -> None:
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
        if tid in run._tool_results_seen:
            continue
        is_error = bool(block.is_error)
        raw = block.content
        if isinstance(raw, list):
            text = "".join(str(c.get("text") or "")
                           for c in raw if isinstance(c, dict) and c.get("type") == "text")
        else:
            text = raw or ""
        run.tool_result(
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                          "content": text, "is_error": is_error}]},
            call_id=tid, name=run.tool_names.get(tid),
            is_error=is_error, src_seq=run._call_seqs.get(tid),
        )
        run._tool_results_seen.add(tid)
        run._tool_pending = True  # 工具结果后 → 下条 assistant 消息(message_start)开 step 边界


# ---------------------------------------------------------------------------
# Claude Code(SDK)包装
# ---------------------------------------------------------------------------


class ClaudeCode(BaseGenerator):
    """cc:Claude Code 命令。配置形态:file 类 —— config_path 指向 settings JSON。

    ClaudeConfig → ClaudeAgentOptions(**config),config_path → settings(SDK 传
    --settings,只装载所选文件);凭证随所选 settings 文件(SDK CLI 侧登录)。
    构造期即建立客户端(claude_options 绑定 + ClaudeSDKClient 对象):
    一个实例 = 一个客户端 = 一次上游对话;连接进入(子进程 spawn)仍在调用期。
    """

    generator = "cc"

    def __init__(self, config: dict):
        super().__init__(config)
        from claude_agent_sdk import ClaudeSDKClient  # lazy:测试可喂假模块

        self._client = ClaudeSDKClient(options=claude_options(config))

    async def stream(self, prompt: str, *, session: str | None = None,
                     session_name: str | None = None, run_id: str | None = None,
                     session_ns: str | None = None,
                     context: list[dict] | None = None, retry: dict | None = None,
                     meta: dict | None = None):
        """Claude Code 流式应答(监控 + 执行)。

        对外产出:assistant 文本增量(StreamEvent text_delta 优先,AssistantMessage
        兜底,ResultMessage.is_error → RequestFailedError(detail));thinking/工具
        增量仅进事件流。config 整体透传为 ClaudeAgentOptions(config_path →
        settings)。context = 上下文说明事件列表(context/modify,{type,data}
        形),重放于 session/start 之后。
        """
        from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, UserMessage

        config = self.config
        run = self._recorder(session=session, session_ns=session_ns, run_id=run_id,
                        session_name=session_name, meta=meta)
        run.init_config(config)
        run.start(context=context, retry=retry)
        run.user_message({"role": "user", "content": [{"type": "text", "text": prompt}]})
        yielded = False
        async with self._guard(run), self._client as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    _handle_stream_event(run, msg.event)
                    event = msg.event
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta") or {}
                        # 工具结果文本增量已归属 tool/result(_handle_stream_event),
                        # 不构成 assistant 产出 → 不得 yield(与 text_chars 同口径)
                        if delta.get("type") == "text_delta" and delta.get("text") \
                                and run._tool_result is None:
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
                elif isinstance(msg, UserMessage):
                    _handle_user_message(run, msg)  # 工具结果(partial 模式经 user 消息透传)
                elif isinstance(msg, ResultMessage):
                    if msg.is_error:
                        detail = (msg.errors or [])[-1] if msg.errors else msg.result
                        raise RequestFailedError(detail or msg.subtype)
                    run.result_meta(msg)

    async def result(self, prompt: str, *, session: str | None = None,
                     session_name: str | None = None, run_id: str | None = None,
                     session_ns: str | None = None,
                     context: list[dict] | None = None, retry: dict | None = None,
                     meta: dict | None = None) -> str:
        """非流式最终结果:只拿最后一轮 —— 直接取 ResultMessage.result(不从流式文本拼装)。

        保留「agent 未产出最终结果」语义;失败或无结果 → RequestFailedError。
        """
        from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, UserMessage

        config = self.config
        run = self._recorder(session=session, session_ns=session_ns, run_id=run_id,
                        session_name=session_name, meta=meta)
        run.init_config(config)
        run.start(context=context, retry=retry)
        run.user_message({"role": "user", "content": [{"type": "text", "text": prompt}]})
        async with self._guard(run), self._client as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    _handle_stream_event(run, msg.event)
                elif isinstance(msg, AssistantMessage):
                    _handle_assistant_message(run, msg, already_yielded=run.text_chars > 0)
                elif isinstance(msg, UserMessage):
                    _handle_user_message(run, msg)  # 工具结果(partial 模式经 user 消息透传)
                elif isinstance(msg, ResultMessage):
                    if msg.is_error:
                        detail = (msg.errors or [])[-1] if msg.errors else msg.result
                        raise RequestFailedError(detail or msg.subtype)
                    run.result_meta(msg)
                    result = msg.result
                    if not result:
                        raise RequestFailedError("未产出最终结果")
                    return result
            raise RequestFailedError("未产出最终结果")
