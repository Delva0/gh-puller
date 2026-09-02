"""Canonical agent events and the in-process observation bus.

Model and context events form replayable request state. Model and tool activity
records facts without changing that state. Turn and step events are optional semantic
markers; consumers must not depend on their placement for correctness.
"""

import asyncio
import json
import time
import uuid

from gh_puller.utils import _log

CONTEXT_APPEND_TYPES = frozenset({
    "context/append", "context/append/user", "context/append/assistant",
    "context/append/tool",
})
DELTA_TYPES = frozenset({
    "model/delta/text", "model/delta/reasoning", "model/delta/tool-call",
})
TAXONOMY = frozenset({
    "session/start", "session/end", "session/error",
    "turn/start", "turn/end", "step/start", "step/end",
    "model/set", "context/set",
    "model/request", "model/response", "tool/start", "tool/end",
}) | CONTEXT_APPEND_TYPES | DELTA_TYPES
NON_STREAM_TYPES = TAXONOMY - DELTA_TYPES


def _validate_content(data: dict, event_type: str) -> None:
    content = data.get("content")
    if not isinstance(content, list):
        raise TypeError(f"{event_type} requires list content")
    if any(not isinstance(block, dict) or not isinstance(block.get("type"), str)
           for block in content):
        raise ValueError(f"{event_type} content blocks require a string type")


def _validate_message(message, event_type: str) -> None:
    if not isinstance(message, dict) or not isinstance(message.get("role"), str) \
            or not message["role"]:
        raise TypeError(f"{event_type} requires a message role")
    _validate_content(message, event_type)


def new_event(event_type: str, **data) -> dict:
    """Build and validate an event before session-local sequencing.

    Args:
        event_type: Canonical type from ``TAXONOMY``.
        data: Complete payload for the selected event type.

    Returns:
        An unsequenced JSON-compatible event envelope.
    """
    if event_type not in TAXONOMY:
        raise ValueError(f"unknown event type: {event_type!r}")
    if event_type in CONTEXT_APPEND_TYPES:
        _validate_content(data, event_type)
        role = data.get("role")
        if event_type == "context/append":
            if not isinstance(role, str) or not role:
                raise ValueError("context/append requires a non-empty role")
        elif role is not None:
            raise ValueError(f"{event_type} derives role from its type")
    elif event_type == "context/set":
        messages = data.get("messages")
        if not isinstance(messages, list):
            raise ValueError("context/set requires messages")
        for message in messages:
            _validate_message(message, event_type)
    elif event_type == "model/set":
        if not isinstance(data.get("model"), str) or not data["model"]:
            raise ValueError("model/set requires model")
        if not isinstance(data.get("parameters"), dict):
            raise TypeError("model/set requires parameters")
    elif event_type.startswith("model/") and event_type != "model/set":
        if not isinstance(data.get("requestId"), str) or not data["requestId"]:
            raise ValueError(f"{event_type} requires requestId")
    elif event_type.startswith("tool/"):
        if not isinstance(data.get("callId"), str) or not data["callId"]:
            raise ValueError(f"{event_type} requires callId")
        if event_type == "tool/start" and not isinstance(data.get("name"), str):
            raise TypeError("tool/start requires name")
        if event_type == "tool/end" and (("result" in data) == ("error" in data)):
            raise ValueError("tool/end requires exactly one of result or error")
    elif event_type == "session/error" and not isinstance(data.get("error"), dict):
        raise TypeError("session/error requires error")
    elif event_type == "session/end" and not isinstance(data.get("outcome"), str):
        raise TypeError("session/end requires outcome")
    if event_type == "model/response":
        _validate_message(data.get("message"), event_type)
    elif event_type in {"model/delta/text", "model/delta/reasoning"}:
        if not isinstance(data.get("index"), int) or not isinstance(data.get("text"), str):
            raise TypeError(f"{event_type} requires integer index and string text")
    elif event_type == "model/delta/tool-call" and (
            not isinstance(data.get("index"), int)
            or not isinstance(data.get("callId"), str)
            or not isinstance(data.get("argumentsDelta"), str)):
        raise TypeError("model/delta/tool-call requires index, callId, and argumentsDelta")
    event = {"ts": time.time(), "type": event_type, "data": data}
    json.dumps(event)
    return event


