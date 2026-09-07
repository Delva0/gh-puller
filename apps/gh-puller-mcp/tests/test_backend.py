"""Persistent backend tests: lifecycle, wire contract, errors, and resolution."""

from __future__ import annotations

import contextlib
import json
import os

import pytest

from gh_puller_mcp.backend import Backend, BackendConfig, BackendError

OK_SHIM = """
import json, os, sys

if "--version" in sys.argv:
    print("codebase-memory-mcp 0.10.8")
    raise SystemExit

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "notifications/initialized":
        continue
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05",
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "codebase-memory-mcp", "version": "0.10.8"}}
    elif method == "tools/call":
        tool = request["params"]["name"]
        payload = request["params"]["arguments"]
        data = json.dumps({"tool": tool, "args": payload}, ensure_ascii=False, separators=(",", ":"))
        result = {"content": [{"type": "text", "text": data}],
                  "structuredContent": json.loads(data), "isError": False}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\\n")
    sys.stdout.flush()
"""


@contextlib.contextmanager
def running_backend(exe: str, **kwargs):
    backend = Backend(BackendConfig(binary=exe, **kwargs))
    try:
        yield backend
    finally:
        backend.close()


def test_persistent_wire_and_env_contract(shim, tmp_path, monkeypatch) -> None:
    argv_out = tmp_path / "argv.json"
    stdin_out = tmp_path / "stdin.json"
    env_out = tmp_path / "env.txt"
    exe = shim(
        OK_SHIM
        + f"\nopen({str(argv_out)!r}, 'w').write(json.dumps(sys.argv))\n"
        + f"\nopen({str(stdin_out)!r}, 'w').write(json.dumps(payload, ensure_ascii=False))\n"
        + f"\nopen({str(env_out)!r}, 'w').write(os.environ.get('CBM_CACHE_DIR', ''))\n",
    )
    monkeypatch.setenv("CBM_CACHE_DIR", "/tmp/fake-cache-cbm")
    with running_backend(exe) as backend:
        envelope = backend.call_tool("search_graph", {"project": "p", "min_degree": 2})

    assert json.loads(argv_out.read_text()) == [exe]
    assert json.loads(stdin_out.read_text()) == {"project": "p", "min_degree": 2}
    assert env_out.read_text() == "/tmp/fake-cache-cbm"
    text = json.loads(envelope["content"][0]["text"])
    assert text == {"tool": "search_graph", "args": {"project": "p", "min_degree": 2}}


def test_frontend_is_reused_across_calls(shim, tmp_path) -> None:
    starts = tmp_path / "starts.txt"
    exe = shim(
        f"open({str(starts)!r}, 'a').write('start\\n')\n" + OK_SHIM,
    )
    with running_backend(exe) as backend:
        backend.call_tool("list_projects", {})
        backend.call_tool("get_graph_schema", {"project": "p"})
    assert starts.read_text().splitlines() == ["start"]


def test_error_envelope_is_returned(shim) -> None:
    exe = shim(
        """
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "notifications/initialized":
        continue
    if request.get("method") == "initialize":
        result = {"serverInfo": {"version": "0.10.8"}}
    else:
        result = {"content": [{"type": "text", "text": '{"error":"missing project"}'}],
                  "structuredContent": {"error": "missing project"}, "isError": True}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""",
    )
    with running_backend(exe) as backend:
        envelope = backend.call_tool("list_projects", {})
    assert envelope["isError"] is True


def test_nonzero_exit_raises_backend_error(shim) -> None:
    exe = shim("import sys\nsys.stderr.write('kaboom\\n')\nsys.exit(2)\n")
    with running_backend(exe) as backend, pytest.raises(BackendError, match="backend"):
        backend.call_tool("list_projects", {})


def test_unparseable_stdout_raises(shim) -> None:
    exe = shim("import sys\nsys.stdout.write('not json at all\\n')\nsys.stdout.flush()\nsys.stdin.read()\n")
    with running_backend(exe) as backend, pytest.raises(BackendError, match="unparseable backend response"):
        backend.call_tool("list_projects", {})


def test_missing_binary_raises() -> None:
    with running_backend("/nonexistent/cbm") as backend, pytest.raises(BackendError, match="cannot execute"):
        backend.call_tool("list_projects", {})


def test_timeout_kills_frontend(shim) -> None:
    exe = shim(
        """
import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "notifications/initialized":
        continue
    if request.get("method") == "initialize":
        result = {"serverInfo": {"version": "0.10.8"}}
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
    else:
        time.sleep(30)
""",
    )
    with running_backend(exe, timeout=0.3) as backend, pytest.raises(BackendError, match="backend timed out"):
        backend.call_tool("list_projects", {})


def test_stderr_captured_and_forwarded_in_debug(shim, capsys) -> None:
    exe = shim("import os\nos.write(2, b'level=info msg=mem.init budget_mb=1\\n')\n" + OK_SHIM)
    with running_backend(exe) as backend:
        backend.call_tool("list_projects", {})
    captured = capsys.readouterr()
    assert "level=info" not in captured.out
    assert "level=info" not in captured.err

    with running_backend(exe, debug=True) as backend:
        backend.call_tool("list_projects", {})
    captured = capsys.readouterr()
    assert "[backend] level=info" in captured.err


def test_binary_resolution_order(tmp_path, shim, monkeypatch) -> None:
    explicit = shim(OK_SHIM, name="explicit-cbm")
    on_path = shim(OK_SHIM, name="codebase-memory-mcp")
    monkeypatch.setenv("GH_PULLER_MCP_BINARY", explicit)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(tmp_path), "/usr/bin", "/bin", "/usr/local/bin"]),
    )
    assert Backend(BackendConfig()).resolve_binary() in (on_path, explicit)
    other = shim(OK_SHIM, name="other-cbm")
    assert Backend(BackendConfig(binary=other)).resolve_binary() == other
    monkeypatch.setenv("GH_PULLER_MCP_BINARY", explicit)
    assert Backend(BackendConfig()).resolve_binary() == explicit
    del_ = shim(OK_SHIM, name="codebase-memory-mcp")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("GH_PULLER_MCP_BINARY", raising=False)
    assert Backend(BackendConfig()).resolve_binary() == del_
    monkeypatch.setenv("PATH", "/nonexistent-path")
    assert Backend(BackendConfig()).resolve_binary().endswith("/.local/bin/codebase-memory-mcp")
