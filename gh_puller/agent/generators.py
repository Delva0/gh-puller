"""生成器层:BaseGenerator 基类 + ClaudeCode / OpenAI / Dsh / Codex 四个生成器。

层级纪律(本文件 = 生成器层/SDK/HTTP;config 世界在 configs.py,本文件只消费):
- 只依赖标准库、事件层(.events/.sinks)与 config 层(.configs),不认识上层
  业务(prompt/任务/缓存)—— 工具桌经 config 通用注入(mcp_servers),零具体
  工具名/业务 env 硬编码;SDK 字段映射/header 投影/隔离组合装配均在 configs.py;
- 每个生成器自持本体(id/name/config 元数据类属性);config 在**构造时期**注入
  —— `ClaudeCode(config)` 之类,stream/result 只收运行时参数(prompt/会话/run
  元数据),无 config 参数;
- 无注册表实例:集合就是 `GENERATORS`(id → 类的简单映射),上层自排/校验
  config(键集白名单见 configs.py 各 TypedDict)后直接 `GENERATORS[id](config)`
  构造适配器实例、直呼其 stream/result;失败抛 RequestFailedError(detail 为
  失败原因;llm 异常原样,重试留给调用方)。

API 契约(人类开发者正式定义):
- `stream(prompt)`:流式输出 agent 所有 message 产出,其中 assistant 输出包含
  chunk —— 即逐段文本增量 async generator;thinking/工具调用只进监控事件流,
  不构成产出。
- `result(prompt) -> str`:非流式,只拿 agent 最后一轮(最后一次生成轮)的
  assistant 输出;对 llm 而言 result 就是其输出(complete 语义,payload =
  OpenAI 兼容请求体;实现内部经流式端点抽取 —— 事件粒度与 stream 同构,非
  单发整段)。
- 无 text() API。

config 概念(契约与映射见 configs.py 模块 docstring):每生成器一个 TypedDict
(ClaudeConfig/DshConfig/CodexConfig/OpenAIConfig),键可省略;SDK 专属名映射
(config_path → settings/cordis 等)与隔离组合装配(dsh cordis / codex home)
全在 configs.py,本层运行前不做逐键校验。
"""

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from .. import envs  # 模块对象绑定:属性一律调用时取(patch/强刷活性)
from .configs import claude_options, codex_config, codex_home, codex_thread, codex_turn, codex_val, dsh_harness
from .events import TAXONOMY, EventRecorder, _normalize_usage, _session_id


class RequestFailedError(Exception):
    """SDK 层原始失败(detail 为调用方可见的失败原因;文案组合由 dispatch 包装)。"""

    def __init__(self, detail: Any):
        super().__init__(detail)
        self.detail = str(detail)


# ---------------------------------------------------------------------------
# 适配器通用:SDK/HTTP 对象 → 事件 dict(纯 dict 可单测)
# ---------------------------------------------------------------------------


def _stage_of(exc: Exception) -> str:
    """error 事件 stage 分类:http(网络/状态码)/ parse(响应结构)/ run(其余)。"""
    if isinstance(exc, httpx.HTTPError):
        return "http"
    if isinstance(exc, (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError)):
        return "parse"
    return "run"


def _dsh_session_id(session: str) -> str:
    """gh session id → dsh session_id:取最后一个 '/' 之后(与 FileSink 文件名规则一致)。"""
    return (session or "agent").rsplit("/", 1)[-1]


def _dsh_stage(exc: Exception, protocol_errors: tuple) -> str:
    """dsh 异常 stage:协议解析失败(SdkProtocolError/JsonRpcError)归 parse,其余沿 _stage_of。

    protocol_errors = 函数内 lazy import 的错误类元组(测试经假模块注入)。
    """
    if isinstance(exc, protocol_errors):
        return "parse"
    return _stage_of(exc)


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


def _llm_emit_messages(run: EventRecorder, payload: dict) -> None:
    """payload messages → surface 事件(仅 user/assistant 折叠;system 不进折叠)。"""
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


def _llm_headers(headers: dict | None, api_key: str | None) -> dict:
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    if api_key:
        hdrs.setdefault("Authorization", f"Bearer {api_key}")
    return hdrs


# ---------------------------------------------------------------------------
# 共享基类:run 装配 / 事件守卫 / stream、result 契约定形
# ---------------------------------------------------------------------------


class BaseGenerator:
    """生成器共享骨架:cc = 单一权威合成;dsh = 双权威投影(对齐器);llm = 直连 HTTP。

    子类只要写差异驱动循环(stream)与终局语义(result);公共 kwarg(会话/run
    元数据)逐参数一致。config 在**构造时期**注入(副本存 self.config),运行时
    方法不再收 config(契约见模块 docstring)。

    监控侧 agent 自身 meta 只 generator 一项 + config(事件 envelope 即
    generator/model 与各 config 派生事件)。
    """

    generator = ""  # 生成管线 id(cc|dsh|codex|llm)

    def __init__(self, config: dict):
        self.config = dict(config)  # 副本防 SDK 篡改

    def _recorder(self, *, session: str | None = None, session_ns: str | None = None,
             run_id: str | None = None, session_name: str | None = None,
             meta: dict | None = None,
             generator: str | None = None) -> EventRecorder:
        """公共 kwarg → EventRecorder(session id 规则/信封 generator 取类属性;不含 context/retry)。"""
        return EventRecorder(_session_id(session, session_ns, run_id, session_name),
                    generator=generator or self.generator,
                    label=session_name, run_id=run_id, meta=meta)

    @contextlib.asynccontextmanager
    async def _guard(self, run: EventRecorder, *, error_stage=None, epilogue=True,
                     heartbeat_secs: float | None = None):
        """统一收尾:正常 → finish(ok=True);异常 → error(stage)+ raise + finish(False)。

        error_stage:设 lambda 指定 error 事件 stage(None 等价 cc 的硬编码 "run");
        epilogue:bool 或 callable(dsh 传 lambda: not proj.saw_turn_end,调用期求值)。
        只捕获 Exception(消费者提前关闭的 GeneratorExit、CancelledError 为
        BaseException,不落 error 事件,靠 finally 兜底 finish(False) —— 与历史
        try/except/finally 语义一致);run.finish 本身幂等。
        heartbeat_secs:会话保鲜间隔(None → envs 常量;≤0 → 不起保鲜任务)。会话期间
        每该间隔触一次会话文件 mtime(只动时间戳、不发事件、绝不 busy-loop),由
        EventRecorder.start_keepwarm 承担(守卫层只注入 cadence),hub 侧租约据此
        区别"活着但静默"与"进程已死"(见 hub.py 租约扫描)。
        """
        if heartbeat_secs is None:
            heartbeat_secs = envs.AGENT_MONITOR_HEARTBEAT_SECS
        if heartbeat_secs and heartbeat_secs > 0:
            run.start_keepwarm(heartbeat_secs)
        ok = False
        try:
            yield
            ok = True
        except Exception as exc:
            run.error(exc, error_stage(exc) if error_stage else "run")
            raise
        finally:
            # 先停保鲜再收尾:停后文件 mtime 冻结 → 崩溃残留由租约判定 aborted;
            # session/end 恒为最后一条合法行(末行断言依赖)。
            await run.stop_keepwarm()
            run.finish(ok, epilogue=epilogue() if callable(epilogue) else epilogue)

    async def stream(self, prompt: str, *, session: str | None = None,
                     session_name: str | None = None, run_id: str | None = None,
                     session_ns: str | None = None,
                     context: list[dict] | None = None, retry: dict | None = None,
                     meta: dict | None = None) -> AsyncIterator[str]:
        """流式应答(子类必写):assistant 文本增量 async generator(见模块契约)。"""
        raise NotImplementedError

    async def result(self, prompt: str, *, session: str | None = None,
                     session_name: str | None = None, run_id: str | None = None,
                     session_ns: str | None = None,
                     context: list[dict] | None = None, retry: dict | None = None,
                     meta: dict | None = None) -> str:
        """非流式最终结果(子类必写):只拿最后一轮 assistant 输出(见模块契约)。"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Claude Code(SDK)包装
# ---------------------------------------------------------------------------


class ClaudeCode(BaseGenerator):
    """cc:Claude Code 命令。配置形态:file 类 —— config_path 指向 settings JSON。

    ClaudeConfig → ClaudeAgentOptions(**config),config_path → settings(SDK 传
    --settings,只装载所选文件);凭证随所选 settings 文件(SDK CLI 侧登录)。
    构造期即建立客户端(configs.claude_options 绑定 + ClaudeSDKClient 对象):
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