def message_of(event: dict) -> dict | None:
    """Return the context message appended by an event, if any."""
    event_type = event.get("type")
    if event_type not in CONTEXT_APPEND_TYPES:
        return None
    data = dict(event.get("data") or {})
    if event_type == "context/append":
        return data
    data["role"] = event_type.rsplit("/", 1)[-1]
    return data


def fold_request_state(events: list[dict]) -> dict:
    """Fold model and context state from an ordered event prefix.

    Args:
        events: Events in log order. Activity and semantic markers are ignored.

    Returns:
        ``model`` and ``context`` snapshots after the prefix.
    """
    state = {"model": None, "context": []}
    for event in events:
        event_type = event.get("type")
        data = event.get("data") or {}
        if event_type == "model/set":
            state["model"] = data
        elif event_type == "context/set":
            state["context"] = list(data["messages"])
        else:
            message = message_of(event)
            if message is not None:
                state["context"] = [*state["context"], message]
    return state


def truncate(text: str | None, n: int) -> tuple[int, str]:
    """Return original length and a bounded preview."""
    if not text:
        return (0, "")
    if len(text) <= n:
        return (len(text), text)
    return (len(text), text[:n] + "…")


def _jsonable(value):
    """Convert assembly values to JSON, replacing opaque objects by type names."""
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


def _session_id(session: str | None, run_id: str | None, session_name: str | None) -> str:
    """Return an explicit session id or derive one from stable caller metadata."""
    if session:
        return session
    namespace = run_id or session_name or "agent"
    return f"{namespace}/{uuid.uuid4()}"


def _value(value, keys: tuple[str, ...]):
    for key in keys:
        item = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
        if item is not None:
            return item
    return None


def _normalize_usage(value) -> dict | None:
    """Normalize provider token counters without manufacturing zero values."""
    if not value:
        return None
    mapping = {
        "input": ("input", "input_tokens", "prompt_tokens", "inputTokens"),
        "output": ("output", "output_tokens", "completion_tokens", "outputTokens"),
        "cacheRead": ("cacheRead", "cache_read_input_tokens", "cacheReadTokens"),
        "cacheWrite": ("cacheWrite", "cache_write_input_tokens", "cacheWriteTokens"),
        "reasoning": ("reasoning", "reasoning_tokens", "reasoningTokens"),
    }
    result = {name: token for name, keys in mapping.items()
              if (token := _value(value, keys)) is not None}
    return result or None


_active_bus: "EventBus | None" = None


def set_active_bus(bus: "EventBus | None") -> None:
    """Set the observation bus used by new recorder events."""
    global _active_bus
    _active_bus = bus


def _ensure_maybe_bus() -> "EventBus | None":
    bus = _active_bus
    if bus is None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return None
        from .sinks import ensure_bus

        ensure_bus()
        bus = _active_bus
    return bus


