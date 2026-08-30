"""webui 组装层 server/generators.py 的本地测试。

本体 = 原 gh_puller.deepwiki.utils 的 graphify 部分整体上移:图产物路径
(graph_dir/graph_path/index_ready)、graphify MCP 工具桌描述(_graphify_mcp /
_graphify_server)、图查询 + 子图解析检索簇(GraphService.context/subgraph_hits/
subgraph_src_blocks)、索引保障(ensure_index)、覆盖构造参数注入(runtime_config)。
引擎侧(deepwiki.utils)已零 graphify/零 claude_agent_sdk —— 装配契约在此验证。

不调 Claude agent:generator 经 GENERATORS[gid] → _FakeGenerator 替换(零 SDK
构造副作用);graphify.query/extract 全部 fake 或纯本地(envs 由 conftest 钉临时根)。
"""

from pathlib import Path

import pytest
from gh_puller.agent import GENERATORS
from gh_puller.agent.generators.codex import codex_home_setup
from gh_puller.agent.generators.dsh import dsh_cordis_path
from gh_puller.deepwiki.utils import adapter
from gh_puller.utils import Repo

import generators


class _FakeGenerator:
    """适配器假类:零 SDK 构造副作用(真 Dsh/Codex 构造会写隔离目录,禁用);

    构造期捕获 config 供装配断言(与 agent 契约同形:config 整体注入)。
    """

    generator = "cc"  # 测试按 gid monkeypatch 覆盖

    def __init__(self, config):
        self.config = dict(config)


# ---------------------------------------------------------------------------
# runtime_config:覆盖构造参数注入(选型 + 图知识 → generator_config)
# ---------------------------------------------------------------------------


def test_runtime_config_injects_graphify_by_backend(tmp_path):
    """图知识按后端注入:所有路得 graph 服务;cc/dsh/codex 得 mcp_servers

    (+ codex env.GRAPHIFY_OUT / cc allowed_tools 图工具名);llm 无 mcp 位。
    """
    repo = Repo(str(tmp_path), "local")

    cc_gc = generators.runtime_config("cc", {"config_path": "/tmp/settings.json"}, repo=repo)
    assert cc_gc["config_path"] == "/tmp/settings.json"  # 用户键保留
    assert isinstance(cc_gc["graph"], generators.GraphService)
    assert cc_gc["mcp_servers"] and "graphify" in cc_gc["mcp_servers"]
    assert cc_gc["allowed_tools"] == ["graphify_query", "mcp__graphify__graphify_query"]
    assert cc_gc["tool_note"] == generators.agent_note("cc")
    assert cc_gc["codemap_note"] == generators.codemap_note()

    dsh_gc = generators.runtime_config("dsh", {}, repo=repo)
    assert dsh_gc["mcp_servers"] == generators._graphify_mcp("dsh")
    assert "env" not in dsh_gc
    assert dsh_gc["tool_note"] == generators.agent_note("dsh")

    codex_gc = generators.runtime_config("codex", {}, repo=repo)
    assert codex_gc["mcp_servers"] == generators._graphify_mcp("codex")
    assert codex_gc["env"]["GRAPHIFY_OUT"] == str(generators.graph_dir(repo))
    assert codex_gc["tool_note"] == generators.agent_note("codex")

    llm_gc = generators.runtime_config("llm", {"model": "m1"}, repo=repo)
    assert llm_gc["model"] == "m1"
    assert "mcp_servers" not in llm_gc and "env" not in llm_gc
    assert "tool_note" not in llm_gc and "codemap_note" not in llm_gc  # llm 无 agent 指引
    assert isinstance(llm_gc["graph"], generators.GraphService)

    bare = generators.runtime_config("cc", {}, repo=None)
    assert "graph" not in bare and "mcp_servers" not in bare  # 无 repo:不注入图位(同"repo 非空才落 mcp")


def test_adapter_chain_gets_injected_graphify_config(tmp_path, monkeypatch):
    """链:runtime_config → 引擎 adapter —— 白名单透传后落地 SDK 组装 config。"""
    repo = Repo(str(tmp_path), "local")
    monkeypatch.setitem(GENERATORS, "dsh", _FakeGenerator)
    monkeypatch.setattr(_FakeGenerator, "generator", "dsh")
    gc = generators.runtime_config("dsh", {}, repo=repo)
    cfg = adapter("dsh", generator_config=gc, system_prompt="sys", repo=repo).config
    assert cfg["mcp_servers"] == generators._graphify_mcp("dsh")
    assert "graph" not in cfg and "tool_note" not in cfg and "codemap_note" not in cfg  # 引擎私有键剥离,不落 SDK 配置


