"""Adapt Claude Code SDK messages to canonical agent events."""

from typing import TypedDict

from ..base import BaseAgent, RequestFailedError
from ..events import (
    EventRecorder,
    _normalize_usage,
    function_call_item,
    reasoning_item,
    text_message,
)


class ClaudeConfig(TypedDict, total=False):
    """cc runtime config: keys are ClaudeAgentOptions field names (see __init__.py)."""

    model: str
    system_prompt: str
    allowed_tools: list[str]
    mcp_servers: dict
    cwd: str
    settings: str  # --settings load (only that file; credentials ride the file)
    add_dirs: list[str]
    permission_mode: str
    setting_sources: list[str]
    include_partial_messages: bool
    max_turns: int
    strict_mcp_config: bool


def claude_options(config: dict):
    """ClaudeConfig → ClaudeAgentOptions instance (keys passthrough, see __init__.py).

    Non-obvious behavior: an empty settings value is dropped (SDK default
    isolation); strict_mcp_config defaults to True — headless cc recognizes only
    the injected tool desk (mcp_servers), ignoring machine-level MCP config.
    """
    from claude_agent_sdk import ClaudeAgentOptions  # lazy: fake module in tests

    sdk_options = dict(config)
    if not sdk_options.get("settings"):  # empty → SDK default isolation
        sdk_options.pop("settings", None)
    sdk_options.setdefault("strict_mcp_config", True)  # default isolation (see above)
    # Partial messages are the only source of monitor text/thinking increments.
    sdk_options.setdefault("include_partial_messages", True)
    return ClaudeAgentOptions(**sdk_options)


def _block_kind(block) -> str:
    """Identify SDK blocks that may omit their discriminant attribute."""
    t = getattr(block, "type", None)
    if t is not None:
        return t
    if hasattr(block, "thinking"):
        return "thinking"
    if hasattr(block, "text"):
        return "text"
    if hasattr(block, "id"):
        return "tool_use"
    return ""


class _ClaudeSynth:
    """Hold SDK assembly state for one Claude call."""

    def __init__(self, request_id: str):
        self.request_id = request_id
        self.tool_pending = False
        self.active_tool_use: dict[int, str] = {}
        self.tool_result: dict | None = None
        self.tool_results_seen: set[str] = set()
        self.tool_names: dict[str, str] = {}
        self.message_id: str | None = None
        self.message_model: str | None = None
        self.complete_reasoning: list[str] = []
        self.complete_text: list[str] = []
        self.complete_calls: list[dict] = []
        self.message_usage = None
        self.message_text = ""
        self.message_reasoning = ""
        self.message_stop_reason: str | None = None
        self.message_yielded = False

    def reset_message(self) -> None:
        self.message_id = None
        self.message_model = None
        self.complete_reasoning = []
        self.complete_text = []
        self.complete_calls = []
        self.message_usage = None
        self.message_text = ""
        self.message_reasoning = ""
        self.message_stop_reason = None
        self.message_yielded = False


def _commit_assistant_message(event_recorder: EventRecorder, state: _ClaudeSynth) -> None:
    """Commit all SDK fragments belonging to one provider message."""
    reasoning = "".join(state.complete_reasoning) or state.message_reasoning
    text = "".join(state.complete_text) or state.message_text
    output = []
    if reasoning:
        output.append(reasoning_item(reasoning))
    if text:
        output.append(text_message("assistant", text))
    output.extend(state.complete_calls)
    if not output:
        state.reset_message()
        return
    event_recorder.model_response(
        output, request_id=state.request_id, model=state.message_model,
        usage=state.message_usage, stop_reason=state.message_stop_reason)
    event_recorder.append_context(output, role="assistant")
    state.reset_message()


def _has_assistant_output(state: _ClaudeSynth) -> bool:
    return bool(
        state.complete_reasoning or state.complete_text or state.complete_calls
        or state.message_reasoning or state.message_text,
    )


def _begin_next_request(event_recorder: EventRecorder, state: _ClaudeSynth) -> None:
    event_recorder.begin_step()
    state.request_id = event_recorder.model_request()
    state.tool_pending = False