# ---------------------------------------------------------------------------
# OpenAI 兼容(httpx)包装
# ---------------------------------------------------------------------------


class OpenAI(BaseGenerator):
    """llm:OpenAI 兼容端点(httpx 直连)。配置形态:object 类(OpenAIConfig)。

    构造期即建 HTTP 客户端(一实例一客户端);单次调用的 timeout/headers/请求体
    (payload)仍按调用覆盖(请求级 timeout)。
    """

    generator = "llm"

    def __init__(self, config: dict):
        super().__init__(config)
        # 流式长连接不设全局超时,由请求级 timeout 覆盖
        self._client = httpx.AsyncClient(timeout=None)  # noqa: S113 - 流式长连接不设全局超时,由请求级覆盖

    async def result(
        self, payload: dict, *,
        timeout: httpx.Timeout | None = None, headers: dict | None = None,  # noqa: ASYNC109 - httpx.Timeout 请求级形参,非 asyncio 超时模式
        session: str | None = None, session_name: str | None = None,
        run_id: str | None = None, session_ns: str | None = None,
        context: list[dict] | None = None, retry: dict | None = None,
        meta: dict | None = None,
    ) -> str:
        """OpenAI 兼容补全终局语义:返回最终文本(异常原样抛,重试留给调用方)。

        config = OpenAIConfig(model/base_url/api_key) 构造注入;payload 为
        chat/completions 请求体(messages;model 可省略 —— 经 config 注入;其余
        键原样透传,如 response_format/temperature/max_tokens)。事件:请求体全量
        消息 → surface(可折叠恢复该请求输入);响应当次 text + assistant/message
        + 每 tool_call 一个 tool/call(原始 arguments 字符串)。

        实现:内部经流式端点抽取(stream() 拖到底)—— **事件面与 stream 同构**:
        逐 delta chunk,观测粒度与 cc/codex 一致(旧实现单发非流式 complete,
        整个回答只发 1 条 assistant/chunk)。
        """
        parts = [part async for part in self.stream(
            payload, timeout=timeout, headers=headers, session=session,
            session_name=session_name, run_id=run_id, session_ns=session_ns,
            context=context, retry=retry, meta=meta)]
        return "".join(parts)

    async def stream(
        self, payload: dict, *,
        timeout: httpx.Timeout | None = None, headers: dict | None = None,  # noqa: ASYNC109 - httpx.Timeout 请求级形参,非 asyncio 超时模式
        session: str | None = None, session_name: str | None = None,
        run_id: str | None = None, session_ns: str | None = None,
        context: list[dict] | None = None, retry: dict | None = None,
        meta: dict | None = None,
    ):
        """OpenAI 兼容流式补全(SSE 逐 delta):config/payload 分工与 result 同,请求体附加 stream=True。

        delta.tool_calls 分片归并 → tool/call + tool_use 块(旧实现只认 text delta,
        流式工具调用不可观测)。
        """
        config = self.config
        body = dict(payload)
        body["model"] = payload.get("model") or config.get("model") or ""
        body["stream"] = True
        run = self._recorder(session=session, session_ns=session_ns,
                        run_id=run_id, session_name=session_name, meta=meta)
        run.init_config(config)
        run.start(context=context, retry=retry)
        _llm_emit_messages(run, body)
        full = ""
        full_reasoning = ""
        tools: dict[int, dict] = {}  # index → {id, name, pieces}(delta.tool_calls 分片归并)
        seg = None  # 当前段:thinking|content|tool_call;段完成即发该段的 assistant/message 聚合

        def _close_seg():
            """当前段完成 → 一条 assistant/message(该段全量聚合;段状 = 粘合边界)。"""
            nonlocal seg
            if seg is None:
                return
            if seg == "thinking":
                blocks = [{"type": "thinking", "text": full_reasoning}]
            elif seg == "content":
                blocks = [{"type": "content", "text": full}]
            else:  # tool_call:每调用一个块(与 tool/call 事件同序列)
                blocks = []
                for slot in tools.values():
                    args = "".join(slot["pieces"])
                    try:
                        parsed = json.loads(args) if args else None
                    except json.JSONDecodeError:
                        parsed = args
                    blocks.append({"type": "tool_call", "id": slot["id"],
                                   "name": slot["name"], "input": parsed})
                    run.tool_call(slot["id"], slot["name"], args)
            seg = None
            run.event(
                "assistant/message", turn=run.turn, step=run.step,
                message={"role": "assistant", "content": blocks},
                surfaceOp="append", sourceSeqs=list(run._chunk_seqs),
            )

        url = config.get("base_url")
        api_key = config.get("api_key")
        async with (self._guard(run, error_stage=_stage_of),
                    self._client.stream("POST", f"{url}/chat/completions", json=body,
                                        headers=_llm_headers(headers, api_key),
                                        timeout=timeout) as resp):
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[len("data:"):].strip()
                if not chunk or chunk == "[DONE]":
                    break
                data = json.loads(chunk)
                choices = data.get("choices") or []
                delta = (choices[0].get("delta") or {}) if choices else {}
                if data.get("usage"):
                    run.result_usage = _normalize_usage(data["usage"])  # 末块 usage(可选扩展)
                fin = choices[0].get("finish_reason") if choices else None
                if fin:
                    run.result_stop_reason = fin  # 末块 finish_reason → session/end
                thinking = delta.get("reasoning_content") or ""
                text = delta.get("content") or ""
                if thinking:  # 思考增量 → thinking chunk(段序 0;cc/dsh/codex 同位语义)
                    if seg in ("content", "tool_call"):
                        _close_seg()
                    full_reasoning += thinking
                    run.chunk({"type": "thinking", "index": 0, "text": thinking})
                    seg = "thinking"
                if text:
                    if seg in ("thinking", "tool_call"):
                        _close_seg()  # 段边界:thinking 段完成先聚合(→ 本行后才出 content 段)
                    full += text
                    run.text(text, index=1 if full_reasoning else 0)  # 段序:thinking 之后 → 1
                    seg = "content"
                    yield text
                for tc in delta.get("tool_calls") or []:
                    if seg in ("thinking", "content"):
                        _close_seg()
                    seg = "tool_call"
                    slot = tools.setdefault(tc.get("index", 0),
                                            {"id": "", "name": "", "pieces": []})
                    fn = tc.get("function") or {}
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["pieces"].append(fn["arguments"])
            _close_seg()  # 流末:收尾当前段(纯 thinking/无后续段也保证聚合)

