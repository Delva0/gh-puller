"""CBM indexing transports used by the persistent archive builder."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_DELTA_ARGUMENTS = frozenset(
    {
        "delta_closure_overflow",
        "delta_closure_cost_percent",
        "delta_dependent_scope",
        "delta_new_surface",
        "delta_reference_fanout_cap",
        "delta_pair_outputs",
        "delta_pair_refresh_budget",
        "delta_pair_input_missing",
    },
)


class CBMTransportError(RuntimeError):
    pass


class ResourceMonitorLike(Protocol):
    child_pid: int | None
    exceeded: bool

    def sample(self) -> None: ...


def _response_object_from_envelope(envelope: object, key: str) -> dict | None:
    if isinstance(envelope, dict):
        value = envelope.get(key)
        if isinstance(value, dict):
            return value
        for value in envelope.values():
            found = _response_object_from_envelope(value, key)
            if found is not None:
                return found
    elif isinstance(envelope, list):
        for value in envelope:
            found = _response_object_from_envelope(value, key)
            if found is not None:
                return found
    elif isinstance(envelope, str) and envelope.lstrip().startswith(("{", "[")):
        try:
            return _response_object_from_envelope(json.loads(envelope), key)
        except json.JSONDecodeError:
            pass
    return None


def index_execution_from_envelope(envelope: object) -> dict | None:
    """Find CBM's machine-readable route evidence in CLI or MCP envelopes."""
    execution = _response_object_from_envelope(envelope, "index_execution")
    return execution if execution is not None and isinstance(execution.get("route"), str) else None


