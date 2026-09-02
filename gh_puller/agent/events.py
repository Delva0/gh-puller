"""Define the canonical Agent observation language and its in-process bus.

``agent/*`` records opaque control state, while ``context/*`` folds an ordered Item
sequence asserted by an adapter. System-input semantics are defined in ``context``.
A model inference produces
``reasoning? -> message? -> function_call*`` in one ``model/response``; committing
that output is a separate Context fact. Lifecycle, turn, and step events are semantic
markers and never affect the fold.

Configuration and effect remain separate facts. Credential-shaped Agent configuration
fields are redacted before reaching any sink.
"""

import asyncio
import json
import time
import uuid

from gh_puller.utils import _log

CONTEXT_APPEND_ROLES = frozenset({"system", "user", "assistant", "tool"})
CONTEXT_APPEND_TYPES = frozenset({
    "context/append",
    *(f"context/append/{role}" for role in CONTEXT_APPEND_ROLES),
})
DELTA_TYPES = frozenset({
    "model/delta/text", "model/delta/reasoning", "model/delta/tool-call",
})
MODEL_OUTPUT_TYPES = ("reasoning", "message", "function_call")
_MODEL_OUTPUT_RANK = {item_type: rank for rank, item_type in enumerate(MODEL_OUTPUT_TYPES)}
_DELTA_BACKLOG = 5000
EVENT_TYPES = frozenset({
    "session/start", "session/end", "session/error",
    "turn/start", "turn/end", "step/start", "step/end",
    "agent/set", "context/set",
    "model/request", "model/response", "tool/start", "tool/end",
}) | CONTEXT_APPEND_TYPES | DELTA_TYPES


def _agent_facet(event_type: str) -> str | None:
    prefix = "agent/set/"
    facet = event_type.removeprefix(prefix)
    return facet if event_type.startswith(prefix) and facet and "/" not in facet else None


def is_event_type(event_type: str) -> bool:
    """Return whether a type belongs to the canonical language."""
    return event_type in EVENT_TYPES or _agent_facet(event_type) is not None


def is_compact_event(event_type: str) -> bool:
    """Return whether an event is retained in replay-equivalent compact history."""
    return is_event_type(event_type) and event_type not in DELTA_TYPES


def _put_event(queue: asyncio.Queue[dict], event: dict) -> None:
    """Enqueue every durable fact while bounding dispensable stream backlog."""
    if event.get("type") in DELTA_TYPES and queue.qsize() >= _DELTA_BACKLOG:
        return
    queue.put_nowait(event)


def _validate_content(content, event_type: str) -> None:
    if not isinstance(content, list):
        raise TypeError(f"{event_type} requires list content")
    if any(not isinstance(block, dict) or not isinstance(block.get("type"), str)
           for block in content):
        raise ValueError(f"{event_type} content parts require a string type")


def _validate_item(item, event_type: str) -> None:
    if not isinstance(item, dict) or not isinstance(item.get("type"), str) or not item["type"]:
        raise TypeError(f"{event_type} requires typed items")
    item_type = item["type"]
    if item_type == "message":
        if not isinstance(item.get("role"), str) or not item["role"]:
            raise TypeError(f"{event_type} message requires role")
        _validate_content(item.get("content"), event_type)
    elif item_type == "reasoning":
        if "content" in item:
            _validate_content(item["content"], event_type)
    elif item_type == "function_call":
        if not isinstance(item.get("call_id"), str) or not item["call_id"]:
            raise ValueError(f"{event_type} function_call requires call_id")
        if not isinstance(item.get("name"), str) or not isinstance(item.get("arguments"), str):
            raise TypeError(f"{event_type} function_call requires name and string arguments")
    elif item_type == "function_call_output":
        if not isinstance(item.get("call_id"), str) or not item["call_id"]:
            raise ValueError(f"{event_type} function_call_output requires call_id")
        if "output" not in item:
            raise ValueError(f"{event_type} function_call_output requires output")


def _validate_items(items, event_type: str) -> None:
    if not isinstance(items, list):
        raise TypeError(f"{event_type} requires list items")
    for item in items:
        _validate_item(item, event_type)


