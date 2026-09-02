"""Configure DeepSeek Harness and adapt its native event model to canonical events."""

import asyncio
import contextlib
import hashlib
import tempfile
from pathlib import Path
from typing import TypedDict

from ..base import BaseAgent, RequestFailedError
from ..events import (
    EventRecorder,
    _normalize_usage,
    function_call_item,
    function_output_item,
    message_item,
    reasoning_item,
    text_message,
)


class DshConfig(TypedDict, total=False):
    """dsh runtime config: keys are DeepSeekHarness kwargs names (see __init__.py)."""

    provider: str
    model: str
    system_prompt: str
    max_tokens: int
    cwd: str
    runtime_cwd: str
    session_root: str
    env: dict
    cordis: str  # composition file path (missing → built-in isolated composition)
    mcp_servers: list[dict]
    base_url: str
    api_key: str
    runtime_bin: str
    launch_args_override: list[str]
    request_timeout_seconds: float
    shutdown_timeout_seconds: float


def dsh_fields(config: dict) -> dict:
    """DshConfig → DeepSeekHarness constructor kwargs (key names passthrough).

    DeepSeekHarnessConfig is a dataclass (no model_dump) and
    DeepSeekHarness.__init__(config=None, **kwargs) type-errors when both are
    passed — kwargs only; system_prompt is the single key-aware mapping
    (→ env.DSH_SYSTEM_PROMPT, existing env key wins); no cordis → built-in
    isolated composition (see dsh_cordis_path).
    """
    names = ("provider", "model", "max_tokens", "cwd", "runtime_cwd", "session_root",
             "env", "runtime_bin", "launch_args_override",
             "request_timeout_seconds", "shutdown_timeout_seconds", "base_url", "api_key",
             "cordis")
    fields = {k: v for k, v in ((k, config.get(k)) for k in names) if v is not None}
    if config.get("system_prompt"):
        env = dict(fields.get("env") or {})
        env.setdefault("DSH_SYSTEM_PROMPT", config["system_prompt"])
        fields["env"] = env
    fields.setdefault("cordis", dsh_cordis_path(config.get("mcp_servers")))
    return fields


def dsh_harness(config: dict):
    """Build the optional DSH SDK client from normalized fields."""
    from deepseek_harness import DeepSeekHarness  # Lazy optional SDK import.

    return DeepSeekHarness(**dsh_fields(config))


_DSH_CORDIS_FILE: str | None = None


def dsh_cordis_path(mcp_servers: list[dict] | None = None) -> str:
    """Return a content-addressed isolated Cordis composition path."""
    global _DSH_CORDIS_FILE
    if mcp_servers is None:
        if _DSH_CORDIS_FILE is None:
            _DSH_CORDIS_FILE = _dsh_cordis_write(None)
        return _DSH_CORDIS_FILE
    return _dsh_cordis_write(mcp_servers)


def _dsh_cordis_write(mcp_servers: list[dict] | None) -> str:
    """Write the minimal composition plus injected MCP servers once per content hash."""
    # SHA-1 is a cache key, not a security primitive.
    digest = hashlib.sha1(_DSH_CORDIS_YAML.encode()).hexdigest()[:8]  # noqa: S324
    text = _DSH_CORDIS_YAML
    for spec in mcp_servers or []:
        text = _dsh_mcp_section(spec) + text
        digest = hashlib.sha1((digest + repr(sorted(spec.items()))).encode()).hexdigest()[:8]  # noqa: S324
    path = Path(tempfile.gettempdir()) / "gh-puller" / f"dsh-cordis-{digest}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(text, encoding="utf-8")
    return str(path)


def _dsh_mcp_section(spec: dict) -> str:
    """Render one generic local MCP server as a Cordis YAML section."""
    args = " ".join(f"'{a}'" for a in spec.get("args") or [])
    return (
        f"- id: {spec.get('id', 'mcp-server')}\n"
        f"  name: '@deepseek-ai/dsh-mcp-client'\n"
        f"  config:\n"
        f"    serverName: {spec.get('serverName', '')}\n"
        f"    transport: stdio\n"
        f"    command: {spec.get('command', 'python3')}\n"
        f"    args: [{args}]\n"
        f"    cwd: !!js process.env.DSH_CWD ?? process.cwd()\n"
        f"    failOnStartupError: true\n"
        f"    reconnect:\n      enabled: false\n"
    )