# ---------------------------------------------------------------------------
# DeepSeek Harness(SDK)投影全家(模块级:纯 dict 构造,测试可喂假 Notification)
# ---------------------------------------------------------------------------


class _DshProj:
    """dsh 会话事件 → gh 事件流的投影状态(单次 stream 调用一个实例)。

    seq_map: dsh session 事件 seq → gh 封套 seq。dsh seq 统计包括被跳过的插件
    事件(字段悬空),gh seq 只算本项目 TAXONOMY 投影 —— sourceEventSeqs 必须
    经本表映射为 gh sourceSeqs,未映射者丢弃;任何时刻不拷贝原始 dsh seq。
    tool_names: callId → 工具名(tool/result 补 name,来自 dsh tool/call 转发);
    tool_pieces: 本 step 内块 index → {id, name, pieces}(tool-call-delta 碎片,
    step/start 归零 —— 块 index 每 step 从 0 重新计数);
    saw_user_message / saw_turn_end:兜底与终局判定;last_finish_kind:最近 finish
    chunk 的 reason(assistant/message 与 session/end 的 stop_reason 来源)。

    为什么区分"合成"与"投影"(dsh 事件同源却比 cc 多一套 _DshProj):
    - cc 是唯一权威 → 合成器。Claude SDK 给出的是原料流(增量 StreamEvent、聚合
    AssistantMessage/ResultMessage),不携带 session/turn/step 编号、无生命周期事件、
    无 sourceSeqs。适配器只维护一套自己造的编号(seq 自己数、turn/step 自开合、
    tool/call|result 自合成、sourceSeqs 引用自己的 _chunk_seqs),正确性靠自洽,
    去重是布尔判断(already_yielded、tid in _call_seqs)。
    - dsh 是第二权威 → 对齐器。词汇虽与 TAXONOMY 同源,但 dsh 已把会话语义做掉一半,
    且用的是 dsh 自己的编号:seq 按 log.length 对全部会话事件计数(含被跳过的插件
    事件),turn/step 生命周期由 dsh 发,同一 tool-call 既有原料(block-end 完整
    arguments)又有成品(显式 tool/call),sourceEventSeqs 引用 dsh 编号空间,
    字段命名(camelCase usage、tool-result/toolCallId/isError 卡片)也不同。适配器
    不能发明、只能对账:seq_map 重映射 + 生命周期让渡(prologue=False /
    epilogue=not saw_turn_end)+ synth 双表达去重 + 字段改名 —— 即 _DshProj。
    """

    def __init__(self, run: EventRecorder, prompt: str, session_id: str):
        self.run = run
        self.prompt = prompt
        self.session_id = session_id  # dsh session_id(JSONL 文件名;子代理会话过滤用)
        self.seq_map: dict[int, int] = {}
        self.tool_names: dict[str, str] = {}
        self.tool_pieces: dict[int, dict] = {}
        self.synth: dict[str, int] = {}  # callId → 块端已合成 tool/call 的 gh seq(显式事件去重)
        self.saw_user_message = False
        self.saw_turn_end = False
        self.last_finish_kind: str | None = None

    def track(self, dsh_seq: int, action) -> int | None:
        """执行一次恰好发布单事件的 action(如 run.text/tool_call),记录 dsh_seq → gh seq。

        bus disabled 时 run.seq 不增长(事件不构造)→ 不记录映射,防悬空 seq;
        返回 gh seq(未发布返回 None)。
        """
        before = self.run.seq
        action()
        if self.run.seq > before and dsh_seq is not None:
            self.seq_map[dsh_seq] = self.run.seq - 1
            return self.run.seq - 1
        return None

    def forward(self, dsh_seq: int, evt_type: str, **data) -> dict | None:
        """发布 gh 事件并记录 seq 映射;返回事件(无 sink 时 None)。"""
        evt = self.run.event(evt_type, **data)
        if evt is not None and dsh_seq is not None:
            self.seq_map[dsh_seq] = evt["seq"]
        return evt

    def source_seqs(self, envelope: dict) -> list[int]:
        """dsh sourceEventSeqs → gh sourceSeqs(经 seq_map 映射;未映射者丢弃)。"""
        return [self.seq_map[s] for s in (envelope.get("sourceEventSeqs") or [])
                if s in self.seq_map]


