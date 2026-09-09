"""Implement the pooled MCP frontend transport for the CBM client.

Each child process is an independent thin MCP frontend. The pool starts lazily,
reuses frontends bound to the same source root, and expands only when concurrent
calls or distinct roots require it.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from .utils import (
    _DELTA_ARGUMENTS,
    CBMTransportError,
    _ProjectTransport,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from ...utils import ResourceMonitorLike
    from ..binary import CBMBinary

_STREAM_CLOSED = object()
_MAX_FRONTENDS = 8


def capabilities_from_tools_list(result: object) -> frozenset[str]:
    """Derive index capabilities from an MCP tools/list result.

    Args:
        result: MCP result containing advertised tool definitions.

    Returns:
        Capabilities proven by the live frontend schema.
    """
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        return frozenset()
    index_tool = next(
        (tool for tool in result["tools"] if isinstance(tool, dict) and tool.get("name") == "index_repository"),
        None,
    )
    if not isinstance(index_tool, dict):
        return frozenset()
    capabilities = {"repository-index"}
    schema = index_tool.get("inputSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return frozenset(capabilities)
    if "force_full" in properties:
        capabilities.add("force-full-route")
    if properties.keys() >= _DELTA_ARGUMENTS:
        capabilities.add("granular-delta-controls")
    return frozenset(capabilities)


class _MCPFrontend:
    """Own one initialized MCP stdio frontend."""

    def __init__(
        self,
        binary: Path,
        cache_root: Path,
        timeout: float,
        monitor: ResourceMonitorLike,
        environment: Mapping[str, str],
        source_root: Path | None,
    ):
        self.timeout = timeout
        self.monitor = monitor
        self.source_root = source_root
        self._next_id = 0
        self._messages: queue.Queue[object] = queue.Queue()
        self._pending: dict[int, dict] = {}
        self._stderr_tail: deque[str] = deque(maxlen=256)
        self._request_lock = threading.RLock()
        self._closed = False
        self.process = subprocess.Popen(
            [str(binary)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            bufsize=1,
            env={
                **os.environ,
                **environment,
                "CBM_CACHE_DIR": str(cache_root),
            },
            cwd=source_root,
        )
        if self.process.stdin is None or self.process.stdout is None or self.process.stderr is None:
            self._terminate()
            raise CBMTransportError("CBM MCP pipes could not be created")
        self.monitor.add_child(self.process.pid)
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        try:
            initialized = self._request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "gh-puller-codebase", "version": "1"},
                },
                min(timeout, 60),
            )
            instructions = initialized.get("instructions", "")
            if not isinstance(instructions, str):
                raise CBMTransportError("CBM MCP initialization instructions must be text")
            self._notify("notifications/initialized", {})
        except BaseException:
            self._terminate()
            raise

    @property
    def closed(self) -> bool:
        return self._closed

    def _stderr_text(self) -> str:
        return "".join(self._stderr_tail)[-4000:]

    def _read_stdout(self) -> None:
        stream = self.process.stdout
        if stream is None:
            self._messages.put(_STREAM_CLOSED)
            return
        with stream:
            for line in stream:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._stderr_tail.append(f"invalid MCP stdout: {line[-500:]}")
                    continue
                self._messages.put(message)
        self._messages.put(_STREAM_CLOSED)

    def _read_stderr(self) -> None:
        stream = self.process.stderr
        if stream is None:
            return
        with stream:
            for line in stream:
                self._stderr_tail.append(line)

    def _send(self, payload: dict) -> None:
        if self._closed or self.process.poll() is not None or self.process.stdin is None:
            raise CBMTransportError(f"CBM MCP frontend is not running: {self._stderr_text()}")
        try:
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CBMTransportError(f"CBM MCP write failed: {self._stderr_text()}") from exc

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict, timeout: float | None = None) -> dict:
        with self._request_lock:
            self._next_id += 1
            request_id = self._next_id
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
            )
            deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
            while True:
                if request_id in self._pending:
                    response = self._pending.pop(request_id)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate()
                    raise CBMTransportError(f"CBM MCP request {method} timed out")
                try:
                    message = self._messages.get(timeout=remaining)
                except queue.Empty:
                    self._terminate()
                    raise CBMTransportError(f"CBM MCP request {method} timed out") from None
                if message is _STREAM_CLOSED:
                    self._terminate()
                    raise CBMTransportError(
                        f"CBM MCP frontend exited while handling {method}: {self._stderr_text()}",
                    )
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                if message_id == request_id:
                    response = message
                    break
                if isinstance(message_id, int):
                    self._pending[message_id] = message
            self.monitor.sample()
            if self.monitor.exceeded:
                raise CBMTransportError("memory limit exceeded while running CBM")
            if response.get("error") is not None:
                raise CBMTransportError(f"CBM MCP error for {method}: {response['error']}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise CBMTransportError(f"CBM MCP returned an invalid result for {method}")
            return result

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> dict[str, Any]:
        result = self._request("tools/call", {"name": name, "arguments": dict(arguments)})
        if result.get("isError"):
            detail = json.dumps(result, ensure_ascii=False)[-2000:]
            raise CBMTransportError(f"CBM tool {name} failed: {detail}")
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        definitions = []
        arguments = {}
        while True:
            result = self._request("tools/list", arguments)
            tools = result.get("tools")
            if not isinstance(tools, list) or not all(isinstance(tool, dict) for tool in tools):
                raise CBMTransportError("CBM MCP returned invalid tool definitions")
            definitions.extend(tools)
            cursor = result.get("nextCursor")
            if not cursor:
                return definitions
            if not isinstance(cursor, str):
                raise CBMTransportError("CBM MCP returned an invalid tools/list cursor")
            arguments = {"cursor": cursor}

    def _join_readers(self) -> None:
        for name in ("_stdout_thread", "_stderr_thread"):
            if reader := getattr(self, name, None):
                reader.join(timeout=2)

    def _terminate(self) -> None:
        with self._request_lock:
            if self._closed:
                return
            self._closed = True
            if self.process.stdin is not None:
                with suppress(OSError):
                    self.process.stdin.close()
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self._join_readers()
            self.monitor.remove_child(self.process.pid)

    def close(self) -> None:
        with self._request_lock:
            if self._closed:
                return
            self._closed = True
            if self.process.stdin is not None:
                with suppress(OSError):
                    self.process.stdin.close()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self._join_readers()
            self.monitor.remove_child(self.process.pid)
            self.monitor.sample()


class MCPTransport(_ProjectTransport):
    """Pool MCP frontends behind one transport contract."""

    name = "mcp"

    def __init__(
        self,
        binary: CBMBinary,
        cache_root: Path,
        timeout: float,
        monitor: ResourceMonitorLike,
        environment: Mapping[str, str],
        *,
        max_frontends: int = _MAX_FRONTENDS,
    ):
        super().__init__(binary)
        if max_frontends < 1:
            raise ValueError("max_frontends must be positive")
        self.cache_root = cache_root
        self.timeout = timeout
        self.monitor = monitor
        self.environment = dict(environment)
        self.max_frontends = max_frontends
        self._condition = threading.Condition()
        self._frontends: set[_MCPFrontend] = set()
        self._idle: list[_MCPFrontend] = []
        self._creating = 0
        self._closed = False
        self._tools_lock = threading.Lock()
        self._tools: tuple[dict[str, Any], ...] | None = None

    def _new_frontend(self, source_root: Path | None) -> _MCPFrontend:
        return _MCPFrontend(
            self._binary.path,
            self.cache_root,
            self.timeout,
            self.monitor,
            self.environment,
            source_root,
        )

    def _acquire(self, source_root: Path | None) -> _MCPFrontend:
        deadline = time.monotonic() + self.timeout
        while True:
            retired = None
            with self._condition:
                if self._closed:
                    raise CBMTransportError("CBM MCP transport is closed")
                for index in range(len(self._idle) - 1, -1, -1):
                    if self._idle[index].source_root == source_root:
                        return self._idle.pop(index)
                if len(self._frontends) + self._creating < self.max_frontends:
                    self._creating += 1
                    break
                if self._idle:
                    retired = self._idle.pop()
                    self._frontends.remove(retired)
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CBMTransportError("timed out waiting for a CBM MCP frontend")
                    self._condition.wait(remaining)
            if retired is not None:
                retired.close()
        try:
            frontend = self._new_frontend(source_root)
        except BaseException:
            with self._condition:
                self._creating -= 1
                self._condition.notify()
            raise
        with self._condition:
            self._creating -= 1
            if self._closed:
                frontend.close()
                raise CBMTransportError("CBM MCP transport is closed")
            self._frontends.add(frontend)
            return frontend

    def _release(self, frontend: _MCPFrontend) -> None:
        with self._condition:
            if self._closed or frontend.closed:
                self._frontends.discard(frontend)
                close = not frontend.closed
            else:
                self._idle.append(frontend)
                close = False
            self._condition.notify()
        if close:
            frontend.close()

    def _use(self, operation, source_root: Path | None = None):
        frontend = self._acquire(source_root)
        try:
            return operation(frontend)
        finally:
            self._release(frontend)

    def _call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        source_root: Path | None,
    ) -> dict[str, Any]:
        return self._use(
            lambda frontend: frontend.call_tool(name, arguments),
            source_root,
        )

    def _tool_definitions(self) -> tuple[dict[str, Any], ...]:
        with self._tools_lock:
            if self._tools is None:
                self._tools = tuple(self._use(lambda frontend: frontend.list_tools()))
            return self._tools

    def supports(self, operation: str) -> bool:
        """Return whether the live MCP frontend advertises an operation."""
        if operation == "project_graph":
            return True
        if operation == "open_store":
            return False
        return any(tool.get("name") == operation for tool in self._tool_definitions())

    def capabilities(self) -> frozenset[str]:
        """Return index capabilities advertised by one live frontend."""
        return capabilities_from_tools_list({"tools": list(self._tool_definitions())})

    @property
    def frontend_pids(self) -> frozenset[int]:
        """Return frontend process IDs for diagnostics and tests."""
        with self._condition:
            return frozenset(frontend.process.pid for frontend in self._frontends)

    def close(self) -> None:
        """Close every frontend owned by this transport."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            frontends = tuple(self._frontends)
            self._frontends.clear()
            self._idle.clear()
            self._condition.notify_all()
        for frontend in frontends:
            frontend.close()
