"""Run a minimal tool-using agent through the BaseAgent observation contract.

The loop is model -> tools -> model, with sequential tool execution and unchanged
history. Tool implementations own filesystem isolation and subprocess cleanup;
this module neither supplies a sandbox nor executes repository code. A/B/C differ
only in the supplied tools. See gh_puller.agent.base for session ownership and
gh_puller.agent.events for the canonical observation language.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from types import SimpleNamespace

import httpx
from jsonschema import Draft202012Validator, ValidationError

from gh_puller.agent.base import BaseAgent, RequestFailedError
from gh_puller.agent.context import instruction, system_message, tool_defs
from gh_puller.agent.events import function_call_item, normalize_usage, reasoning_item, text_message


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema for one argument object.
    invoke: Callable[[dict], Awaitable[str]]  # Must propagate cancellation and clean up children.

    def definition(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": deepcopy(self.parameters),
        }}


@dataclass(frozen=True)
class Limits:
    steps: int = 12  # Model requests per user turn; no implicit final-answer request.
    tool_calls: int = 24  # Attempted calls, including argument and execution failures.
    output_tokens: int = 4096  # Per-request completion cap, including provider-counted reasoning.
    tool_chars: int = 16000  # Per-result text cap; the result envelope reports truncation.
    seconds: float = 600  # Whole-turn deadline, including model and tool waits.

    def __post_init__(self):
        if any(value <= 0 for value in asdict(self).values()):
            raise ValueError("Agent limits must be positive")


DEFAULT_LIMITS = Limits()


class BudgetExceededError(RequestFailedError):
    """Stop an unfinished turn without presenting intermediate text as a final answer."""

    def __init__(self, detail, *, reason_code="budget_exhausted"):
        super().__init__(detail, reason_code=reason_code)


class MinimalAgent(BaseAgent):
    agent = "graphub-v1"

    def __init__(self, config: dict, tools: list[Tool], *, limits: Limits = DEFAULT_LIMITS,
                 transport: httpx.AsyncBaseTransport | None = None):
        """Construct one explicitly configured chat-completion tool loop.

        Args:
            config: Required model and base_url (API root); optional api_key,
                system_prompt, parameters (provider sampling/reasoning options), and
                max_tokens_field (max_completion_tokens by default, or max_tokens).
                Request structure and budget fields cannot be overridden in parameters.
            tools: Unique function definitions and cancellable asynchronous handlers.
                Handlers receive parsed, schema-validated arguments, without credentials.
            limits: Per-turn limits. Input/output usage is recorded, not estimated as
                zero when absent; output_tokens is not a total billing-token budget.
            transport: Optional HTTP transport for offline protocol tests. Omission
                uses direct HTTP without automatic retries or a hosted agent runtime.
        """
        if not config.get("model") or not config.get("base_url"):
            raise ValueError("An explicit model and API base_url are required")
        if config.get("max_tokens_field", "max_completion_tokens") not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("Unknown provider output-token parameter")
        reserved = {"model", "messages", "tools", "stream", "n", "tool_choice", "max_tokens", "max_completion_tokens"}
        if reserved & config.get("parameters", {}).keys():
            raise ValueError("Provider parameters cannot override the agent protocol or limits")
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("Tool names must be unique")
        for tool in tools:
            Draft202012Validator.check_schema(tool.parameters)
        super().__init__(deepcopy(config) | {
            "tools": [tool.definition() for tool in tools], "limits": asdict(limits),
        })
        self.tools = {tool.name: tool for tool in tools}
        self.limits = limits
        self.transport = transport
        self.stats: dict = {}
        self._running = False

    async def _enter(self):
        self._client = httpx.AsyncClient(transport=self.transport, timeout=self.limits.seconds)
        self._messages = []
        self._call_ids = set()
        self._usage = []
        self._broken = False
        prompt = self.config.get("system_prompt", "")
        if prompt:
            self._messages.append({"role": "system", "content": prompt})
        self._require_event_recorder().append_context(system_message([
            *([instruction(prompt)] if prompt else []), tool_defs(self.config["tools"]),
        ]))

    async def _exit(self, exc):
        await self._client.aclose()
        fields = set.intersection(*(set(usage) for usage in self._usage)) if self._usage else set()
        total = {key: sum(usage[key] for usage in self._usage) for key in fields}
        recorder = self._require_event_recorder()
        recorder.result_meta(SimpleNamespace(usage=total, stop_reason=recorder.result_stop_reason))

    async def _infer(self):
        recorder = self._require_event_recorder()
        token_field = self.config.get("max_tokens_field", "max_completion_tokens")
        parameters = self.config.get("parameters", {}) | {token_field: self.limits.output_tokens}
        request = {"model": self.config["model"], "parameters": parameters}
        request_id = recorder.model_request(**request)
        body = parameters | {"model": self.config["model"], "messages": self._messages, "stream": False, "n": 1}
        if self.config["tools"]:
            body["tools"] = self.config["tools"]
        headers = {"Authorization": f"Bearer {self.config['api_key']}"} if self.config.get("api_key") else {}
        self.stats["steps"] += 1
        self.stats["usage"].append(None)
        self.stats["models"].append(None)
        self._usage.append({})
        response = await self._client.post(
            self.config["base_url"].rstrip("/") + "/chat/completions", json=body, headers=headers,
        )
        response.raise_for_status()
        packet = response.json()
        self.stats["usage"][-1] = packet.get("usage")
        self.stats["models"][-1] = packet.get("model")
        self._usage[-1] = normalize_usage(packet.get("usage")) or {}
        if len(packet["choices"]) != 1:
            raise RequestFailedError("Expected exactly one model choice")
        choice = packet["choices"][0]
        message = choice["message"]
        calls = message.get("tool_calls") or []
        ids = [call["id"] for call in calls]
        if (message["role"] != "assistant" or any(not identifier for identifier in ids)
                or len(set(ids)) != len(ids) or self._call_ids.intersection(ids)):
            raise RequestFailedError("Invalid assistant role or duplicate/empty tool call identity")
        self._call_ids.update(ids)
        output = []
        if reasoning := message.get("reasoning_content"):
            output.append(reasoning_item(reasoning))
        if text := message.get("content"):
            output.append(text_message("assistant", text))
        for call in calls:
            if call["type"] != "function":
                raise RequestFailedError("Only function tools are supported")
            output.append(function_call_item(call["id"], call["function"]["name"], call["function"]["arguments"]))
        recorder.model_response(output, request_id=request_id, model=packet.get("model"),
                                stop_reason=choice["finish_reason"], usage=packet.get("usage"))
        recorder.append_context(output, role="assistant")
        self._messages.append(message)
        if message.get("refusal") or choice["finish_reason"] not in {"stop", "tool_calls"}:
            raise RequestFailedError(f"Model did not finish normally: {choice['finish_reason']}")
        if bool(calls) != (choice["finish_reason"] == "tool_calls"):
            raise RequestFailedError("Model stop reason disagrees with its tool calls")
        return message, calls

    async def _invoke(self, call):
        recorder = self._require_event_recorder()
        name, raw = call["function"]["name"], call["function"]["arguments"]
        recorder.tool_call(call["id"], name, raw)
        self.stats["tool_calls"] += 1
        try:
            arguments = json.loads(raw)
            tool = self.tools[name]
            Draft202012Validator(tool.parameters).validate(arguments)
            text = await tool.invoke(arguments)
            if not isinstance(text, str):
                raise TypeError("Tool handlers must return text")
            result = {"ok": True, "text": text[:self.limits.tool_chars],
                      "characters": len(text), "truncated": len(text) > self.limits.tool_chars}
        except asyncio.CancelledError:
            # Tool cleanup has completed before the interrupted turn is closed.
            recorder.tool_result("Tool execution cancelled", call_id=call["id"], name=name, is_error=True)
            raise
        except Exception as exc:  # Tool failures are observations the model can act on.
            detail = exc.message if isinstance(exc, ValidationError) else str(exc)
            text = f"{type(exc).__name__}: {detail}"
            result = {"ok": False, "text": text[:self.limits.tool_chars],
                      "characters": len(text), "truncated": len(text) > self.limits.tool_chars}
            self.stats["tool_errors"] += 1
        content = json.dumps(result, ensure_ascii=False)
        recorder.tool_result(content, call_id=call["id"], name=name, is_error=not result["ok"])
        self._messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})

    async def _loop(self):
        recorder = self._require_event_recorder()
        for step in range(self.limits.steps):
            recorder.begin_step()
            message, calls = await self._infer()
            if not calls:
                if not message.get("content"):
                    raise RequestFailedError("Model returned no final answer")
                recorder.end_step()
                return message["content"]
            if step + 1 == self.limits.steps:
                raise BudgetExceededError("Model-step budget exhausted before a final answer")
            if self.stats["tool_calls"] + len(calls) > self.limits.tool_calls:
                raise BudgetExceededError("Tool-call batch exceeds the remaining budget")
            for call in calls:
                await self._invoke(call)
            recorder.end_step()
        raise BudgetExceededError("Model-step budget exhausted")

    async def result(self, prompt: str) -> str:
        """Run a sequential user turn, retaining history until the session ends.

        Args:
            prompt: Exact user input. No candidate selection or retrieval plan is added.

        Returns:
            Final assistant text; stats holds measured per-turn usage and termination.

        Raises:
            BudgetExceededError: A limit interrupted the turn; observations retain partial work.
            RequestFailedError: The provider failed to produce a complete supported response.
            RuntimeError: The session is absent, busy, or has an interrupted history.
        """
        recorder = self._require_event_recorder()
        if self._running or self._broken:
            raise RuntimeError("A busy or interrupted agent cannot start another turn")
        self._running = True
        self.stats = {"steps": 0, "tool_calls": 0, "tool_errors": 0, "usage": [], "models": [], "outcome": "failed"}
        started = time.monotonic()
        recorder.begin_turn()
        self._messages.append({"role": "user", "content": prompt})
        recorder.append_context(text_message("user", prompt))
        try:
            try:
                async with asyncio.timeout(self.limits.seconds):
                    answer = await self._loop()
            except TimeoutError as exc:
                raise BudgetExceededError("Whole-turn deadline exceeded", reason_code="timeout") from exc
        except BaseException:  # Cancellation also leaves an unfinished provider history.
            self._broken = True
            raise
        else:
            self.stats["outcome"] = "completed"
            return answer
        finally:
            self.stats["seconds"] = time.monotonic() - started
            recorder.end_turn(outcome=self.stats["outcome"],
                              reason="final_response" if self.stats["outcome"] == "completed" else "interrupted")
            self._running = False

    async def stream(self, prompt: str):
        """Yield the completed answer as one chunk; intermediate work stays in observations.

        Args:
            prompt: User input, with the same execution and failure contract as result.
        """
        yield await self.result(prompt)