# The composition disables ambient context, skills, built-in tools, and MCP discovery.
_DSH_CORDIS_YAML = """- id: sdk-jsonrpc-server
  name: '@deepseek-ai/dsh-sdk-jsonrpc-server'
  config:
    maxTokensAsSuccess: false
- id: llm-deepseek
  name: '@deepseek-ai/dsh-llm-deepseek'
- id: sandbox
  name: '@deepseek-ai/dsh-sandbox-local'
- id: sandbox-policy
  name: '@deepseek-ai/dsh-sandbox-policy'
  config:
    mode: danger-full-access
    workspaceRoot: !!js process.env.DSH_CWD ?? process.cwd()
- id: subprocess
  name: '@deepseek-ai/dsh-subprocess-local'
- id: pty
  name: '@deepseek-ai/dsh-terminal'
- id: terminal-bash
  name: '@deepseek-ai/dsh-terminal-bash'
  config:
    timeoutMs: 300000
- id: fs-local
  name: '@deepseek-ai/dsh-fs-local'
  config:
    cwd: !!js process.env.DSH_CWD ?? process.cwd()
- id: agent-spine
  name: '@deepseek-ai/dsh-agent-spine-demo'
  config:
    includeHarnessIdentity: false
    includeRuntimeContext: false
    persona: !!js process.env.DSH_SYSTEM_PROMPT ?? 'You are a helpful software engineer assistant.'
    workspaceContext: false
    skills:
      enabled: false
    toolBash: false
    toolJobs: false
    goals: false
- id: persistent-bash
  name: '@deepseek-ai/dsh-tool-bash-persistent'
  config:
    timeoutMs: 300000
- id: str-replace-editor
  name: '@deepseek-ai/dsh-tool-str-replace-editor'
  config:
    maxOutputChars: 16000
- id: sessions
  name: '@deepseek-ai/dsh-session-persistence-jsonl'
  config:
    root: !!js process.env.DSH_SESSION_ROOT ?? './.sessions'
    compression: none
"""


def _dsh_session_id(session: str) -> str:
    """Map the canonical session id to DSH's final path segment."""
    return (session or "agent").rsplit("/", 1)[-1]


class _DshProj:
    """State required to translate one DSH session without importing its event model."""

    def __init__(self, event_recorder: EventRecorder, prompt: str, session_id: str,
                 previous: "_DshProj | None" = None):
        self.event_recorder = event_recorder
        self.prompt = prompt
        self.session_id = session_id
        same_session = previous is not None and previous.session_id == session_id
        context = event_recorder.context()
        self.header = ([dict(item) for item in previous.header] if same_session else [
            item for item in context
            if item.get("type") == "message" and item.get("role") in {"system", "developer"}
        ])
        self.surface = ([(seq, [dict(item) for item in items])
                         for seq, items in previous.surface]
                        if same_session else [
                            (None, [item]) for item in context
                            if not (item.get("type") == "message"
                                    and item.get("role") in {"system", "developer"})
                        ])
        self.tool_names = dict(previous.tool_names) if same_session else {}
        self.tool_pieces: dict[int, dict] = {}
        self.request: dict = {}
        self.request_id: str | None = None
        self.saw_user_message = False
        self.last_finish_kind: str | None = None
        self.usage = None

    def context(self) -> list[dict]:
        """Return the model context represented by the current DSH surface."""
        return [*self.header, *(item for _, items in self.surface for item in items)]

    def apply_header(self, items: list[dict]) -> None:
        """Replace DSH request metadata after projecting it into context."""
        if items == self.header:
            return
        if not self.header and not self.surface and items:
            self.header = items
            self.event_recorder.append_context(items, role="system")
            return
        self.header = items
        self.event_recorder.set_context(self.context())

    def begin_request(self) -> str:
        """Return the current DSH model correlation, opening it when needed."""
        if self.request_id is None:
            self.request_id = self.event_recorder.model_request(**self.request)
        return self.request_id

    def commit(self, envelope: dict, items: list[dict], *, role: str | None = None) -> None:
        """Apply one DSH surface operation as a canonical context operation."""
        if not items:
            return
        seq = envelope.get("seq")
        op = envelope.get("surfaceOp") or "append"
        if op == "append":
            self.surface.append((seq, items))
            self.event_recorder.append_context(items, role=role)
            return
        start = next(index for index, (source, _) in enumerate(self.surface)
                     if source == op["start"])
        end = next(index for index, (source, _) in enumerate(self.surface)
                   if source == op["end"])
        self.surface[start:end + 1] = [(seq, items)]
        self.event_recorder.set_context(self.context())


def _dsh_input_items(message: dict) -> list[dict]:
    """Normalize a DSH input message at the native boundary."""
    role = message.get("role") or "user"
    content = []
    for raw in message.get("content") or []:
        block = dict(raw)
        if block.get("type") == "text":
            block["type"] = "input_text"
        content.append(block)
    return [message_item(role, content)]


