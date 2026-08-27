"""wiki 任务 runtime 包装(server/tasks.py)的本地测试。

不调 Claude agent(不依赖 API key / CLI):
- 环境变量要求:ANTHROPIC_API_KEY 不设;DEEPWIKI_ROOT 指向临时目录(见 tests/conftest.py)。
- 覆盖:调度机(join 去重/缓存胜/续跑恢复/按 target 隔离)、主流程(索引→结构→页面→
  成品缓存,离线:生成函数全部 monkeypatch 或交付文件预置)、页级并发与错误占位。
- 引擎契约/纯函数/生成协议测试仍在根 tests/test_deepwiki.py。

patch 约定:tasks 自身模块全局(tasks.generate_repo_wiki / tasks._WIKI_TASK_TTL_SECONDS /
tasks._generate_page_with_retry / tasks._WIKI_PAGE_CONCURRENCY)与 tasks.registry.* 直指
server;引擎 patch 位点为其属主子模块(AgentWikiPipeline._deliver 类方法 /
deepwiki.utils.llm_stream(llm 路直呼)、deepwiki.graphify.query、管线类方法、
deepwiki.envs.DEEPWIKI_GENERATOR)—— 引擎模块内调用时经模块对象属性动态解析。
"""

import asyncio
import dataclasses
import json
import os
from pathlib import Path

import pytest
from gh_puller import deepwiki
from gh_puller.deepwiki import (
    WikiPage,
    WikiStructureModel,
    delete_resume_state,
    delete_wiki_cache,
    save_wiki_cache,
    write_resume_state,
)
from gh_puller.deepwiki import utils as deepwiki_utils
from gh_puller.deepwiki.cache import (
    _generator_digest,
    _graph_dir,
    _resume_state_path,
    _wiki_cache_dir,
)
from gh_puller.utils import Repo, TaskStatus

import tasks

# ---------------------------------------------------------------------------
# 测试辅助(与根 tests 同名拷贝;根保留一份供缓存 IO 原语测试用)
# ---------------------------------------------------------------------------


def _make_page(page_id: str) -> WikiPage:
    return WikiPage(
        id=page_id,
        title=f"Page {page_id}",
        content="",
        filePaths=["src/main.py"],
        importance="medium",
        relatedPages=[],
    )


def _make_structure(page_ids: list[str]) -> WikiStructureModel:
    return WikiStructureModel(
        id="wiki", title="T", description="", pages=[_make_page(p) for p in page_ids]
    )


def _make_request(owner: str, repo: str) -> dict:
    return {
        "repo_url": "/tmp/gh-puller-test-repo", "type": "local", "owner": owner,
        "repo": repo, "language": "en", "target": {}, "token": None,
        "comprehensive": True,
        "excluded_dirs": [], "excluded_files": [], "included_dirs": [], "included_files": [],
    }


def _digest_of(choice: "dict | None") -> str:
    """选型 dict → 稳定摘要(测试与实现共用一个函数)。"""
    return _generator_digest((choice or {}).get("generator"), (choice or {}).get("generator_config"))


def _proj(request) -> str:
    """散装参数测试辅助:请求的 repo 键(type_owner_repo)。"""
    return deepwiki.repo_key_of(request["type"], request["owner"], request["repo"])


def _prepared() -> tasks.PreparedRepo:
    return tasks.PreparedRepo(Repo("/tmp/gh-puller-test-repo", "local"), "main", [], "")


_STRUCT_XML = """<wiki_structure>
<title>T</title>
<page id="p1"><title>A</title><file_path>src/a.py</file_path></page>
<page id="p2"><title>B</title><file_path>src/b.py</file_path></page>
<page id="p3"><title>C</title><file_path>src/c.py</file_path></page>
</wiki_structure>"""


# ---------------------------------------------------------------------------
# 主流程编排(离线)
# ---------------------------------------------------------------------------


def test_pending_pages_subtracts_done():
    """纯函数:按结构顺序去掉已完成页;done 含不存在 id 时原样返回。"""
    structure = _make_structure(["p1", "p2", "p3"])
    assert [p.id for p in tasks._pending_pages(structure, {"p1": _make_page("p1")})] == ["p2", "p3"]
    assert tasks._pending_pages(structure, {p.id: _make_page(p.id) for p in structure.pages}) == []
    assert [p.id for p in tasks._pending_pages(structure, {"pX": _make_page("pX")})] == ["p1", "p2", "p3"]