class EventRecorder:
    """Publish one session's canonical state, activity, and semantic markers."""

    def __init__(self, session: str, *, generator: str = "", label: str | None = None,
                 run_id: str | None = None):
        self.session = session
        self.generator = generator
        self.label = label or session
        self.run_id = run_id
        self.seq = 0
        self.text_chars = 0
        self.started_at = time.monotonic()
        self.request_n = 0
        self.request_id: str | None = None
        self.turn_open = False
        self.step_open = False
        self.ended = False
        self.reason: str | None = None
        self.result_usage: dict | None = None
        self.result_stop_reason: str | None = None
        self.result_cost_usd: float | None = None
        self._keepwarm_task: asyncio.Task | None = None
        self._tool_calls_seen: set[str] = set()
        self._context: list[dict] = []

    def event(self, event_type: str, **data) -> dict | None:
        """Validate, sequence, and publish one event."""
        bus = _ensure_maybe_bus()
        if bus is None or not bus.enabled:
            return None
        event = new_event(event_type, **data)
        event["session"] = self.session
        event["seq"] = self.seq
        self.seq += 1
        bus.publish(event)
        return event

    def start(self) -> None:
        """Open the session without imposing turn or step semantics."""
        data = {"generator": self.generator, "label": self.label}
        if self.run_id is not None:
            data["runId"] = self.run_id
        self.event("session/start", **data)

    def set_model(self, model: str, *, provider: str | None = None,
                  parameters: dict | None = None) -> None:
        """Replace the complete model configuration."""
        data = {"model": model, "parameters": _jsonable(parameters or {})}
        if provider:
            data["provider"] = provider
        self.event("model/set", **data)

    def append_context(self, message: dict) -> None:
        """Append a message using a role-specialized event when possible."""
        data = dict(message)
        role = data.pop("role", None)
        event_type = f"context/append/{role}" if role in {"user", "assistant", "tool"} else "context/append"
        if event_type == "context/append":
            data["role"] = role
        self.event(event_type, **data)
        self._context.append(dict(message))

    def set_context(self, messages: list[dict]) -> None:
        """Replace the complete ordered model context."""
        self.event("context/set", messages=messages)
        self._context = [dict(message) for message in messages]

    def context(self) -> list[dict]:
        """Return the recorder's current context snapshot."""
        return [dict(message) for message in self._context]

    def begin_turn(self) -> None:
        """Open a conventional turn marker, closing an earlier one first."""
        self.end_turn()
        self.event("turn/start")
        self.turn_open = True

    def end_turn(self, *, outcome: str = "completed", reason: str | None = None) -> None:
        """Close the recorder-managed turn marker when one is open."""
        if not self.turn_open:
            return
        self.end_step(outcome=outcome)
        data = {"outcome": outcome}
        if reason:
            data["reason"] = reason
        self.event("turn/end", **data)
        self.turn_open = False

    def begin_step(self) -> None:
        """Open a conventional step marker, closing an earlier one first."""
        self.end_step()
        self.event("step/start")
        self.step_open = True

    def end_step(self, *, outcome: str = "completed") -> None:
        """Close the recorder-managed step marker when one is open."""
        if not self.step_open:
            return
        self.event("step/end", outcome=outcome)
        self.step_open = False

    def model_request(self) -> str:
        """Open one model activity correlation and return its request id."""
        self.result_usage = None
        self.result_stop_reason = None
        self.result_cost_usd = None
        self.request_n += 1
        self.request_id = f"r{self.request_n}"
        self.event("model/request", requestId=self.request_id)
        return self.request_id

    def delta(self, kind: str, **data) -> None:
        """Emit one model delta for the active request."""
        request_id = self.request_id or self.model_request()
        self.event(f"model/delta/{kind}", requestId=request_id, **data)

    def text(self, text: str, *, index: int = 0) -> None:
        """Emit a text delta and update output accounting."""
        self.text_chars += len(text)
        self.delta("text", index=index, text=text)

    def reasoning(self, text: str, *, index: int = 0) -> None:
        """Emit a reasoning delta without adding it to context."""
        self.delta("reasoning", index=index, text=text)

    def tool_call_delta(self, *, index: int, call_id: str, name: str | None = None,
                        arguments_delta: str = "") -> None:
        """Emit one streaming tool-call fragment."""
        data = {"index": index, "callId": call_id, "argumentsDelta": arguments_delta}
        if name:
            data["name"] = name
        self.delta("tool-call", **data)

    def model_response(self, message: dict, *, stop_reason: str | None = None,
                       usage=None) -> None:
        """Record a complete model output without committing it to context."""
        request_id = self.request_id or self.model_request()
        data = {"requestId": request_id, "message": message}
        normalized = _normalize_usage(usage)
        if normalized:
            data["usage"] = normalized
            self.result_usage = normalized
        if stop_reason:
            data["stopReason"] = stop_reason
            self.result_stop_reason = stop_reason
        self.event("model/response", **data)
        self.request_id = None

    def tool_start(self, call_id: str, name: str, arguments) -> None:
        """Record the start of a local tool invocation."""
        self._tool_calls_seen.add(call_id)
        self.event("tool/start", callId=call_id, name=name, arguments=_jsonable(arguments))

    def tool_end(self, call_id: str, *, result=None, error: dict | None = None) -> None:
        """Record the terminal local tool result."""
        data = {"callId": call_id}
        if error is None:
            data["result"] = _jsonable(result)
        else:
            data["error"] = _jsonable(error)
        self.event("tool/end", **data)

    def step_boundary(self) -> None:
        """Open the next conventional step and model request."""
        self.begin_step()
        self.model_request()

    def tool_call(self, call_id: str, name: str | None, arguments) -> None:
        """Record a tool execution start from adapters that observe it atomically."""
        if call_id in self._tool_calls_seen:
            return
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError:
            parsed = arguments
        self.tool_start(call_id, name or "", parsed)

    def tool_result(self, message: dict, *, call_id: str, name: str | None,
                    is_error: bool, **_ignored) -> None:
        """Record a tool terminal and commit the model-visible result."""
        if call_id not in self._tool_calls_seen:
            self.tool_start(call_id, name or "", None)
        content = message.get("content") or []
        block = content[0] if content and isinstance(content[0], dict) else {}
        result = block.get("content", content)
        error = ({"type": "ToolError", "message": str(result)} if is_error else None)
        self.tool_end(call_id, result=result, error=error)
        committed = {
            "role": "tool", "callId": call_id, "name": name or "",
            "isError": is_error,
            "content": [{"type": "text", "text": str(result or "")}],
        }
        self.append_context(committed)

    def result_meta(self, message) -> None:
        """Retain provider summary fields for the session footer."""
        self.result_usage = _normalize_usage(getattr(message, "usage", None))
        self.result_stop_reason = getattr(message, "stop_reason", None)
        self.result_cost_usd = getattr(message, "total_cost_usd", None)

    def error(self, exc: Exception, scope: str = "agent") -> None:
        """Record an unhandled session error."""
        self.reason = str(exc)[:2000]
        self.event("session/error", scope=scope,
                   error={"type": type(exc).__name__, "message": str(exc)})

    def finish(self, ok: bool) -> None:
        """Close managed markers and publish the terminal event once."""
        if self.ended:
            return
        self.ended = True
        if self._keepwarm_task is not None:
            self._keepwarm_task.cancel()
            self._keepwarm_task = None
        outcome = "completed" if ok else "failed"
        self.end_turn(outcome=outcome, reason="final_response" if ok else "error")
        data = {"outcome": outcome,
                "durationMs": int((time.monotonic() - self.started_at) * 1000)}
        if self.reason:
            data["reason"] = self.reason
        if self.result_usage:
            data["usage"] = self.result_usage
        if self.result_stop_reason:
            data["stopReason"] = self.result_stop_reason
        if self.result_cost_usd is not None:
            data["costUsd"] = self.result_cost_usd
        self.event("session/end", **data)

    def start_keepwarm(self, interval: float) -> None:
        """Touch the session file periodically without adding protocol events."""
        if self._keepwarm_task is None and interval > 0 and _active_bus is not None:
            self._keepwarm_task = asyncio.create_task(self._keepwarm_loop(interval))

    async def stop_keepwarm(self) -> None:
        """Cancel and await the keep-warm task."""
        task = self._keepwarm_task
        self._keepwarm_task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _keepwarm_loop(self, interval: float) -> None:
        from .sinks import touch

        while True:
            await asyncio.sleep(interval)
            await touch(self.session)

class EventBus:
    """Loop-affine, non-blocking fan-out to isolated bounded sink queues."""

    def __init__(self):
        self._sinks: list[asyncio.Queue[dict]] = []
        self._tasks: list[asyncio.Task] = []

    @property
    def enabled(self) -> bool:
        return bool(self._sinks)

    def add(self, consume) -> None:
        """Register one asynchronous event consumer."""
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=5000)
        self._sinks.append(queue)
        self._tasks.append(asyncio.create_task(self._drain(consume, queue)))

    async def _drain(self, consume, queue) -> None:
        while True:
            event = await queue.get()
            try:
                await consume(event)
            except Exception as exc:
                _log(f"sink consume failed: {type(exc).__name__}: {exc}")

    def publish(self, event: dict) -> None:
        """Enqueue one event for every sink without blocking the producer."""
        for queue in self._sinks:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                queue.get_nowait()
                queue.put_nowait(event)

    def shutdown(self) -> None:
        """Cancel every sink worker."""
        for task in self._tasks:
            task.cancel()
