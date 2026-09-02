"""OpenAI-compatible streaming adapter with canonical request-state observation."""

import contextlib
import json
from typing import TypedDict

import httpx

from .base import BaseGenerator


class OpenAIConfig(TypedDict, total=False):
    """Construction-time connection and default model configuration."""

    model: str
    base_url: str
    api_key: str
    provider: str


def _headers(headers: dict | None, api_key: str | None) -> dict:
    result = {"Content-Type": "application/json"}
    result.update(headers or {})
    if api_key:
        result.setdefault("Authorization", f"Bearer {api_key}")
    return result


def _blocks(content) -> list[dict]:
    if isinstance(content, list):
        return [dict(block) for block in content]
    return [{"type": "text", "text": content or ""}]


def _message(message: dict) -> dict:
    result = {"role": message.get("role") or "user", "content": _blocks(message.get("content"))}
    if message.get("tool_call_id"):
        result["callId"] = message["tool_call_id"]
    calls = message.get("tool_calls") or []
    for call in calls:
        function = call.get("function") or {}
        arguments = function.get("arguments") or ""
        with contextlib.suppress(json.JSONDecodeError):
            arguments = json.loads(arguments)
        result["content"].append({
            "type": "tool_call", "callId": call.get("id") or "",
            "name": function.get("name") or "", "arguments": arguments,
        })
    return result


def _tool_blocks(tools: list[dict]) -> list[dict]:
    result = []
    for tool in tools:
        function = tool.get("function") or tool
        result.append({
            "type": "tool_definition", "name": function.get("name") or "",
            "description": function.get("description") or "",
            "inputSchema": function.get("parameters") or function.get("inputSchema") or {},
        })
    return result


def _request_context(messages: list[dict], tools: list[dict]) -> list[dict]:
    """Normalize the complete model-visible context of one request."""
    context = [_message(message) for message in messages]
    definitions = _tool_blocks(tools)
    if not definitions:
        return context
    leading = 0
    while leading < len(context) and context[leading]["role"] in {"system", "developer"}:
        leading += 1
    context.insert(leading, {"role": "system", "content": definitions})
    return context


class OpenAI(BaseGenerator):
    """Direct ``chat/completions`` client; one session may carry multiple requests."""

    generator = "llm"
    provider = "openai"

    def __init__(self, config: dict):
        super().__init__(config)
        self._client = httpx.AsyncClient(timeout=None)  # noqa: S113 - requests set timeout

    def _initial_context(self) -> list[dict]:
        return []

    async def _enter(self):
        await self._client.__aenter__()

    async def _exit(self, exc):
        await self._client.__aexit__(*exc)

    async def result(self, payload: dict, *, timeout: httpx.Timeout | None = None,  # noqa: ASYNC109
                     headers: dict | None = None) -> str:
        """Return the final text produced by one streaming request.

        Args:
            payload: OpenAI-compatible request body.
            timeout: Request-local HTTP timeout.
            headers: Request-local headers merged over connection defaults.

        Returns:
            Concatenated assistant text deltas.
        """
        return "".join([part async for part in self.stream(
            payload, timeout=timeout, headers=headers)])

    async def stream(self, payload: dict, *, timeout: httpx.Timeout | None = None,  # noqa: ASYNC109
                     headers: dict | None = None):
        """Yield assistant text while recording the effective request and output."""
        body = dict(payload)
        body["model"] = payload.get("model") or self.config.get("model") or ""
        body["stream"] = True
        messages = list(body.get("messages") or [])
        recorder = self._require_event_recorder()
        recorder.begin_turn()
        parameters = {key: value for key, value in body.items()
                      if key not in {"model", "messages", "tools", "stream"}}
        recorder.set_model(body["model"], provider=self.config.get("provider") or self.provider,
                           parameters=parameters)
        recorder.set_context(_request_context(messages, list(body.get("tools") or [])))
        recorder.begin_step()
        recorder.model_request()

        text = ""
        reasoning = ""
        calls: dict[int, dict] = {}
        usage = None
        stop_reason = None
        url = self.config.get("base_url") or ""
        async with self._client.stream(
            "POST", f"{url}/chat/completions", json=body,
            headers=_headers(headers, self.config.get("api_key")), timeout=timeout,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line.removeprefix("data:").strip()
                if not raw or raw == "[DONE]":
                    break
                packet = json.loads(raw)
                usage = packet.get("usage") or usage
                choices = packet.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                stop_reason = choice.get("finish_reason") or stop_reason
                delta = choice.get("delta") or {}
                thought = delta.get("reasoning_content") or ""
                if thought:
                    reasoning += thought
                    recorder.reasoning(thought)
                part = delta.get("content") or ""
                if part:
                    text += part
                    recorder.text(part, index=1 if reasoning else 0)
                    yield part
                for tool_call in delta.get("tool_calls") or []:
                    index = tool_call.get("index", 0)
                    slot = calls.setdefault(index, {"callId": "", "name": "", "parts": []})
                    function = tool_call.get("function") or {}
                    slot["callId"] = tool_call.get("id") or slot["callId"]
                    slot["name"] = function.get("name") or slot["name"]
                    fragment = function.get("arguments") or ""
                    slot["parts"].append(fragment)
                    recorder.tool_call_delta(
                        index=index, call_id=slot["callId"], name=slot["name"],
                        arguments_delta=fragment)

        content = []
        if reasoning:
            content.append({"type": "reasoning", "text": reasoning})
        if text:
            content.append({"type": "text", "text": text})
        for slot in calls.values():
            raw_arguments = "".join(slot["parts"])
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = raw_arguments
            content.append({
                "type": "tool_call", "callId": slot["callId"],
                "name": slot["name"], "arguments": arguments,
            })
        message = {"role": "assistant", "content": content}
        recorder.model_response(message, stop_reason=stop_reason, usage=usage)
        recorder.append_context(message)
        recorder.end_step()
        recorder.end_turn(reason="final_response")
