"""opencode+MCP 示例:gh_puller.agent API(OpenCode)装配 gh-puller 的 MCP 服务器,调查 ../vllm 仓库并回答。

    uv run python apps/gh-puller-mcp/examples/opencode_mcp_vllm.py

展示的 gh_puller.agent API(契约一律见 gh_puller/agent/generators/__init__.py
包 docstring;用法与 cc_mcp_vllm.py 同式):OpenCode(config) 构造期注入 config,
键集白名单见 OpenCodeConfig —— mcp_servers 为通用工具桌描述 list({id,
command, args, env_vars}),生成器渲染为 opencode mcp 配置段(命令行 stdio
子进程启动,同 cc 的 McpStdioServerConfig 语义但形状不同 —— opencode 侧原生
mcp);`async with oc.session(...)` 一次上游对话;stream/result 契约见包 docstring。

注:opencode 无 cc 的 allowed_tools 轴(工具桌/白名单由 opencode 自身配置决定,
经 --auto 无头自动批准);thinking 为 config 字段(OpenCodeConfig.thinking)——
CLI 的 JSON 流默认不发推理事件,显式传 True 才打开(reasoning → thinking chunk,
只进监控事件流不产出文本);本示例未显式传 system_prompt —— 生成器缺省不注入。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from gh_puller.agent import OpenCode

# ../vllm:被调查仓库(子进程 cwd 缺省 = 启动目录;相对路径取仓库根需 --dir,
# 此处直接用绝对路径,与问题文本自洽)
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
    """gh-puller 的 MCP 服务器 → opencode mcp_servers 通用描述(CLI 按 stdio 子进程启动)。

    服务器即本仓库 apps/gh-puller-mcp(AGENTS.md apps 约定:独立 uv 项目,经 --directory 定位);
    env_vars 为运行环境注入白名单(本示例无,可省 —— 值不内联进配置)。
    """
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
            # --auto 恒置 True(opencode.py 缺省):无交互主机,权限未明拒即自动批准;
            # --pure 恒传:禁外部插件隔离用户级注入,工具桌 = 本示例装配的 gh-puller 服务。
            "auto": True,
            "thinking": True,  # config 字段显式打开推理块(JSON 流默认不发 reasoning 事件)
        },
    )
    parts: list[str] = []
    async with oc.session(session_name="example:opencode-mcp-vllm"):
        async for chunk in oc.stream(QUESTION):
            parts.append(chunk)
            print(chunk, end="", flush=True)  # 流式增量(终端观察即可)
    print(f"\n\n== 最终回答 ==\n{''.join(parts) or '(无产出)'}")


if __name__ == "__main__":
    asyncio.run(main())
