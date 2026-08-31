"""生成器运行时装配(webui 专属):deepwiki 引擎与 graphify/agent SDK 的全部耦合点。

本体 = 原 gh_puller.deepwiki.utils 的 graphify 部分整体上移:图产物路径
(graph_dir/graph_path/index_ready)、四路适配器所需的 graphify MCP 工具桌
(进程内 server[cc]/子进程描述[dsh/codex])、图查询 + 子图解析检索簇
(GraphService.context = 原 graphify_context;subgraph_hits/子图源窗口)、
索引保障(ensure_index)。

注入方式:runtime_config 在 generator_config(覆盖构造参数集)上注入——
- 所有路:`graph`(图服务:ready/context),引擎经 deepwiki.utils.graph_service 取用;
- cc:`mcp_servers={"graphify": 进程内 server}` + `allowed_tools` 图工具名;
- dsh:`mcp_servers` 子进程描述;codex:`mcp_servers` 子进程描述 + `env.GRAPHIFY_OUT`;
引擎侧 adapter 只做白名单透传,零图知识。

导入副作用(引擎导入零副作用之外,本模块被 app.py/tasks.py 及其测试导入才生效):
无(图服务经 generator_config 显式传入,不做模块绑定)。
"""

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from gh_puller import envs  # 模块对象绑定:属性调用时取(测试 patch/强刷活性)
from gh_puller.deepwiki.utils import resolve_generator
from gh_puller.utils import Repo

import graphify_wrapper as graphify  # 图封装层(原 gh_puller.graphify 迁入本 app;主包已移除 graphifyy 依赖)

# ---------------------------------------------------------------------------
# 图产物路径/索引就绪(原 deepwiki.utils;图知识归本组装层)
# ---------------------------------------------------------------------------


def graph_dir(repo: Repo) -> Path:
    """单仓库图产物根(extract 的 out_dir):graphify/{type}_{name},无日期层,路径稳定以支持已缓存即跳过。"""
    return Path(envs.DEEPWIKI_ROOT) / "graphify" / f"{repo.repo_type}_{repo.name}"


def graph_path(repo: Repo) -> Path:
    """graph.json 规范路径(extract 的 out_dir 即最终目录,无 graphify-out 层)。"""
    return graph_dir(repo) / "graph.json"


def index_ready(repo: Repo) -> bool:
    """索引完成信号 = graph.json 已存在(复用 graphify._load_graph 的存在性语义)。"""
    return graph_path(repo).exists()


# ---------------------------------------------------------------------------
# graphify MCP 工具桌(本层自持 → runtime_config 经 generator_config 注入;适配层零工具名)
# ---------------------------------------------------------------------------


def _graphify_mcp(backend: str) -> list[dict]:
    """图工具桌 → 适配层通用 mcp_servers 描述。

    backend="dsh":组合 mcp-client 行(id mcp-graphify / serverName graphify);
    backend="codex":config.toml 段(id graphify + env_vars 白名单透传 GRAPHIFY_OUT)。
    """
    command = os.environ.get("GRAPHIFY_MCP_PYTHON") or sys.executable
    if backend == "dsh":
        return [{"id": "mcp-graphify", "serverName": "graphify",
                 "command": command, "args": ["-m", "graphify.serve"]}]
    return [{"id": "graphify", "command": command, "args": ["-m", "graphify.serve"],
             "env_vars": ["GRAPHIFY_OUT"]}]


def _graphify_server(repo: Repo):
    """进程内 MCP server:把 graphify.query 封装为 graphify_query 工具(闭包绑定图路径)。"""
    gp = graph_path(repo)

    @tool(
        "graphify_query",
        "Query this repository's code graph and return the related code subgraph "
        "(functions/classes/call relationships as text), with `Source: <file path> L<line> "
        "markers. Use it to get code structure and line-number references.",
        {"question": str},
    )
    async def graphify_query(args: dict) -> dict:
        try:
            question = (args.get("question") or "").strip()
            result = graphify.query(question, graph_path=gp)
            text = result.get("answer") or ""
        except Exception as exc:
            text = f"Graph query failed: {type(exc).__name__}: {exc}"
        return {"content": [{"type": "text", "text": text.strip() or "(No matching results in code graph)"}]}

    return create_sdk_mcp_server("graphify", tools=[graphify_query])


# ---------------------------------------------------------------------------
# 图查询 + 子图解析检索簇(原 deepwiki.utils;llm 路专用,失败即 raise,不许"带病继续")
# ---------------------------------------------------------------------------


# 纯 LLM 路径单文件内联截断(字符)
_FILE_INLINE_CAP = 8000


