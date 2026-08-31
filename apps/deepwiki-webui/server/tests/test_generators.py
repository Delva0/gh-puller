"""webui 组装层 server/generators.py 的本地测试。

本体 = 原 gh_puller.deepwiki.utils 的 graphify 部分整体上移后改接 gh-puller-mcp:
索引就绪(db 文件)、MCP 工具桌描述(_gh_puller_mcp)、索引保障(ensure_index)、
覆盖构造参数注入(runtime_config)。引擎侧(gh_puller.deepwiki)已零 graphify/零
生成器 SDK —— 装配契约在此验证。

不调模型:generator 经 GENERATORS[gid] → _FakeGenerator 替换(零 SDK 构造副作用);
MCP 建图调用面(_run_index)全部 fake(CBM_CACHE_DIR 由 conftest 钉临时根 ——
index_ready 的 fs 判定确定性)。
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


def test_runtime_config_injects_tooltable_by_backend(tmp_path, monkeypatch):
    """工具桌按后端注入:cc/dsh/codex/opencode 得 mcp_servers

    (cc McpStdioServerConfig + 图工具名 / dsh 子进程描述 / codex/opencode 子进程描述 +
    env 条件透传 CBM_*)+ 工具指引文本;llm 无工具桌位(直连 HTTP)。
    """
    monkeypatch.setenv("CBM_CACHE_DIR", "/tmp/cbm-cache")
    monkeypatch.setenv("CBM_RUNTIME_DIR", "/tmp/cbm-runtime")
    repo = Repo(str(tmp_path), "local")

    cc_gc = generators.runtime_config("cc", {"config_path": "/tmp/settings.json"}, repo=repo)
    assert cc_gc["config_path"] == "/tmp/settings.json"  # 用户键保留
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

    monkeypatch.delenv("CBM_CACHE_DIR", raising=False)
    monkeypatch.delenv("CBM_RUNTIME_DIR", raising=False)
    codex_gc = generators.runtime_config("codex", {}, repo=repo)
    assert "env" not in codex_gc  # 未设 CBM_*:两侧同用缺省根,不注入 env 键

    dsh_gc = generators.runtime_config("dsh", {}, repo=repo)
    assert "env" not in dsh_gc  # dsh 无 env 注入位(子进程环境继承)

    llm_gc = generators.runtime_config("llm", {"model": "m1"}, repo=repo)
    assert llm_gc["model"] == "m1"
    assert "mcp_servers" not in llm_gc and "env" not in llm_gc
    assert "tool_note" not in llm_gc and "codemap_note" not in llm_gc  # llm 无工具指引

    bare = generators.runtime_config("cc", {}, repo=None)
    assert "mcp_servers" not in bare  # 无 repo:不注入工具桌位(同"repo 非空才落 mcp")


def test_adapter_chain_gets_injected_graphify_config(tmp_path, monkeypatch):
    """链:runtime_config → 引擎 adapter —— 白名单透传后落地 SDK 组装 config。"""
    repo = Repo(str(tmp_path), "local")
    monkeypatch.setitem(GENERATORS, "dsh", _FakeGenerator)
    monkeypatch.setattr(_FakeGenerator, "generator", "dsh")
    gc = generators.runtime_config("dsh", {}, repo=repo)
    cfg = adapter("dsh", generator_config=gc, system_prompt="sys", repo=repo).config
    assert cfg["mcp_servers"] == generators._gh_puller_mcp("dsh")
    assert "tool_note" not in cfg and "codemap_note" not in cfg  # 引擎私有键剥离,不落 SDK 配置


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
