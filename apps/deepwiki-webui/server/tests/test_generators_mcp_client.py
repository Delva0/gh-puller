"""Test the real MCP client shape against gh-puller-mcp and a backend shim.

The contract covers a short-lived stdio connection, explicit initialization, error
mapping, and structured-content parsing. ``GH_PULLER_MCP_BINARY`` points the server at
the local shim; environments without uv skip because server installation is external.
"""

import json
import shutil
import textwrap

import pytest

import generators

_SERVER_ENVELOPE = {
    "content": [{"type": "text", "text": json.dumps({
        "total": 1, "cols": ["qn", "label", "file", "lines", "rank"],
        "rows": [["p.demo.f", "Function", "demo.py", "3-7", -1.0]],
    })}],
    "structuredContent": {
        "total": 1, "cols": ["qn", "label", "file", "lines", "rank"],
        "rows": [["p.demo.f", "Function", "demo.py", "3-7", -1.0]],
    },
    "isError": False,
}

_ERR_ENVELOPE = {
    "content": [{"type": "text", "text": "backend error: no such binary"}],
    "isError": True,
}


def _shim(tmp_path, *, raise_on: str | None = None) -> str:
    """Create an executable shim for the codebase-memory JSON CLI."""
    body = textwrap.dedent(f"""
        import json, sys
        tool = sys.argv[-1]
        arguments = json.load(sys.stdin)
        if tool == {raise_on!r} and {raise_on!r} is not None:
            print(json.dumps({_ERR_ENVELOPE!r}))
        else:
            print(json.dumps({_SERVER_ENVELOPE!r}))
    """)
    path = tmp_path / "cbm-shim"
    path.write_text("#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@pytest.mark.asyncio
async def test_call_tool_search_graph_shim(tmp_path, monkeypatch):
    if shutil.which("uv") is None:
        pytest.skip("uv 不可用(gh-puller-mcp 经 uv 启动)")
    monkeypatch.setenv("GH_PULLER_MCP_BINARY", _shim(tmp_path))
    data = await generators._call_mcp_tool("search_graph", {"project": "p", "query": "f"})
    # The MCP envelope unwraps structured content into the tool result mapping.
    assert data["cols"] == ["qn", "label", "file", "lines", "rank"]
    assert data["rows"][0][2] == "demo.py" and data["rows"][0][3] == "3-7"


@pytest.mark.asyncio
async def test_call_tool_is_error_raises(tmp_path, monkeypatch):
    if shutil.which("uv") is None:
        pytest.skip("uv 不可用(gh-puller-mcp 经 uv 启动)")
    monkeypatch.setenv("GH_PULLER_MCP_BINARY", _shim(tmp_path, raise_on="index_repository"))
    with pytest.raises(RuntimeError, match="backend error"):
        await generators._call_mcp_tool("index_repository", {"repo_path": "/x"})
