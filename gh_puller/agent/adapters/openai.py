"""Adapt one stateful OpenAI-compatible chat session to canonical Agent events."""

import json
from typing import TypedDict

import httpx

from ..base import BaseAgent
from ..context import instruction, system_message, tool_defs
from ..events import (
    function_call_item,
    reasoning_item,
    text_message,
)


class OpenAIConfig(TypedDict, total=False):
    """Construction-time connection, Context, and request configuration."""

    model: str
    base_url: str
    api_key: str
    provider: str
    system_prompt: str
    tools: list[dict]
    parameters: dict


def _headers(headers: dict | None, api_key: str | None) -> dict:
    result = {"Content-Type": "application/json"}
    result.update(headers or {})
    if api_key:
        result.setdefault("Authorization", f"Bearer {api_key}")
    return result


def _system_item(config: dict) -> dict | None:
    content = []
    if prompt := config.get("system_prompt"):
        content.append(instruction(prompt))
    if "tools" in config:
        content.append(tool_defs(config.get("tools") or []))
    return system_message(content) if content else None


class OpenAI(BaseAgent):
    """Run sequential chat-completion turns with session-owned native history."""

    agent = "llm"

    def __init__(self, config: dict):
        super().__init__(config)
        self._client = httpx.AsyncClient(timeout=None)  # noqa: S113 - requests set timeout
        self._messages: list[dict] = []

    async def _enter(self):
        await self._client.__aenter__()
        self._messages = []
        if prompt := self.config.get("system_prompt"):
            self._messages.append({"role": "system", "content": prompt})
        if item := _system_item(self.config):
            self._require_event_recorder().append_context(item, role="system")

    async def _exit(self, exc):
        await self._client.__aexit__(*exc)

    async def result(
        self,
        prompt: str,
        *,
        timeout: httpx.Timeout | None = None,  # noqa: ASYNC109
        headers: dict | None = None,
    ) -> str:
        """Return the visible text from one turn.

        Args:
            prompt: User text appended to the session Context.
            timeout: Request-local HTTP timeout.
            headers: Request-local headers merged over connection defaults.

        Returns:
            Concatenated assistant text deltas.
        """
        return "".join([
            part async for part in self.stream(prompt, timeout=timeout, headers=headers)
        ])

    async def stream(
        self,
        prompt: str,
        *,
        timeout: httpx.Timeout | None = None,  # noqa: ASYNC109
        headers: dict | None = None,
    ):
        """Yield visible text from one turn and retain its complete native message.

        Args:
            prompt: User text appended to the session Context.
            timeout: Request-local HTTP timeout.
            headers: Request-local headers merged over connection defaults.
        """
        recorder = self._require_event_recorder()
        recorder.begin_turn()
        user = {"role": "user", "content": prompt}
        self._messages.append(user)
        recorder.append_context(text_message("user", prompt))
        recorder.begin_step()

        parameters = {
            key: value for key, value in (self.config.get("parameters") or {}).items()
            if key not in {"messages", "model", "stream", "tools"}
        }
        body = {
            **parameters,
            "model": self.config.get("model") or "",
            "messages": list(self._messages),
            "stream": True,
        }
        tools = list(self.config.get("tools") or [])
        if tools:
            body["tools"] = tools
        request = {"model": body["model"], "parameters": parameters}
        if provider := self.config.get("provider"):
            request["provider"] = provider
        request_id = recorder.model_request(**request)

        text = ""
        reasoning = ""
        calls: dict[int, dict] = {}
        usage = None
        stop_reason = None
        response_model = None
        url = self.config.get("base_url") or ""
        async with self._client.stream(
            "POST",
            f"{url}/chat/completions",
            json=body,
            headers=_headers(headers, self.config.get("api_key")),
            timeout=timeout,
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
                response_model = packet.get("model") or response_model
                choices = packet.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                stop_reason = choice.get("finish_reason") or stop_reason
                delta = choice.get("delta") or {}
                thought = delta.get("reasoning_content") or ""
                if thought:
                    reasoning += thought
                    recorder.reasoning(thought, request_id=request_id)
                part = delta.get("content") or ""
                if part:
                    text += part
                    recorder.text(part, request_id=request_id, index=1 if reasoning else 0)
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
                        request_id=request_id,
                        index=index,
                        call_id=slot["callId"],
                        name=slot["name"],
                        arguments_delta=fragment,
                    )

        output = []
        if reasoning:
            output.append(reasoning_item(reasoning))
        if text:
            output.append(text_message("assistant", text))
        native_calls = []
        for _, slot in sorted(calls.items()):
            arguments = "".join(slot["parts"])
            output.append(function_call_item(slot["callId"], slot["name"], arguments))
            native_calls.append({
                "id": slot["callId"],
                "type": "function",
                "function": {"name": slot["name"], "arguments": arguments},
            })
        assistant = {"role": "assistant", "content": text or None}
        if reasoning:
            assistant["reasoning_content"] = reasoning
        if native_calls:
            assistant["tool_calls"] = native_calls
        self._messages.append(assistant)

        recorder.model_response(
            output,
            request_id=request_id,
            model=response_model,
            stop_reason=stop_reason,
            usage=usage,
        )
        recorder.append_context(output, role="assistant")
        recorder.end_step()
        recorder.end_turn(reason="final_response")
