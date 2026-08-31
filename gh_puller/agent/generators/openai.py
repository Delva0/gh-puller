"""llm:OpenAI 兼容端点(httpx)包装 —— 配置世界(OpenAIConfig)+ 适配器本体。

本文件 = llm 的独立扩展点(直连 HTTP,无 SDK);config 只有 OpenAIConfig
{model, base_url, api_key},请求体(payload = OpenAI 兼容 chat/completions
请求体)独立于 config 运行时传入;字段映射与适配器同文件,模块 import 面零 SDK。
"""

import contextlib
import json
from typing import TypedDict

import httpx

from ..events import EventRecorder, _normalize_usage
from .base import BaseGenerator
from .utils import _stage_of


class OpenAIConfig(TypedDict, total=False):
    """llm runtime config (model/base_url/api_key); the request body (payload) is passed separately."""

    model: str
    base_url: str
    api_key: str
    provider: str


# ---------------------------------------------------------------------------
# 适配器:请求体/头部 → 事件 dict(纯 dict 可单测)
# ---------------------------------------------------------------------------


def _llm_emit_messages(event_recorder: EventRecorder, payload: dict) -> None:
    """Emit payload messages as surface events (only user/assistant fold; system stays out of the fold)."""
    for m in payload.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant"):
            continue
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
        message = {"role": role, "content": blocks}
        if role == "user":
            event_recorder.user_message(message)
        else:  # 历史 assistant 消息:无 usage/停止原因,仅内容折叠
            event_recorder.event("assistant/message", turn=event_recorder.turn,
                                 step=event_recorder.step, message=message,
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
    """llm: OpenAI-compatible endpoint (httpx direct). Config shape: object-class (OpenAIConfig).

    HTTP client built at construction (one instance one client = connection pool);
    the client lifetime follows one session (`async with gen.session(...)`: pool
    enter/exit). Per-call timeout/headers/body (payload) still override at the call
    level (request-level params live in stream/result).
    """

    generator = "llm"
    provider = "openai"

    def __init__(self, config: dict):
        super().__init__(config)
        # 流式长连接不设全局超时,由请求级 timeout 覆盖
        self._client = httpx.AsyncClient(timeout=None)  # noqa: S113 - 流式长连接不设全局超时,由请求级覆盖

    async def _enter(self):
        await self._client.__aenter__()  # 连接池进入

    async def _exit(self, exc):
        await self._client.__aexit__(*exc)  # 连接池关闭

    @contextlib.asynccontextmanager
    async def session(self, **kw):
        """llm session: error stage hooks http/parse (_stage_of); rest follows base orchestration."""
        async with super().session(error_stage=_stage_of, **kw):
            yield

    async def result(
        self, payload: dict, *,
        timeout: httpx.Timeout | None = None, headers: dict | None = None,  # noqa: ASYNC109 - httpx.Timeout 请求级形参,非 asyncio 超时模式
    ) -> str:
        """Return the final text from an OpenAI-compatible completion (exceptions propagate; retry left to the caller).

        config = OpenAIConfig(model/base_url/api_key) injected at construction; payload
        is the chat/completions body (messages; model optional — injected from config;
        other keys pass through, e.g. response_format/temperature/max_tokens). Events:
        the body's full message list → surface (foldable request input); the response
        text + assistant/message + one tool/call per tool_call (raw arguments string).

        Implementation drains the streaming endpoint (stream() to the end) — the event
        surface matches stream: per-delta chunks, same granularity as cc/codex.

        Args:
            payload: chat/completions request body (model injected from config when omitted).
            timeout: Request-level HTTP timeout (None = no per-request timeout).
            headers: Request headers (merged over the bearer/Content-Type defaults).

        Returns:
            Final assistant text of the final round.
        """
        parts = [part async for part in self.stream(payload, timeout=timeout, headers=headers)]
        return "".join(parts)

    async def stream(
        self, payload: dict, *,
        timeout: httpx.Timeout | None = None, headers: dict | None = None,  # noqa: ASYNC109 - httpx.Timeout 请求级形参,非 asyncio 超时模式
    ):
        """Stream an OpenAI-compatible completion (SSE per delta); body gets stream=True, config split as result.

        delta.tool_calls fragments merge into the tool/call + tool_use blocks (the
        legacy implementation knew only text deltas — streaming tool calls unobservable).

        Args:
            payload: chat/completions request body (see `result`).
            timeout: Request-level HTTP timeout.
            headers: Request headers.

        Returns:
            Async iterator of text deltas.
        """
        config = self.config
        body = dict(payload)
        body["model"] = payload.get("model") or config.get("model") or ""
        body["stream"] = True
        event_recorder = self._require_event_recorder()
        _llm_emit_messages(event_recorder, body)
        full = ""
        full_reasoning = ""
        tools: dict[int, dict] = {}  # index → {id, name, pieces}(delta.tool_calls 分片归并)
        seg = None  # 当前段:thinking|content|tool_call;段完成即发该段的 assistant/message 聚合

        def _close_seg():
            """Segment done → one assistant/message (full segment aggregation; segment shape = glue boundary)."""
            nonlocal seg
            if seg is None:
                return
            if seg == "thinking":
                blocks = [{"type": "thinking", "text": full_reasoning}]
            elif seg == "content":
                blocks = [{"type": "content", "text": full}]
            else:  # tool_call:工具调用只经 tool/call 事件,不入 assistant/message 消息块
                blocks = []
                for slot in tools.values():
                    args = "".join(slot["pieces"])
                    event_recorder.tool_call(slot["id"], slot["name"], args)
            seg = None
            event_recorder.event(
                "assistant/message", turn=event_recorder.turn, step=event_recorder.step,
                message={"role": "assistant", "content": blocks},
                surfaceOp="append", sourceSeqs=list(event_recorder._chunk_seqs),
            )

        url = config.get("base_url")
        api_key = config.get("api_key")
        async with self._client.stream("POST", f"{url}/chat/completions", json=body,
                                       headers=_llm_headers(headers, api_key),
                                       timeout=timeout) as resp:
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
                    event_recorder.result_usage = _normalize_usage(data["usage"])  # 末块 usage(可选扩展)
                fin = choices[0].get("finish_reason") if choices else None
                if fin:
                    event_recorder.result_stop_reason = fin  # 末块 finish_reason → session/end
                thinking = delta.get("reasoning_content") or ""
                text = delta.get("content") or ""
                if thinking:  # 思考增量 → thinking chunk(段序 0;与各 agent 生成器同位语义)
                    if seg in ("content", "tool_call"):
                        _close_seg()
                    full_reasoning += thinking
                    event_recorder.chunk({"type": "thinking", "index": 0, "text": thinking})
                    seg = "thinking"
                if text:
                    if seg in ("thinking", "tool_call"):
                        _close_seg()  # 段边界:thinking 段完成先聚合(→ 本行后才出 content 段)
                    full += text
                    event_recorder.text(text, index=1 if full_reasoning else 0)  # 段序:thinking 之后 → 1
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
