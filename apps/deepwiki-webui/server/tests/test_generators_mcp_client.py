"""generators._call_tool 的真实客户端形状测试(gh-puller-mcp 真服务器 + 后端 shim)。

锁住的是 mcp SDK 客户端的调用形态:stdio 短连接 + 显式 initialize + 信封解析
(isError → RuntimeError / structuredContent 解析)。后端二进制经
GH_PULLER_MCP_BINARY 指向本文件提供的 shim(与 gh-puller-mcp tests 同式);
无 uv 环境则跳过(gh-puller-mcp 未装,不属本 app 契约)。
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
    """可执行 Python 脚本冒充 C 二进制(codebase-memory-mcp cli --json <tool>)。"""
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
    # 信封 structuredContent 解析 → 工具形状(dict 直出)
    assert data["cols"] == ["qn", "label", "file", "lines", "rank"]
    assert data["rows"][0][2] == "demo.py" and data["rows"][0][3] == "3-7"


@pytest.mark.asyncio
async def test_call_tool_is_error_raises(tmp_path, monkeypatch):
    if shutil.which("uv") is None:
        pytest.skip("uv 不可用(gh-puller-mcp 经 uv 启动)")
    monkeypatch.setenv("GH_PULLER_MCP_BINARY", _shim(tmp_path, raise_on="index_repository"))
    with pytest.raises(RuntimeError, match="backend error"):
        await generators._call_mcp_tool("index_repository", {"repo_path": "/x"})
