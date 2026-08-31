"""Subprocess bridge to the codebase-memory-mcp client binary (`cbm cli`).

Every MCP tool call is delegated to the binary's CLI interface:

    codebase-memory-mcp cli --json <tool_name>        # args on stdin as JSON

`--json` makes the CLI print the raw MCP CallToolResult envelope
(content / structuredContent / isError) to stdout; exit code 1 when the
envelope carries isError. The stdout envelope is passed through verbatim.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BINARY_NAME = "codebase-memory-mcp"
BINARY_ENV = "GH_PULLER_MCP_BINARY"
BINARY_FALLBACK = "/home/delva/.local/bin/codebase-memory-mcp"
FALLBACK_VERSION = "0.10.8"

_STDERR_TAIL = 8 << 10  # bytes of stderr kept for --debug


class BackendError(Exception):
    """The CLI could not be run, or its output was not a CallToolResult envelope."""


@dataclass(frozen=True)
class BackendConfig:
    binary: str | None = None  # override; else env GH_PULLER_MCP_BINARY, PATH, fallback path
    timeout: float | None = None  # per-tool-call wall clock; None = wait forever
    debug: bool = False  # forward backend stderr to our stderr


class Backend:
    """Runs `cli --json <tool>` and returns the parsed CallToolResult envelope."""

    def __init__(self, config: BackendConfig | None = None) -> None:
        self._config = config or BackendConfig()
        self._version: str | None = None

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
        """Cached `--version` parse; falls back to the known release."""
        if self._version is not None:
            return self._version
        try:
            proc = subprocess.run(
                [self.resolve_binary(), "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,  # 版本探测失败由下方 fallback 兜底,不在此抛
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

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """Run one tool; returns the CLI's CallToolResult envelope dict verbatim.

        Raises BackendError when the binary is missing, fails to start, times
        out, or does not print a parseable envelope.
        """
        binary = self.resolve_binary()
        payload = json.dumps(
            arguments if arguments is not None else {},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        cmd = [binary, "cli", "--json", tool_name]
        # subprocess inherits our environment by default (CBM_CACHE_DIR etc.)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise BackendError(f"cannot execute {binary}: {exc}") from None
        try:
            out, err = proc.communicate(input=payload, timeout=self._config.timeout)
        except subprocess.TimeoutExpired:
            self._kill_group(proc)
            raise BackendError("backend timed out") from None
        if proc.returncode not in (0, 1):
            tail = err[-_STDERR_TAIL:].decode("utf-8", "replace").strip()
            raise BackendError(f"backend exited with status {proc.returncode}: {tail}")
        if self._config.debug and err:
            self._forward_stderr(err)
        try:
            envelope = json.loads(out)
        except ValueError:
            raise BackendError("unparseable backend response") from None
        if not isinstance(envelope, dict):
            raise BackendError("unparseable backend response")
        return envelope

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            proc.wait()

    @staticmethod
    def _forward_stderr(err: bytes) -> None:
        for line in err.decode("utf-8", "replace").splitlines():
            sys.stderr.write(f"[backend] {line}\n")
        sys.stderr.flush()