def _handle_stream_event(event_recorder: EventRecorder, state: _ClaudeSynth,
                         event: dict) -> None:
    """Translate one Claude streaming event without changing yielded text."""
    typ = event.get("type")
    if typ == "message_start":
        has_output = _has_assistant_output(state)
        if has_output:
            _commit_assistant_message(event_recorder, state)
        if (has_output or state.tool_pending) \
                and (event.get("message") or {}).get("role") == "assistant":
            _begin_next_request(event_recorder, state)
        state.reset_message()
        message = event.get("message") or {}
        state.message_id = message.get("id")
        state.message_model = message.get("model")
        return
    if typ == "message_delta":
        stop = (event.get("delta") or {}).get("stop_reason")
        if stop:
            state.message_stop_reason = stop
        return
    if typ == "content_block_start":
        cb = event.get("content_block") or {}
        idx = event.get("index", -1)
        btype = cb.get("type")
        if btype == "tool_use":
            tid = cb.get("id") or ""
            state.tool_names[tid] = cb.get("name") or ""
            state.active_tool_use[idx] = tid
        elif btype == "tool_result":
            state.tool_pending = True
            state.tool_result = {
                "id": cb.get("tool_use_id") or "", "pieces": [], "is_error": bool(cb.get("is_error")),
            }
        return
    if typ == "content_block_delta":
        delta = event.get("delta") or {}
        dtype = delta.get("type")
        idx = event.get("index", -1)
        if state.tool_result is not None and dtype == "text_delta":
            state.tool_result["pieces"].append(delta.get("text") or "")
            return
        if dtype == "text_delta":
            text = delta.get("text") or ""
            state.message_text += text
            state.message_yielded = True
            event_recorder.text(text, request_id=state.request_id, index=idx)
            return
        if dtype == "thinking_delta":
            text = delta.get("thinking") or ""
            state.message_reasoning += text
            event_recorder.reasoning(text, request_id=state.request_id, index=idx)
            return
        if dtype == "input_json_delta":
            piece = delta.get("partial_json") or ""
            tid = state.active_tool_use.get(idx, "pending")
            event_recorder.tool_call_delta(
                request_id=state.request_id, index=idx, call_id=tid,
                name=state.tool_names.get(tid),
                arguments_delta=piece)
        return
    if typ == "content_block_stop":
        idx = event.get("index", -1)
        if state.tool_result is not None:
            text = "".join(state.tool_result["pieces"])
            tid = state.tool_result["id"]
            event_recorder.tool_result(
                text,
                call_id=tid, name=state.tool_names.get(tid),
                is_error=state.tool_result["is_error"],
            )
            state.tool_results_seen.add(tid)
            state.tool_result = None
            return
        if idx in state.active_tool_use:
            state.active_tool_use.pop(idx, None)
            return
        return
    if typ == "message_stop":
        return


def _handle_assistant_message(event_recorder: EventRecorder, state: _ClaudeSynth,
                              msg) -> list[str]:
    """Accumulate one SDK fragment and return text absent from partial events."""
    message_id = getattr(msg, "message_id", None)
    if _has_assistant_output(state) and message_id and state.message_id \
            and message_id != state.message_id:
        _commit_assistant_message(event_recorder, state)
        _begin_next_request(event_recorder, state)
    if message_id:
        state.message_id = message_id
    if model := getattr(msg, "model", None):
        state.message_model = model
    if usage := _normalize_usage(getattr(msg, "usage", None)):
        state.message_usage = {**(state.message_usage or {}), **usage}
    if stop_reason := getattr(msg, "stop_reason", None):
        state.message_stop_reason = stop_reason
    fallback = []
    for b in msg.content:
        t = _block_kind(b)
        if t == "text":
            text = getattr(b, "text", None) or ""
            if text and not state.message_yielded:
                event_recorder.text(text, request_id=state.request_id)
                fallback.append(text)
            state.complete_text.append(text)
        elif t == "thinking":
            state.complete_reasoning.append(getattr(b, "thinking", None) or "")
        elif t == "tool_use":
            call_id = getattr(b, "id", None) or ""
            name = getattr(b, "name", None) or ""
            state.tool_names[call_id] = name
            state.complete_calls.append(function_call_item(
                call_id, name, getattr(b, "input", None) or {},
            ))
    return fallback


