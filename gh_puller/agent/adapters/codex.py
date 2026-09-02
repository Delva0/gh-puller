"""Configure Codex SDK isolation and adapt its notifications to canonical events."""

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any, TypedDict

from ..base import BaseAgent, RequestFailedError
from ..context import OPAQUE, instruction, mcps, system_message, tool_defs
from ..events import (
    EventRecorder,
    _normalize_usage,
    function_call_item,
    message_item,
    reasoning_item,
    text_message,
)


class CodexConfig(TypedDict, total=False):
    """Configuration mapped to Codex client, thread, and turn arguments."""

    model: str
    system_prompt: str
    cwd: str
    codex_bin: str
    codex_home: str
    config_path: str
    sandbox: str
    approval_mode: str
    token: str
    env: dict
    timeout_seconds: float
    mcp_servers: list[dict]
    effort: str
    output_schema: dict
    config_overrides: dict
    launch_args_override: list[str]
    base_instructions: str
    developer_instructions: str
    service_tier: str
    summary: dict
    web_search: bool


def codex_config_fields(config: dict) -> dict:
    """Select non-null SDK client fields; ``CODEX_HOME`` is added later."""
    names = ("cwd", "codex_bin", "config_overrides", "launch_args_override", "env")
    return {k: v for k, v in ((k, config.get(k)) for k in names) if v is not None}


def codex_thread_fields(config: dict) -> dict:
    """Select thread fields and map ``system_prompt`` to base instructions."""
    names = ("base_instructions", "developer_instructions", "personality", "ephemeral",
             "model", "model_provider", "service_tier", "config", "cwd",
             "session_start_source", "thread_source")
    fields = {k: v for k, v in ((k, config.get(k)) for k in names) if v is not None}
    if config.get("system_prompt") and "base_instructions" not in fields:
        fields["base_instructions"] = config["system_prompt"]
    return fields


def codex_turn_fields(config: dict) -> dict:
    """Select non-null turn fields."""
    names = ("cwd", "effort", "output_schema", "personality", "service_tier", "summary")
    return {k: v for k, v in ((k, config.get(k)) for k in names) if v is not None}


def _codex_system_items(config: dict) -> list[dict]:
    """Project system inputs whose contents the Codex boundary exposes."""
    prompt = config.get("base_instructions") or config.get("system_prompt")
    content = [instruction(prompt) if prompt else instruction(), tool_defs([OPAQUE])]
    content.extend(mcps(config.get("mcp_servers")))
    items = [system_message(content)]
    if developer := config.get("developer_instructions"):
        items.append(message_item("developer", [instruction(developer)]))
    return items


def codex_config(config: dict):
    """Build the SDK client configuration with an isolated ``CODEX_HOME``."""
    from openai_codex import CodexConfig  # Lazy optional SDK import.

    fields = codex_config_fields(config)
    env = dict(config.get("env") or {})
    env["CODEX_HOME"] = codex_home(config)
    fields["env"] = env
    return CodexConfig(**fields)


def codex_thread(config: dict) -> dict:
    """Build thread arguments, resolving sandbox and approval enums."""
    from openai_codex import ApprovalMode, Sandbox  # Lazy optional SDK import.

    fields = codex_thread_fields(config)
    if (sandbox := config.get("sandbox")) is not None:
        fields["sandbox"] = codex_lookup(sandbox, Sandbox, "Sandbox")
    if (approval := config.get("approval_mode")) is not None:
        fields["approval_mode"] = codex_lookup(approval, ApprovalMode, "approval_mode")
    return fields


def codex_turn(config: dict) -> dict:
    """Build turn arguments, requesting visible reasoning summaries by default."""
    fields = codex_turn_fields(config)
    fields.setdefault("summary", "detailed")
    return fields


def codex_home(config: dict) -> str:
    """Prepare and return the configured isolated Codex home."""
    return codex_home_setup(config.get("codex_home") or codex_home_path(),
                            config_path=config.get("config_path"),
                            mcp_servers=config.get("mcp_servers"),
                            web_search=config.get("web_search") or False)