def _validate_model_output(output, event_type: str) -> None:
    _validate_items(output, event_type)
    ranks = []
    for item in output:
        if item["type"] not in _MODEL_OUTPUT_RANK:
            raise ValueError(f"{event_type} unsupported output item: {item['type']!r}")
        ranks.append(_MODEL_OUTPUT_RANK[item["type"]])
    if ranks != sorted(ranks):
        raise ValueError(f"{event_type} output must be reasoning -> message -> function_call")


def new_event(event_type: str, **data) -> dict:
    """Build and validate an event before session-local sequencing.

    Args:
        event_type: Canonical event type.
        data: Complete payload for the selected event type.

    Returns:
        An unsequenced JSON-compatible event envelope.
    """
    if not is_event_type(event_type):
        raise ValueError(f"unknown event type: {event_type!r}")
    facet = _agent_facet(event_type)
    if event_type == "agent/set":
        if not isinstance(data.get("agent"), str) or not data["agent"]:
            raise ValueError("agent/set requires agent")
        if not isinstance(data.get("config"), dict):
            raise TypeError("agent/set requires config")
    elif facet is not None:
        if facet not in data:
            raise ValueError(f"{event_type} requires {facet}")
    elif event_type in CONTEXT_APPEND_TYPES or event_type == "context/set":
        _validate_items(data.get("items"), event_type)
    elif event_type.startswith("model/"):
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
        _validate_model_output(data.get("output"), event_type)
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


def items_of(event: dict) -> list[dict] | None:
    """Return the Context items appended by an event, if any."""
    if event.get("type") not in CONTEXT_APPEND_TYPES:
        return None
    return list((event.get("data") or {}).get("items") or [])


def fold_state(events: list[dict]) -> dict:
    """Fold Agent and Context state from an ordered event prefix.

    Args:
        events: Events in log order. Activity and semantic markers are ignored.

    Returns:
        ``agent`` and ``context`` snapshots after the prefix.
    """
    state = {"agent": None, "context": []}
    for event in events:
        event_type = event.get("type")
        data = event.get("data") or {}
        if event_type == "agent/set":
            state["agent"] = {"agent": data["agent"], "config": dict(data["config"])}
        elif (facet := _agent_facet(event_type)) is not None:
            agent = state["agent"] or {"agent": None, "config": {}}
            state["agent"] = {
                **agent,
                "config": {**agent["config"], facet: data[facet]},
            }
        elif event_type == "context/set":
            state["context"] = list(data["items"])
        else:
            items = items_of(event)
            if items is not None:
                state["context"] = [*state["context"], *items]
    return state


def truncate(text: str | None, n: int) -> tuple[int, str]:
    """Return original length and a bounded preview."""
    if not text:
        return (0, "")
    if len(text) <= n:
        return (len(text), text)
    return (len(text), text[:n] + "…")


_REDACTED = "<redacted>"
_SECRET_SUFFIXES = (
    "apikey", "authkey", "accesstoken", "refreshtoken", "idtoken", "privatekey",
    "secretkey", "accesskey", "clientsecret", "password", "passwd", "authorization",
    "credential", "credentials", "token",
)


def _secret_key(key: str) -> bool:
    normalized = "".join(char for char in key.lower() if char.isalnum())
    return normalized.endswith(_SECRET_SUFFIXES)


