"""Project OpenAI-compatible inference into canonical Agent events.

Inference projection uses a caller-owned HTTP client and recorder. It does not
execute tools or commit Context; conversation owners make those decisions.
"""

import json
from hashlib import sha256
from typing import TypedDict

import httpx

from ..base import BaseAgent, RequestFailedError
from ..context import instruction, system_message, tool_defs
from ..events import (
    EventRecorder,
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


class ChatCompletion:
    """Project one streamed inference without owning an Agent loop or its history."""

    def __init__(self, client: httpx.AsyncClient, recorder: EventRecorder, *,
                 body: dict, base_url: str, headers: dict | None = None,
                 timeout: httpx.Timeout | None = None, provider: str | None = None):
        """Bind one inference to an existing observation session.

        Args:
            client: Caller-owned transport, including authentication and timeout defaults.
            recorder: Active canonical observation recorder.
            body: Native request with one choice. Streaming is enabled; sampling,
                limits, usage options and tool definitions remain caller-controlled.
            base_url: Chat-completion API root.
            headers: Request-local headers; never included in observation events.
            timeout: Request override; omission uses the client's configured timeout.
            provider: Optional asserted provider identity for the request event.
        """
        self.client, self.recorder = client, recorder
        self.body = body | {"stream": True}
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.options = {"headers": headers}
        if timeout is not None:
            self.options["timeout"] = timeout
        self.provider = provider
        self.message: dict = {}
        self.output: list[dict] = []

    async def result(self) -> dict:
        """Consume the stream and return the complete native assistant message.

        Raises:
            RequestFailedError: The stream has no normal, single-choice completion.
                HTTP and cancellation errors propagate without retries.
        """
        async for _ in self.stream():
            pass
        return self.message

    async def stream(self):
        """Yield visible text while publishing reasoning and tool-call deltas.

        Call once per instance. ``message`` and ``output`` become available after
        complete consumption. Partial text is not a successful completion.
        """
        parameters = {key: value for key, value in self.body.items()
                      if key not in {"model", "messages", "tools", "stream"}}
        request = {"model": self.body.get("model"), "parameters": parameters,
                   "requestSha256": sha256(json.dumps(self.body, sort_keys=True).encode()).hexdigest()}
        if self.provider:
            request["provider"] = self.provider
        request_id = self.recorder.model_request(**request)
        try:
            async for part in self._stream(request_id):
                yield part
        except BaseException as exc:
            self.recorder.model_error(exc, request_id=request_id)
            raise

    async def _stream(self, request_id: str):
        text = reasoning = refusal = ""
        calls = {}
        usage = stop_reason = response_model = None
        async with self.client.stream("POST", self.url, json=self.body, **self.options) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line.removeprefix("data:").strip()
                if raw == "[DONE]":
                    break
                if not raw:
                    continue
                packet = json.loads(raw)
                if packet.get("error"):
                    raise RequestFailedError(packet["error"])
                usage = packet.get("usage") or usage
                response_model = packet.get("model") or response_model
                choices = packet.get("choices") or []
                if not choices:
                    continue
                if len(choices) != 1 or choices[0].get("index", 0) != 0:
                    raise RequestFailedError("Expected exactly one model choice")
                choice = choices[0]
                stop_reason = choice.get("finish_reason") or stop_reason
                delta = choice.get("delta") or {}
                if delta.get("role", "assistant") != "assistant":
                    raise RequestFailedError("Invalid assistant role")
                refusal += delta.get("refusal") or ""
                if thought := delta.get("reasoning_content"):
                    reasoning += thought
                    self.recorder.reasoning(thought, request_id=request_id)
                if part := delta.get("content"):
                    text += part
                    self.recorder.text(part, request_id=request_id, index=1 if reasoning else 0)
                    yield part
                for call in delta.get("tool_calls") or []:
                    if call.get("type", "function") != "function":
                        raise RequestFailedError("Only function tools are supported")
                    index = call.get("index", 0)
                    slot = calls.setdefault(index, {"id": "", "type": "function",
                                                    "function": {"name": "", "arguments": ""}})
                    function = call.get("function") or {}
                    slot["id"] = call.get("id") or slot["id"]
                    slot["function"]["name"] += function.get("name") or ""
                    fragment = function.get("arguments") or ""
                    slot["function"]["arguments"] += fragment
                    self.recorder.tool_call_delta(request_id=request_id, index=index, call_id=slot["id"],
                                                  name=slot["function"]["name"], arguments_delta=fragment)
        native_calls = [slot for _, slot in sorted(calls.items())]
        ids = [call["id"] for call in native_calls]
        if any(not identifier for identifier in ids) or len(ids) != len(set(ids)):
            raise RequestFailedError("Duplicate/empty tool call identity")
        self.message = {"role": "assistant", "content": text or None}
        if reasoning:
            self.message["reasoning_content"] = reasoning
            self.output.append(reasoning_item(reasoning))
        if text:
            self.output.append(text_message("assistant", text))
        if native_calls:
            self.message["tool_calls"] = native_calls
            self.output.extend(function_call_item(call["id"], call["function"]["name"], call["function"]["arguments"])
                               for call in native_calls)
        if refusal or stop_reason not in {"stop", "tool_calls"}:
            raise RequestFailedError(f"Model did not finish normally: {stop_reason}")
        if bool(native_calls) != (stop_reason == "tool_calls"):
            raise RequestFailedError("Model stop reason disagrees with its tool calls")
        self.recorder.model_response(self.output, request_id=request_id, model=response_model,
                                     stop_reason=stop_reason, usage=usage)


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
        completion = ChatCompletion(self._client, recorder, body=body, base_url=self.config.get("base_url") or "",
                                    headers=_headers(headers, self.config.get("api_key")), timeout=timeout,
                                    provider=self.config.get("provider"))
        async for part in completion.stream():
            yield part
        self._messages.append(completion.message)
        recorder.append_context(completion.output, role="assistant")
        recorder.end_step()
        recorder.end_turn(reason="final_response")