def _dsh_output_items(message: dict) -> list[dict]:
    """Normalize one complete DSH assistant output into ordered Items."""
    reasoning = []
    content = []
    calls = []
    for raw in message.get("content") or []:
        block = dict(raw)
        block_type = block.get("type")
        if block_type in {"thinking", "reasoning"}:
            if text := block.get("text") or block.get("thinking"):
                reasoning.append(text)
        elif block_type in {"tool-call", "tool_call", "function_call"}:
            calls.append(function_call_item(
                block.get("id") or block.get("callId") or block.get("call_id") or "",
                block.get("name") or "", block.get("arguments") or {},
            ))
        else:
            if block_type == "text":
                block["type"] = "output_text"
            content.append(block)
    output = []
    if reasoning:
        output.append(reasoning_item("".join(reasoning)))
    if content:
        output.append(message_item("assistant", content))
    output.extend(calls)
    return output


def _dsh_header_items(header: dict) -> list[dict]:
    """Project one DSH request header into a model-visible system message."""
    content = []
    if system := header.get("system"):
        content.append({"type": "input_text", "text": system})
    for tool in header.get("tools") or []:
        function = tool.get("function") or tool
        content.append({
            "type": "tool_definition", "name": function.get("name") or "",
            "description": function.get("description") or "",
            "inputSchema": function.get("parameters") or function.get("inputSchema") or {},
        })
    return [message_item("system", content)] if content else []


def _project_dsh_chunk(event_recorder: EventRecorder, proj: _DshProj, chunk: dict) -> list[str]:
    """Translate one DSH model delta and return any user-visible text."""
    ctype = chunk.get("type")
    if ctype == "text-delta":
        text = chunk.get("text") or ""
        event_recorder.text(
            text, request_id=proj.begin_request(), index=chunk.get("index", 0))
        return [text] if text else []
    if ctype == "reasoning-delta":
        event_recorder.reasoning(
            chunk.get("text") or "", request_id=proj.begin_request(),
            index=chunk.get("index", 0))
        return []
    if ctype == "tool-call-delta":
        idx = chunk.get("index", -1)
        slot = proj.tool_pieces.setdefault(
            idx, {"id": chunk.get("id") or "", "name": chunk.get("name") or "", "pieces": []})
        if chunk.get("name"):
            slot["name"] = chunk["name"]
        piece = chunk.get("argumentsDelta") or ""
        slot["pieces"].append(piece)
        event_recorder.tool_call_delta(
            request_id=proj.begin_request(), index=idx,
            call_id=slot["id"] or "pending", name=slot["name"],
            arguments_delta=piece)
        return []
    if ctype == "block-end":
        block = chunk.get("block") or {}
        if block.get("type") == "tool-call":
            slot = proj.tool_pieces.pop(chunk.get("index", -1), None)
            call_id = block.get("id") or (slot or {}).get("id") or ""
            name = block.get("name") or (slot or {}).get("name")
            if call_id and name:
                proj.tool_names[call_id] = name
        return []
    if ctype == "usage":
        usage = chunk.get("usage")
        if usage:
            proj.usage = _normalize_usage(usage)
            event_recorder.result_usage = proj.usage
        return []
    if ctype == "finish":
        reason = chunk.get("reason")
        kind = reason.get("kind") if isinstance(reason, dict) else reason
        if kind:
            proj.last_finish_kind = kind
            event_recorder.result_stop_reason = kind
        return []
    return []