def _jsonable(value):
    """Convert adapter values to JSON-compatible observation values."""
    if isinstance(value, dict):
        return {name: _jsonable(item) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


def _redacted(value, key: str | None = None):
    """Convert Agent configuration while hiding credential-shaped fields."""
    if key is not None and _secret_key(key) and value is not None:
        return _REDACTED
    if isinstance(value, dict):
        return {name: _redacted(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redacted(item) for item in value]
    return _jsonable(value)


def message_item(role: str, content: list[dict]) -> dict:
    """Build a Responses-style message item.

    Args:
        role: Model-context role.
        content: Ordered input or output content parts.
    """
    return {"type": "message", "role": role, "content": content}


def text_message(role: str, text: str) -> dict:
    """Build a text message, selecting input or output text from its role.

    Args:
        role: Message role; ``assistant`` selects ``output_text``.
        text: Complete visible message text.
    """
    part_type = "output_text" if role == "assistant" else "input_text"
    return message_item(role, [{"type": part_type, "text": text}])


def reasoning_item(text: str) -> dict:
    """Build one exposed reasoning output item.

    Args:
        text: Complete reasoning text observed by the adapter.
    """
    return {"type": "reasoning", "content": [{"type": "reasoning_text", "text": text}]}


def function_call_item(call_id: str, name: str, arguments) -> dict:
    """Build one model-produced function call.

    Args:
        call_id: Correlation shared with execution and output.
        name: Function name.
        arguments: Serialized or structured arguments.
    """
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": raw}


def function_output_item(call_id: str, output) -> dict:
    """Build one function result returned to the model Context.

    Args:
        call_id: Correlation shared with the originating function call.
        output: Backend-visible function result.
    """
    return {"type": "function_call_output", "call_id": call_id, "output": output}


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
    """Normalize provider counters, omitting absent and all-zero reports."""
    if not value:
        return None
    mapping = {
        "input": ("input", "input_tokens", "prompt_tokens", "inputTokens"),
        "output": ("output", "output_tokens", "completion_tokens", "outputTokens"),
        "cacheRead": (
            "cacheRead", "cache_read_input_tokens", "cached_input_tokens", "cacheReadTokens",
        ),
        "cacheWrite": ("cacheWrite", "cache_write_input_tokens", "cacheWriteTokens"),
        "reasoning": (
            "reasoning", "reasoning_tokens", "reasoning_output_tokens", "reasoningTokens",
        ),
    }
    result = {name: token for name, keys in mapping.items()
              if (token := _value(value, keys)) is not None}
    return result if any(result.values()) else None


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

    def __init__(self, session: str, *, agent: str = "", config: dict | None = None,
                 label: str | None = None, run_id: str | None = None):
        """Create an unstarted session recorder.

        Args:
            session: Stable event-stream identity.
            agent: Initial Agent identifier; empty defers ``agent/set``.
            config: Complete initial Agent configuration.
            label: Human-readable session label.
            run_id: Optional caller correlation.
        """
        self.session = session
        self.agent = agent
        self.config = dict(config or {})
        self.label = label or session
        self.run_id = run_id
        self.seq = 0
        self.started_at = time.monotonic()
        self.request_n = 0
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
        """Validate, sequence, and publish one event.

        Args:
            event_type: Canonical event route.
            data: Complete route payload.

        Returns:
            The published envelope, or ``None`` when observation is disabled.
        """
        bus = _ensure_maybe_bus()
        if bus is None or not bus.enabled:
            return None
        event = new_event(event_type, **_jsonable(data))
        event["session"] = self.session
        event["seq"] = self.seq
        self.seq += 1
        bus.publish(event)
        return event

    def start(self) -> None:
        """Open the session without imposing turn or step semantics."""
        data = {"label": self.label}
        if self.run_id is not None:
            data["runId"] = self.run_id
        self.event("session/start", **data)
        if self.agent:
            self.set_agent(self.agent, self.config)

    def set_agent(self, agent: str, config: dict) -> None:
        """Replace the observed Agent identity and opaque configuration.

        Args:
            agent: Agent identifier.
            config: Complete configuration without semantic interpretation.
        """
        self.event("agent/set", agent=agent, config=_redacted(config))

    def set_agent_facet(self, facet: str, value) -> None:
        """Replace one explicitly observed Agent control facet.

        Args:
            facet: Single route segment and configuration key.
            value: New facet value.
        """
        self.event(f"agent/set/{facet}", **{facet: _redacted(value, facet)})

    def append_context(self, items: dict | list[dict], *, role: str | None = None) -> None:
        """Append one atomic Item batch using a role-specialized route when possible.

        Args:
            items: One Item or an ordered Item sequence.
            role: Semantic producer role used only to select the event route. A
                single message item supplies its own role when omitted.
        """
        batch = [items] if isinstance(items, dict) else list(items)
        if role is None and len(batch) == 1 and batch[0].get("type") == "message":
            role = batch[0].get("role")
        event_type = f"context/append/{role}" if role in CONTEXT_APPEND_ROLES else "context/append"
        items = _jsonable(batch)
        self.event(event_type, items=items)
        self._context.extend(items)

    def set_context(self, items: list[dict]) -> None:
        """Replace the complete ordered model Context.

        Args:
            items: New complete Item sequence.
        """
        snapshot = _jsonable(items)
        self.event("context/set", items=snapshot)
        self._context = snapshot

    def context(self) -> list[dict]:
        """Return the recorder's current context snapshot.

        Returns:
            A shallow copy of the current Item sequence.
        """
        return [dict(item) for item in self._context]

    def begin_turn(self) -> None:
        """Open a conventional turn marker, closing an earlier one first."""
        self.end_turn()
        self.event("turn/start")
        self.turn_open = True

    def end_turn(self, *, outcome: str = "completed", reason: str | None = None) -> None:
        """Close the recorder-managed turn marker when one is open.

        Args:
            outcome: Adapter-defined turn outcome.
            reason: Optional adapter-defined completion reason.
        """
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
        """Close the recorder-managed step marker when one is open.

        Args:
            outcome: Adapter-defined step outcome.
        """
        if not self.step_open:
            return
        self.event("step/end", outcome=outcome)
        self.step_open = False

    def model_request(self, *, request_id: str | None = None, **data) -> str:
        """Record one model request and return its independent correlation id.

        Args:
            request_id: Caller correlation; omitted to allocate a session-local id.
            data: Model facts observed for this request.

        Returns:
            The explicit or allocated request id.
        """
        self.result_usage = None
        self.result_stop_reason = None
        self.result_cost_usd = None
        if request_id is None:
            self.request_n += 1
            request_id = f"r{self.request_n}"
        self.event("model/request", requestId=request_id, **_jsonable(data))
        return request_id

    def delta(self, kind: str, *, request_id: str, **data) -> None:
        """Emit one model delta for an explicit request correlation.

        Args:
            kind: Delta route suffix.
            request_id: Owning model request.
            data: Complete delta payload.
        """
        self.event(f"model/delta/{kind}", requestId=request_id, **data)

    def text(self, text: str, *, request_id: str, index: int = 0) -> None:
        """Emit a text delta for a model request.

        Args:
            text: Incremental text.
            request_id: Owning model request.
            index: Adapter-local text stream lane.
        """
        self.delta("text", request_id=request_id, index=index, text=text)

    def reasoning(self, text: str, *, request_id: str, index: int = 0) -> None:
        """Emit a reasoning delta without adding it to context.

        Args:
            text: Incremental reasoning text.
            request_id: Owning model request.
            index: Adapter-local reasoning stream lane.
        """
        self.delta("reasoning", request_id=request_id, index=index, text=text)

    def tool_call_delta(self, *, request_id: str, index: int, call_id: str,
                        name: str | None = None, arguments_delta: str = "") -> None:
        """Emit one streaming tool-call fragment.

        Args:
            request_id: Owning model request.
            index: Adapter-local tool-call stream lane.
            call_id: Tool-call correlation.
            name: Tool name when available.
            arguments_delta: Incremental serialized arguments.
        """
        data = {"index": index, "callId": call_id, "argumentsDelta": arguments_delta}
        if name:
            data["name"] = name
        self.delta("tool-call", request_id=request_id, **data)

    def model_response(self, output: list[dict], *, request_id: str, model: str | None = None,
                       stop_reason: str | None = None, usage=None) -> None:
        """Record a complete model output without committing it to context.

        Args:
            output: Complete ordered model output Items.
            request_id: Owning model request.
            model: Effective response model when exposed by the backend.
            stop_reason: Backend completion reason when exposed.
            usage: Backend token counters when exposed.
        """
        data = {"requestId": request_id, "output": output}
        if model:
            data["model"] = model
        normalized = _normalize_usage(usage)
        if normalized:
            data["usage"] = normalized
            self.result_usage = normalized
        if stop_reason:
            data["stopReason"] = stop_reason
            self.result_stop_reason = stop_reason
        self.event("model/response", **data)

    def tool_start(self, call_id: str, name: str, arguments) -> None:
        """Record the start of a local tool invocation.

        Args:
            call_id: Tool-call correlation.
            name: Tool name.
            arguments: Effective invocation arguments.
        """
        self._tool_calls_seen.add(call_id)
        self.event("tool/start", callId=call_id, name=name, arguments=_jsonable(arguments))

    def tool_end(self, call_id: str, *, result=None, error: dict | None = None) -> None:
        """Record the terminal local tool result.

        Args:
            call_id: Tool-call correlation.
            result: Successful result; mutually exclusive with ``error``.
            error: Structured failure; mutually exclusive with ``result``.
        """
        data = {"callId": call_id}
        if error is None:
            data["result"] = _jsonable(result)
        else:
            data["error"] = _jsonable(error)
        self.event("tool/end", **data)

    def tool_call(self, call_id: str, name: str | None, arguments) -> None:
        """Record an atomically observed tool execution start.

        Args:
            call_id: Tool-call correlation.
            name: Tool name when available.
            arguments: Structured or serialized arguments.
        """
        if call_id in self._tool_calls_seen:
            return
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError:
            parsed = arguments
        self.tool_start(call_id, name or "", parsed)

    def tool_result(self, output, *, call_id: str, name: str | None,
                    is_error: bool, **_ignored) -> None:
        """Record a tool terminal and commit its model-visible result.

        Args:
            output: Model-visible function result.
            call_id: Tool-call correlation.
            name: Tool name when available.
            is_error: Whether execution failed.
            _ignored: Adapter metadata without canonical semantics.
        """
        if call_id not in self._tool_calls_seen:
            self.tool_start(call_id, name or "", None)
        error = ({"type": "ToolError", "message": str(output)} if is_error else None)
        self.tool_end(call_id, result=output, error=error)
        self.append_context(function_output_item(call_id, output), role="tool")

    def result_meta(self, message) -> None:
        """Retain backend summary fields for the session footer.

        Args:
            message: Backend result exposing optional usage, reason, and cost.
        """
        self.result_usage = _normalize_usage(getattr(message, "usage", None))
        self.result_stop_reason = getattr(message, "stop_reason", None)
        self.result_cost_usd = getattr(message, "total_cost_usd", None)

    def error(self, exc: Exception, scope: str = "agent") -> None:
        """Record an unhandled session error.

        Args:
            exc: Failure exposed to the caller.
            scope: Component that failed.
        """
        self.reason = str(exc)[:2000]
        self.event("session/error", scope=scope,
                   error={"type": type(exc).__name__, "message": str(exc)})

    def finish(self, ok: bool) -> None:
        """Close managed markers and publish the terminal event once.

        Args:
            ok: Whether the Agent session completed successfully.
        """
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
        """Touch the session file periodically without adding protocol events.

        Args:
            interval: Seconds between touches; non-positive disables the task.
        """
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
    """Loop-affine, non-blocking fan-out that never drops compact events."""

    def __init__(self):
        self._sinks: list[asyncio.Queue[dict]] = []
        self._tasks: list[asyncio.Task] = []

    @property
    def enabled(self) -> bool:
        return bool(self._sinks)

    def add(self, consume) -> None:
        """Register one asynchronous event consumer.

        Args:
            consume: Coroutine function accepting one event envelope.
        """
        queue: asyncio.Queue[dict] = asyncio.Queue()
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
        """Enqueue one event for every sink without blocking the producer.

        Args:
            event: Canonical event envelope.
        """
        for queue in self._sinks:
            _put_event(queue, event)

    def shutdown(self) -> None:
        """Cancel every sink worker."""
        for task in self._tasks:
            task.cancel()
