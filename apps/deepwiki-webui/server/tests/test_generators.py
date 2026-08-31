"""webui 组装层 server/generators.py 的本地测试。

本体 = 原 gh_puller.deepwiki.utils 的 graphify 部分整体上移后改接 gh-puller-mcp:
索引就绪(db 文件)、MCP 工具桌描述(_gh_puller_mcp)、检索簇(GraphService.context /
rows_to_hits / subgraph_src_blocks)、索引保障(ensure_index)、覆盖构造参数注入
(runtime_config)。引擎侧(deepwiki.utils)已零 graphify/零 claude_agent_sdk ——
装配契约在此验证。

不调 Claude agent:generator 经 GENERATORS[gid] → _FakeGenerator 替换(零 SDK
构造副作用);MCP 调用面(_search_hits/_run_index)全部 fake(envs 由 conftest
钉临时根,CBM_CACHE_DIR 同为 tmp —— index_ready 的 fs 判定确定性)。
"""

from pathlib import Path

import pytest
from gh_puller import envs
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


def test_runtime_config_injects_graphify_by_backend(tmp_path, monkeypatch):
    """图知识按后端注入:所有路得 graph 服务;cc/dsh/codex/opencode 得 mcp_servers

    (cc McpStdioServerConfig + 图工具名 / dsh 子进程描述 / codex/opencode 子进程描述 +
    env 条件透传 CBM_*);llm 无 mcp 位。
    """
    monkeypatch.setenv("CBM_CACHE_DIR", "/tmp/cbm-cache")
    monkeypatch.setenv("CBM_RUNTIME_DIR", "/tmp/cbm-runtime")
    repo = Repo(str(tmp_path), "local")

    cc_gc = generators.runtime_config("cc", {"config_path": "/tmp/settings.json"}, repo=repo)
    assert cc_gc["config_path"] == "/tmp/settings.json"  # 用户键保留
    assert isinstance(cc_gc["graph"], generators.GraphService)
    assert "gh_puller" in cc_gc["mcp_servers"]
    cfg = cc_gc["mcp_servers"]["gh_puller"]  # TypedDict:运行时为 dict(SDK 按 stdio 子进程启动)
    assert cfg["command"] == "uv" and "gh_puller_mcp" in cfg["args"]
    assert cc_gc["allowed_tools"] == [
        *generators._SCOUT_TOOLS, *[f"mcp__gh_puller__{n}" for n in generators._SCOUT_TOOLS],
    ]
    assert cc_gc["tool_note"] == generators.agent_note("cc")
    assert cc_gc["codemap_note"] == generators.codemap_note()

    dsh_gc = generators.runtime_config("dsh", {}, repo=repo)
    assert dsh_gc["mcp_servers"] == generators._gh_puller_mcp("dsh")
    assert "env" not in dsh_gc
    assert dsh_gc["tool_note"] == generators.agent_note("dsh")

    codex_gc = generators.runtime_config("codex", {}, repo=repo)
    assert codex_gc["mcp_servers"] == generators._gh_puller_mcp("codex")
    assert codex_gc["env"] == {"CBM_CACHE_DIR": "/tmp/cbm-cache",
                               "CBM_RUNTIME_DIR": "/tmp/cbm-runtime"}  # 环境已设才透传(索引根一致)
    assert codex_gc["tool_note"] == generators.agent_note("codex")

    opencode_gc = generators.runtime_config("opencode", {}, repo=repo)
    assert opencode_gc["mcp_servers"] == generators._gh_puller_mcp("opencode")  # 子进程形态与 codex 同式
    assert opencode_gc["env"] == {"CBM_CACHE_DIR": "/tmp/cbm-cache",
                                  "CBM_RUNTIME_DIR": "/tmp/cbm-runtime"}
    assert opencode_gc["tool_note"] == generators.agent_note("opencode")
    assert opencode_gc["codemap_note"] == generators.codemap_note("opencode")
    assert isinstance(opencode_gc["graph"], generators.GraphService)

    monkeypatch.delenv("CBM_CACHE_DIR", raising=False)
    monkeypatch.delenv("CBM_RUNTIME_DIR", raising=False)
    codex_gc = generators.runtime_config("codex", {}, repo=repo)
    assert "env" not in codex_gc  # 未设 CBM_*:两侧同用缺省根,不注入 env 键

    dsh_gc = generators.runtime_config("dsh", {}, repo=repo)
    assert "env" not in dsh_gc  # dsh 无 env 注入位(子进程环境继承)

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
    assert cfg["mcp_servers"] == generators._gh_puller_mcp("dsh")
    assert "graph" not in cfg and "tool_note" not in cfg and "codemap_note" not in cfg  # 引擎私有键剥离,不落 SDK 配置