def _project_dsh_chunk(run: EventRecorder, proj: _DshProj, dsh_seq, chunk: dict) -> list[str]:
    """dsh StreamChunk → gh assistant/chunk 增量投影(逐条),返回文本增量。

    文本增量走 run.text(计入 text_chars 且 yield);thinking/tool_input 只进
    事件流不改变产出(与 cc 漏斗一致);tool-call 收尾经 block-end 的完整
    arguments 合成 tool/call(整串优先,缺失时拼 delta 碎片)。
    """
    ctype = chunk.get("type")
    if ctype == "text-delta":
        text = chunk.get("text") or ""
        proj.track(dsh_seq, lambda: run.text(text, index=chunk.get("index", 0)))
        return [text] if text else []
    if ctype == "reasoning-delta":
        proj.track(dsh_seq, lambda: run.chunk({"type": "thinking", "index": chunk.get("index", 0),
                                               "text": chunk.get("text") or ""}))
        return []
    if ctype == "tool-call-delta":
        idx = chunk.get("index", -1)
        slot = proj.tool_pieces.setdefault(
            idx, {"id": chunk.get("id") or "", "name": chunk.get("name") or "", "pieces": []})
        if chunk.get("name"):
            slot["name"] = chunk["name"]
        piece = chunk.get("argumentsDelta") or ""
        slot["pieces"].append(piece)
        proj.track(dsh_seq, lambda: run.chunk({"type": "tool_call", "index": idx,
                                               "partial_json": piece}))
        return []
    if ctype == "block-end":
        block = chunk.get("block") or {}
        if block.get("type") == "tool-call":
            slot = proj.tool_pieces.pop(chunk.get("index", -1), None)
            call_id = block.get("id") or (slot or {}).get("id") or ""
            name = block.get("name") or (slot or {}).get("name")
            if call_id and name:
                proj.tool_names[call_id] = name
            args = block.get("arguments")
            if not isinstance(args, str) or args == "":
                args = "".join((slot or {}).get("pieces", []))
            # 会话日志随后仍有显式 tool/call 同 id 事件:先合成,后者去重映射(见 tool/call 分支)
            gh_seq = proj.track(dsh_seq, lambda: run.tool_call(call_id, name, args or ""))
            if gh_seq is not None and call_id:
                proj.synth[call_id] = gh_seq
        return []
    if ctype == "usage":
        usage = chunk.get("usage")
        if usage:
            run.result_usage = _normalize_usage(usage)
        return []
    if ctype == "finish":
        reason = chunk.get("reason")
        kind = reason.get("kind") if isinstance(reason, dict) else reason
        if kind:
            proj.last_finish_kind = kind
            run.result_stop_reason = kind
        return []
    return []  # block-start 与未知块:信息无需事件(增量已逐条投影)


def _project_dsh_event(run: EventRecorder, proj: _DshProj, notif) -> list[str]:
    """dsh 通知 → gh TAXONOMY 事件投影(纯 dict 构造,测试可喂假 Notification)。

    返回本事件产生的文本增量(供 stream yield)。规则:
    - 只认 session.event 且 sessionId 匹配本运行(SDK 在过滤前即回调所有会话通知,
      子代理等其它会话在此静默丢弃);
    - 非 TAXONOMY 类型(dsh 插件扩展事件)静默跳过 —— new_event 对未知 type 抛 ValueError;
    - surfaceOp/sourceEventSeqs 在信封层(sourceEventSeqs 经 proj.seq_map 映射);
    - user/message 扁平化重塑、tool/result 卡片改名(规范见 events.py 折叠契约)。
    """
    if getattr(notif, "method", None) != "session.event":
        return []
    payload = getattr(notif, "payload", None) or {}
    if payload.get("sessionId") != proj.session_id:
        return []
    envelope = payload.get("event") or {}
    evt_type = envelope.get("type")
    if evt_type not in TAXONOMY:
        return []
    data = envelope.get("data") or {}
    dsh_seq = envelope.get("seq")

    # 兜底:极罕见流缺 user/message 时,首个 assistant 事件前合成 prompt 消息
    # (绝不在 prologue 预发 —— dsh 会发自己的,且含插件/注入消息)。
    if evt_type.startswith("assistant/") and not proj.saw_user_message:
        proj.saw_user_message = True
        run.user_message({"role": "user",
                          "content": [{"type": "text", "text": proj.prompt}]})

    if evt_type == "turn/start":
        run.turn = data.get("turn", run.turn)
        proj.forward(dsh_seq, "turn/start", turn=run.turn)
    elif evt_type == "step/start":
        run.turn = data.get("turn", run.turn)
        run.step = data.get("step", run.step)
        proj.tool_pieces = {}  # 块 index 每 step 从 0 重新计数
        run._step_open = True  # 崩溃路径(无 step/end)epilogue 有可合的 step/end
        proj.forward(dsh_seq, "step/start", turn=run.turn, step=run.step)
    elif evt_type == "step/end":
        run.turn = data.get("turn", run.turn)
        run.step = data.get("step", run.step)
        run._step_open = False
        proj.forward(dsh_seq, "step/end", turn=run.turn, step=run.step)
    elif evt_type == "turn/end":
        run.turn = data.get("turn", run.turn)
        proj.saw_turn_end = True
        reason = data.get("reason")
        kind = reason.get("kind") if isinstance(reason, dict) else reason
        detail = {"turn": run.turn, "reason": kind}
        if isinstance(reason, dict):
            rest = {k: v for k, v in reason.items() if k != "kind"}
            if rest:
                detail["detail"] = rest  # 结构化失败细目透传(UI 排查素材)
        if not proj.last_finish_kind and kind:
            run.result_stop_reason = kind  # 无 finish chunk 的兜底 source
        proj.forward(dsh_seq, "turn/end", **detail)
    elif evt_type == "user/message":
        proj.saw_user_message = True
        message = {"role": data.get("role", "user"), "content": data.get("content") or []}
        proj.track(dsh_seq, lambda: run.user_message(
            message, source=data.get("source"),
            surface_op=envelope.get("surfaceOp") or "append"))
    elif evt_type == "assistant/chunk":
        run.turn = data.get("turn", run.turn)
        run.step = data.get("step", run.step)
        return _project_dsh_chunk(run, proj, dsh_seq, data.get("chunk") or {})
    elif evt_type == "assistant/message":
        run.turn = data.get("turn", run.turn)
        run.step = data.get("step", run.step)
        evt_data = {
            "turn": run.turn, "step": run.step,
            "message": data.get("message") or {},
            "surfaceOp": envelope.get("surfaceOp") or "append",
        }
        if "usage" in data:
            evt_data["usage"] = _normalize_usage(data["usage"])
        if data.get("interrupted"):
            evt_data["interrupted"] = True
        if proj.last_finish_kind:
            evt_data["stop_reason"] = proj.last_finish_kind
        src = proj.source_seqs(envelope)
        if src:
            evt_data["sourceSeqs"] = src
        proj.forward(dsh_seq, "assistant/message", **evt_data)
        if data.get("usage"):
            run.result_usage = _normalize_usage(data["usage"])  # 末条为准 → session/end
    elif evt_type == "tool/call":
        run.turn = data.get("turn", run.turn)
        run.step = data.get("step", run.step)
        call_id = data.get("callId") or ""
        name = data.get("name")
        if call_id and name:
            proj.tool_names[call_id] = name
        if call_id in proj.synth:
            # 块端(assistant/chunk block-end)已合成同 id 的 tool/call:显式事件不再重复,
            # 其 dsh seq 映射到已合成事件 —— 供 tool/result.sourceEventSeqs 溯源到同一 gh 事件
            if dsh_seq is not None:
                proj.seq_map[dsh_seq] = proj.synth[call_id]
        else:
            proj.track(dsh_seq, lambda: run.tool_call(call_id, name, data.get("arguments") or ""))
    elif evt_type == "tool/result":
        run.turn = data.get("turn", run.turn)
        run.step = data.get("step", run.step)
        msg = data.get("message") or {}
        card = ((msg.get("content") or [{}])[0]
                if isinstance(msg.get("content"), list) else {})
        call_id = (msg.get("source") or {}).get("callId") or (card.get("toolCallId") or "")
        is_error = bool(card.get("isError"))
        # 卡片改名:dsh tool-result/toolCallId/isError → gh tool_result/tool_use_id/is_error;
        # 文本块拼接为 content 字符串(cc 先例:全量不截断)
        text = "".join(b.get("text") or "" for b in (card.get("content") or [])
                       if isinstance(b, dict) and b.get("type") == "text")
        evt_data = {
            "turn": run.turn, "step": run.step,
            "message": {"role": "user", "content": [{"type": "tool_result",
                                                     "tool_use_id": call_id,
                                                     "content": text, "is_error": is_error}]},
            "callId": call_id, "is_error": is_error, "surfaceOp": "append",
        }
        if call_id in proj.tool_names:
            evt_data["name"] = proj.tool_names[call_id]
        src = proj.source_seqs(envelope)
        if src:
            evt_data["sourceSeqs"] = src
        if isinstance(data.get("error"), dict):
            evt_data["error"] = data["error"]
        proj.forward(dsh_seq, "tool/result", **evt_data)
    return []