@pytest.mark.asyncio
async def test_generate_pages_concurrency_bounded(monkeypatch):
    """页级并发:Semaphore 调用时读模块全局,并发=4 时同时至多 4 个在途页。"""
    active = 0
    max_active = 0

    async def fake_generate(task, page, pipeline, prepared):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return dataclasses.replace(page, content=f"content-{page.id}")

    monkeypatch.setattr(tasks, "_generate_page_with_retry", fake_generate)
    monkeypatch.setattr(tasks, "_WIKI_PAGE_CONCURRENCY", 4)
    task = tasks.WikiTask(request=_make_request("conc-io", "demo"))
    task.wiki_structure = _make_structure([f"p{i}" for i in range(8)])
    pages = await tasks._generate_pages(
        task, tasks._wiki_pipeline(task.request["target"].get("generator"), task.request["target"].get("generator_config")), _prepared()
    )
    assert max_active == 4
    assert len(pages) == 8 and len(task.generated_pages) == 8
    assert task.pages_done == 8
    assert task.current_page_ids == []
    await delete_resume_state("conc-io", "demo", "local", "en")


# ---------------------------------------------------------------------------
# 调度机(join/缓存胜/续跑/按 target 隔离)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_resume_restores_task(monkeypatch):
    """同仓库再次提交:命中落盘状态 → resumed=True,复用结构/进度恢复;state 快照胜出;
    提交明文凭证入请求、落盘状态剥离(仅公开 target),续跑时合并当前提交凭证。"""
    structure = _make_structure(["p1", "p2", "p3"])
    state = {
        "version": 1,
        "request": {**_make_request("resume-io", "demo"), "target": {
            "generator": "llm", "generator_config": {"provider": "openai", "model": "m1"}}},
        "status": TaskStatus.GENERATING,
        "wiki_structure": dataclasses.asdict(structure),
        "generated_pages": {"p1": dataclasses.asdict(_make_page("p1")), "p2": dataclasses.asdict(_make_page("p2"))},
        "default_branch": "main",
        "submitted_at": 9876543210,
        "error": None,
    }
    digest = _generator_digest(state["request"]["target"].get("generator"), state["request"]["target"].get("generator_config"))
    assert await write_resume_state("resume-io", "demo", "local", "en", state, digest=digest) is True

    async def fake_generate(task):
        task.status = TaskStatus.COMPLETED

    monkeypatch.setattr(tasks, "generate_repo_wiki", fake_generate)
    monkeypatch.setattr(tasks, "_WIKI_TASK_TTL_SECONDS", 0.2)

    key = f"local_resume-io_demo@{digest}"
    fresh = _make_request("resume-io", "demo")
    fresh["comprehensive"] = False  # 与首次不一致:落盘快照应胜出
    fresh["target"] = {"generator": "llm", "generator_config": {
        "provider": "openai", "model": "m1",  # 公开部分与落盘快照同轨(digest 匹配)
        "api_key": "sk-live-1", "base_url": "https://custom/v1"}}
    try:
        res = await tasks.registry.submit(tasks.WikiTask.from_wiki_request(fresh))
        assert res.created is True
        assert res.resumed is True
        assert res.status == TaskStatus.GENERATING
        task = tasks.registry.get(key)
        assert task is not None
        assert [p.id for p in task.wiki_structure.pages] == ["p1", "p2", "p3"]
        assert task.pages_done == 2
        assert task.submitted_at == 9876543210
        assert task.request["comprehensive"] is True
        # 续跑合并凭证:公开部分取自落盘快照,凭证来自当前提交(object 类)
        assert task.request["target"]["generator_config"]["api_key"] == "sk-live-1"
        assert task.request["target"]["generator_config"]["base_url"] == "https://custom/v1"
        assert task.request["target"]["generator_config"]["provider"] == "openai"
        await task.task
        assert task.status == TaskStatus.COMPLETED
    finally:
        await tasks.registry.remove(key)
        await delete_resume_state("resume-io", "demo", "local", "en", digest=digest)
        await asyncio.sleep(0.25)  # 让 TTL 移除计时器自然结束,避免挂起的任务告警


