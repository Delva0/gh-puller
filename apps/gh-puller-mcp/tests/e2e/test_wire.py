"""Process-level wire tests against `python -m gh_puller_mcp` (mcp SDK stdio loop).

These drive the real subprocess over newline-framed JSON-RPC, covering the
handshake, tools.list both shapes, envelope passthrough, prompts, and the
documented SDK divergences (missing tool name -> -32602, resources not served).
Backend is a fake `codebase-memory-mcp` shim pointed at via GH_PULLER_MCP_BINARY.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

PROJECT_ROOT = Path(__file__).resolve().parents[2]

INIT_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {"sampling": {}, "roots": {}},
    "clientInfo": {"name": "probe", "version": "0.0.1"},
}

OK_SHIM = """
import json, sys

tool = [a for a in sys.argv[1:] if not a.startswith("-")][-1]
payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
data = json.dumps({"tool": tool, "args": payload}, ensure_ascii=False, separators=(",", ":"))
sys.stdout.write(json.dumps({"content": [{"type": "text", "text": data}],
                             "structuredContent": json.loads(data), "isError": False}))
"""


class WireClient:
    """One stdio MCP server, driven by newline-framed JSON-RPC lines."""

    def __init__(self, argv: list[str], env: dict | None = None) -> None:
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=PROJECT_ROOT,
            env=env if env is not None else os.environ.copy(),
        )
        self._n = 0

    def ask(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        self._n += 1
        request: dict = {"jsonrpc": "2.0", "id": self._n, "method": method}
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
            raise RuntimeError(f"server closed stdout on {method}")
        return json.loads(line)

    def notify(self, method: str) -> None:
        wire = json.dumps({"jsonrpc": "2.0", "method": method}, separators=(",", ":")).encode() + b"\n"
        self.proc.stdin.write(wire)
        self.proc.stdin.flush()

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=15)
        assert self.proc.returncode == 0


def start(shim: str, extra_argv: list[str] | None = None) -> WireClient:
    env = os.environ.copy()
    env["GH_PULLER_MCP_BINARY"] = shim
    return WireClient([sys.executable, "-m", "gh_puller_mcp"] + (extra_argv or []), env=env)


def test_wire_handshake_tools_and_pass_through(shim) -> None:
    client = start(shim(OK_SHIM))
    try:
        r = client.ask("initialize", INIT_PARAMS)
        result = r["result"]
        assert r["jsonrpc"] == "2.0"
        assert result["protocolVersion"] == "2025-06-18"
        assert result["serverInfo"]["name"] == "codebase-memory-mcp"
        assert result["capabilities"]["tools"] == {"listChanged": False}
        assert "experimental" in result["capabilities"]  # SDK default (documented)
        assert result["instructions"].startswith("Use graph tools first for structural code discovery:")
        client.notify("notifications/initialized")

        tools = client.ask("tools/list")["result"]["tools"]
        assert len(tools) == 15
        assert tools[0]["name"] == "index_repository"
        assert set(tools[0]) == {"name", "title", "description", "inputSchema", "annotations"}
        assert "outputSchema" not in tools[0]
        page = client.ask("tools/list", {"cursor": "8"})["result"]
        assert len(page["tools"]) == 7
        assert "nextCursor" not in page

        call = client.ask("tools/call", {"name": "list_projects", "arguments": {"limit": 1}})["result"]
        text_payload = json.loads(call["content"][0]["text"])
        assert text_payload == {"tool": "list_projects", "args": {"limit": 1}}
        assert call["structuredContent"] == text_payload
        assert call["isError"] is False

        unknown = client.ask("tools/call", {"name": "bogus"})["result"]
        assert unknown["content"][0]["text"] == "unknown tool: bogus"
        assert unknown["isError"] is True

        # documented divergence: the SDK rejects a missing tool name via -32602
        missing = client.ask("tools/call", {})
        assert missing["error"]["code"] == -32602

        # documented divergence: resources are not served (and not advertised)
        assert client.ask("resources/list")["error"]["code"] == -32601
        assert client.ask("resources/templates/list")["error"]["code"] == -32601

        assert client.ask("ping")["result"] == {}
        assert client.ask("sample/createMessage")["error"]["code"] == -32601
    finally:
        client.close()


def test_wire_prompts(shim) -> None:
    client = start(shim(OK_SHIM))
    try:
        client.ask("initialize", INIT_PARAMS)
        client.notify("notifications/initialized")
        prompts = client.ask("prompts/list")["result"]["prompts"]
        assert [p["name"] for p in prompts] == ["explore_codebase", "review_change_impact"]
        assert prompts[0]["arguments"][0] == {
            "name": "project",
            "title": "Project",
            "description": "Indexed project name from list_projects.",
            "required": True,
        }
        got = client.ask(
            "prompts/get", {"name": "explore_codebase", "arguments": {"project": "myproj", "question": "q?"}},
        )["result"]
        assert got["description"] == "Graph-first codebase exploration"
        assert got["messages"][0]["content"]["text"].startswith(
            'Explore project "myproj" to answer: q?\n\n',
        )
        error = client.ask("prompts/get", {"name": "not_a_prompt"})
        assert error["error"]["code"] == -32602
        assert error["error"]["message"] == "Invalid prompt name"
    finally:
        client.close()


def test_wire_profile_scout(shim) -> None:
    client = start(shim(OK_SHIM), ["--tool-profile", "scout"])
    try:
        r = client.ask("initialize", INIT_PARAMS)
        assert r["result"]["instructions"].startswith("This is the scout tool profile")
        client.notify("notifications/initialized")
        assert len(client.ask("tools/list")["result"]["tools"]) == 7
        blocked = client.ask("tools/call", {"name": "query_graph", "arguments": {}})["result"]
        assert blocked["isError"] is True
        assert blocked["content"][0]["text"] == (
            "tool 'query_graph' is not available in the scout tool profile"
        )
    finally:
        client.close()


@pytest.mark.parametrize("bad_arg", [None])
def test_wire_backend_shim_missing_binary_reports_envelope(bad_arg) -> None:
    # The shim binary is expected in GH_PULLER_MCP_BINARY; point it at a dead path
    env = os.environ.copy()
    env["GH_PULLER_MCP_BINARY"] = "/nonexistent/cbm-binary"
    client = WireClient([sys.executable, "-m", "gh_puller_mcp"], env=env)
    try:
        client.ask("initialize", INIT_PARAMS)
        client.notify("notifications/initialized")
        result = client.ask("tools/call", {"name": "list_projects", "arguments": {}})["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"].startswith("backend error:")
        assert result["structuredContent"]["error"].startswith("backend error:")
    finally:
        client.close()