# ---------------------------------------------------------------------------
# 工具指引文本(原 deepwiki 内嵌提示词 → 本层持;引擎零工具假设)
# ---------------------------------------------------------------------------


def test_agent_note_tool_name_by_generator():
    """图工具指引按后端前缀:cc/dsh/codex = mcp__gh_puller__;opencode = servername_(非 mcp__)。"""
    for generator in ("cc", "dsh", "codex"):
        note = generators.agent_note(generator)
        assert "mcp__gh_puller__search_graph" in note
        assert "graphify" not in note
    opencode_note = generators.agent_note("opencode")
    assert "gh_puller_search_graph" in opencode_note
    assert "mcp__" not in opencode_note


def test_codemap_note_content():
    """codemap 指引(仅 agent 路用):先查图谱再构造,引用行号取自 search_graph 的 file/lines。"""
    note = generators.codemap_note("cc")
    assert "Before answering" in note and "mcp__gh_puller__search_graph" in note
    assert "'file'" in note and "'lines'" in note
    assert "<note>" in note
    assert "gh_puller_search_graph" in generators.codemap_note("opencode")


# ---------------------------------------------------------------------------
# gh-puller-mcp 工具桌隔离(agent 侧组合文件/隔离 home)
# ---------------------------------------------------------------------------


def test_dsh_cordis_isolation_and_graphify():
    """内置组合 = 完全隔离(逐项关断本地/用户级配置);工具桌由本层 _gh_puller_mcp 注入。

    与 cc 的 setting_sources=[] 同语义:workspaceContext(本地 AGENTS.md 链)/
    skills(用户/项目/捆绑技能)关断;默认组合(无 mcp_servers)不含任何工具服务器,
    mcp 段仅经 _gh_puller_mcp 显式注入(适配层零工具名)。
    """
    text = Path(dsh_cordis_path()).read_text(encoding="utf-8")
    assert "workspaceContext: false" in text
    assert "includeHarnessIdentity: false" in text
    assert "includeRuntimeContext: false" in text
    assert "toolBash: false" in text and "toolJobs: false" in text
    assert "goals: false" in text
    assert "mcp-gh-puller" not in text  # 默认组合无工具桌(经 runtime_config 注入)
    with_mcp = Path(dsh_cordis_path(generators._gh_puller_mcp("dsh"))).read_text(encoding="utf-8")
    assert "- id: mcp-gh-puller" in with_mcp
    assert "serverName: gh_puller" in with_mcp
    assert "gh_puller_mcp" in with_mcp and "--tool-profile" in with_mcp


def test_codex_home_isolation_and_graphify(tmp_path):
    """codex 隔离 home:config.toml 仅 gh-puller 单服务器 + env_vars 白名单,无用户配置面

    (与 cc setting_sources=[] / dsh 内置 cordis 同语义)。
    """
    home = codex_home_setup(str(tmp_path / "graph"), auth_src=False,
                            mcp_servers=generators._gh_puller_mcp("codex"))
    text = Path(home, "config.toml").read_text(encoding="utf-8")
    assert text.startswith("[mcp_servers.gh_puller]")
    assert "gh_puller_mcp" in text
    assert 'env_vars = ["CBM_CACHE_DIR", "CBM_RUNTIME_DIR"]' in text
    assert text.count("[mcp_servers.") == 1  # 无第三方服务器/无设置节(隔离边界)
    assert not (Path(home) / "auth.json").exists()  # auth_src=False:纯隔离无凭证态


# ---------------------------------------------------------------------------
# 子图 → 真实代码上下文(GraphService.context 检索簇)
# ---------------------------------------------------------------------------

_ROWS_DATA = {
    "total": 3,
    "cols": ["qn", "label", "file", "lines", "rank"],
    "rows": [
        ["p.a.f", "Function", "src/a.py", "5-10", -1.0],
        ["p.a.g", "Method", "src/a.py", "40", -2.0],
        ["p.b.h", "Function", "src/b.py", "12-33", -3.0],
    ],
    "has_more": False,
}