@pytest.mark.asyncio
async def test_resume_isolated_by_target(monkeypatch):
    """续跑按公开 target 摘要隔离:同 repo 切 generator(如 cc → llm)即换状态文件,
    旧快照不复活;新目标全新一代(不进同一队列)。"""
    state = {
        "version": 1,
        "request": _make_request("gen-switch", "demo"),
        "status": TaskStatus.GENERATING,
        "wiki_structure": dataclasses.asdict(_make_structure(["p1", "p2"])),
        "generated_pages": {"p1": dataclasses.asdict(_make_page("p1"))},
        "default_branch": "main",
        "submitted_at": 424242,
        "error": None,
    }
    digest_cc = _generator_digest(None, None)  # 空 target → env 缺省 cc
    assert await write_resume_state("gen-switch", "demo", "local", "en", state, digest=digest_cc) is True

    async def fake_generate(task):
        task.status = TaskStatus.COMPLETED

    monkeypatch.setattr(tasks, "generate_repo_wiki", fake_generate)
    monkeypatch.setattr(tasks, "_WIKI_TASK_TTL_SECONDS", 0.2)
    key_cc = f"local_gen-switch_demo@{digest_cc}"
    try:
        # 同 target(env 缺省 cc)提交:命中旧快照 → 续跑
        res_ok = await tasks.registry.submit(
            tasks.WikiTask.from_wiki_request(_make_request("gen-switch", "demo"))
        )
        assert res_ok.resumed is True
        task_ok = tasks.registry.get(key_cc)
        await task_ok.task
        assert task_ok.status == TaskStatus.COMPLETED
        await tasks.registry.remove(key_cc)

        # 显式 llm target:新摘要 → 全新一代;cc 状态文件仍在(按目标隔离共存)
        res = await tasks.registry.submit(
            tasks.WikiTask.from_wiki_request(
                {**_make_request("gen-switch", "demo"), "target": {"generator": "llm"}}
            )
        )
        assert res.created is True
        assert res.resumed is False
        assert os.path.exists(_resume_state_path("gen-switch", "demo", "local", "en", digest=digest_cc))
        key_llm = "local_gen-switch_demo@" + _generator_digest(
            "llm", None)
        # 任务键 = repo 键 + target 摘要(与 cc 槽位隔离)
        task = tasks.registry.get(key_llm)
        assert task.key != key_cc
        await task.task
        assert task.status == TaskStatus.COMPLETED
    finally:
        await tasks.registry.remove(key_cc)
        await tasks.registry.remove("local_gen-switch_demo@" + _generator_digest(
            "llm", None))
        await delete_resume_state("gen-switch", "demo", "local", "en", digest=digest_cc)
        await asyncio.sleep(0.25)  # 让 TTL 移除计时器自然结束,避免挂起的任务告警


@pytest.mark.asyncio
async def test_cache_hit_respects_target(monkeypatch, tmp_path):
    """成品缓存按公开 target 身份校验:同轨(generator + file:config_path / object:provider|model)
    才命中;换 generator(同 repo)即另一份缓存,不否定旧成品、全新重新生成。"""
    cfg = tmp_path / "settings.json"
    cfg.write_text("{}", encoding="utf-8")
    cc_target = {"generator": "cc", "generator_config": {"config_path": str(cfg)}}
    cache = {
        "wiki_structure": dataclasses.asdict(_make_structure(["p1"])),
        "generated_pages": {"p1": dataclasses.asdict(_make_page("p1"))},
        "repo_url": "/tmp/gh-puller-test-repo",
        "repo": {
            "owner": "gen-cache", "repo": "demo", "type": "local",
            "token": None, "localPath": None, "repoUrl": "/tmp/gh-puller-test-repo",
        },
        "generator": "cc",
        "config_path": str(cfg),
        "provider": None,
        "model": None,
    }
    digest_cc = _generator_digest((cc_target or {}).get("generator"), (cc_target or {}).get("generator_config"))
    assert await save_wiki_cache("gen-cache", "demo", "local", "en", cache, digest=digest_cc) is True

    calls = []

    async def fake_run(task):
        calls.append(task.key)
        task.status = TaskStatus.COMPLETED

    monkeypatch.setattr(tasks, "generate_repo_wiki", fake_run)
    monkeypatch.setattr(tasks, "_WIKI_TASK_TTL_SECONDS", 0.2)
    key_cc = f"local_gen-cache_demo@{digest_cc}"
    try:
        # 同轨(同 config_path):缓存命中,不运行
        res_ok = await tasks.registry.submit(
            tasks.WikiTask.from_wiki_request(
                {**_make_request("gen-cache", "demo"), "target": cc_target}
            )
        )
        assert res_ok.from_cache is True
        assert res_ok.status == TaskStatus.COMPLETED
        assert calls == []

        # 换 target(llm):新摘要 → 不命中,重新生成;旧 cc 成品不被复用也不被删除
        res = await tasks.registry.submit(
            tasks.WikiTask.from_wiki_request(
                {**_make_request("gen-cache", "demo"),
                 "target": {"generator": "llm"}}
            )
        )
        assert res.from_cache is False
        assert res.created is True
        # llm 任务走自己摘要的注册表槽位(cc 键无新任务)
        assert tasks.registry.get(key_cc) is None
        task_llm = tasks.registry.get("local_gen-cache_demo@" + _generator_digest(
            "llm", None))
        assert task_llm is not None
        await task_llm.task
        assert task_llm.status == TaskStatus.COMPLETED
        assert calls == [task_llm.key]
    finally:
        await tasks.registry.remove(key_cc)
        await tasks.registry.remove("local_gen-cache_demo@" + _generator_digest(
            "llm", None))
        await delete_wiki_cache("gen-cache", "demo", "local", "en", digest=digest_cc)
        await asyncio.sleep(0.25)  # 让 TTL 移除计时器自然结束,避免挂起的任务告警