# ---------------------------------------------------------------------------
# 工具指引文本(原 deepwiki 内嵌提示词 → 本层持;引擎零工具假设)
# ---------------------------------------------------------------------------


def test_agent_note_tool_name_by_generator():
    """图工具指引按后端切换(cc 的 graphify_query / dsh/codex 的 mcp__graphify__query_graph)。"""
    assert "graphify_query" in generators.agent_note("cc")
    assert "mcp__graphify__query_graph" not in generators.agent_note("cc")
    assert "mcp__graphify__query_graph" in generators.agent_note("dsh")
    assert "graphify_query" not in generators.agent_note("dsh")
    assert "mcp__graphify__query_graph" in generators.agent_note("codex")
    assert "graphify_query" not in generators.agent_note("codex")


def test_codemap_note_content():
    """codemap 指引(仅 cc/agent 路用):先查图谱再构造,引用行号取自 Source 标记。"""
    note = generators.codemap_note()
    assert "Before answering" in note and "graphify_query" in note
    assert "<note>" in note


# ---------------------------------------------------------------------------
# graphify MCP 工具桌隔离(agent 侧组合文件/隔离 home)
# ---------------------------------------------------------------------------


def test_dsh_cordis_isolation_and_graphify():
    """内置组合 = 完全隔离(逐项关断本地/用户级配置);图工具桌由本层 _graphify_mcp 注入。

    与 cc 的 setting_sources=[] 同语义:workspaceContext(本地 AGENTS.md 链)/
    skills(用户/项目/捆绑技能)关断;默认组合(无 mcp_servers)不含任何工具服务器,
    mcp 段仅经 _graphify_mcp 显式注入(适配层零工具名)。
    """
    text = Path(dsh_cordis_path()).read_text(encoding="utf-8")
    assert "workspaceContext: false" in text
    assert "includeHarnessIdentity: false" in text
    assert "includeRuntimeContext: false" in text
    assert "toolBash: false" in text and "toolJobs: false" in text
    assert "goals: false" in text
    assert "mcp-graphify" not in text  # 默认组合无图工具(经 runtime_config 注入)
    with_mcp = Path(dsh_cordis_path(generators._graphify_mcp("dsh"))).read_text(encoding="utf-8")
    assert "- id: mcp-graphify" in with_mcp
    assert "serverName: graphify" in with_mcp
    assert "graphify.serve" in with_mcp


def test_codex_home_isolation_and_graphify(tmp_path):
    """codex 隔离 home:config.toml 仅 graphify 单服务器 + env_vars 白名单,无用户配置面

    (与 cc setting_sources=[] / dsh 内置 cordis 同语义)。
    """
    home = codex_home_setup(str(tmp_path / "graph"), auth_src=False,
                            mcp_servers=generators._graphify_mcp("codex"))
    text = Path(home, "config.toml").read_text(encoding="utf-8")
    assert text.startswith("[mcp_servers.graphify]")
    assert "graphify.serve" in text and 'env_vars = ["GRAPHIFY_OUT"]' in text
    assert text.count("[mcp_servers.") == 1  # 无第三方服务器/无设置节(隔离边界)
    assert not (Path(home) / "auth.json").exists()  # auth_src=False:纯隔离无凭证态


# ---------------------------------------------------------------------------
# 子图 → 真实代码上下文(GraphService.context 检索簇)
# ---------------------------------------------------------------------------

_SUBGRAPH_ANSWER = """Graph: /tmp/g.json (10 nodes) | Traversal: BFS depth=2 | Start: ['x'] | 4 nodes found

NODE a [src=src/a.py loc=L5 community=c1]
NODE long label [src=src/a.py loc=L40 community=c2]
EDGE a --calls [EXTRACTED]--> b at=src/a.py:L12
[!] TRUNCATED: showing 3 of 10 nodes (~500-token budget)...
... (truncated - remaining nodes omitted)
[i] Complete answer over budget: 2200 tokens (budget 2000)
NODE c [src=src/b.py loc= community=c1]
NODE d [src=src/c.py community=c1]
"""


def test_subgraph_hits_parse():
    """解析 NODE(src/loc)/EDGE(at=) 标注:容忍截断前缀行,空 loc 或无 loc 跳过。"""
    hits = generators.subgraph_hits(_SUBGRAPH_ANSWER)
    assert hits == {"src/a.py": [5, 12, 40]}
    assert "src/b.py" not in hits and "src/c.py" not in hits
    assert generators.subgraph_hits("no matches here\n") == {}