def _dsh_worker(harness, prompt: str, session_id: str, pump):
    """同步线程体:进入已构造的 harness(子进程 spawn + initialize)→ 阻塞 run 至 idle。

    整个体在 executor 线程执行;即使外层 asyncio task 被取消(消费者提前退场),
    线程仍会自然跑完 —— with 块负责回收子进程,不泄漏。
    """
    with harness as h:
        return h.run(prompt, session_id=session_id, on_notification=pump)


# ---------------------------------------------------------------------------
# DeepSeek Harness(SDK)包装
# ---------------------------------------------------------------------------


class Dsh(BaseGenerator):
    """dsh:DeepSeek Harness 组合。配置形态:file 类 —— config_path 指向 cordis 文件。

    DshConfig → DeepSeekHarness kwargs(configs.dsh_fields:config_path → cordis、
    system_prompt → env.DSH_SYSTEM_PROMPT;未提供 cordis 时回退内置隔离组合);模型/
    凭证随组合配置(SDK 读进程环境兜底)。事件词汇同源但 dsh 是第二权威 → _DshProj
    投影对齐(why 见 _DshProj docstring)。构造期即建 harness 对象(configs.dsh_harness
    绑定 config):一个实例 = 一个 harness = 一次 dsh 会话;子进程 spawn/initialize
    在进入(with)时进行。
    """

    generator = "dsh"

    def __init__(self, config: dict):
        super().__init__(config)
        self._harness = dsh_harness(config)

    @contextlib.asynccontextmanager
    async def _run_context(self, prompt: str, *, session: str | None = None,
                           session_name: str | None = None, run_id: str | None = None,
                           session_ns: str | None = None,
                           context: list[dict] | None = None, retry: dict | None = None,
                           meta: dict | None = None):
        """dsh 运行装配(stream/result 共用):recorder/proj/队列/worker 线程。

        guard 内 yield (run, proj, queue),退出取消未完成 worker。
        """
        from deepseek_harness import errors as _dsh_errors

        config = self.config
        run = self._recorder(session=session, session_ns=session_ns, run_id=run_id,
                        session_name=session_name, meta=meta)
        proj = _DshProj(run, prompt, _dsh_session_id(run.session))
        queue: asyncio.Queue = asyncio.Queue()  # 无界:1:1 于运行时事件流,永不阻塞发布侧
        loop = asyncio.get_running_loop()

        def pump(notif) -> None:  # 运行时线程回调:跨线程入队(loop 已关闭竞态静默丢)
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(queue.put_nowait, ("notif", notif))

        async def _worker() -> None:
            try:
                result = await asyncio.to_thread(_dsh_worker, self._harness, prompt, proj.session_id, pump)
                loop.call_soon_threadsafe(queue.put_nowait, ("done", result))
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("exc", exc))

        run.init_config(config)
        run.start(context=context, retry=retry, prologue=False)  # dsh 自带 turn/step 生命周期
        async with self._guard(
            run,
            error_stage=lambda exc: _dsh_stage(exc, (_dsh_errors.SdkProtocolError,
                                                     _dsh_errors.JsonRpcError)),
            epilogue=lambda: not proj.saw_turn_end,
        ):
            task = asyncio.create_task(_worker())
            try:
                yield run, proj, queue
            finally:
                if not task.done():
                    task.cancel()  # 消费者提前退场:executor 线程继续自然跑完(见 _dsh_worker)

    async def stream(self, prompt: str, *, session: str | None = None,
                     session_name: str | None = None, run_id: str | None = None,
                     session_ns: str | None = None,
                     context: list[dict] | None = None, retry: dict | None = None,
                     meta: dict | None = None):
        """dsh 流式应答(监控 + 执行)。

        对外产出:assistant 文本增量(assistant/chunk 的 text-delta);turn 非
        completed → RequestFailedError;thinking/工具增量只进事件流。dsh 原生
        事件 1:1 投影为监控事件流(经 _project_dsh_event);SDK run() 为同步
        阻塞 → asyncio.to_thread 执行(单次运行一 turn to_idle,无逐 prompt 取消)。
        """
        async with self._run_context(prompt, session=session, session_name=session_name,
                                     run_id=run_id, session_ns=session_ns,
                                     context=context, retry=retry, meta=meta) as (run, proj, queue):
            while True:
                kind, item = await queue.get()
                if kind == "notif":
                    for delta in _project_dsh_event(run, proj, item):
                        yield delta
                elif kind == "exc":
                    raise item
                else:  # ("done", RunResult):run() 返回前所有通知已交付,无竞态
                    if item.finish_reason != "completed":
                        raise RequestFailedError(item.finish_reason)
                    break

    async def result(self, prompt: str, *, session: str | None = None,
                     session_name: str | None = None, run_id: str | None = None,
                     session_ns: str | None = None,
                     context: list[dict] | None = None, retry: dict | None = None,
                     meta: dict | None = None) -> str:
        """非流式最终结果:只拿最后一轮 —— RunResult.final_response;非 completed 或未产出 → RequestFailedError。"""
        async with self._run_context(prompt, session=session, session_name=session_name,
                                     run_id=run_id, session_ns=session_ns,
                                     context=context, retry=retry, meta=meta) as (run, proj, queue):
            while True:
                kind, item = await queue.get()
                if kind == "notif":
                    for _ in _project_dsh_event(run, proj, item):
                        pass
                elif kind == "exc":
                    raise item
                else:
                    if item.finish_reason != "completed":
                        raise RequestFailedError(item.finish_reason)
                    final = item.final_response or ""
                    if not final:
                        raise RequestFailedError("未产出最终结果")
                    return final


# ---------------------------------------------------------------------------
# Codex(OpenAI Codex SDK)包装
# ---------------------------------------------------------------------------