# ---------------------------------------------------------------------------
# cc/llm 双路径(离线:交付文件预置 / agent 与 llm_stream 全部 monkeypatch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_structure_by_generator(monkeypatch):
    """DEEPWIKI_GENERATOR 分派:同一 _determine_structure 按 env 经 _wiki_pipeline 选路。"""
    calls = []

    async def fake_llm(**kwargs):
        calls.append("llm")
        return _make_structure(["p1"])

    async def fake_cc(**kwargs):
        calls.append("cc")
        return _make_structure(["p1"])

    monkeypatch.setattr(deepwiki.LlmWikiPipeline, "determine_structure", staticmethod(fake_llm))
    monkeypatch.setattr(deepwiki.AgentWikiPipeline, "determine_structure", staticmethod(fake_cc))
    request = _make_request("dispatch-io", "demo")
    task = tasks.WikiTask(request=request)

    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "cc")
    await tasks._determine_structure(task, tasks._wiki_pipeline(request["target"].get("generator"), request["target"].get("generator_config")), _prepared())
    assert calls == ["cc"]
    assert task.default_branch == "main"  # 结构确定时记录分支

    calls.clear()
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "llm")
    await tasks._determine_structure(task, tasks._wiki_pipeline(request["target"].get("generator"), request["target"].get("generator_config")), _prepared())
    assert calls == ["llm"]


@pytest.mark.asyncio
async def test_determine_structure_cc_skips_when_file_exists(monkeypatch):
    """cc 结构续跑:structure.md 已存在则直接解析,不启动 agent。"""
    request = _make_request("cc-struct", "demo")
    struct_path = deepwiki.AgentWikiPipeline()._agent_cache_structure_path(_proj(request), request["target"].get("generator"), request["target"].get("generator_config"))
    struct_path.parent.mkdir(parents=True, exist_ok=True)
    struct_path.write_text(_STRUCT_XML, encoding="utf-8")

    async def boom(*args, **kwargs):
        raise AssertionError("agent must not be called")

    monkeypatch.setattr(deepwiki.AgentWikiPipeline, "_deliver", boom)
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "cc")
    repo = Repo("/tmp/gh-puller-test-repo", "local")
    s = await deepwiki.AgentWikiPipeline().determine_structure(
        generator=request["target"].get("generator"), generator_config=request["target"].get("generator_config"), repo=repo, owner=request["owner"], repo_name=request["repo"],
        file_tree=["src/a.py"], readme="", comprehensive=request["comprehensive"],
        language=request["language"], run_id=_proj(request),
    )
    assert [p.id for p in s.pages] == ["p1", "p2", "p3"]


