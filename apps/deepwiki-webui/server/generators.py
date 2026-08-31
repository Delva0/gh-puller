"""生成器运行时装配(webui 专属):deepwiki 引擎与 gh-puller-mcp/agent SDK 的全部耦合点。

后端 = gh-puller-mcp(自有 MCP 服务器,codebase-memory-mcp 协议 1:1 复刻,后端 =
C 二进制透传):图产物 = codebase-memory 索引(<CBM_CACHE_DIR>/<project name>.db;
index_ready = db 存在);服务器进程内 MCP 客户端仅两用途(建图/检索,其余检索面
交给 agent 工具桌);agent 工具桌经通用 mcp_servers 描述注入 gh-puller-mcp。

注入方式:runtime_config 在 generator_config(覆盖构造参数集)上注入 graph 与
mcp_servers/工具名(per-路细节见实现);引擎侧 adapter 只做白名单透传,零图知识/
零工具名。

导入副作用(引擎导入零副作用之外,本模块被 app.py/tasks.py 及其测试导入才生效):
无(图服务经 generator_config 显式传入,不做模块绑定)。
"""

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from claude_agent_sdk.types import McpStdioServerConfig
from gh_puller import envs  # 模块对象绑定:属性调用时取(测试 patch/强刷活性)
from gh_puller.deepwiki.utils import resolve_generator
from gh_puller.utils import Repo
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# ---------------------------------------------------------------------------
# 索引就绪(图产物 = codebase-memory 索引 db;原 graph.json 产物退役)
# ---------------------------------------------------------------------------

_CBM_DEFAULT_CACHE = "~/.cache/codebase-memory-mcp"


def _cbm_cache_dir() -> Path:
    """codebase-memory 索引根(env CBM_CACHE_DIR 覆盖,缺省同 C 二进制;调用时读,测试可 monkeypatch)。"""
    return Path(os.environ.get("CBM_CACHE_DIR") or _CBM_DEFAULT_CACHE).expanduser()


def project_name(repo: Repo) -> str:
    """codebase-memory 的项目名(索引 db 文件名;ASCII 白名单,与 C 侧 name 归一化相容)。"""
    return re.sub(r"[^A-Za-z0-9._-]", "-", f"{repo.repo_type}_{repo.name}")


def index_ready(repo: Repo) -> bool:
    """索引完成信号 = 项目索引 db 已存在(<CBM_CACHE_DIR>/<project>.db)。"""
    return (_cbm_cache_dir() / f"{project_name(repo)}.db").exists()


# ---------------------------------------------------------------------------
# gh-puller-mcp 服务器(MCP 客户端 + 通用描述;本层自持 → runtime_config 注入;适配层零工具名)
# ---------------------------------------------------------------------------

_MCP_ENTRY = ["run", "python", "-m", "gh_puller_mcp"]
#: 与 gh_puller_mcp.manifest.SCOUT_TOOLS 逐名一致(agent 只读正查面;变更工具被服务器拦截)
_SCOUT_TOOLS = ("search_graph", "trace_path", "get_code_snippet", "get_architecture",
                "list_projects", "index_status", "check_index_coverage")
_SEARCH_LIMIT = 50  # search_graph 一次检索的 rows 上限
_SEARCH_TIMEOUT_SEC = 120.0  # 检索超时护栏;超时 → isError 信封 → RuntimeError
_INDEX_TIMEOUT_SEC: float | None = None  # index_repository 无护栏(与原 extract "建完为准"对等)

#: 索引串行化(后端 staged 写,同仓双写为最坏情形;索引罕见,串行无害)
_INDEX_LOCK = asyncio.Lock()


def _mcp_project_root() -> Path:
    """gh-puller-mcp 项目根:默认 apps/gh-puller-mcp(同仓布局);env DEEPWIKI_CBM_MCP_ROOT 覆盖。"""
    root = Path(os.environ.get("DEEPWIKI_CBM_MCP_ROOT")
                or Path(__file__).resolve().parents[2] / "gh-puller-mcp")
    if not (root / "gh_puller_mcp" / "__main__.py").exists():
        raise RuntimeError(f"gh-puller-mcp 未找到: {root}(可设 DEEPWIKI_CBM_MCP_ROOT 覆盖)")
    return root.resolve()


def _gh_puller_mcp(backend: str):
    """gh-puller-mcp 工具桌 → 适配层通用 mcp_servers 描述(零工具名,scout 档)。

    backend="dsh":组合 mcp-client 行(id mcp-gh-puller / serverName gh_puller);
    backend="codex"/"opencode":子进程配置段(id gh_puller + env_vars 白名单 CBM_*);
    backend="cc":McpStdioServerConfig(SDK stdio 子进程启动)。
    """
    args = ["--directory", str(_mcp_project_root()), *_MCP_ENTRY, "--tool-profile", "scout"]
    if backend == "dsh":
        return [{"id": "mcp-gh-puller", "serverName": "gh_puller", "command": "uv", "args": args}]
    if backend in ("codex", "opencode"):
        return [{"id": "gh_puller", "command": "uv", "args": args,
                 "env_vars": ["CBM_CACHE_DIR", "CBM_RUNTIME_DIR"]}]
    return McpStdioServerConfig(command="uv", args=args)


