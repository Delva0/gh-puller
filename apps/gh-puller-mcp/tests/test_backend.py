"""Backend bridge tests: argv/stdin/env contract, rc semantics, errors, resolution."""

from __future__ import annotations

import json
import os

import pytest

from gh_puller_mcp.backend import Backend, BackendConfig, BackendError

OK_SHIM = """
import json, os, sys

tool = [a for a in sys.argv[1:] if not a.startswith("-")][-1]
payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
data = json.dumps({"tool": tool, "args": payload}, ensure_ascii=False, separators=(",", ":"))
sys.stdout.write(json.dumps({"content": [{"type": "text", "text": data}],
                             "structuredContent": json.loads(data), "isError": False}))
os.write(2, b"level=info msg=mem.init budget_mb=1\\n")
"""


def test_argv_stdin_env_contract(shim, tmp_path) -> None:
    argv_out = tmp_path / "argv.json"
    stdin_out = tmp_path / "stdin.json"
    exe = shim(
        OK_SHIM
        + f"\nopen({str(argv_out)!r}, 'w').write(json.dumps(sys.argv))\n"
        + f"\nopen({str(stdin_out)!r}, 'w').write(json.dumps(payload, ensure_ascii=False))\n",
    )
    backend = Backend(BackendConfig(binary=exe))
    envelope = backend.call_tool("search_graph", {"project": "p", "min_degree": 2})
    argv = json.loads(argv_out.read_text())
    stdin_json = json.loads(stdin_out.read_text())
    assert argv == [exe, "cli", "--json", "search_graph"]
    assert stdin_json == {"project": "p", "min_degree": 2}
    text = json.loads(envelope["content"][0]["text"])
    assert text == {"tool": "search_graph", "args": stdin_json}


def test_rc_one_still_returns_error_envelope(shim) -> None:
    exe = shim(
        """
import json, sys
sys.stdin.read()
sys.stdout.write(json.dumps({"content": [{"type": "text", "text": '{"error":"missing project"}'}],
                             "structuredContent": {"error": "missing project"}, "isError": True}))
sys.exit(1)
""",
    )
    envelope = Backend(BackendConfig(binary=exe)).call_tool("list_projects", {})
    assert envelope["isError"] is True


def test_nonzero_rc_raises_shim_error(shim) -> None:
    exe = shim("import sys\nsys.stderr.write('kaboom\\n')\nsys.exit(2)\n")
    with pytest.raises(BackendError, match="status 2"):
        Backend(BackendConfig(binary=exe)).call_tool("list_projects", {})


def test_unparseable_stdout_raises(shim) -> None:
    exe = shim("import sys\nsys.stdout.write('not json at all')\n")
    with pytest.raises(BackendError, match="unparseable backend response"):
        Backend(BackendConfig(binary=exe)).call_tool("list_projects", {})


def test_missing_binary_raises() -> None:
    with pytest.raises(BackendError, match="cannot execute"):
        Backend(BackendConfig(binary="/nonexistent/cbm")).call_tool("list_projects", {})


def test_timeout_kills_process_group(shim) -> None:
    exe = shim("import time\ntime.sleep(30)\n")
    with pytest.raises(BackendError, match="backend timed out"):
        Backend(BackendConfig(binary=exe, timeout=0.3)).call_tool("list_projects", {})


def test_stderr_captured_and_forwarded_in_debug(shim, capsys) -> None:
    exe = shim(OK_SHIM)
    Backend(BackendConfig(binary=exe)).call_tool("list_projects", {})
    captured = capsys.readouterr()
    assert "level=info" not in captured.out

    Backend(BackendConfig(binary=exe, debug=True)).call_tool("list_projects", {})
    captured = capsys.readouterr()
    assert "[backend] level=info" in captured.err


def test_env_passthrough(shim, tmp_path) -> None:
    env_out = tmp_path / "env.txt"
    exe = shim(OK_SHIM + f"\nopen({str(env_out)!r}, 'w').write(os.environ.get('CBM_CACHE_DIR', ''))\n")
    os.environ["CBM_CACHE_DIR"] = "/tmp/fake-cache-cbm"
    try:
        Backend(BackendConfig(binary=exe)).call_tool("list_projects", {})
        assert env_out.read_text() == "/tmp/fake-cache-cbm"
    finally:
        del os.environ["CBM_CACHE_DIR"]


def test_binary_resolution_order(tmp_path, shim, monkeypatch) -> None:
    explicit = shim(OK_SHIM, name="explicit-cbm")
    on_path = shim(OK_SHIM, name="codebase-memory-mcp")
    monkeypatch.setenv("GH_PULLER_MCP_BINARY", explicit)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(tmp_path), "/usr/bin", "/bin", "/usr/local/bin"]),
    )
    assert Backend(BackendConfig()).resolve_binary() in (on_path, explicit)
    # explicit flag beats env
    other = shim(OK_SHIM, name="other-cbm")
    assert Backend(BackendConfig(binary=other)).resolve_binary() == other
    # env beats PATH
    monkeypatch.setenv("GH_PULLER_MCP_BINARY", explicit)
    assert Backend(BackendConfig()).resolve_binary() == explicit
    # PATH beats the hardcoded fallback
    del_ = shim(OK_SHIM, name="codebase-memory-mcp")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("GH_PULLER_MCP_BINARY", raising=False)
    assert Backend(BackendConfig()).resolve_binary() == del_
    # no PATH hit -> hardcoded fallback path
    monkeypatch.setenv("PATH", "/nonexistent-path")
    assert Backend(BackendConfig()).resolve_binary().endswith("/.local/bin/codebase-memory-mcp")