@pytest.mark.asyncio
async def test_determine_structure_cc_calls_agent_no_inline(tmp_path, monkeypatch):
    """cc 结构走 agent 并读回文件;提示词只有文件树路径,不内联任何文件内容。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("SECRET_CODE_BODY", encoding="utf-8")
    (tmp_path / "README.md").write_text("SECRET_README_BODY", encoding="utf-8")
    request = {
        "repo_url": str(tmp_path), "type": "local", "owner": "local",
        "repo": "demo", "language": "en", "target": {}, "token": None,
        "comprehensive": True,
    }
    captured = {}

    async def fake_deliver(self, adapter, prompt, out_path, label=None, run_id=None):
        captured["prompt"] = prompt
        captured["run_id"] = run_id
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(_STRUCT_XML, encoding="utf-8")
        return _STRUCT_XML

    monkeypatch.setattr(deepwiki.AgentWikiPipeline, "_deliver", fake_deliver)
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "cc")
    repo = Repo(str(tmp_path), "local")
    s = await deepwiki.AgentWikiPipeline().determine_structure(
        generator=request["target"].get("generator"), generator_config=request["target"].get("generator_config"), repo=repo, owner=request["owner"], repo_name=request["repo"],
        file_tree=["src/a.py"], readme="", comprehensive=request["comprehensive"],
        language=request["language"], run_id=_proj(request),
    )
    assert [p.id for p in s.pages] == ["p1", "p2", "p3"]
    assert "<file_tree>" in captured["prompt"]
    assert "src/a.py" in captured["prompt"]
    assert "SECRET_CODE_BODY" not in captured["prompt"]
    assert "SECRET_README_BODY" not in captured["prompt"]
    assert captured["run_id"] == _proj(request)  # 任务级会话组关联


@pytest.mark.asyncio
async def test_page_cc_skips_when_file_exists(monkeypatch):
    """cc 页续跑:page_<id>.md 已存在则直接读回(文件为权威),不启动 agent。"""
    request = _make_request("cc-page", "demo")
    out = deepwiki.AgentWikiPipeline()._agent_cache_page_path(_proj(request), "p1", request["target"].get("generator"), request["target"].get("generator_config"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("## PA-REAL\n\nbody\n", encoding="utf-8")

    async def boom(*args, **kwargs):
        raise AssertionError("agent must not be called")

    monkeypatch.setattr(deepwiki.AgentWikiPipeline, "_deliver", boom)
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "cc")
    task = tasks.WikiTask(request=request)
    got = await tasks._generate_page(task, _make_page("p1"), tasks._wiki_pipeline(request["target"].get("generator"), request["target"].get("generator_config")), _prepared())
    assert "PA-REAL" in got.content
    assert got.id == "p1"


@pytest.mark.asyncio
async def test_page_cc_calls_agent_and_reads_file(monkeypatch):
    """cc 页走 agent 落盘后读回;提示词只给路径并含交付指令,不内联文件内容。"""
    request = _make_request("cc-page2", "demo")
    captured = {}

    async def fake_deliver(self, adapter, prompt, out_path, label=None, run_id=None):
        captured["prompt"] = prompt
        captured["run_id"] = run_id
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text("## PB-REAL\n\ncontent\n", encoding="utf-8")
        return "## PB-REAL\n\ncontent\n"

    monkeypatch.setattr(deepwiki.AgentWikiPipeline, "_deliver", fake_deliver)
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "cc")
    task = tasks.WikiTask(request=request)
    got = await tasks._generate_page(task, _make_page("p2"), tasks._wiki_pipeline(request["target"].get("generator"), request["target"].get("generator_config")), _prepared())
    assert "PB-REAL" in got.content
    assert "- [src/main.py](src/main.py)" in captured["prompt"]
    assert "DELIVERABLE" in captured["prompt"]
    assert "SECRET" not in captured["prompt"]


@pytest.mark.asyncio
async def test_generate_page_with_retry_exhausted_writes_placeholder(tmp_path, monkeypatch):
    """重试耗尽:返回错误占位页且 cc 交付文件落盘(占位文本不经格式化,续跑可跳过)。"""
    request = _make_request("retry-io", "demo")

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    pipeline = deepwiki.AgentWikiPipeline()
    monkeypatch.setattr(pipeline, "generate_page", boom)
    task = tasks.WikiTask(request=request)
    monkeypatch.setattr(tasks, "_WIKI_PAGE_RETRIES", 1)
    got = await tasks._generate_page_with_retry(task, _make_page("p1"), pipeline, _prepared())
    assert got.content.startswith("Error generating content:")
    out = pipeline._agent_cache_page_path(_proj(request), "p1", request["target"].get("generator"), request["target"].get("generator_config"))
    assert out.exists()
    assert out.read_text(encoding="utf-8").startswith("Error generating content:")


@pytest.mark.asyncio
async def test_page_llm_through_research_chat(tmp_path, monkeypatch):
    """llm 页(原版同式):页面提示词(仅链接,不内联内容)作为查询经 research_chat
    等价流;SIMPLE 角色 + /no_think + 检索上下文 <START_OF_CONTEXT> 注入。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    captured = {}

    def fake_query(question, **kw):
        captured["query"] = question
        return {"answer": "NODE f [src=src/a.py loc=L1 community=c0]\n"}

    async def fake_llm_stream(prompt, *, generator=None, generator_config=None, session_name=None, run_id=None,
                              **kw):
        captured["session"] = session_name
        captured["prompt"] = prompt
        captured["generator"] = generator
        yield "LLM-CONTENT"

    monkeypatch.setattr(deepwiki.graphify, "query", fake_query)
    monkeypatch.setattr(deepwiki_utils, "llm_stream", fake_llm_stream)
    request = {**_make_request("llm-page", "demo"), "target": {"generator": "llm"}}
    page = WikiPage(
        id="p1", title="Page p1", content="", filePaths=["src/a.py"],
        importance="medium", relatedPages=[],
    )
    repo = Repo(str(tmp_path), "local")
    content = await deepwiki.LlmWikiPipeline().generate_page(
        generator=request["target"].get("generator"), generator_config=request["target"].get("generator_config"), repo=repo, page=page, language=request["language"],
        default_branch="main", run_id=_proj(request),
    )
    # 终态格式化收进 pipeline:引用详情块注入、链接按规范式生成
    assert "LLM-CONTENT" in content
    assert content.startswith("<details>")
    assert "- [src/a.py](src/a.py)" in content
    assert captured["session"] == "wiki:page:p1"
    assert captured["generator"] == "llm"  # model/url/api_key 由选型经 adapter 注入
    user_msg = captured["prompt"]  # 模块级 llm_stream 只收单条 user 消息(payload 内部形态)
    assert "/no_think " in user_msg and "expert code analyst" in user_msg  # SIMPLE 角色模板
    assert "<START_OF_CONTEXT>" in user_msg and "<END_OF_CONTEXT>" in user_msg
    assert "## File Path: src/a.py" in user_msg and "[lines 1-1]" in user_msg
    assert "<details>" in user_msg and "Page p1" in user_msg  # 页面提示词为查询
    assert "<file path=\"" not in user_msg  # 不再内联内容(原版由检索上下文提供)
    assert "<query>\n" in user_msg and "\nAssistant: " in user_msg
    assert captured["query"].startswith("You are an expert technical writer")