def subgraph_hits(answer: str) -> dict[str, list[int]]:
    """解析 graphify.query 的 answer 标注 → {file: [行号...]}。

    NODE 行形如 `NODE <label> [src=<file> loc=L<line> community=<cid>]`,
    EDGE 行以 ` at=<file>:L<line>` 结尾;容忍 [i] TRUNCATED / over-budget
    前缀行与 ... (truncated 尾行;非匹配行忽略,loc 缺失/为空的文件跳过)。
    """
    hits: dict[str, list[int]] = {}
    for raw in answer.splitlines():
        m = re.match(r"^NODE\s+.+?\s+\[([^\]]*)\]$", raw)
        if m:
            src = re.search(r"\bsrc=(\S+)\b", m.group(1))
            loc = re.search(r"\bloc=(L?\d+)\b", m.group(1))
            if src and loc:
                hits.setdefault(src.group(1), []).append(int(loc.group(1).lstrip("L")))
            continue
        m = re.search(r"\bat=([^:\s]+):(L\d+)$", raw)
        if m:
            hits.setdefault(m.group(1), []).append(int(m.group(2)[1:]))
    return {p: sorted(set(lines)) for p, lines in hits.items()}


def subgraph_src_blocks(
    save_path: str,
    hits: dict[str, list[int]],
    *,
    radius: int = 12,
    per_file_cap: int = _FILE_INLINE_CAP,
    budget_chars: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """命中行窗提取:{file: [行号]} → [{path,text,start_line,end_line}...], (blocks, degraded)。

    每文件:命中行 ±radius 展开、相邻窗(间距 ≤ 2*radius)合并、夹到文件行界;
    单文件累计字符封顶 per_file_cap(溢出窗截断,后续窗丢弃);
    全量累计超 budget_chars(缺省 CHAT_TOKEN_LIMIT_ESTIMATE*4)即整组降级
    (返回 ([], True));调用方(GraphService.context)据此 raise。
    OSError/解码失败的文件跳过(其余文件正常)。
    """
    budget = budget_chars if budget_chars is not None else envs.CHAT_TOKEN_LIMIT_ESTIMATE * 4
    blocks: list[dict[str, Any]] = []
    total = 0
    for path in sorted(hits):
        try:
            full_text = Path(save_path, path).read_text(encoding="utf-8")
        except OSError:
            continue
        lines = full_text.splitlines()
        n_lines = len(lines)
        # 命中行 → 合并后的窗区间
        windows: list[tuple[int, int]] = []
        for line in sorted(set(hits[path])):
            start = max(1, line - radius)
            end = min(n_lines, line + radius)
            if windows and start <= windows[-1][1] + 1:
                windows[-1] = (windows[-1][0], max(windows[-1][1], end))
            else:
                windows.append((start, end))
        file_chars = 0
        for start, end in windows:
            seg_text = "\n".join(lines[start - 1:end])
            if not seg_text.strip():
                continue
            remain = per_file_cap - file_chars
            if len(seg_text) > remain:
                if remain <= 0:
                    break
                seg_text = seg_text[:remain]
                end = start + seg_text.count("\n")  # noqa: PLW2901 - 行窗被截断后终点须回算,同名字段语义不变
            file_chars += len(seg_text)
            if total + len(seg_text) > budget:  # 先按单文件截断,再整体预算判断
                return [], True
            blocks.append({"path": path, "text": seg_text, "start_line": start, "end_line": end})
            total += len(seg_text)
    return blocks, False


class GraphService:
    """图服务(引擎经 generator_config['graph'] 注入后以 duck 接口使用)。

    - ready(repo):索引就绪判定(codemap 就绪门);
    - async context(repo, question):graphify.query + 子图 → 真实代码行窗。
      检索是正确作答的前提:图谱查询失败与超预算降级均直接 raise(不以
      "无检索增强"降级继续)。
    """

    def __init__(self, repo: Repo):
        self._repo = repo

    def ready(self, repo: Repo) -> bool:
        return index_ready(repo)

    async def context(self, repo: Repo, question: str) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(
                graphify.query, question, graph_path=str(graph_path(repo)),
            )
            answer = result.get("answer") or ""
        except Exception as exc:
            raise RuntimeError(f"代码图谱不可用: {type(exc).__name__}: {exc}") from exc
        hits = subgraph_hits(answer)
        if not hits:
            return {"hits": {}, "blocks": []}
        blocks, degraded = subgraph_src_blocks(repo.save_path, hits)
        if degraded:
            raise RuntimeError("检索上下文超出预算")
        return {"hits": hits, "blocks": blocks}


# ---------------------------------------------------------------------------
# 索引保障服务(clone + 建图;/repo/prepare 与 wiki 任务主流程共用。
# 未索引前置校验属端点守卫,在 app.py)
# ---------------------------------------------------------------------------


async def _run_extract(repo: Repo, *, extra_excludes: list[str] | None = None) -> dict:
    """graphify.extract 建图(code_only 纯本地 AST,无 key 可跑);失败返回错误态 dict,不抛。"""
    return await asyncio.to_thread(
        graphify.extract,
        path=repo.save_path,
        code_only=True,
        out_dir=graph_dir(repo),
        extra_excludes=extra_excludes,
    )


async def ensure_index(repo: Repo, *, extra_excludes: list[str] | None = None) -> None:
    """索引保障(克隆 + 建图):已 ready 直接返回。

    克隆判断独立于 ready 与否 —— graph.json 在但克隆目录被删时补克隆
    (防文件树静默退化为空);建图失败(extract 错误态)→ RuntimeError 上抛,
    由调用方决定上报/置任务 FAILED。
    """
    if not repo.downloaded and not repo.is_local:
        await asyncio.to_thread(repo.download)
    if not index_ready(repo):
        result = await _run_extract(repo, extra_excludes=extra_excludes)
        if result.get("error"):
            raise RuntimeError(result["error"])


# ---------------------------------------------------------------------------
# 工具指引文本(原 deepwiki 内嵌提示词 → 本层持;引擎只插入不假设任何工具)
# ---------------------------------------------------------------------------


def agent_note(generator: str) -> str:
    """注入到 agent 路 user 消息的指引段(按后端切换图工具名;runtime_config 注入 tool_note)。

    (原 gh_puller.deepwiki.utils.agent_note 上移 —— 引擎零工具假设,文本在此自持)
    """
    if generator in ("dsh", "codex"):
        return (
            "<note>You may use the mcp__graphify__query_graph tool to inspect this "
            "repository's code graph whenever you need code context or exact file/line "
            "references for citations. "
            "Its results mark sources as `Source: <file path> L<line number>`.</note>\n\n"
        )
    return (
        "<note>You may use the graphify_query tool to inspect this repository's code graph "
        "whenever you need code context or exact file/line references for citations. "
        "Its results mark sources as `Source: <file path> L<line number>`.</note>\n\n"
    )


def codemap_note() -> str:
    """codemap 指引(仅 agent 路用:先查图谱再构造,引用行号取自 Source 标记)。

    (原 gh_puller.deepwiki.codemap._codemap_note 上移;runtime_config 注入 codemap_note)
    """
    return (
        "<note>Before answering, use the graphify_query tool to inspect the repository "
        "code graph (its result carries `Source: <file path> L<line>` markers). "
        "When filling citation.file_path / start_line / end_line, use those paths and line "
        "numbers, and make the 'snippet' a verbatim substring of the code shown in the result.</note>\n\n"
    )


# ---------------------------------------------------------------------------
# 覆盖构造参数注入(生成器选型 + 图知识/工具指引 → generator_config)
# ---------------------------------------------------------------------------


def runtime_config(generator: str | None = None, generator_config: dict | None = None,
                   *, repo: Repo | None = None, get_env=None) -> dict:
    """选型 → 覆盖构造参数集(generator_config 基础上注入图知识)。

    图服务(generator_config["graph"])所有路注入,graphify MCP 工具桌按后端注入:
    cc 得进程内 server({"graphify": ...})+ 图工具名;dsh/codex 得子进程描述
    (+ codex env.GRAPHIFY_OUT 图产物根);agent 路再注入工具指引文本
    (tool_note/codemap_note,引擎外置文本);repo 为 None 时跳过注入(与原
    adapter "repo 非空才落 mcp" 同语义)。引擎侧 adapter 白名单透传,图服务经
    deepwiki.utils.graph_service 取用。
    """
    result = dict(generator_config or {})
    if repo is None:
        return result
    result["graph"] = GraphService(repo)
    gid, _ = resolve_generator(generator, generator_config, get_env)
    if gid == "dsh":
        result["mcp_servers"] = _graphify_mcp("dsh")
    elif gid == "codex":
        result["mcp_servers"] = _graphify_mcp("codex")
        env = dict(result.get("env") or {})
        env["GRAPHIFY_OUT"] = str(graph_dir(repo))
        result["env"] = env
    elif gid == "llm":
        pass  # llm 无 MCP 工具桌(直连 HTTP,检索经 graph 服务)
    else:  # cc
        result["mcp_servers"] = {"graphify": _graphify_server(repo)}
        result["allowed_tools"] = ["graphify_query", "mcp__graphify__graphify_query"]
    if gid != "llm":  # 工具指引仅 agent 路注入(引擎零工具假设,文本在上层)
        result["tool_note"] = agent_note(gid)
        result["codemap_note"] = codemap_note()
    return result
