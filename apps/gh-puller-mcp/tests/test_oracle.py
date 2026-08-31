"""Oracle parity test: the Python re-implementation vs the real codebase-memory-mcp server.

Runs unconditionally against the real binary on this machine (spawned as its
stdio MCP server next to `python -m gh_puller_mcp`), fed the same newline-framed
request sequence and compared SEMANTICALLY (dict-equality after normalizing the
documented SDK divergences).

The wire serialization of the mcp SDK puts keys in its own (alphabetical) order
and advertises an extra `experimental` capability, so byte equality with the
C server is intentionally relaxed; tool result *content* (text strings) is
still compared exactly.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BINARY = shutil.which("codebase-memory-mcp") or "/home/delva/.local/bin/codebase-memory-mcp"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

_INIT_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {"sampling": {}, "roots": {}},
    "clientInfo": {"name": "oracle-probe", "version": "0.0.1"},
}

if not Path(BINARY).exists():  # fail loudly, never skip: the oracle IS the parity acceptance
    pytest.fail(f"codebase-memory-mcp 二进制不可用: {BINARY}")


class MCPLineClient:
    """One stdio MCP server, driven by newline-framed JSON-RPC."""

    def __init__(self, argv: list[str], cwd: Path = PROJECT_ROOT) -> None:
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=os.environ.copy(),
        )
        self._counter = 0

    def ask(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        self._counter += 1
        request: dict = {"jsonrpc": "2.0", "id": self._counter, "method": method}
        if params is not None:
            request["params"] = params
        wire = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        self.proc.stdin.write(wire)
        self.proc.stdin.flush()
        readable, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not readable:
            raise TimeoutError(f"no response to {method} within {timeout}s")
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"server {self.proc.pid} closed stdout on {method}")
        return json.loads(line)

    def notify(self, method: str) -> None:
        wire = json.dumps({"jsonrpc": "2.0", "method": method}, separators=(",", ":")).encode() + b"\n"
        self.proc.stdin.write(wire)
        self.proc.stdin.flush()

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=15)
        stderr = self.proc.stderr.read(4096).decode("utf-8", "replace")
        assert self.proc.returncode == 0, f"server exited {self.proc.returncode}: {stderr}"


def _normalize(payload: dict, deep: bool = False) -> dict:
    """Normalize documented divergences; copy-on-write."""
    out = json.loads(json.dumps(payload))
    with contextlib.suppress(KeyError, TypeError):
        out["result"]["serverInfo"]["version"] = "X"
    try:
        capabilities = out["result"]["capabilities"]
    except (KeyError, TypeError):
        capabilities = None
    if isinstance(capabilities, dict):
        for extra in ("experimental", "resources", "logging", "completions", "extensions", "tasks"):
            capabilities.pop(extra, None)
    if deep:
        with contextlib.suppress(KeyError, TypeError):
            for tool in out["result"]["tools"]:
                tool.pop("execution", None)
    return out


def _assert_equal(real: dict, ours: dict) -> None:
    assert _normalize(real) == _normalize(ours)


def _replicate(profile: str | None) -> list[str]:
    argv = [sys.executable, "-m", "gh_puller_mcp"]
    if profile:
        argv += ["--tool-profile", profile]
    return argv


@pytest.mark.parametrize("profile", [None, "analysis", "scout"])
def test_oracle_sequence(profile: str | None) -> None:
    real = MCPLineClient([BINARY] + (["--tool-profile", profile] if profile else []))
    ours = MCPLineClient(_replicate(profile))
    try:
        # ── handshake ──
        r, o = real.ask("initialize", _INIT_PARAMS), ours.ask("initialize", _INIT_PARAMS)
        _assert_equal(r, o)
        real.notify("notifications/initialized")
        ours.notify("notifications/initialized")

        # ── tools/list: full list and paginated pages ──
        _assert_equal(real.ask("tools/list"), ours.ask("tools/list"))
        for cursor in ("4", "8"):
            _assert_equal(
                real.ask("tools/list", {"cursor": cursor}),
                ours.ask("tools/list", {"cursor": cursor}),
            )

        # ── prompts (static) ──
        for name, arguments in (
            ("explore_codebase", {"project": "demo", "question": "how does x work?"}),
            ("review_change_impact", {"project": "demo", "change": "the change"}),
        ):
            _assert_equal(
                real.ask("prompts/get", {"name": name, "arguments": arguments}),
                ours.ask("prompts/get", {"name": name, "arguments": arguments}),
            )

        # ── dynamic: list_projects to pick a real project ──
        r_lp = real.ask("tools/call", {"name": "list_projects", "arguments": {"limit": 1}})
        o_lp = ours.ask("tools/call", {"name": "list_projects", "arguments": {"limit": 1}})
        _assert_equal(r_lp, o_lp)

        def _pick_project(resp: dict) -> str | None:
            try:
                return json.loads(resp["result"]["content"][0]["text"])["projects"][0]["name"]
            except (KeyError, IndexError, json.JSONDecodeError):
                return None

        project = _pick_project(o_lp)

        # ── dynamic tool calls on the real index (identical args to both) ──
        if project:  # search_graph is available in every profile
            args = {"project": project, "query": "list", "limit": 1}
            _assert_equal(
                real.ask("tools/call", {"name": "search_graph", "arguments": args}),
                ours.ask("tools/call", {"name": "search_graph", "arguments": args}),
            )
            args_json = dict(args, format="json")
            _assert_equal(
                real.ask("tools/call", {"name": "search_graph", "arguments": args_json}),
                ours.ask("tools/call", {"name": "search_graph", "arguments": args_json}),
            )

        # ── guaranteed error envelope (nonexistent project) ──
        err_args = {"query": "MATCH (n) RETURN n LIMIT 1", "project": "no-such-project-oracle"}
        _assert_equal(
            real.ask("tools/call", {"name": "query_graph", "arguments": err_args}),
            ours.ask("tools/call", {"name": "query_graph", "arguments": err_args}),
        )

        # ── profile-blocked call (blocked before execution, so safe to probe) ──
        blocked_name = {"analysis": "delete_project", "scout": "query_graph"}.get(profile)
        if blocked_name:
            _assert_equal(
                real.ask("tools/call", {"name": blocked_name, "arguments": {"project": "x"}}),
                ours.ask("tools/call", {"name": blocked_name, "arguments": {"project": "x"}}),
            )
    finally:
        real.close()
        ours.close()