@pytest.mark.asyncio
async def test_page_llm_input_too_large_skips_retrieval(tmp_path, monkeypatch):
    """llm 页(原版 MAX_INPUT_TOKENS 语义):查询估算超限 → 跳过检索,注入
    "Answering without retrieval augmentation." note,历史/页提示词照常。"""
    captured = {}

    def fake_query(question, **kw):
        captured["query"] = question
        return {"answer": ""}

    async def fake_llm_stream(prompt, *, generator=None, generator_config=None, session_name=None, run_id=None,
                              **kw):
        captured["prompt"] = prompt
        yield "HUGE"

    monkeypatch.setattr(deepwiki.graphify, "query", fake_query)
    monkeypatch.setattr(deepwiki_utils, "llm_stream", fake_llm_stream)
    request = {**_make_request("llm-huge", "demo"), "target": {"generator": "llm"}}
    page = WikiPage(
        id="p1", title="x" * 40000, content="", filePaths=[],
        importance="medium", relatedPages=[],
    )
    repo = Repo(str(tmp_path), "local")
    assert await deepwiki.LlmWikiPipeline().generate_page(
        generator=request["target"].get("generator"), generator_config=request["target"].get("generator_config"), repo=repo, page=page, language=request["language"],
        default_branch="main", run_id=_proj(request),
    ) == "HUGE"
    assert "query" not in captured  # 检索未被调用
    user_msg = captured["prompt"]
    assert "Answering without retrieval augmentation." in user_msg
    assert "\nAssistant: " in user_msg


