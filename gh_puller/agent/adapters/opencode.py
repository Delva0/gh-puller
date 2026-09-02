"""Run OpenCode's JSONL CLI and synthesize canonical agent events."""

import asyncio
import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, TypedDict

from ..base import BaseAgent, RequestFailedError
from ..context import instruction, mcps, system_message, tool_defs
from ..events import (
    EventRecorder,
    _normalize_usage,
    function_call_item,
    message_item,
    reasoning_item,
    text_message,
)

# OpenCode 1.18 emits cumulative text snapshots keyed by part id.
_CUMULATIVE_TEXT = True


class OpenCodeConfig(TypedDict, total=False):
    """Configuration mapped to OpenCode CLI and injected JSON settings."""

    model: str
    system_prompt: str
    cwd: str
    opencode_bin: str
    config: str
    agent: str
    variant: str
    auto: bool
    thinking: bool
    session: str
    env: dict
    mcp_servers: list[dict]
    timeout_seconds: float


def _opencode_argv(config: dict, prompt: str) -> list[str]:
    """Build an isolated, non-interactive ``opencode run`` command."""
    argv = [config.get("opencode_bin") or "opencode", "--pure", "run"]
    for key, flag in (("model", "--model"), ("agent", "--agent"),
                      ("variant", "--variant"), ("session", "--session")):
        if config.get(key):
            argv += [flag, config[key]]
    if config.get("auto", True):
        argv.append("--auto")
    if config.get("thinking"):
        argv.append("--thinking")
    argv += ["--format", "json", prompt]
    return argv


def _opencode_env(config: dict) -> dict:
    """Build the child environment while isolating machine Claude skills by default."""
    env = dict(os.environ)
    env.setdefault("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS", "1")
    env.update(config.get("env") or {})
    if cfg := config.get("config"):
        env["OPENCODE_CONFIG"] = cfg
    return env


def _opencode_mcp_section(mcp_servers: list[dict], env: dict) -> dict:
    """Render injected local MCP servers, resolving only allowlisted environment keys."""
    section: dict[str, dict] = {}
    for spec in mcp_servers or []:
        entry: dict[str, Any] = {
            "type": "local",
            "command": [spec.get("command", ""), *spec.get("args", [])],
            "enabled": True,
        }
        env_vars = {k: env[k] for k in spec.get("env_vars", []) if env.get(k)}
        if env_vars:
            entry["environment"] = env_vars
        section[spec.get("id", "mcp-server")] = entry
    return section


def _opencode_config_content(config: dict, instruction_path: str | None, env: dict) -> dict:
    """Build the highest-precedence injected instruction and MCP section."""
    content: dict = {}
    if instruction_path:
        content["instructions"] = [instruction_path]
    if config.get("mcp_servers"):
        content["mcp"] = _opencode_mcp_section(config["mcp_servers"], env)
    return content


def _opencode_system_item(config: dict) -> dict:
    """Project the system inputs selected at the OpenCode boundary."""
    prompt = config.get("system_prompt")
    content = [instruction(prompt) if prompt else instruction(), tool_defs(["opaque"])]
    content.extend(mcps(config.get("mcp_servers")))
    return system_message(content)


def _opencode_args_json(arguments) -> str:
    """Preserve string arguments and encode structured arguments as JSON."""
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments)


class _OpencodeSynth:
    """Hold JSONL assembly state for one OpenCode call."""

    def __init__(self, request_id: str):
        self.request_id = request_id
        self.parts: dict[str, str] = {}
        self.step_parts: dict[str, str] = {}
        self.step_reasoning: dict[str, str] = {}
        self.pending_tools: list[dict] = []
        self.request_open = True
        self.usage = None
        self.stop_reason: str | None = None
        self.model: str | None = None
        self.open_steps = 0
        self.saw_stop = False
        self.final_response = ""
        self.session_id: str | None = None
        self.stderr_tail: list[str] = []


def _flush_step_response(event_recorder: EventRecorder, st: _OpencodeSynth) -> None:
    """Commit the complete assistant output before buffered tool executions."""
    if not st.request_open:
        return
    output = []
    reasoning = "\n".join(text for text in st.step_reasoning.values() if text)
    if reasoning:
        output.append(reasoning_item(reasoning))
    content = [{"type": "output_text", "text": text}
               for text in st.step_parts.values() if text]
    if content:
        output.append(message_item("assistant", content))
    output.extend(
        function_call_item(
            spec["call_id"], spec["name"], spec["arguments"],
        ) for spec in st.pending_tools
    )
    event_recorder.model_response(
        output, request_id=st.request_id, model=st.model,
        usage=st.usage, stop_reason=st.stop_reason)
    if output:
        event_recorder.append_context(output, role="assistant")
    st.step_parts = {}
    st.step_reasoning = {}
    st.request_open = False
    st.usage = None
    st.stop_reason = None
    st.model = None


