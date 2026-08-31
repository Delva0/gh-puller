"""cc+MCP 示例:gh_puller.agent API(ClaudeCode)装配 gh-puller 的 MCP 服务器,调查 ../vllm 仓库并回答。

    uv run python apps/gh-puller-mcp/examples/cc_mcp_vllm.py

展示的 gh_puller.agent API(契约见 gh_puller/agent/__init__.py):
- ClaudeCode(config):构造期注入 config,键集白名单见 ClaudeConfig
  —— mcp_servers 经 ClaudeAgentOptions 透传(SDK 子进程启动 stdio MCP 服务器);
- async with cc.session(...):一次上游对话(客户端 spawn 与监控装配同寿);
- stream(prompt):流式产出 assistant 文本增量(thinking/工具调用只进监控事件流,
  不构成产出);result(prompt) 则只取最后一轮终结答案。

注:无交互主机须放行工具许可(permission_mode=bypassPermissions);allowed_tools
仍白名单只读面(index/建图/删项目等变更工具不在列)。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from claude_agent_sdk.types import McpStdioServerConfig
from gh_puller.agent import ClaudeCode

# ../vllm:被调查仓库(cc 进程 cwd 为仓库根,相对路径即上一个平级目录)
VLLM_ROOT = "/home/delva/projects/vllm"

QUESTION = (
    f"调查 {VLLM_ROOT}(即 ../vllm)仓库,回答:\n"
    "1) 这个仓库是干什么的(一句话总结);\n"
    "2) 推理引擎的调度器(scheduler)核心逻辑在哪些文件、关键函数名是什么。\n"
    "回答时注明引用的文件路径。"
)

# 白名单:只读图工具 + 基础只读工具(变更面 index_repository / delete_project / manage_adr /
# ingest_traces 不列;工具调用数不限制 —— 回答由 agent 按需推进)
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
    """gh-puller 的 MCP 服务器 → cc mcp_servers 描述(SDK 按 stdio 子进程启动)。

    服务器即本仓库 apps/gh-puller-mcp(AGENTS.md apps 约定:独立 uv 项目,经 --directory 定位)。
    """
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
            # cc 默认隔离(cc.py):未显式给 strict_mcp_config 时取 True —— 只认本配置的 mcp_servers,
            # 忽略本机用户级 MCP 配置,工具桌 = 本示例装配的 gh-puller 服务。
            "allowed_tools": ALLOWED_TOOLS,
            "permission_mode": "bypassPermissions",  # 无交互主机:工具免确认(白名单仍限只读面)
            "include_partial_messages": True,  # StreamEvent 路径真实产 chunk(监控重建+增量必备)
            # 不设 max_turns:工具调用数不限制,由 agent 按需推进
        },
    )
    parts: list[str] = []
    async with cc.session(session_name="example:cc-mcp-vllm"):
        async for chunk in cc.stream(QUESTION):
            parts.append(chunk)
            print(chunk, end="", flush=True)  # 流式增量(终端观察即可)
    print(f"\n\n== 最终回答 ==\n{''.join(parts) or '(无产出)'}")


if __name__ == "__main__":
    asyncio.run(main())
