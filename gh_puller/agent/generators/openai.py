"""llm:OpenAI 兼容端点(httpx)包装 —— 配置世界(OpenAIConfig)+ 适配器本体。

本文件 = llm 的独立扩展点(直连 HTTP,无 SDK);config 只有 OpenAIConfig
{model, base_url, api_key},请求体(payload = OpenAI 兼容 chat/completions
请求体)独立于 config 运行时传入;字段映射与适配器同文件,模块 import 面零 SDK。
"""

import json
from typing import TypedDict

import httpx

from ..events import EventRecorder, _normalize_usage
from .base import BaseGenerator
from .utils import _stage_of


class OpenAIConfig(TypedDict, total=False):
    """llm 运行时 config(model/base_url/api_key);请求体(payload)独立传入。"""

    model: str
    base_url: str
    api_key: str
    provider: str


# ---------------------------------------------------------------------------
# 适配器:请求体/头部 → 事件 dict(纯 dict 可单测)
# ---------------------------------------------------------------------------


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