def codex_val(x):
    """Unwrap an SDK enum-like value."""
    return getattr(x, "value", x)


def codex_lookup(v, enum_cls, label):
    """Resolve an enum member from a member, name, or value."""
    if v is None:
        return None
    if isinstance(v, enum_cls):
        return v
    raw = codex_val(v)
    for member in enum_cls:
        if raw in (member.name, member.value):
            return member
    raise ValueError(f"invalid codex {label} (choose {[m.name for m in enum_cls]}): {v!r}")


def codex_home_path() -> str:
    """Return the stable default directory used as the Codex isolation boundary."""
    return str(Path.home() / ".gh-puller" / "codex-home")


def _codex_config_toml_content(*, mcp_servers: list[dict] | None = None,
                               web_search: bool = False) -> str:
    """Render only injected MCP servers and the explicit web-search feature."""
    sections = []
    for spec in mcp_servers or []:
        name = spec.get("id", "mcp-server")
        cmd = json.dumps(spec.get("command", ""))
        args = json.dumps(spec.get("args", []))
        env_vars = json.dumps(spec.get("env_vars", []))
        sections.append(
            f"[mcp_servers.{name}]\n"
            f"command = {cmd}\n"
            f"args = {args}\n"
            f"env_vars = {env_vars}\n"
            "startup_timeout_sec = 30\n"
            "required = true\n",
        )
    if web_search:
        sections.append("[features]\nweb_search_request = true\n")
    return "\n".join(sections)