@pytest.mark.asyncio
async def test_subgraph_src_blocks_windows_merge(tmp_path):
    """相邻命中行合并为一个窗(radius=8):L5/L9 → (1,17);L5/L45 → 两个窗。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "\n".join(f"line {i}" for i in range(1, 101)), encoding="utf-8",
    )
    blocks, degraded = generators.subgraph_src_blocks(
        str(tmp_path), {"src/a.py": [5, 9]}, radius=8,
    )
    assert not degraded and len(blocks) == 1
    assert blocks[0]["start_line"] == 1 and blocks[0]["end_line"] == 17
    assert blocks[0]["text"] == "\n".join(f"line {i}" for i in range(1, 18))
    blocks, degraded = generators.subgraph_src_blocks(
        str(tmp_path), {"src/a.py": [5, 45]}, radius=8,
    )
    assert not degraded and len(blocks) == 2
    assert [b["start_line"] for b in blocks] == [1, 37]
    assert [b["end_line"] for b in blocks] == [13, 53]


def test_subgraph_src_blocks_per_file_cap_and_budget(tmp_path):
    """单文件超 cap 截断;全局超 budget 整体降级([] , True);缺文件跳过。"""
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.py").write_text("\n".join(f"line {i}" for i in range(1, 51)), encoding="utf-8")
    blocks, degraded = generators.subgraph_src_blocks(
        str(tmp_path), {"src/a.py": [25]}, radius=8, per_file_cap=30,
    )
    assert not degraded and len(blocks) == 1
    assert len(blocks[0]["text"]) == 30
    assert blocks[0]["end_line"] == blocks[0]["start_line"] + blocks[0]["text"].count("\n")
    blocks, degraded = generators.subgraph_src_blocks(
        str(tmp_path), {"src/a.py": [25]}, radius=8, budget_chars=5,
    )
    assert (blocks, degraded) == ([], True)
    blocks, degraded = generators.subgraph_src_blocks(str(tmp_path), {"nope.py": [1]})
    assert (blocks, degraded) == ([], False)


@pytest.mark.asyncio
async def test_graph_service_context(monkeypatch, tmp_path):
    """GraphService.context:query → hits/块;无命中 → 空块;查询失败 → "代码图谱不可用" raise。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    repo = Repo(str(tmp_path), "local")
    service = generators.GraphService(repo)

    def fake_query(question, graph_path=None):
        assert graph_path == str(generators.graph_path(repo))
        return {"answer": "NODE f [src=src/a.py loc=L1 community=c0]\n"}

    monkeypatch.setattr(generators.graphify, "query", fake_query)
    ctx = await service.context(repo, "how does it work?")
    assert ctx["hits"] == {"src/a.py": [1]}
    assert ctx["blocks"][0]["path"] == "src/a.py"
    assert ctx["blocks"][0]["start_line"] == 1

    def empty_query(question, graph_path=None):
        return {"answer": ""}

    monkeypatch.setattr(generators.graphify, "query", empty_query)
    assert await service.context(repo, "q") == {"hits": {}, "blocks": []}

    def boom(question, graph_path=None):
        raise RuntimeError("graph is gone")

    monkeypatch.setattr(generators.graphify, "query", boom)
    with pytest.raises(RuntimeError, match="代码图谱不可用"):
        await service.context(repo, "q")


def test_graph_service_ready_by_graph_json(tmp_path):
    """就绪判定 = graph.json 存在(路径稳定,复用 graphify 存在性语义)。"""
    repo = Repo(str(tmp_path), "local")
    service = generators.GraphService(repo)
    assert service.ready(repo) is False
    gd = generators.graph_dir(repo)
    gd.mkdir(parents=True, exist_ok=True)
    (gd / "graph.json").write_text("{}", encoding="utf-8")
    assert service.ready(repo) is True


# ---------------------------------------------------------------------------
# 索引保障(ensure_index:ready 早退 / 建图失败上抛)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_index_skips_when_ready(tmp_path):
    """已 ready 直接返回(不调 extract);图产物根/路径按单仓库稳定布局。"""
    repo = Repo(str(tmp_path), "local")
    gd = generators.graph_dir(repo)
    gd.mkdir(parents=True, exist_ok=True)
    (gd / "graph.json").write_text("{}", encoding="utf-8")
    await generators.ensure_index(repo)  # 无异常 = 通过(extract 未被调用,无 key 也可跑)


@pytest.mark.asyncio
async def test_ensure_index_raises_on_extract_error(tmp_path, monkeypatch):
    """建图失败(extract 错误态)→ RuntimeError 上抛,由调用方决定上报/置任务 FAILED。"""
    repo = Repo(str(tmp_path), "local")

    async def fake_extract(*args, **kwargs):
        return {"error": "extract exploded"}

    monkeypatch.setattr(generators, "_run_extract", fake_extract)
    with pytest.raises(RuntimeError, match="extract exploded"):
        await generators.ensure_index(repo)