def _handle_user_message(event_recorder: EventRecorder, state: _ClaudeSynth, msg) -> None:
    """Record tool results delivered as Claude user messages."""
    from claude_agent_sdk import ToolResultBlock  # Lazy optional SDK import.

    blocks = getattr(msg, "content", None)
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if not isinstance(block, ToolResultBlock):
            continue
        tid = block.tool_use_id or ""
        if tid in state.tool_results_seen:
            continue
        is_error = bool(block.is_error)
        raw = block.content
        if isinstance(raw, list):
            text = "".join(str(c.get("text") or "")
                           for c in raw if isinstance(c, dict) and c.get("type") == "text")
        else:
            text = raw or ""
        event_recorder.tool_result(
            text,
            call_id=tid, name=state.tool_names.get(tid),
            is_error=is_error,
        )
        state.tool_results_seen.add(tid)
        state.tool_pending = True


class ClaudeCode(BaseAgent):
    """Run a reusable Claude Code SDK client inside one observation session."""

    agent = "cc"

    def __init__(self, config: dict):
        super().__init__(config)
        from claude_agent_sdk import ClaudeSDKClient  # Lazy optional SDK import.

        self._client = ClaudeSDKClient(options=claude_options(config))

    async def _enter(self):
        await self._client.__aenter__()
        if prompt := self.config.get("system_prompt"):
            self._require_event_recorder().append_context(text_message("system", prompt))

    async def _exit(self, exc):
        await self._client.__aexit__(*exc)

    async def stream(self, prompt: str):
        """Stream assistant deltas from the Claude Code SDK query (payload-only; metadata via session()).

        Yielded text: StreamEvent text_delta first, AssistantMessage whole-text fallback,
        ResultMessage.is_error → RequestFailedError(detail); thinking/tool increments
        go to the event stream only. config passes through as ClaudeAgentOptions.
        """
        from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, UserMessage

        event_recorder = self._require_event_recorder()
        event_recorder.begin_turn()
        event_recorder.append_context(text_message("user", prompt))
        event_recorder.begin_step()
        state = _ClaudeSynth(event_recorder.model_request())
        await self._client.query(prompt)
        async for msg in self._client.receive_response():
            if isinstance(msg, StreamEvent):
                _handle_stream_event(event_recorder, state, msg.event)
                event = msg.event
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text") \
                            and state.tool_result is None:
                        yield delta["text"]
            elif isinstance(msg, AssistantMessage):
                for text in _handle_assistant_message(event_recorder, state, msg):
                    yield text
            elif isinstance(msg, UserMessage):
                _commit_assistant_message(event_recorder, state)
                _handle_user_message(event_recorder, state, msg)
            elif isinstance(msg, ResultMessage):
                _commit_assistant_message(event_recorder, state)
                if msg.is_error:
                    detail = (msg.errors or [])[-1] if msg.errors else msg.result
                    raise RequestFailedError(detail or msg.subtype)
                event_recorder.result_meta(msg)
        event_recorder.end_step()
        event_recorder.end_turn(reason="final_response")

    async def result(self, prompt: str) -> str:
        """Return the final round's output: ResultMessage.result directly; failure or no output → RequestFailedError."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, UserMessage

        event_recorder = self._require_event_recorder()
        event_recorder.begin_turn()
        event_recorder.append_context(text_message("user", prompt))
        event_recorder.begin_step()
        state = _ClaudeSynth(event_recorder.model_request())
        await self._client.query(prompt)
        async for msg in self._client.receive_response():
            if isinstance(msg, StreamEvent):
                _handle_stream_event(event_recorder, state, msg.event)
            elif isinstance(msg, AssistantMessage):
                _handle_assistant_message(event_recorder, state, msg)
            elif isinstance(msg, UserMessage):
                _commit_assistant_message(event_recorder, state)
                _handle_user_message(event_recorder, state, msg)
            elif isinstance(msg, ResultMessage):
                _commit_assistant_message(event_recorder, state)
                if msg.is_error:
                    detail = (msg.errors or [])[-1] if msg.errors else msg.result
                    raise RequestFailedError(detail or msg.subtype)
                event_recorder.result_meta(msg)
                result = msg.result
                if not result:
                    raise RequestFailedError("未产出最终结果")
                event_recorder.end_step()
                event_recorder.end_turn(reason="final_response")
                return result
        raise RequestFailedError("未产出最终结果")