def _codex_args_json(arguments) -> str:
    """codex 工具 arguments(Any:dict/str/None)→ tool/call 的原始 JSON 字符串。

    与 cc 的 raw 字符串契约一致:dict → json.dumps,str 原样(可能本身是 JSON 文本),
    None/空 → ""(UI 端解析失败原样展示)。
    """
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments)


def _codex_tool_name(server: str, tool: str) -> str:
    """codex MCP 工具名归一:mcp__{server}__{tool}(server 缺省时裸 tool)。"""
    return f"mcp__{server}__{tool}" if server else tool


def _codex_stage(exc: Exception, protocol_errors: tuple) -> str:
    """codex 异常 stage:JSON-RPC 协议层(JsonRpcError 家族)归 parse,其余沿 _stage_of。

    protocol_errors = 方法内 lazy import 的错误类元组(测试经假模块注入);turn failed
    的 RuntimeError("agent 执行失败: ...")不是协议错误 → run(正确语义)。
    """
    if isinstance(exc, protocol_errors):
        return "parse"
    return _stage_of(exc)


class _CodexSynth:
    """codex 通知 → gh 事件流的合成状态(单次 stream 调用一个实例)。

    为什么是合成而非投影(对比 _DshProj):codex 通知不携带 seq/turn/step 编号、无
    生命周期事件、无 sourceEventSeqs —— 流顺序是唯一权威(与 cc 同构);合成器只维护
    自洽编号(run.seq 自己数、turn/step 自开合、tool/call|result 自合成),去重是
    字典/布尔判断,没有第二套编号要伺候。
    """

    def __init__(self, run: EventRecorder, prompt: str):
        self.run = run
        self.prompt = prompt
        self.turn_id: str | None = None  # turn/started 的 turn.id(记录用;路由已由 SDK 按 turn 过滤)
        self.agent_pieces: dict[str, list[str]] = {}  # itemId → agentMessage 增量碎片(去重/消息组装)
        self.reasoning_seen: set[str] = set()  # 已流式化 thinking 的 reasoning itemId(completed 兜底去重)
        self.tool_round_open = False  # 本轮已发 tool/result → 下次 LLM item 开一次 step 边界(聚合并行工具)
        self.plan_items: set[str] = set()  # 已发 plan 文本的 itemId(防 delta/completed 双投)
        self.saw_turn_completed = False
        self.final_response = ""  # 末条 agentMessage 文本(result 终局语义,同 TurnResult)


def _codex_item(item) -> Any:
    """ThreadItem(RootModel)→ 实际 item(RootModel 不代理属性访问;普通对象原样)。"""
    return getattr(item, "root", item)


def _codex_item_type(item) -> str:
    return getattr(_codex_item(item), "type", None) or ""


def _codex_tool_result(item, itype: str) -> dict:
    """工具类 completed item → tool/call|result 归一数据块(name/content/is_error/arguments)。

    工具项只在 item/completed 合成(完整 arguments/结果一处齐;item/started 的
    arguments 可能不完整 —— v1 舍弃提前位,未来可提前到 started 只发 tool/call)。
    """
    if itype == "mcpToolCall":
        name = _codex_tool_name(getattr(item, "server", None) or "",
                                getattr(item, "tool", None) or "")
        content_parts = []
        result = getattr(item, "result", None)
        if result is not None:
            for part in getattr(result, "content", None) or []:
                inner = _codex_item(part)
                if getattr(inner, "text", None):
                    content_parts.append(inner.text)
            structured = getattr(result, "structured_content", None)
            if structured is not None:
                content_parts.append(json.dumps(structured))
        is_error = (getattr(item, "error", None) is not None
                    or codex_val(getattr(item, "status", None)) == "failed")
        arguments = _codex_args_json(getattr(item, "arguments", None))
    elif itype == "dynamicToolCall":
        name = getattr(item, "tool", None) or ""
        content_parts = [
            getattr(_codex_item(ci), "text", None) or ""
            for ci in (getattr(item, "content_items", None) or [])
            if _codex_item_type(ci) == "inputText"
        ]
        is_error = (getattr(item, "success", None) is False
                    or codex_val(getattr(item, "status", None)) == "failed")
        arguments = _codex_args_json(getattr(item, "arguments", None))
    else:  # commandExecution
        name = "shell"
        content_parts = [getattr(item, "aggregated_output", None) or ""]
        command = getattr(item, "command", None) or ""
        cwd = getattr(item, "cwd", None) or ""
        is_error = (getattr(item, "exit_code", None) not in (None, 0)
                    or codex_val(getattr(item, "status", None)) == "failed")
        # 字段为 LegacyAppPathString(pydantic 路径类型,str 子类)→ 归一为纯 str 再 JSON
        arguments = json.dumps({k: str(v) for k, v in (("command", command), ("cwd", cwd)) if v})
    return {"name": name, "content": "\n".join(p for p in content_parts if p),
            "is_error": is_error, "arguments": arguments}


def _codex_item_completed(run: EventRecorder, st: _CodexSynth, payload) -> list[str]:
    """item/completed → surface/工具事件合成(纯 dict 构造,测试可喂假 Notification)。"""
    item = _codex_item(payload.item)
    itype = _codex_item_type(item)
    item_id = getattr(item, "id", None) or ""
    if itype == "agentMessage":
        pieces = st.agent_pieces.get(item_id) or []
        text = "".join(pieces) or (getattr(item, "text", None) or "")
        if not pieces and text:
            run.text(text)  # 兜底:无增量事件(流缺 chunk)→ 整块一次(cc AssistantMessage 兜底对齐)
        if text:
            st.final_response = text  # 末条 agentMessage = result 终局文本
        message = {"role": "assistant", "content": [{"type": "content", "text": text}]}
        phase = getattr(item, "phase", None)
        if phase is not None:
            message["content"][0]["phase"] = codex_val(phase)
        run.event("assistant/message", turn=run.turn, step=run.step, message=message,
                  surfaceOp="append", sourceSeqs=list(run._chunk_seqs))
        return [text] if not pieces and text else []
    if itype in ("dynamicToolCall", "mcpToolCall", "commandExecution"):
        st.tool_round_open = True
        info = _codex_tool_result(item, itype)
        call_id = item_id
        run.tool_call(call_id, info["name"], info["arguments"])
        run.tool_result(
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                                          "content": info["content"], "is_error": info["is_error"]}]},
            call_id=call_id, name=info["name"], is_error=info["is_error"],
            src_seq=run._call_seqs.get(call_id),
        )
        return []
    if itype == "reasoning":
        # thinking 已逐条流式化(见 reasoning/textDelta · summaryTextDelta);仅无 delta
        # 的项整块兜底一次:全量 CoT(content)优先,加密模型仅有摘要(summary)
        content = getattr(item, "content", None) or []
        pieces = content or (getattr(item, "summary", None) or [])
        if pieces and item_id not in st.reasoning_seen:
            run.chunk({"type": "thinking", "index": 0, "text": "\n".join(pieces)})
        return []
    if itype == "plan":
        text = getattr(item, "text", None) or ""
        if text and item_id not in st.plan_items:
            st.plan_items.add(item_id)
            run.chunk({"type": "plan", "index": 0, "text": text})
        return []
    if itype == "webSearch":
        # Codex 内置网络搜索(web_search_request):started 是空壳占位(query=""/action=None),
        # 只认 completed 全字段(item 无 error/status —— 失败由 turn 级表达,见 turn/completed)
        st.tool_round_open = True
        act = _codex_item(getattr(item, "action", None))
        action = None
        if act is not None:
            action = {"type": getattr(act, "type", None) or "other"}
            for k in ("query", "queries", "url", "pattern"):
                v = getattr(act, k, None)
                if isinstance(v, (str, list)) and v:
                    action[k] = v
        arguments = json.dumps({"query": getattr(item, "query", None) or "",
                                **({"action": action} if action else {})},
                               ensure_ascii=False)
        results = getattr(item, "results", None) or []  # opaque JSON(SDK 不保证字段形状)
        content = json.dumps(results, ensure_ascii=False, default=str) if results else ""
        run.tool_call(item_id, "web_search", arguments)
        run.tool_result(
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": item_id,
                                          "content": content, "is_error": False}]},
            call_id=item_id, name="web_search", is_error=False,
            src_seq=run._call_seqs.get(item_id),
        )
        return []
    return []  # userMessage 已由 run.user_message 合成;fileChange/子代理等 v1 静默跳过


