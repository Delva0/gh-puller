"""Investigate a vLLM checkout with Claude Code and the gh-puller MCP server.

    uv run python apps/gh-puller-mcp/examples/cc_mcp_vllm.py

The example injects an SDK-managed stdio MCP server into ``ClaudeCode``. Headless
execution bypasses permission prompts, while ``allowed_tools`` still limits the Agent to
read-only operations.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from claude_agent_sdk.types import McpStdioServerConfig
from gh_puller.agent import ClaudeCode

# Repository investigated by the child Claude Code process.
VLLM_ROOT = "/home/delva/projects/vllm"

QUESTION = (
    f"调查 {VLLM_ROOT}(即 ../vllm)仓库,回答:\n"
    "1) 这个仓库是干什么的(一句话总结);\n"
    "2) 推理引擎的调度器(scheduler)核心逻辑在哪些文件、关键函数名是什么。\n"
    "回答时注明引用的文件路径。"
)

# Mutation tools are intentionally absent from this allowlist.
MEMORY_TOOLS = [
    "list_projects",
    "get_architecture",
    "search_graph",
    "search_code",
    "query_graph",
    "trace_path",
    "get_code_snippet",
    "get_graph_schema",
    "index_status",
    "check_index_coverage",
    "detect_changes",
]
ALLOWED_TOOLS = (
    MEMORY_TOOLS
    + [f"mcp__gh_puller__{name}" for name in MEMORY_TOOLS]
    + ["Read", "Grep", "Glob"]
)


def mcp_servers() -> dict:
    """Build the SDK stdio configuration for this monorepo's MCP server."""
    mcp_project = Path(__file__).resolve().parent.parent
    return {
        "gh_puller": McpStdioServerConfig(
            command="uv",
            args=["--directory", str(mcp_project), "run", "python", "-m", "gh_puller_mcp"],
        ),
    }


async def main() -> None:
    cc = ClaudeCode(
        {
            "mcp_servers": mcp_servers(),
            # Default isolation ignores user-level MCP configuration.
            "allowed_tools": ALLOWED_TOOLS,
            "permission_mode": "bypassPermissions",
            "include_partial_messages": True,
        },
    )
    parts: list[str] = []
    async with cc.session(session_name="example:cc-mcp-vllm"):
        async for chunk in cc.stream(QUESTION):
            parts.append(chunk)
            print(chunk, end="", flush=True)
    print(f"\n\n== 最终回答 ==\n{''.join(parts) or '(无产出)'}")


if __name__ == "__main__":
    asyncio.run(main())