def _project_dsh_event(event_recorder: EventRecorder, proj: _DshProj, notif) -> list[str]:
    """Translate one native DSH notification into canonical state or activity."""
    if getattr(notif, "method", None) != "session.event":
        return []
    payload = getattr(notif, "payload", None) or {}
    if payload.get("sessionId") != proj.session_id:
        return []
    envelope = payload.get("event") or {}
    evt_type = envelope.get("type")
    data = envelope.get("data") or {}

    if evt_type.startswith("assistant/") and not proj.saw_user_message:
        proj.saw_user_message = True
        proj.commit({}, [text_message("user", proj.prompt)], role="user")

    if evt_type == "turn/start":
        event_recorder.event("turn/start")
    elif evt_type == "step/start":
        proj.tool_pieces = {}
        proj.request_id = None
        proj.usage = None
        proj.last_finish_kind = None
        event_recorder.event("step/start")
    elif evt_type == "step/end":
        event_recorder.event("step/end")
    elif evt_type == "turn/end":
        reason = data.get("reason")
        kind = reason.get("kind") if isinstance(reason, dict) else reason
        event_recorder.event("turn/end", outcome="completed", **({"reason": kind} if kind else {}))
    elif evt_type == "user/message":
        proj.saw_user_message = True
        role = data.get("role", "user")
        proj.commit(envelope, _dsh_input_items({
            "role": role, "content": data.get("content") or [],
        }), role=role)
    elif evt_type == "assistant/chunk":
        return _project_dsh_chunk(event_recorder, proj, data.get("chunk") or {})
    elif evt_type == "assistant/message":
        output = _dsh_output_items(
            data.get("message") or {"role": "assistant", "content": []})
        event_recorder.model_response(
            output, request_id=proj.begin_request(), usage=data.get("usage") or proj.usage,
            stop_reason=proj.last_finish_kind)
        proj.request_id = None
        proj.commit(envelope, output, role="assistant")
    elif evt_type == "tool/call":
        call_id = data.get("callId") or ""
        name = data.get("name") or ""
        if call_id and name:
            proj.tool_names[call_id] = name
        event_recorder.tool_call(call_id, name, data.get("arguments") or "")
    elif evt_type == "tool/result":
        msg = data.get("message") or {}
        card = ((msg.get("content") or [{}])[0]
                if isinstance(msg.get("content"), list) else {})
        call_id = (msg.get("source") or {}).get("callId") or (card.get("toolCallId") or "")
        is_error = bool(card.get("isError"))
        text = "".join(b.get("text") or "" for b in (card.get("content") or [])
                       if isinstance(b, dict) and b.get("type") == "text")
        name = proj.tool_names.get(call_id) or ""
        event_recorder.tool_call(call_id, name, None)
        error = {"type": "ToolError", "message": text} if is_error else None
        event_recorder.tool_end(call_id, result=text, error=error)
        proj.commit(envelope, [function_output_item(call_id, text)], role="tool")
    elif evt_type == "request/context":
        proj.begin_request()
    elif evt_type == "request/header":
        header = data.get("header") or {}
        config = header.get("config") or {}
        proj.request = {
            **({"model": config["model"]} if config.get("model") else {}),
            **({"provider": config["provider"]} if config.get("provider") else {}),
        }
        parameters = {key: value for key, value in config.items()
                      if key not in {"provider", "model"}}
        if parameters:
            proj.request["parameters"] = parameters
        proj.apply_header(_dsh_header_items(header))
    return []


def _dsh_worker(harness, prompt: str, session_id: str, pump):
    """Run the blocking harness until idle in its executor thread."""
    return harness.run(prompt, session_id=session_id, on_notification=pump)


class Dsh(BaseAgent):
    """Bridge a reusable synchronous DSH harness into the Agent contract."""

    agent = "dsh"

    def __init__(self, config: dict):
        super().__init__(config)
        self._harness = dsh_harness(config)
        self._run_task: asyncio.Task | None = None
        self._proj: _DshProj | None = None

    async def _enter(self):
        await asyncio.to_thread(self._harness.__enter__)

    async def _exit(self, exc):
        task, self._run_task = self._run_task, None
        if task is not None and not task.done():
            await task
        await asyncio.to_thread(self._harness.__exit__, *exc)

    def _run_assembly(self, prompt: str) -> tuple[EventRecorder, _DshProj, asyncio.Queue]:
        """Assemble per-run parts for stream/result (proj on the instance; worker not cancelled here)."""
        event_recorder = self._require_event_recorder()
        proj = _DshProj(
            event_recorder, prompt, _dsh_session_id(event_recorder.session), self._proj)
        self._proj = proj
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def pump(notif) -> None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(queue.put_nowait, ("notif", notif))

        async def _worker() -> None:
            try:
                result = await asyncio.to_thread(_dsh_worker, self._harness, prompt, proj.session_id, pump)
                loop.call_soon_threadsafe(queue.put_nowait, ("done", result))
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("exc", exc))

        self._run_task = asyncio.create_task(_worker())
        return event_recorder, proj, queue

    async def stream(self, prompt: str):
        """Project dsh native events 1:1; yield assistant text deltas.

        Turn non-completed → RequestFailedError; thinking/tool increments only to the
        event stream. SDK run() is sync-blocking → asyncio.to_thread (one turn to_idle
        per run, no per-prompt cancellation).
        """
        event_recorder, proj, queue = self._run_assembly(prompt)
        while True:
            kind, item = await queue.get()
            if kind == "notif":
                for delta in _project_dsh_event(event_recorder, proj, item):
                    yield delta
            elif kind == "exc":
                raise item
            else:
                if item.finish_reason != "completed":
                    raise RequestFailedError(item.finish_reason)
                break

    async def result(self, prompt: str) -> str:
        """Return the final output: RunResult.final_response; non-completed/no output → RequestFailedError."""
        event_recorder, proj, queue = self._run_assembly(prompt)
        while True:
            kind, item = await queue.get()
            if kind == "notif":
                for _ in _project_dsh_event(event_recorder, proj, item):
                    pass
            elif kind == "exc":
                raise item
            else:
                if item.finish_reason != "completed":
                    raise RequestFailedError(item.finish_reason)
                final = item.final_response or ""
                if not final:
                    raise RequestFailedError("未产出最终结果")
                return final