def _handle_codex_notification(run: EventRecorder, st: _CodexSynth, notif) -> list[str]:
    """codex 通知 → gh TAXONOMY 事件合成(纯鸭子读取,测试可喂假 Notification)。

    返回本事件产生的文本增量(供 stream yield);codex 无 seq,顺序即通知流顺序;
    turn/step 生命周期由 run.start / step_boundary 合成(prologue 同 cc),codex 的
    turn/started|completed 只贡献 stop_reason / 失败判定。
    """
    method = getattr(notif, "method", "")
    payload = getattr(notif, "payload", None)
    if method == "turn/started":
        turn = getattr(payload, "turn", None)
        st.turn_id = getattr(turn, "id", None)
        return []
    if method == "turn/completed":
        st.saw_turn_completed = True
        turn = getattr(payload, "turn", None) or {}
        kind = codex_val(getattr(turn, "status", None))
        run.result_stop_reason = kind if isinstance(kind, str) else None
        if kind != "completed":
            error = getattr(turn, "error", None) or {}
            detail = getattr(error, "message", None) or kind
            raise RequestFailedError(detail)
        return []
    if method == "item/started":
        item = _codex_item(getattr(payload, "item", None))
        itype = _codex_item_type(item)
        if itype == "agentMessage":
            st.agent_pieces.setdefault(getattr(item, "id", None) or "", [])
        if itype in ("agentMessage", "reasoning", "plan") and st.tool_round_open:
            st.tool_round_open = False
            run.step_boundary()  # 工具结果后新一轮 LLM 请求 → 新 step(单次翻转,聚合并行工具)
        return []
    if method == "item/agentMessage/delta":
        text = getattr(payload, "delta", None) or ""
        if not text:
            return []
        st.agent_pieces.setdefault(getattr(payload, "item_id", None) or "", []).append(text)
        run.text(text)
        return [text]
    if method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
        delta = getattr(payload, "delta", None) or ""
        if delta:
            st.reasoning_seen.add(getattr(payload, "item_id", None) or "")
            # 全量 CoT 增量段位 = content_index;摘要增量段位 = summary_index(真实段序,
            # 段界即 index 跳变;summaryPartAdded 无文本不产事件,段位语义由此承载)
            index = (getattr(payload, "content_index", None)
                     if method == "item/reasoning/textDelta"
                     else getattr(payload, "summary_index", None))
            run.chunk({"type": "thinking", "index": index if index is not None else 0,
                       "text": delta})
        return []
    if method == "item/plan/delta":
        # plan 与文本并行增量:逐条 plan chunk;completed 兜底防重复见 st.plan_items
        delta = getattr(payload, "delta", None) or ""
        if delta:
            st.plan_items.add(getattr(payload, "item_id", None) or "")
            run.chunk({"type": "plan", "index": 0, "text": delta})  # 无段位字段,单文档
        return []
    if method == "item/completed":
        return _codex_item_completed(run, st, payload)
    if method == "thread/tokenUsage/updated":
        usage = getattr(payload, "token_usage", None) or {}
        breakdown = getattr(usage, "total", None) or getattr(usage, "last", None)
        if breakdown is not None:
            run.result_usage = _normalize_usage(breakdown)  # 末条为准 → session/end(同 dsh)
        return []
    return []  # summaryPartAdded(段位由 delta 的 summary_index 承载)、outputDelta、progress
    # 等:无文本增量或属日志型,v1 不进流


async def _codex_drain(handle, run: EventRecorder, st: _CodexSynth):
    """codex 通知流 → 文本增量(stream/result 共用):turn 未完成 → RequestFailedError。

    result 与 stream 同构:终局文本取 st.final_response(末条 agentMessage);
    不再直取 TurnResult(handle.run() 不暴露通知,事件流将只有生命周期)。
    """
    async for notif in handle.stream():
        for delta in _handle_codex_notification(run, st, notif):
            yield delta
    if not st.saw_turn_completed:
        raise RequestFailedError("turn 未收到完成事件")