def codex_home_setup(home, *, auth_src: str | Path | None = None,
                     config_path: str | Path | None = None,
                     mcp_servers: list[dict] | None = None,
                     web_search: bool = False) -> str:
    """Prepare isolated config and credential links without rewriting stable files.

    ``config_path`` is linked verbatim; otherwise the minimal injected config is
    rendered. Credentials link to ``auth_src`` or the user's Codex auth file unless
    explicitly disabled. Filesystems without symlink support fall back to copies.
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    cfg_path = home / "config.toml"
    if config_path is not None:
        p = Path(config_path)
        if not p.is_file():
            raise FileNotFoundError(f"codex config does not exist: {p}")
        if cfg_path.is_symlink() or cfg_path.exists():
            cfg_path.unlink()
        try:
            cfg_path.symlink_to(p)
        except OSError:  # Some filesystems cannot create symlinks.
            shutil.copyfile(p, cfg_path)
        return str(home)
    content = _codex_config_toml_content(mcp_servers=mcp_servers, web_search=web_search)
    if not cfg_path.exists() or cfg_path.is_symlink() \
            or cfg_path.read_text(encoding="utf-8") != content:
        if cfg_path.is_symlink():
            cfg_path.unlink()
        cfg_path.write_text(content, encoding="utf-8")
    auth = home / "auth.json"
    if auth_src is not False and not auth.is_symlink():
        src = Path(auth_src) if auth_src else Path.home() / ".codex" / "auth.json"
        if src.exists():
            if auth.exists() or auth.is_symlink():
                auth.unlink()
            try:
                auth.symlink_to(src)
            except OSError:  # Some filesystems cannot create symlinks.
                shutil.copyfile(src, auth)
    return str(home)


def _codex_args_json(arguments) -> str:
    """Preserve string arguments and encode structured arguments as JSON."""
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments)


def _codex_tool_name(server: str, tool: str) -> str:
    """Return the canonical Codex MCP tool name."""
    return f"mcp__{server}__{tool}" if server else tool


class _CodexSynth:
    """Hold notification assembly state for one Codex call."""

    def __init__(self, request_id: str):
        self.request_id = request_id
        self.turn_id: str | None = None
        self.agent_pieces: dict[str, list[str]] = {}
        self.reasoning_order: list[str] = []
        self.reasoning_pieces: dict[str, list[str]] = {}
        self.complete_reasoning: dict[str, str] = {}
        self.message_parts: list[dict] = []
        self.pending_tools: list[dict] = []
        self.plan_pieces: dict[str, list[str]] = {}
        self.completed_plans: set[str] = set()
        self.request_open = True
        self.usage = None
        self.total_usage = None
        self.saw_turn_completed = False
        self.final_response = ""


def _codex_item(item) -> Any:
    """Unwrap an SDK root-model item."""
    return getattr(item, "root", item)


def _codex_item_type(item) -> str:
    return getattr(_codex_item(item), "type", None) or ""


def _codex_tool_result(item, itype: str) -> dict:
    """Normalize one completed Codex tool item."""
    if itype == "mcpToolCall":
        name = _codex_tool_name(getattr(item, "server", None) or "",
                                getattr(item, "tool", None) or "")
        content_parts = []
        result = getattr(item, "result", None)
        if result is not None:
            for part in getattr(result, "content", None) or []:
                inner = _codex_item(part)
                if getattr(inner, "text", None):
                    content_parts.append(inner.text)
            structured = getattr(result, "structured_content", None)
            if structured is not None:
                content_parts.append(json.dumps(structured))
        is_error = (getattr(item, "error", None) is not None
                    or codex_val(getattr(item, "status", None)) == "failed")
        arguments = _codex_args_json(getattr(item, "arguments", None))
    elif itype == "dynamicToolCall":
        name = getattr(item, "tool", None) or ""
        content_parts = [
            getattr(_codex_item(ci), "text", None) or ""
            for ci in (getattr(item, "content_items", None) or [])
            if _codex_item_type(ci) == "inputText"
        ]
        is_error = (getattr(item, "success", None) is False
                    or codex_val(getattr(item, "status", None)) == "failed")
        arguments = _codex_args_json(getattr(item, "arguments", None))
    else:  # commandExecution
        name = "shell"
        content_parts = [getattr(item, "aggregated_output", None) or ""]
        command = getattr(item, "command", None) or ""
        cwd = getattr(item, "cwd", None) or ""
        is_error = (getattr(item, "exit_code", None) not in (None, 0)
                    or codex_val(getattr(item, "status", None)) == "failed")
        arguments = json.dumps({k: str(v) for k, v in (("command", command), ("cwd", cwd)) if v})
    return {"name": name, "content": "\n".join(p for p in content_parts if p),
            "is_error": is_error, "arguments": arguments}


def _flush_codex_response(event_recorder: EventRecorder, st: _CodexSynth) -> None:
    """Commit one inferred model output, followed by its completed tools."""
    if not st.request_open:
        return
    reasoning = []
    for item_id in st.reasoning_order:
        text = st.complete_reasoning.get(item_id) or "".join(
            st.reasoning_pieces.get(item_id) or [],
        )
        if text:
            reasoning.append(text)
    output = []
    if reasoning:
        output.append(reasoning_item("\n".join(reasoning)))
    if st.message_parts:
        output.append(message_item("assistant", list(st.message_parts)))
    output.extend(
        function_call_item(
            spec["call_id"], spec["name"], spec["arguments"],
        ) for spec in st.pending_tools
    )
    event_recorder.model_response(output, request_id=st.request_id, usage=st.usage)
    if st.total_usage:
        event_recorder.result_usage = st.total_usage
    if output:
        event_recorder.append_context(output, role="assistant")
    for spec in st.pending_tools:
        event_recorder.tool_call(spec["call_id"], spec["name"], spec["arguments"])
        event_recorder.tool_result(
            spec["content"],
            call_id=spec["call_id"], name=spec["name"], is_error=spec["is_error"])
    st.agent_pieces = {}
    st.reasoning_order = []
    st.reasoning_pieces = {}
    st.complete_reasoning = {}
    st.message_parts = []
    st.pending_tools = []
    st.plan_pieces = {}
    st.completed_plans = set()
    st.request_open = False
    st.usage = None


def _codex_item_completed(event_recorder: EventRecorder, st: _CodexSynth, payload) -> list[str]:
    """Translate one completed Codex item and return fallback visible text."""
    item = _codex_item(payload.item)
    itype = _codex_item_type(item)
    item_id = getattr(item, "id", None) or ""
    if itype == "agentMessage":
        pieces = st.agent_pieces.get(item_id) or []
        text = "".join(pieces) or (getattr(item, "text", None) or "")
        if not pieces and text:
            event_recorder.text(text, request_id=st.request_id)
        if text:
            st.final_response = text
        if text:
            st.message_parts.append({"type": "output_text", "text": text})
        return [text] if not pieces and text else []
    if itype in ("dynamicToolCall", "mcpToolCall", "commandExecution"):
        info = _codex_tool_result(item, itype)
        st.pending_tools.append({"call_id": item_id, **info})
        return []
    if itype == "reasoning":
        # Completed items provide the fallback when no reasoning delta was emitted.
        content = getattr(item, "content", None) or []
        pieces = content or (getattr(item, "summary", None) or [])
        if not pieces:
            return []
        text = "".join(pieces)
        if item_id not in st.reasoning_order:
            st.reasoning_order.append(item_id)
        st.complete_reasoning[item_id] = text
        if item_id not in st.reasoning_pieces:
            event_recorder.reasoning(text, request_id=st.request_id)
        return []
    if itype == "plan":
        streamed = "".join(st.plan_pieces.get(item_id) or [])
        text = streamed or (getattr(item, "text", None) or "")
        if text and not streamed:
            event_recorder.delta("text", request_id=st.request_id, index=0, text=text)
        if text and item_id not in st.completed_plans:
            st.completed_plans.add(item_id)
            st.message_parts.append({"type": "output_text", "text": text})
        return []
    if itype == "webSearch":
        # The started web-search item is empty; only completed carries useful data.
        act = _codex_item(getattr(item, "action", None))
        action = None
        if act is not None:
            action = {"type": getattr(act, "type", None) or "other"}
            for k in ("query", "queries", "url", "pattern"):
                v = getattr(act, k, None)
                if isinstance(v, (str, list)) and v:
                    action[k] = v
        arguments = json.dumps({"query": getattr(item, "query", None) or "",
                                **({"action": action} if action else {})},
                               ensure_ascii=False)
        results = getattr(item, "results", None) or []
        content = json.dumps(results, ensure_ascii=False, default=str) if results else ""
        st.pending_tools.append({
            "call_id": item_id, "name": "web_search", "arguments": arguments,
            "content": content, "is_error": False,
        })
        return []
    return []


def _handle_codex_notification(event_recorder: EventRecorder, st: _CodexSynth, notif) -> list[str]:
    """Translate one Codex notification and return visible text deltas."""
    method = getattr(notif, "method", "")
    payload = getattr(notif, "payload", None)
    if method == "turn/started":
        turn = getattr(payload, "turn", None)
        st.turn_id = getattr(turn, "id", None)
        return []
    if method == "turn/completed":
        st.saw_turn_completed = True
        turn = getattr(payload, "turn", None) or {}
        kind = codex_val(getattr(turn, "status", None))
        event_recorder.result_stop_reason = kind if isinstance(kind, str) else None
        if kind != "completed":
            error = getattr(turn, "error", None) or {}
            detail = getattr(error, "message", None) or kind
            raise RequestFailedError(detail)
        _flush_codex_response(event_recorder, st)
        return []
    if method == "item/started":
        item = _codex_item(getattr(payload, "item", None))
        itype = _codex_item_type(item)
        if itype in ("agentMessage", "reasoning", "plan") and st.pending_tools:
            _flush_codex_response(event_recorder, st)
            event_recorder.begin_step()
            st.request_id = event_recorder.model_request()
            st.request_open = True
        if itype == "agentMessage":
            st.agent_pieces.setdefault(getattr(item, "id", None) or "", [])
        elif itype == "reasoning":
            item_id = getattr(item, "id", None) or ""
            if item_id not in st.reasoning_order:
                st.reasoning_order.append(item_id)
        return []
    if method == "item/agentMessage/delta":
        text = getattr(payload, "delta", None) or ""
        if not text:
            return []
        st.agent_pieces.setdefault(getattr(payload, "item_id", None) or "", []).append(text)
        event_recorder.text(text, request_id=st.request_id)
        return [text]
    if method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
        delta = getattr(payload, "delta", None) or ""
        if delta:
            item_id = getattr(payload, "item_id", None) or ""
            if item_id not in st.reasoning_order:
                st.reasoning_order.append(item_id)
            st.reasoning_pieces.setdefault(item_id, []).append(delta)
            # Preserve SDK segment indexes across full and summarized reasoning.
            index = (getattr(payload, "content_index", None)
                     if method == "item/reasoning/textDelta"
                     else getattr(payload, "summary_index", None))
            event_recorder.reasoning(
                delta, request_id=st.request_id,
                index=index if index is not None else 0)
        return []
    if method == "item/plan/delta":
        delta = getattr(payload, "delta", None) or ""
        if delta:
            item_id = getattr(payload, "item_id", None) or ""
            st.plan_pieces.setdefault(item_id, []).append(delta)
            event_recorder.delta(
                "text", request_id=st.request_id, index=0, text=delta)
        return []
    if method == "item/completed":
        return _codex_item_completed(event_recorder, st, payload)
    if method == "thread/tokenUsage/updated":
        usage = getattr(payload, "token_usage", None) or {}
        latest = getattr(usage, "last", None)
        total = getattr(usage, "total", None)
        st.usage = _normalize_usage(latest)
        st.total_usage = _normalize_usage(total) or st.usage
        event_recorder.result_usage = st.total_usage
        return []
    return []


async def _codex_drain(handle, event_recorder: EventRecorder, st: _CodexSynth):
    """Consume notifications, yielding text and requiring a completed turn."""
    async for notif in handle.stream():
        for delta in _handle_codex_notification(event_recorder, st, notif):
            yield delta
    if not st.saw_turn_completed:
        raise RequestFailedError("turn 未收到完成事件")


class Codex(BaseAgent):
    """Run reusable Codex SDK calls from an isolated Codex home."""

    agent = "codex"

    def __init__(self, config: dict):
        super().__init__(config)
        from openai_codex import AsyncCodex  # Lazy optional SDK import.

        self._codex = AsyncCodex(config=codex_config(config))
        self._thread = None

    async def _enter(self):
        await self._codex.__aenter__()
        config = self.config
        home = codex_home(config)
        if token := config.get("token") or "":
            auth = Path(home) / "auth.json"
            if auth.is_symlink():
                auth.unlink()
            await self._codex.login_api_key(token)
        self._thread = await self._codex.thread_start(**codex_thread(config))
        recorder = self._require_event_recorder()
        recorder.append_context(_codex_system_items(config), role="system")

    async def _exit(self, exc):
        self._thread = None
        await self._codex.__aexit__(*exc)

    async def stream(self, prompt: str):
        """Yield visible text while translating the complete notification stream."""
        config = self.config
        event_recorder = self._require_event_recorder()
        event_recorder.begin_turn()
        event_recorder.append_context(text_message("user", prompt))
        event_recorder.begin_step()
        st = _CodexSynth(event_recorder.model_request())
        timeout = config.get("timeout_seconds")
        handle = await self._thread.turn(prompt, **codex_turn(config))

        if timeout is not None:
            async with asyncio.timeout(timeout):
                async for chunk in _codex_drain(handle, event_recorder, st):
                    yield chunk
        else:
            async for chunk in _codex_drain(handle, event_recorder, st):
                yield chunk
        event_recorder.end_step()
        event_recorder.end_turn(reason="final_response")

    async def result(self, prompt: str) -> str:
        """Return the last agent-message text after consuming all notifications."""
        config = self.config
        event_recorder = self._require_event_recorder()
        event_recorder.begin_turn()
        event_recorder.append_context(text_message("user", prompt))
        event_recorder.begin_step()
        st = _CodexSynth(event_recorder.model_request())
        timeout = config.get("timeout_seconds")
        handle = await self._thread.turn(prompt, **codex_turn(config))
        if timeout is not None:
            async with asyncio.timeout(timeout):
                async for _ in _codex_drain(handle, event_recorder, st):
                    pass
        else:
            async for _ in _codex_drain(handle, event_recorder, st):
                pass
        final = st.final_response or ""
        if not final:
            raise RequestFailedError("未产出最终结果")
        event_recorder.end_step()
        event_recorder.end_turn(reason="final_response")
        return final