def capabilities_from_tools_list(result: object) -> frozenset[str]:
    """Derive archive-relevant CBM capabilities from an MCP tools/list result."""
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        return frozenset()
    index_tool = next(
        (tool for tool in result["tools"] if isinstance(tool, dict) and tool.get("name") == "index_repository"),
        None,
    )
    if not isinstance(index_tool, dict):
        return frozenset()
    schema = index_tool.get("inputSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return frozenset()
    capabilities = {"persistent-mcp"}
    if "force_full" in properties:
        capabilities.add("force-full-route")
    if properties.keys() >= _DELTA_ARGUMENTS:
        capabilities.add("granular-delta-controls")
    return frozenset(capabilities)


def _index_arguments(
    tree: Path,
    project: str,
    mode: str,
    force_full: bool,
    incremental_controls: Mapping[str, str | int] | None,
) -> dict:
    arguments = {
        "repo_path": str(tree),
        "name": project,
        "mode": mode,
        "persistence": False,
        "force_full": force_full,
    }
    if incremental_controls is not None:
        arguments.update({f"delta_{key}": value for key, value in incremental_controls.items()})
    return arguments


def _checked_index_execution(
    envelope: object,
    force_full: bool,
    incremental_controls: Mapping[str, str | int] | None,
) -> dict:
    execution = index_execution_from_envelope(envelope)
    if force_full and (execution is None or execution.get("route") != "full"):
        raise CBMTransportError("CBM did not confirm the requested force_full build")
    if incremental_controls is not None:
        reported = _response_object_from_envelope(envelope, "incremental_controls")
        if reported != dict(incremental_controls):
            raise CBMTransportError("CBM did not confirm the requested incremental controls")
    return execution or {"route": "unreported"}


class CLITransport:
    """Reference transport: launch one local CLI process for every operation."""

    def __init__(
        self,
        binary: Path,
        cache_root: Path,
        timeout: float,
        monitor: ResourceMonitorLike,
        extra_environment: Mapping[str, str] | None = None,
    ):
        self.binary = binary
        self.cache_root = cache_root
        self.timeout = timeout
        self.monitor = monitor
        self.extra_environment = dict(extra_environment or {})

    @property
    def name(self) -> str:
        return "cli"

    def _run(self, arguments: list[str]) -> object:
        process = subprocess.Popen(
            [str(self.binary), "cli", "--json", *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env={
                **os.environ,
                **self.extra_environment,
                "CBM_CACHE_DIR": str(self.cache_root),
            },
        )
        self.monitor.child_pid = process.pid
        try:
            stdout, stderr = process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise CBMTransportError(f"CBM command timed out after {self.timeout:g}s") from None
        finally:
            self.monitor.child_pid = None
        self.monitor.sample()
        if self.monitor.exceeded:
            raise CBMTransportError("memory limit exceeded while running CBM")
        if process.returncode:
            raise CBMTransportError(f"CBM exited {process.returncode}: {stderr[-2000:]}")
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise CBMTransportError(f"CBM returned invalid JSON: {exc}") from exc
        if not isinstance(envelope, dict):
            raise CBMTransportError("CBM returned a non-object JSON envelope")
        nested_result = envelope.get("result")
        if envelope.get("isError") or (isinstance(nested_result, dict) and nested_result.get("isError")):
            raise CBMTransportError(f"CBM command failed: {stdout[-2000:]}")
        return envelope

    def call_tool(self, name: str, arguments: dict) -> dict:
        envelope = self._run([name, json.dumps(arguments, separators=(",", ":"), ensure_ascii=False)])
        if not isinstance(envelope, dict):
            raise CBMTransportError(f"CBM tool {name} returned a non-object envelope")
        return envelope

    def index(
        self,
        tree: Path,
        project: str,
        mode: str,
        *,
        force_full: bool = False,
        incremental_controls: Mapping[str, str | int] | None = None,
    ) -> dict:
        envelope = self.call_tool(
            "index_repository",
            _index_arguments(tree, project, mode, force_full, incremental_controls),
        )
        return _checked_index_execution(envelope, force_full, incremental_controls)

    def delete_project(self, project: str) -> tuple[bool, str]:
        try:
            envelope = self.call_tool("delete_project", {"project": project})
        except CBMTransportError as exc:
            return False, str(exc)[-1000:]
        return True, json.dumps(envelope, ensure_ascii=False)[-1000:]

    def capabilities(self) -> frozenset[str]:
        """Probe the MCP schema even when subsequent indexing uses one-shot CLI."""
        probe = PersistentMCPTransport(
            self.binary,
            self.cache_root,
            self.timeout,
            self.monitor,
            self.extra_environment,
        )
        try:
            return probe.capabilities()
        finally:
            probe.close()

    def close(self) -> None:
        return


_STREAM_CLOSED = object()


class PersistentMCPTransport:
    """Keep one MCP frontend alive and issue sequential tools/call requests."""

    def __init__(
        self,
        binary: Path,
        cache_root: Path,
        timeout: float,
        monitor: ResourceMonitorLike,
        extra_environment: Mapping[str, str] | None = None,
    ):
        self.binary = binary
        self.cache_root = cache_root
        self.timeout = timeout
        self.monitor = monitor
        self.extra_environment = dict(extra_environment or {})
        self._next_id = 0
        self._messages: queue.Queue[object] = queue.Queue()
        self._pending: dict[int, dict] = {}
        self._stderr_tail: deque[str] = deque(maxlen=256)
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
                **self.extra_environment,
                "CBM_CACHE_DIR": str(cache_root),
            },
        )
        if self.process.stdin is None or self.process.stdout is None or self.process.stderr is None:
            self._terminate()
            raise CBMTransportError("CBM MCP pipes could not be created")
        self.monitor.child_pid = self.process.pid
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        try:
            self._request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "gh-puller-codebase", "version": "1"},
                },
                min(timeout, 60),
            )
            self._notify("notifications/initialized", {})
        except Exception:
            self._terminate()
            raise

    @property
    def name(self) -> str:
        return "persistent-mcp"

    def _stderr_text(self) -> str:
        return "".join(self._stderr_tail)[-4000:]

    def _read_stdout(self) -> None:
        stream = self.process.stdout
        if stream is None:
            self._messages.put(_STREAM_CLOSED)
            return
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
        for line in stream:
            self._stderr_tail.append(line)

    def _send(self, payload: dict) -> None:
        if self._closed or self.process.poll() is not None or self.process.stdin is None:
            raise CBMTransportError(f"CBM MCP process is not running: {self._stderr_text()}")
        try:
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CBMTransportError(f"CBM MCP write failed: {self._stderr_text()}") from exc

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict, timeout: float | None = None) -> dict:
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
                raise CBMTransportError(f"CBM MCP process exited while handling {method}: {self._stderr_text()}")
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

    def call_tool(self, name: str, arguments: dict) -> dict:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise CBMTransportError(f"CBM tool {name} failed: {json.dumps(result, ensure_ascii=False)[-2000:]}")
        return result

    def index(
        self,
        tree: Path,
        project: str,
        mode: str,
        *,
        force_full: bool = False,
        incremental_controls: Mapping[str, str | int] | None = None,
    ) -> dict:
        result = self.call_tool(
            "index_repository",
            _index_arguments(tree, project, mode, force_full, incremental_controls),
        )
        return _checked_index_execution(result, force_full, incremental_controls)

    def delete_project(self, project: str) -> tuple[bool, str]:
        try:
            result = self.call_tool("delete_project", {"project": project})
        except CBMTransportError as exc:
            return False, str(exc)[-1000:]
        return True, json.dumps(result, ensure_ascii=False)[-1000:]

    def capabilities(self) -> frozenset[str]:
        """Return capabilities advertised by the live server's index schema."""
        return capabilities_from_tools_list({"tools": self.list_tools()})

    def list_tools(self) -> list[dict]:
        """Return native tool definitions across all advertised MCP cursor pages."""
        definitions = []
        arguments = {}
        while True:
            result = self._request("tools/list", arguments)
            definitions.extend(result["tools"])
            if not result.get("nextCursor"):
                return definitions
            arguments = {"cursor": result["nextCursor"]}

    def _terminate(self) -> None:
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
        self.monitor.child_pid = None

    def close(self) -> None:
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
        self._stdout_thread.join(timeout=2)
        self._stderr_thread.join(timeout=2)
        self.monitor.child_pid = None
        self.monitor.sample()


def make_transport(
    name: str,
    binary: Path,
    cache_root: Path,
    timeout: float,
    monitor: ResourceMonitorLike,
    extra_environment: Mapping[str, str] | None = None,
) -> CLITransport | PersistentMCPTransport:
    if name == "cli":
        return CLITransport(binary, cache_root, timeout, monitor, extra_environment)
    if name == "persistent-mcp":
        return PersistentMCPTransport(binary, cache_root, timeout, monitor, extra_environment)
    raise CBMTransportError(f"unknown CBM transport: {name}")