class Codex(BaseGenerator):
    """codex:OpenAI Codex 命令。配置形态:file 类 —— config_path 指向 config.toml。

    SDK 原料通知流 → 本地合成(唯一权威,无投影层;why 见 _CodexSynth docstring);
    CodexConfig → SDK 装配件(configs.codex_*:codex_home/codex_config/codex_thread/
    codex_turn);config_path 纯透传(home config.toml 符号链接),mcp_servers 通用注入
    工具桌;codex_home 缺省回退内置隔离目录,system_prompt → base_instructions。
    构造期即建 SDK 客户端(AsyncCodex 绑定 config);连接与 thread 建立在调用期。
    """

    generator = "codex"

    def __init__(self, config: dict):
        super().__init__(config)
        from openai_codex import AsyncCodex  # lazy:测试可喂假模块

        self._codex = AsyncCodex(config=codex_config(config))

    async def stream(self, prompt: str, *, session: str | None = None,
                     session_name: str | None = None, run_id: str | None = None,
                     session_ns: str | None = None,
                     context: list[dict] | None = None, retry: dict | None = None,
                     meta: dict | None = None):
        """Codex 流式应答(监控 + 执行)。

        对外产出:assistant 文本增量(item/agentMessage/delta 优先、item/completed
        整块兜底),turn 非 completed → RequestFailedError;thinking/plan/工具增量
        只进事件流。codex 通知 1:1 合成 TAXONOMY(无 seq 编号 → 本地合成,见
        _CodexSynth);session/turn/step 生命周期由本层合成。

        config 键集即 configs.CodexConfig(装配见 configs.codex_config/thread/turn)。

        凭证(cc 同形:环境隔离不隔离凭证通道):零配置缺省符号链接引用真实
        ~/.codex/auth.json;显式 config.token → login_api_key 写本隔离 home。
        """
        from openai_codex import JsonRpcError

        config = self.config
        run = self._recorder(session=session, session_ns=session_ns, run_id=run_id,
                        session_name=session_name, meta=meta)
        run.init_config(config)
        run.start(context=context, retry=retry)
        run.user_message({"role": "user", "content": [{"type": "text", "text": prompt}]})
        st = _CodexSynth(run, prompt)
        timeout = config.get("timeout_seconds")
        home = codex_home(config)  # config → 隔离 home(装配在 configs.py)
        guard = self._guard(run, error_stage=lambda exc: _codex_stage(exc, (JsonRpcError,)))
        async with guard, self._codex as codex:
            if (token := config.get("token") or ""):
                # 显式 token → 登录凭证属本隔离 home:先断符号链接防穿透写坏用户 ~/.codex/auth.json
                auth = Path(home) / "auth.json"
                if auth.is_symlink():
                    auth.unlink()
                await codex.login_api_key(token)
            thread = await codex.thread_start(**codex_thread(config))
            handle = await thread.turn(prompt, **codex_turn(config))

            if timeout is not None:
                async with asyncio.timeout(timeout):  # 兜底 review/approval 等待挂流
                    async for chunk in _codex_drain(handle, run, st):
                        yield chunk
            else:
                async for chunk in _codex_drain(handle, run, st):
                    yield chunk

    async def result(self, prompt: str, *, session: str | None = None,
                     session_name: str | None = None, run_id: str | None = None,
                     session_ns: str | None = None,
                     context: list[dict] | None = None, retry: dict | None = None,
                     meta: dict | None = None) -> str:
        """非流式最终结果:只拿最后一轮 —— 通知流末条 agentMessage 文本(st.final_response)。

        turn 非 completed / 未产出 → RequestFailedError。与 stream 同构消费通知流
        (_codex_drain):事件全量合成。旧实现直取 handle.run() 的 TurnResult ——
        不暴露通知,事件流只有生命周期,观测不到 assistant/工具事件。
        """
        from openai_codex import JsonRpcError

        config = self.config
        run = self._recorder(session=session, session_ns=session_ns, run_id=run_id,
                        session_name=session_name, meta=meta)
        run.init_config(config)
        run.start(context=context, retry=retry)
        run.user_message({"role": "user", "content": [{"type": "text", "text": prompt}]})
        st = _CodexSynth(run, prompt)  # result 与 stream 同构:合成器照常驱动
        timeout = config.get("timeout_seconds")
        home = codex_home(config)  # config → 隔离 home(装配在 configs.py)
        guard = self._guard(run, error_stage=lambda exc: _codex_stage(exc, (JsonRpcError,)))
        async with guard, self._codex as codex:
            if (token := config.get("token") or ""):
                auth = Path(home) / "auth.json"
                if auth.is_symlink():
                    auth.unlink()
                await codex.login_api_key(token)
            thread = await codex.thread_start(**codex_thread(config))
            handle = await thread.turn(prompt, **codex_turn(config))
            if timeout is not None:
                async with asyncio.timeout(timeout):  # 兜底 review/approval 等待挂流
                    async for _ in _codex_drain(handle, run, st):
                        pass
            else:
                async for _ in _codex_drain(handle, run, st):
                    pass
        final = st.final_response or ""
        if not final:
            raise RequestFailedError("未产出最终结果")
        return final


# ---------------------------------------------------------------------------
# 生成器映射:id → 类(简单映射;构造 = GENERATORS[id](config),config 契约见 configs.py)
# ---------------------------------------------------------------------------

GENERATORS: dict[str, type[BaseGenerator]] = {"cc": ClaudeCode, "dsh": Dsh,
                                             "codex": Codex, "llm": OpenAI}


# ---------------------------------------------------------------------------
# 直白 API 用法演示:cc/codex/llm 三生成器真实任务(stream/result 上层 API 参考)
# `python -m gh_puller.agent.generators`
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    # cc/codex/llm 三生成器 stream/result 演示(真实任务):访问 GitHub 仓库并一句话
    # 介绍 —— result() 各生成器最后一轮语义(多轮工具用后只取终稿)在真实任务下可验。
    # cc/codex 零配置走缺省隔离(llm 按环境取值 OPENAI_BASE_URL/LLM_MODEL/OPENAI_API_KEY);
    # 真实任务各生成器可加工具授权:cc 见下方 allowed_tools,codex 以 web_search。
    # 注:dsh 不在此演示 —— 本机 SDK 载体(runtime exe)未构建,见 dsh 真机测试恢复点。

    QUESTION = "请访问 https://github.com/yankils/hello-world 并写一句话介绍这个仓库。"

    async def _demo(gid: str) -> None:
        config: dict = {}
        if gid == "cc":
            # 访问类任务:开放 WebFetch/WebSearch,多轮预算放宽(工具轮次 + 终局)
            config = {"allowed_tools": ["WebFetch", "WebSearch"], "max_turns": 6}
        elif gid == "codex":
            config = {"web_search": True,  # Codex 内置网络搜索:默认安全关闭,须显式启用
                      "sandbox": "full_access", "approval_mode": "auto_review"}
        else:  # llm
            config = {
                "base_url": os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
                "model": os.environ.get("LLM_MODEL") or "gpt-5.6-luna",
                "api_key": os.environ.get("OPENAI_API_KEY"),
            }
        gen = GENERATORS[gid](config)  # 构造期注入 config;stream/result 只收运行时参数
        prompt = (
            {"messages": [{"role": "user", "content": QUESTION}], "max_tokens": 256}
            if gid == "llm" else QUESTION
        )

        print(f"[{gid}] 流式(stream):")
        parts = [c async for c in gen.stream(prompt, session_name=f"demo:{gid}")]
        print("".join(parts) or "(无产出)")

        print(f"[{gid}] 终局(result):")
        final = await gen.result(prompt, session_name=f"demo-result:{gid}")
        print(final or "(空)")

    async def _main() -> None:
        for gid in GENERATORS:
            if gid == "dsh":
                continue  # 载体未构建,见 dsh 真机测试 TODO(不阻塞演示)
            try:
                await _demo(gid)
            except Exception as e:
                print(f"[{gid}] 失败: {type(e).__name__}: {e}")
        print("演示结束")

    asyncio.run(_main())

