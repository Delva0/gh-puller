"""Investigate a vLLM checkout with OpenCode and the gh-puller MCP server.

    uv run python apps/gh-puller-mcp/examples/opencode_mcp_vllm.py

The example supplies a generic stdio process description that the adapter renders into
native OpenCode MCP configuration. Auto approval supports headless execution, pure mode
isolates external plugins, and ``thinking`` enables reasoning events in the JSON stream.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from gh_puller.agent import OpenCode

# Use an absolute checkout path because the child defaults to the launch directory.
VLLM_ROOT = "/home/delva/projects/vllm"

QUESTION = (
    f"调查 {VLLM_ROOT}(即 ../vllm)仓库,回答:\n"
    "1) 这个仓库是干什么的(一句话总结);\n"
    "2) 推理引擎的调度器(scheduler)核心逻辑在哪些文件、关键函数名是什么。\n"
    "回答时注明引用的文件路径。\n"
    "调查方式:请优先使用 gh_puller MCP 工具桌的图/索引工具(如"
    " gh_puller_list_projects、gh_puller_get_architecture、gh_puller_search_graph、"
    "gh_puller_search_code、gh_puller_get_code_snippet、gh_puller_trace_path)进行调查;"
    "只有 MCP 工具无法覆盖的事实才用 bash/grep 等普通命令补充。"
)


def mcp_servers() -> list[dict]:
    """Build OpenCode's generic stdio description for this monorepo's MCP server."""
    mcp_project = Path(__file__).resolve().parent.parent
    return [
        {
            "id": "gh_puller",
            "command": "uv",
            "args": ["--directory", str(mcp_project), "run", "python", "-m", "gh_puller_mcp"],
        },
    ]


async def main() -> None:
    oc = OpenCode(
        {
            "mcp_servers": mcp_servers(),
            # Auto approval and pure mode support a headless, isolated example.
            "auto": True,
            "thinking": True,
        },
    )
    parts: list[str] = []
    async with oc.session(session_name="example:opencode-mcp-vllm"):
        async for chunk in oc.stream(QUESTION):
            parts.append(chunk)
            print(chunk, end="", flush=True)
    print(f"\n\n== 最终回答 ==\n{''.join(parts) or '(无产出)'}")


if __name__ == "__main__":
    asyncio.run(main())
