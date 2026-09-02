"""Adapt Claude Code SDK messages to canonical agent events."""

from typing import TypedDict

from ..events import EventRecorder, _normalize_usage
from .base import BaseGenerator
from .utils import RequestFailedError


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

    def __init__(self):
        self.tool_pending = False
        self.active_tool_use: dict[int, str] = {}
        self.tool_result: dict | None = None
        self.tool_results_seen: set[str] = set()
        self.tool_names: dict[str, str] = {}
        self.message_text = ""
        self.message_reasoning = ""
        self.message_stop_reason: str | None = None
        self.message_yielded = False

    def reset_message(self) -> None:
        self.message_text = ""
        self.message_reasoning = ""
        self.message_stop_reason = None
        self.message_yielded = False


def _handle_stream_event(event_recorder: EventRecorder, state: _ClaudeSynth,
                         event: dict) -> None:
    """Translate one Claude streaming event without changing yielded text."""
    typ = event.get("type")
    if typ == "message_start":
        if state.tool_pending and (event.get("message") or {}).get("role") == "assistant":
            event_recorder.step_boundary()
            state.tool_pending = False
        state.reset_message()
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
            event_recorder.text(text, index=idx)
            return
        if dtype == "thinking_delta":
            text = delta.get("thinking") or ""
            state.message_reasoning += text
            event_recorder.reasoning(text, index=idx)
            return
        if dtype == "input_json_delta":
            piece = delta.get("partial_json") or ""
            tid = state.active_tool_use.get(idx, "pending")
            event_recorder.tool_call_delta(
                index=idx, call_id=tid, name=state.tool_names.get(tid),
                arguments_delta=piece)
        return
    if typ == "content_block_stop":
        idx = event.get("index", -1)
        if state.tool_result is not None:
            text = "".join(state.tool_result["pieces"])
            tid = state.tool_result["id"]
            event_recorder.tool_result(
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": text,
                     "is_error": state.tool_result["is_error"]},
                ]},
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


def _handle_assistant_message(event_recorder: EventRecorder, state: _ClaudeSynth,
                              msg) -> list[str]:
    """Commit one complete assistant message, rebuilding empty partial markers."""
    content = []
    fallback = []
    for b in msg.content:
        t = _block_kind(b)
        if t == "text":
            text = getattr(b, "text", None) or ""
            if text and not state.message_yielded:
                event_recorder.text(text)
                fallback.append(text)
            content.append({"type": "text", "text": text})
        elif t == "thinking":
            content.append({"type": "reasoning", "text": getattr(b, "thinking", None) or ""})
        elif t == "tool_use":
            content.append({
                "type": "tool_call", "callId": getattr(b, "id", None) or "",
                "name": getattr(b, "name", None) or "", "arguments": getattr(b, "input", None) or {},
            })
    if not content:
        if state.message_reasoning:
            content.append({"type": "reasoning", "text": state.message_reasoning})
        if state.message_text:
            content.append({"type": "text", "text": state.message_text})
    stop_reason = getattr(msg, "stop_reason", None) or state.message_stop_reason
    message = {"role": "assistant", "content": content}
    event_recorder.model_response(
        message, usage=_normalize_usage(getattr(msg, "usage", None)), stop_reason=stop_reason)
    event_recorder.append_context(message)
    state.reset_message()
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
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                          "content": text, "is_error": is_error}]},
            call_id=tid, name=state.tool_names.get(tid),
            is_error=is_error,
        )
        state.tool_results_seen.add(tid)
        state.tool_pending = True


class ClaudeCode(BaseGenerator):
    """Run a reusable Claude Code SDK client inside one observation session."""

    generator = "cc"
    provider = "anthropic"

    def __init__(self, config: dict):
        super().__init__(config)
        from claude_agent_sdk import ClaudeSDKClient  # Lazy optional SDK import.

        self._client = ClaudeSDKClient(options=claude_options(config))

    async def _enter(self):
        await self._client.__aenter__()

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
        event_recorder.append_context({"role": "user", "content": [{"type": "text", "text": prompt}]})
        event_recorder.begin_step()
        event_recorder.model_request()
        state = _ClaudeSynth()
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
                _handle_user_message(event_recorder, state, msg)
            elif isinstance(msg, ResultMessage):
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
        event_recorder.append_context({"role": "user", "content": [{"type": "text", "text": prompt}]})
        event_recorder.begin_step()
        event_recorder.model_request()
        state = _ClaudeSynth()
        await self._client.query(prompt)
        async for msg in self._client.receive_response():
            if isinstance(msg, StreamEvent):
                _handle_stream_event(event_recorder, state, msg.event)
            elif isinstance(msg, AssistantMessage):
                _handle_assistant_message(event_recorder, state, msg)
            elif isinstance(msg, UserMessage):
                _handle_user_message(event_recorder, state, msg)
            elif isinstance(msg, ResultMessage):
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