def _flush_pending_tools(event_recorder: EventRecorder, st: _OpencodeSynth) -> None:
    """Record buffered local tool executions in their CLI order."""
    for spec in st.pending_tools:
        event_recorder.tool_call(spec["call_id"], spec["name"], spec["arguments"])
        event_recorder.tool_result(
            spec["content"],
            call_id=spec["call_id"], name=spec["name"], is_error=spec["is_error"],
        )
    st.pending_tools = []


def _handle_opencode_line(event_recorder: EventRecorder, st: _OpencodeSynth, evt: dict) -> list[str]:
    """Translate one OpenCode JSONL item and return visible text deltas."""
    kind = evt.get("type", "")
    part = evt.get("part") or {}
    if session_id := evt.get("sessionID") or part.get("sessionID"):
        st.session_id = session_id
    if kind == "step_start":
        st.open_steps += 1
        if st.open_steps >= 2:
            _flush_step_response(event_recorder, st)
            _flush_pending_tools(event_recorder, st)
            event_recorder.begin_step()
            st.request_id = event_recorder.model_request()
        st.request_open = True
        st.parts = {}
        st.step_parts = {}
        st.step_reasoning = {}
        st.model = part.get("modelID") or part.get("model")
        return []
    if model := part.get("modelID") or part.get("model"):
        st.model = model
    if kind == "text":
        text = part.get("text") or ""
        if not text:
            return []
        pid = part.get("id") or ""
        if _CUMULATIVE_TEXT:
            prev = st.parts.get(pid, "")
            delta = text[len(prev):] if prev and text.startswith(prev) else text
            st.parts[pid] = text
            st.step_parts[pid] = text
        else:
            delta = text
            if delta:
                st.step_parts[pid] = (st.step_parts.get(pid) or "") + delta
        if delta:
            st.final_response = text
            event_recorder.text(delta, request_id=st.request_id)
            return [delta]
        return []
    if kind == "reasoning":
        text = part.get("text") or ""
        if not text:
            return []
        pid = part.get("id") or ""
        if _CUMULATIVE_TEXT:
            prev = st.parts.get(pid, "")
            delta = text[len(prev):] if prev and text.startswith(prev) else text
            st.parts[pid] = text
            st.step_reasoning[pid] = text
        else:
            delta = text
            if delta:
                st.step_reasoning[pid] = (st.step_reasoning.get(pid) or "") + delta
        if delta:
            event_recorder.reasoning(delta, request_id=st.request_id)
        return []
    if kind == "tool_use":
        state = part.get("state") or {}
        status = state.get("status")
        if status not in ("completed", "error"):
            return []
        call_id = part.get("callID") or ""
        name = part.get("tool") or ""
        if not call_id:
            return []
        # OpenCode interleaves completed tools with text; buffer to preserve model-first order.
        output = state.get("output")
        content = output if isinstance(output, str) else (
            json.dumps(output, ensure_ascii=False) if output else (state.get("error") or "")
        )
        input_state = state.get("input") or {}
        is_error = (status == "error" or (isinstance(input_state, dict) and bool(input_state.get("error")))
                    or (state.get("metadata") or {}).get("exit") not in (None, 0))
        st.pending_tools.append({
            "call_id": call_id, "name": name,
            "arguments": _opencode_args_json(state.get("input")),
            "content": content, "is_error": is_error,
        })
        return []
    if kind == "step_finish":
        tokens = part.get("tokens") or {}
        cache = tokens.get("cache") or {}
        usage = {"input_tokens": tokens.get("input"), "output_tokens": tokens.get("output"),
                 "cache_read_input_tokens": cache.get("read")}
        if any(v is not None for v in usage.values()):
            st.usage = _normalize_usage(usage)
            event_recorder.result_usage = st.usage
        cost = part.get("cost")
        if isinstance(cost, (int, float)):
            event_recorder.result_cost_usd = float(cost)
        reason = part.get("reason")
        if isinstance(reason, str):
            st.stop_reason = reason
        if reason == "stop":
            st.saw_stop = True
            event_recorder.result_stop_reason = "stop"
        _flush_step_response(event_recorder, st)
        _flush_pending_tools(event_recorder, st)
        return []
    if kind == "error":
        err = evt.get("error") or {}
        detail = (((err.get("data") or {}).get("message") or "") or err.get("name")) \
            or "opencode run 失败"
        raise RequestFailedError(detail)
    return []


_STDERR_TAIL_LINES = 50
_STDERR_TAIL_CHARS = 4096