async def _call_tool(tool: str, arguments: dict, *, timeout: float | None = None) -> dict:  # noqa: ASYNC109 - 透传服务器级 --timeout 墙钟,非本函数超时
    """一次工具调用 = 一次 stdio 短连接(服务器收 EOF 退出);失败(含 isError)→ RuntimeError。

    成功 → 信封 content 解析后的 dict(服务器侧无 outputSchema:structuredContent 命中,
    否则回退 content[0].text 的 JSON)。--timeout 为服务器级 per-call 墙钟,作用于后端
    C 二进制。mcp SDK 的 stdio_client 只按白名单(get_default_environment)继承进程
    环境 —— CBM_*(索引根)与 GH_PULLER_MCP_BINARY(测试 shim)须显式透传。
    """
    args = ["--directory", str(_mcp_project_root()), *_MCP_ENTRY]
    if timeout is not None:
        args += ["--timeout", str(timeout)]
    # C 二进制不自建 cache/runtime 目录:显式设根时必须预建,否则安全协调端点
    # (endpoint)创建失败 → 后端无输出 → unparseable 信封
    for _key in ("CBM_CACHE_DIR", "CBM_RUNTIME_DIR"):
        _dir = os.environ.get(_key)
        if _dir:
            os.makedirs(_dir, exist_ok=True)
    _env: dict[str, str] = {
        k: v for k, v in os.environ.items()
        if k in ("CBM_CACHE_DIR", "CBM_RUNTIME_DIR", "GH_PULLER_MCP_BINARY")
    }
    params = StdioServerParameters(command="uv", args=args, env=_env or None)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()  # SDK 不会自动握手,必须显式 initialize
        result = await session.call_tool(tool, arguments)
    if result.is_error:
        text = result.content[0].text if result.content else ""
        raise RuntimeError(text or f"{tool} failed")
    if result.structured_content is not None:
        return dict(result.structured_content)
    text = result.content[0].text if result.content else ""
    return json.loads(text) if text else {}


# ---------------------------------------------------------------------------
# 图查询 + 子图解析检索簇(原 deepwiki.utils;llm 路专用,失败即 raise,不许"带病继续")
# ---------------------------------------------------------------------------


# 纯 LLM 路径单文件内联截断(字符)
_FILE_INLINE_CAP = 8000