def test_rows_to_hits_parse():
    """rows → {file: [行号...]}:行范围整段展开、单点即自身、缺列/空行/file 缺失行跳过。"""
    assert generators.rows_to_hits(_ROWS_DATA) == {
        "src/a.py": [*range(5, 11), 40],
        "src/b.py": [*range(12, 34)],
    }
    assert generators.rows_to_hits({"cols": ["qn"], "rows": []}) == {}  # 缺 file/lines 列
    assert generators.rows_to_hits({"cols": _ROWS_DATA["cols"], "rows": []}) == {}
    assert generators.rows_to_hits({"cols": _ROWS_DATA["cols"], "rows": [["q", "F", None, "1-2", 0]]}) == {}


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
    """GraphService.context:_search_hits → hits/块;无命中 → 空块;检索失败 → "代码图谱不可用" raise。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    repo = Repo(str(tmp_path), "local")
    service = generators.GraphService(repo)

    async def fake_search(project, question):
        assert project == generators.project_name(repo)
        return {"src/a.py": [1]}

    monkeypatch.setattr(generators, "_search_hits", fake_search)
    ctx = await service.context(repo, "how does it work?")
    assert ctx["hits"] == {"src/a.py": [1]}
    assert ctx["blocks"][0]["path"] == "src/a.py"
    assert ctx["blocks"][0]["start_line"] == 1

    async def empty_search(project, question):
        return {}

    monkeypatch.setattr(generators, "_search_hits", empty_search)
    assert await service.context(repo, "q") == {"hits": {}, "blocks": []}

    async def boom(project, question):
        raise RuntimeError("graph is gone")

    monkeypatch.setattr(generators, "_search_hits", boom)
    with pytest.raises(RuntimeError, match="代码图谱不可用"):
        await service.context(repo, "q")


@pytest.mark.asyncio
async def test_graph_service_context_degraded(monkeypatch, tmp_path):
    """全局超预算 → degraded raise(检索上下文超出预算;与旧子图预算语义一致)。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("\n".join(f"line {i}" for i in range(1, 101)), encoding="utf-8")
    repo = Repo(str(tmp_path), "local")
    service = generators.GraphService(repo)

    async def fake_search(project, question):
        return {"src/a.py": [25]}

    monkeypatch.setattr(generators, "_search_hits", fake_search)
    monkeypatch.setattr(envs, "CHAT_TOKEN_LIMIT_ESTIMATE", 3)  # 预算压到小值:窗文本必超
    with pytest.raises(RuntimeError, match="检索上下文超出预算"):
        await service.context(repo, "q")


def test_graph_service_ready_by_db_file(tmp_path, monkeypatch):
    """就绪判定 = 索引 db 存在(<CBM_CACHE_DIR>/<project>.db;同步 fs 检查)。"""
    monkeypatch.setenv("CBM_CACHE_DIR", str(tmp_path / "cache"))
    repo = Repo(str(tmp_path), "local")
    service = generators.GraphService(repo)
    assert service.ready(repo) is False
    cdir = generators._cbm_cache_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / f"{generators.project_name(repo)}.db").touch()
    assert service.ready(repo) is True


# ---------------------------------------------------------------------------
# 索引保障(ensure_index:ready 早退 / 建图失败上抛)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_index_skips_when_ready(tmp_path, monkeypatch):
    """已 ready 直接返回(不调 _run_index)。"""
    monkeypatch.setenv("CBM_CACHE_DIR", str(tmp_path / "cache"))
    repo = Repo(str(tmp_path), "local")
    cdir = generators._cbm_cache_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / f"{generators.project_name(repo)}.db").touch()

    async def boom(repo):
        raise AssertionError("index_ready 已 True,不得调用 _run_index")

    monkeypatch.setattr(generators, "_run_index", boom)
    await generators.ensure_index(repo)  # 无异常 = 通过(_run_index 未被调用)


@pytest.mark.asyncio
async def test_ensure_index_calls_index_repository(tmp_path, monkeypatch):
    """未 ready → _run_index(index_repository:repo_path/mode='fast'/name=project_name)。"""
    repo = Repo(str(tmp_path), "local")
    calls = []

    async def fake_run_index(repo):
        calls.append(repo)

    monkeypatch.setattr(generators, "_run_index", fake_run_index)
    await generators.ensure_index(repo)
    assert calls == [repo]


@pytest.mark.asyncio
async def test_ensure_index_raises_on_index_error(tmp_path, monkeypatch):
    """建图失败(isError → RuntimeError)→ 上抛,由调用方决定上报/置任务 FAILED。"""
    repo = Repo(str(tmp_path), "local")

    async def fake_run_index(repo):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(generators, "_run_index", fake_run_index)
    with pytest.raises(RuntimeError, match="index exploded"):
        await generators.ensure_index(repo)