@pytest.mark.asyncio
async def test_generate_repo_wiki_cc_assemble_and_resume(tmp_path, monkeypatch):
    """cc 端到端(离线):structure/全部页文件预落盘 → 零 agent 调用完成;
    JSON 正文与文件一致、taskstate 清理、页面完成数按文件水合。"""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    request = {
        "repo_url": str(repo_dir), "type": "local", "owner": "local",
        "repo": "demo", "language": "en", "target": {}, "token": None,
        "comprehensive": True,
    }
    # 预置假索引(与 _graph_dir 命名对齐)
    fake_repo = Repo(str(repo_dir), "local")
    graph_dir = _graph_dir(fake_repo)
    graph_dir.mkdir(parents=True, exist_ok=True)
    (graph_dir / "graph.json").write_text("{}", encoding="utf-8")
    # 预置结构 + 全部页面交付文件
    pipeline = deepwiki.AgentWikiPipeline()
    struct_path = pipeline._agent_cache_structure_path(_proj(request), request["target"].get("generator"), request["target"].get("generator_config"))
    struct_path.parent.mkdir(parents=True, exist_ok=True)
    struct_path.write_text(_STRUCT_XML, encoding="utf-8")
    for pid in ("p1", "p2", "p3"):
        pipeline._agent_cache_page_path(_proj(request), pid, request["target"].get("generator"), request["target"].get("generator_config")).write_text(
            f"## {pid}-REAL\n\nbody\n", encoding="utf-8"
        )

    async def boom(*args, **kwargs):
        raise AssertionError("agent must not be called")

    monkeypatch.setattr(deepwiki.AgentWikiPipeline, "_deliver", boom)
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "cc")
    task = tasks.WikiTask(request=request)
    await tasks.generate_repo_wiki(task)
    assert task.status == TaskStatus.COMPLETED
    digest = _generator_digest(request["target"].get("generator"), request["target"].get("generator_config"))
    cache_path = Path(_wiki_cache_dir()) / f"deepwiki_cache_local_local_demo_en_{digest}.json"
    assert cache_path.exists()
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert set(data["generated_pages"]) == {"p1", "p2", "p3"}
    assert data["generator"] == "cc"  # 成品缓存只记公开 target,无凭证字段
    assert data.get("provider") is None and data.get("model") == ""  # file 类不落 provider/model
    from gh_puller import envs as _envs

    assert data["config_path"] == _envs.DEEPWIKI_CC_CONFIG  # 身份 = config_path(非凭证)
    assert "api_key" not in json.dumps(data) and "base_url" not in json.dumps(data)
    for pid in ("p1", "p2", "p3"):
        assert f"{pid}-REAL" in data["generated_pages"][pid]["content"]
    assert not Path(_resume_state_path("local", "demo", "local", "en", digest=digest)).exists()
    assert task.pages_done == 3


@pytest.mark.asyncio
async def test_determine_structure_llm_streams(monkeypatch):
    """llm 路结构(原版同式):结构提示词作为查询经 research_chat 等价流,
    /no_think + SIMPLE 角色 + 无命中时"无检索增强"note,解析 XML。"""
    captured = {}

    async def fake_llm_stream(prompt, *, generator=None, generator_config=None, session_name=None, run_id=None,
                              **kw):
        captured["session"] = session_name
        captured["prompt"] = prompt
        yield _STRUCT_XML

    def fake_query(question, **kw):
        captured["query"] = question
        return {"answer": ""}

    monkeypatch.setattr(deepwiki_utils, "llm_stream", fake_llm_stream)
    monkeypatch.setattr(deepwiki.graphify, "query", fake_query)
    request = {**_make_request("llm-struct", "demo"), "target": {"generator": "llm"}}
    repo = Repo("/tmp/gh-puller-test-repo", "local")
    s = await deepwiki.LlmWikiPipeline().determine_structure(
        generator=request["target"].get("generator"), generator_config=request["target"].get("generator_config"), repo=repo, owner=request["owner"], repo_name=request["repo"],
        file_tree=["app.py"], readme="", comprehensive=request["comprehensive"],
        language=request["language"], run_id=_proj(request),
    )
    assert [p.id for p in s.pages] == ["p1", "p2", "p3"]
    assert captured["session"] == "wiki:structure"
    user_msg = captured["prompt"]
    assert "/no_think " in user_msg and "expert code analyst" in user_msg
    assert "Answering without retrieval augmentation." in user_msg
    assert captured["query"].startswith("Analyze this GitHub repository llm-struct/demo")
    # 输入仅文件树路径(含 <file_tree> 标签),无任何文件内容内联
    assert "<file_tree>" in user_msg
    assert "app.py" in user_msg
