"""Persistent stdio bridge to one daemon-backed codebase-memory-mcp frontend.

The frontend is started once per gh-puller-mcp server and kept until shutdown.
It owns the supported CBM daemon connection, while this module serializes tool
requests over its newline-framed MCP stream and preserves CallToolResult
envelopes verbatim.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

BINARY_NAME = "codebase-memory-mcp"
BINARY_ENV = "GH_PULLER_MCP_BINARY"
BINARY_FALLBACK = "/home/delva/.local/bin/codebase-memory-mcp"
FALLBACK_VERSION = "0.10.8"

_STARTUP_TIMEOUT = 60.0
_CLOSE_TIMEOUT = 15.0
_STDERR_TAIL = 8 << 10
_STREAM_CLOSED = object()
_INITIALIZE_PARAMS = {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "gh-puller-mcp-backend", "version": "1"},
}


class BackendError(Exception):
    """The persistent CBM frontend could not serve a tool request."""


@dataclass(frozen=True)
class BackendConfig:
    binary: str | None = None  # override; else env GH_PULLER_MCP_BINARY, PATH, fallback path
    timeout: float | None = None  # per-tool-call wall clock; None = wait forever
    debug: bool = False  # forward backend stderr to our stderr


class _PersistentClient:
    """One initialized native MCP frontend with a synchronous request stream."""

    def __init__(self, binary: str, config: BackendConfig) -> None:
        self._config = config
        self._responses: queue.Queue[object] = queue.Queue()
        self._stderr: deque[bytes] = deque(maxlen=32)
        self._pending: dict[int, dict] = {}
        self._next_id = 0
        self._closed = False
        try:
            self._process = subprocess.Popen(
                [binary],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
        except OSError as exc:
            raise BackendError(f"cannot execute {binary}: {exc}") from None
        if self._process.stdin is None or self._process.stdout is None or self._process.stderr is None:
            self._terminate()
            raise BackendError("backend pipes could not be created")
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        try:
            result = self._request("initialize", _INITIALIZE_PARAMS, _STARTUP_TIMEOUT)
            server_info = result.get("serverInfo")
            self.version = server_info.get("version") if isinstance(server_info, dict) else None
            self._notify("notifications/initialized", {})
        except BackendError:
            self._terminate()
            raise

    @property
    def alive(self) -> bool:
        return not self._closed and self._process.poll() is None

    def call_tool(self, tool_name: str, arguments: dict, timeout: float | None) -> dict:
        """Call one tool through the initialized frontend.

        Args:
            tool_name: Native CBM tool name.
            arguments: Tool arguments passed without transformation.
            timeout: Maximum wall time for this request; ``None`` waits indefinitely.
        """
        return self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments if arguments is not None else {}},
            timeout,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.stdin is not None:
            with contextlib.suppress(OSError):
                self._process.stdin.close()
        try:
            self._process.wait(timeout=_CLOSE_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._kill_group()
        self._join_readers()

    def _request(self, method: str, params: dict, timeout: float | None) -> dict:
        if not self.alive or self._process.stdin is None:
            raise BackendError(self._failure("backend is not running"))
        self._next_id += 1
        request_id = self._next_id
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            self._process.stdin.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n",
            )
            self._process.stdin.flush()
        except (BrokenPipeError, OSError):
            self._terminate()
            raise BackendError(self._failure("backend request could not be written")) from None

        while True:
            if request_id in self._pending:
                response = self._pending.pop(request_id)
                break
            try:
                message = self._responses.get(timeout=timeout)
            except queue.Empty:
                self._terminate()
                raise BackendError("backend timed out") from None
            if message is _STREAM_CLOSED:
                self._terminate()
                raise BackendError(self._failure("backend closed its response stream"))
            if isinstance(message, BackendError):
                self._terminate()
                raise message
            if not isinstance(message, dict):
                continue
            message_id = message.get("id")
            if message_id == request_id:
                response = message
                break
            if isinstance(message_id, int):
                self._pending[message_id] = message

        if response.get("error") is not None:
            raise BackendError(f"backend JSON-RPC error: {response['error']}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise BackendError("unparseable backend response")
        return result

    def _notify(self, method: str, params: dict) -> None:
        if not self.alive or self._process.stdin is None:
            raise BackendError(self._failure("backend is not running"))
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            self._process.stdin.write(
                json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n",
            )
            self._process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise BackendError(self._failure("backend notification could not be written")) from None

    def _read_stdout(self) -> None:
        stream = self._process.stdout
        if stream is None:
            self._responses.put(_STREAM_CLOSED)
            return
        for line in stream:
            try:
                self._responses.put(json.loads(line))
            except ValueError:
                self._responses.put(BackendError("unparseable backend response"))
                return
        self._responses.put(_STREAM_CLOSED)

    def _read_stderr(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        for line in stream:
            self._stderr.append(line[-_STDERR_TAIL:])
            if self._config.debug:
                self._forward_stderr(line)

    def _failure(self, message: str) -> str:
        status = self._process.poll()
        tail = b"".join(self._stderr)[-_STDERR_TAIL:].decode("utf-8", "replace").strip()
        details = []
        if status is not None:
            details.append(f"status {status}")
        if tail:
            details.append(tail)
        return f"{message}: {'; '.join(details)}" if details else message

    def _terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.stdin is not None:
            with contextlib.suppress(OSError):
                self._process.stdin.close()
        if self._process.poll() is None:
            self._kill_group()
        self._join_readers()

    def _kill_group(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self._process.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            self._process.wait()

    def _join_readers(self) -> None:
        stdout_thread = getattr(self, "_stdout_thread", None)
        stderr_thread = getattr(self, "_stderr_thread", None)
        if stdout_thread is not None:
            stdout_thread.join(timeout=2)
        if stderr_thread is not None:
            stderr_thread.join(timeout=2)

    @staticmethod
    def _forward_stderr(data: bytes) -> None:
        for line in data.decode("utf-8", "replace").splitlines():
            sys.stderr.write(f"[backend] {line}\n")
        sys.stderr.flush()


class Backend:
    """Own one persistent native MCP frontend for the server lifetime."""

    def __init__(self, config: BackendConfig | None = None) -> None:
        self._config = config or BackendConfig()
        self._version: str | None = None
        self._client: _PersistentClient | None = None
        self._lock = threading.RLock()
        self._closed = False

    @property
    def config(self) -> BackendConfig:
        return self._config

    def resolve_binary(self) -> str:
        """Resolve the binary path: config flag > env > PATH > ~/.local/bin."""
        if self._config.binary:
            return self._config.binary
        env = os.environ.get(BINARY_ENV)
        if env:
            return env
        found = shutil.which(BINARY_NAME)
        if found:
            return found
        return str(Path(BINARY_FALLBACK))

    def version(self) -> str:
        """Return the cached native version, falling back to the pinned release."""
        if self._version is not None:
            return self._version
        try:
            proc = subprocess.run(
                [self.resolve_binary(), "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            match = re.search(
                r"codebase-memory-mcp (\d+\.\d+\.\d+)", proc.stdout + proc.stderr,
            )
            if proc.returncode == 0 and match:
                self._version = match.group(1)
                return self._version
        except (OSError, subprocess.TimeoutExpired):
            pass
        self._version = FALLBACK_VERSION
        return self._version

    def start(self) -> None:
        """Start and initialize the native frontend once."""
        with self._lock:
            self._ensure_client()

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """Run one tool through the persistent frontend.

        Args:
            tool_name: Native CBM tool name.
            arguments: Tool arguments passed without transformation.

        Returns:
            The native CallToolResult envelope.

        Raises:
            BackendError: The frontend could not start or complete the request.
        """
        with self._lock:
            client = self._ensure_client()
            try:
                return client.call_tool(tool_name, arguments, self._config.timeout)
            except BackendError:
                if not client.alive:
                    self._client = None
                raise

    def close(self) -> None:
        """Close the frontend and release its daemon session."""
        with self._lock:
            self._closed = True
            client, self._client = self._client, None
            if client is not None:
                client.close()

    def _ensure_client(self) -> _PersistentClient:
        if self._closed:
            raise BackendError("backend is closed")
        if self._client is not None and self._client.alive:
            return self._client
        if self._client is not None:
            self._client.close()
        self._client = _PersistentClient(self.resolve_binary(), self._config)
        if self._client.version:
            self._version = self._client.version
        return self._client