async def _opencode_stderr_tail(stream, st: _OpencodeSynth) -> None:
    """Drain stderr concurrently into a bounded diagnostic tail."""
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").rstrip()
        st.stderr_tail.append(text)
        if sum(len(x) for x in st.stderr_tail) > _STDERR_TAIL_CHARS:
            st.stderr_tail[:] = st.stderr_tail[-_STDERR_TAIL_LINES:]


@contextlib.asynccontextmanager
async def _opencode_subprocess(argv: list[str], *, cwd: str | None, env: dict,
                               st: _OpencodeSynth):
    """Run OpenCode with concurrent stderr draining and bounded teardown."""
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    err_task = asyncio.create_task(_opencode_stderr_tail(proc.stderr, st))
    try:
        yield proc
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        err_task.cancel()


async def _opencode_drain(proc, event_recorder: EventRecorder, st: _OpencodeSynth):
    """Consume JSONL, requiring both a zero exit status and a stop event."""
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            line = line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(evt, dict):
                continue
            for delta in _handle_opencode_line(event_recorder, st, evt):
                yield delta
    finally:
        rc = await proc.wait()
    if rc != 0:
        detail = f"opencode 退出码 {rc}" + (f": {'; '.join(st.stderr_tail[-20:])}" if st.stderr_tail else "")
        raise RequestFailedError(detail)
    if not st.saw_stop:
        raise RequestFailedError("turn 未收到完成事件")


class OpenCode(BaseAgent):
    """Run reusable OpenCode CLI calls with isolated injected configuration."""

    agent = "opencode"

    def __init__(self, config: dict):
        super().__init__(config)
        self._native_session = config.get("session")

    def _call_config(self) -> dict:
        config = dict(self.config)
        if self._native_session:
            config["session"] = self._native_session
        return config

    async def _enter(self) -> None:
        self._require_event_recorder().append_context(
            _opencode_system_item(self.config), role="system")

    async def _exit(self, exc) -> None:
        """Same: nothing to reap at the client layer (child reaping in _opencode_subprocess)."""

    async def stream(self, prompt: str):
        """Yield visible text while consuming the complete OpenCode JSONL stream."""
        config = self._call_config()
        event_recorder = self._require_event_recorder()
        event_recorder.begin_turn()
        event_recorder.append_context(text_message("user", prompt))
        event_recorder.begin_step()
        st = _OpencodeSynth(event_recorder.model_request())
        with tempfile.TemporaryDirectory(prefix="gh-puller-opencode-") as tmp:
            instruction_path = None
            if system_prompt := config.get("system_prompt"):
                path = Path(tmp) / "instructions.md"
                path.write_text(system_prompt, encoding="utf-8")
                instruction_path = str(path)
            env = _opencode_env(config)
            content = _opencode_config_content(config, instruction_path, env)
            if content:
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps(content, ensure_ascii=False)
            argv = _opencode_argv(config, prompt)
            async with _opencode_subprocess(argv, cwd=config.get("cwd"), env=env, st=st) as proc:
                if (timeout := config.get("timeout_seconds")) is not None:
                    async with asyncio.timeout(timeout):
                        async for chunk in _opencode_drain(proc, event_recorder, st):
                            yield chunk
                else:
                    async for chunk in _opencode_drain(proc, event_recorder, st):
                        yield chunk
        self._native_session = st.session_id or self._native_session
        event_recorder.end_step()
        event_recorder.end_turn(reason="final_response")

    async def result(self, prompt: str) -> str:
        """Return the final output: the last text segment (st.final_response); no output → RequestFailedError."""
        config = self._call_config()
        event_recorder = self._require_event_recorder()
        event_recorder.begin_turn()
        event_recorder.append_context(text_message("user", prompt))
        event_recorder.begin_step()
        st = _OpencodeSynth(event_recorder.model_request())
        with tempfile.TemporaryDirectory(prefix="gh-puller-opencode-") as tmp:
            instruction_path = None
            if system_prompt := config.get("system_prompt"):
                path = Path(tmp) / "instructions.md"
                path.write_text(system_prompt, encoding="utf-8")
                instruction_path = str(path)
            env = _opencode_env(config)
            content = _opencode_config_content(config, instruction_path, env)
            if content:
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps(content, ensure_ascii=False)
            argv = _opencode_argv(config, prompt)
            async with _opencode_subprocess(argv, cwd=config.get("cwd"), env=env, st=st) as proc:
                if (timeout := config.get("timeout_seconds")) is not None:
                    async with asyncio.timeout(timeout):
                        async for _ in _opencode_drain(proc, event_recorder, st):
                            pass
                else:
                    async for _ in _opencode_drain(proc, event_recorder, st):
                        pass
        self._native_session = st.session_id or self._native_session
        final = st.final_response or ""
        if not final:
            raise RequestFailedError("未产出最终结果")
        event_recorder.end_step()
        event_recorder.end_turn(reason="final_response")
        return final