_LINE_RANGE_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def rows_to_hits(data: dict) -> dict[str, list[int]]:
    """search_graph(format=json) 的 rows → {file: [行号...]}。

    按 cols 定位 file/lines 列(row 为 [qn, label, file, "40-70", rank] 同式);行范围
    整段展开为命中行集(单点 "40" 即自身),供子图窗提取;缺列/空行/file 缺失行跳过。
    """
    cols = data.get("cols") or []
    pos = {c: i for i, c in enumerate(cols)}
    if "file" not in pos or "lines" not in pos:
        return {}
    hits: dict[str, list[int]] = {}
    for row in data.get("rows") or []:
        if pos["file"] >= len(row) or pos["lines"] >= len(row):
            continue
        file = row[pos["file"]]
        match = _LINE_RANGE_RE.fullmatch(str(row[pos["lines"]]))
        if not file or not match:
            continue
        start = int(match.group(1))
        end = int(match.group(2) or match.group(1))
        hits.setdefault(file, []).extend(range(start, end + 1))
    return hits


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

    - ready(repo):索引就绪判定(索引 db 已存在;同步 fs 检查,廉价);
    - async context(repo, question):search_graph 检索 + 子图 → 真实代码行窗。
      检索是正确作答的前提:图谱查询失败与超预算降级均直接 raise(不以
      "无检索增强"降级继续)。
    """

    def __init__(self, repo: Repo):
        self._repo = repo

    def ready(self, repo: Repo) -> bool:
        return index_ready(repo)

    async def context(self, repo: Repo, question: str) -> dict[str, Any]:
        try:
            hits = await _search_hits(project_name(repo), question)
        except Exception as exc:
            raise RuntimeError(f"代码图谱不可用: {type(exc).__name__}: {exc}") from exc
        if not hits:
            return {"hits": {}, "blocks": []}
        blocks, degraded = subgraph_src_blocks(repo.save_path, hits)
        if degraded:
            raise RuntimeError("检索上下文超出预算")
        return {"hits": hits, "blocks": blocks}


async def _search_hits(project: str, question: str) -> dict[str, list[int]]:
    """search_graph(BM25, format=json) → 命中行集 {file: [行号...]};无命中返回空 dict。"""
    data = await _call_tool(
        "search_graph",
        {"project": project, "query": question, "format": "json", "limit": _SEARCH_LIMIT},
        timeout=_SEARCH_TIMEOUT_SEC,
    )
    return rows_to_hits(data)


# ---------------------------------------------------------------------------
# 索引保障服务(clone + 建图;/repo/prepare 与 wiki 任务主流程共用。
# 未索引前置校验属端点守卫,在 app.py)
# ---------------------------------------------------------------------------


async def _run_index(repo: Repo) -> None:
    """index_repository 建图(mode=fast:纯结构分析,无 key 可跑);isError → RuntimeError。"""
    await _call_tool(
        "index_repository",
        {"repo_path": repo.save_path, "mode": "fast", "name": project_name(repo)},
        timeout=_INDEX_TIMEOUT_SEC,
    )


async def ensure_index(repo: Repo) -> None:
    """索引保障(克隆 + 建图):已 ready 直接返回。

    克隆判断独立于 ready 与否 —— 索引 db 在但克隆目录被删时补克隆
    (防文件树静默退化为空);建图失败(isError)→ RuntimeError 上抛,
    由调用方决定上报/置任务 FAILED。索引经模块级锁串行化(后端 staged 写
    同仓双写为最坏情形)。
    """
    async with _INDEX_LOCK:
        if not repo.downloaded and not repo.is_local:
            await asyncio.to_thread(repo.download)
        if not index_ready(repo):
            await _run_index(repo)


# ---------------------------------------------------------------------------
# 工具指引文本(原 deepwiki 内嵌提示词 → 本层持;引擎只插入不假设任何工具)
# ---------------------------------------------------------------------------


def agent_note(generator: str) -> str:
    """注入到 agent 路 user 消息的指引段(图工具名;runtime_config 注入 tool_note)。

    工具标签前缀随后端:opencode = servername_(如 gh_puller_search_graph);
    cc/dsh/codex = mcp__。引擎零工具假设,文本在此自持。
    """
    tool = _graph_tool_name(generator)
    return (
        f"<note>You may use the {tool} tool to inspect this repository's "
        "code graph (symbols with file paths and line ranges) whenever you need code context or "
        "exact file/line references for citations.</note>\n\n"
    )


def _graph_tool_name(generator: str) -> str:
    """search_graph 在该后端的工具标签前缀:opencode = servername_(非 mcp__)。"""
    return "gh_puller_search_graph" if generator == "opencode" else "mcp__gh_puller__search_graph"


def codemap_note(generator: str = "cc") -> str:
    """codemap 指引(仅 agent 路用:先查图谱再构造,引用行号取自 search_graph 的 file/lines)。

    (原 gh_puller.deepwiki.codemap._codemap_note 上移;runtime_config 注入 codemap_note)
    """
    tool = _graph_tool_name(generator)
    return (
        f"<note>Before answering, use the {tool} tool (format='json') "
        "to inspect the repository code graph; its rows carry the relative 'file' path and a "
        "'lines' range (e.g. 12-33) per matched symbol. When filling "
        "citation.file_path / start_line / end_line, use those paths and line numbers, and make "
        "the 'snippet' a verbatim substring of the code at those lines.</note>\n\n"
    )


# ---------------------------------------------------------------------------
# 覆盖构造参数注入(生成器选型 + 图知识/工具指引 → generator_config)
# ---------------------------------------------------------------------------


def runtime_config(generator: str | None = None, generator_config: dict | None = None,
                   *, repo: Repo | None = None, get_env=None) -> dict:
    """选型 → 覆盖构造参数集(generator_config 基础上注入图知识)。

    图服务(generator_config["graph"])所有路注入,gh-puller-mcp 工具桌按后端注入:
    cc 得 McpStdioServerConfig(scout 档)+ 图工具名;dsh/codex/opencode 得子进程描述
    (+ codex/opencode env 条件透传 CBM_* 保证索引根一致);agent 路再注入工具指引文本
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
        result["mcp_servers"] = _gh_puller_mcp("dsh")
    elif gid in ("codex", "opencode"):
        result["mcp_servers"] = _gh_puller_mcp(gid)
        env = {k: os.environ[k] for k in ("CBM_CACHE_DIR", "CBM_RUNTIME_DIR") if k in os.environ}
        if env:
            result["env"] = env
    elif gid == "llm":
        pass  # llm 无 MCP 工具桌(直连 HTTP,检索经 graph 服务)
    else:  # cc
        result["mcp_servers"] = {"gh_puller": _gh_puller_mcp("cc")}
        result["allowed_tools"] = [*_SCOUT_TOOLS, *[f"mcp__gh_puller__{n}" for n in _SCOUT_TOOLS]]
    if gid != "llm":  # 工具指引仅 agent 路注入(引擎零工具假设,文本在上层)
        result["tool_note"] = agent_note(gid)
        result["codemap_note"] = codemap_note(gid)
    return result
