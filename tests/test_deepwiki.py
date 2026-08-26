"""gh_puller.deepwiki 引擎/任务层的本地测试。

不调 Claude agent(不依赖 API key / CLI):
- 环境变量要求:ANTHROPIC_API_KEY 不设;DEEPWIKI_ROOT 指向临时目录(见文件头)。
- 覆盖:纯函数层(结构解析 / 引用后处理 / snippet 定位 / JSON 修复)、Repo 克隆语义,
  与生成状态落盘/续跑(离线:生成函数全部 monkeypatch,真实写临时 wikicache)。
- HTTP 端点契约测试(TestClient)在 apps/deepwiki-webui/server/tests/test_app.py(独立项目)。
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# envs 在模块导入时单点读取 —— 必须在 import gh_puller.deepwiki 前把产物根指向临时目录。
# 用强制赋值而非 setdefault:即使外层环境已设 DEEPWIKI_ROOT,本测试也不落用户真实目录
# (测试隔离优先于外部配置)。且全量套件下 agent-monitor 等测试会先 import gh_puller.agent
# → 其模块级 `from .. import envs` 已把真实根快照进 sys.modules 的 gh_puller.envs,
# 之后再设环境变量无效 —— 必须 pop + 清除包属性(仅 pop 时 `from pkg import mod` 会命中
# 包上的缓存属性,仍需 delattr),让 envs 以临时根重新加载(agent 侧继续用旧对象,互不影响)。
os.environ["DEEPWIKI_ROOT"] = tempfile.mkdtemp(prefix="deepwiki-test-")
sys.modules.pop("gh_puller.envs", None)
try:
    delattr(sys.modules["gh_puller"], "envs")
except (AttributeError, KeyError):
    pass

import pytest

from gh_puller import deepwiki
from gh_puller.deepwiki import (
    RepoInfo,
    RepoUrlContext,
    WikiCacheData,
    WikiPage,
    WikiStructureModel,
    WikiTask,
    WikiTaskRequest,
    WikiTaskState,
    _generate_pages,
    _locate_snippet,
    _pending_pages,
    _wiki_state_path,
    delete_wiki_cache,
    delete_wiki_task_state,
    list_wiki_cache,
    parse_wiki_structure,
    post_process_wiki_content,
    read_wiki_task_state,
    save_wiki_cache,
    write_wiki_task_state,
)
from gh_puller.utils import (
    Repo,
    TaskStatus,
    _clone_url_with_token,
    _extract_json,
    _path_is_url,
    _repair_json,
)

# ---------------------------------------------------------------------------
# Repo 克隆语义
# ---------------------------------------------------------------------------


def test_download_failure_hides_token():
    """克隆失败:抛 ValueError 且错误信息不泄露 token(连接 127.0.0.1:1 立即拒绝)。"""
    secret = "ghp_SECRET_TOKEN_123"
    repo = Repo("https://127.0.0.1:1/foo/bar.git", "github", access_token=secret)
    with pytest.raises(ValueError) as ei:
        repo.download()
    assert secret not in str(ei.value)


def test_clone_url_with_token():
    assert _path_is_url("https://github.com/a/b") is True
    assert _path_is_url("/local/dir") is False
    github = _clone_url_with_token("https://github.com/a/b.git", "github", "tok")
    assert github.startswith("https://tok@github.com/a/b.git")
    gitlab = _clone_url_with_token("https://gitlab.com/a/b.git", "gitlab", "tok")
    assert gitlab.startswith("https://oauth2:tok@gitlab.com/a/b.git")
    bb = _clone_url_with_token("https://bitbucket.org/a/b.git", "bitbucket", "ATCTTabc")
    assert bb.startswith("https://x-bitbucket-api-token-auth:ATCTTabc@bitbucket.org/a/b.git")


# ---------------------------------------------------------------------------
# 纯函数单元(移植自 deepwiki-open,行为对齐)
# ---------------------------------------------------------------------------


def test_parse_wiki_structure_full():
    xml = """<wiki_structure>
<title>Demo</title>
<description>desc</description>
<page id="p1"><title>Overview</title><file_path>app.py</file_path><importance>high</importance></page>
<page id="p2"><title>Setup</title><file_path>readme.md</file_path><related>p1</related></page>
</wiki_structure>"""
    s = parse_wiki_structure(xml, comprehensive=False)
    assert s.title == "Demo" and s.description == "desc"
    assert [p.id for p in s.pages] == ["p1", "p2"]
    assert s.pages[0].importance == "high"
    assert s.pages[1].importance == "medium"  # 缺省归一化
    assert s.pages[1].relatedPages == ["p1"]


def test_parse_wiki_structure_truncated():
    """无 </wiki_structure> 的被截断响应:按完整块救取 + 正则兜底两路都命中。"""
    xml = """<wiki_structure>
<title>Demo</title>
<page id="p1"><title>A</title><file_path>app.py</file_path></page>
"""
    s = parse_wiki_structure(xml, comprehensive=False)
    assert s.title == "Demo" and [p.id for p in s.pages] == ["p1"]


def test_parse_wiki_structure_sections():
    xml = """<wiki_structure>
<title>T</title>
<section id="s1"><title>S1</title><page_ref>p1</page_ref></section>
<section id="s2"><title>S2</title><section_ref>s1</section_ref></section>
<page id="p1"><title>A</title></page>
</wiki_structure>"""
    s = parse_wiki_structure(xml, comprehensive=True)
    assert [sec.id for sec in s.sections] == ["s1", "s2"]
    assert s.sections[0].pages == ["p1"]
    assert s.rootSections == ["s2"]  # s1 被 s2 引用


def test_parse_wiki_structure_invalid_raises():
    with pytest.raises(ValueError):
        parse_wiki_structure("no xml here", comprehensive=False)


def test_post_process_github_links():
    ctx = RepoUrlContext(type="github", repo_url="https://github.com/foo/bar", default_branch="main")
    content = "See [app.py]() and [Sources: app.py:10]()\n\n<!-- tail -->"
    out = post_process_wiki_content(content, ["app.py"], ctx)
    assert "https://github.com/foo/bar/blob/main/app.py" in out
    # 无"()"残留(Source 链接换址)
    assert "Source: [" in out or "[Sources: " not in out
    assert "(]()" not in out


def test_post_process_details_block():
    ctx = RepoUrlContext(type="local", repo_url="", default_branch="main")
    out = post_process_wiki_content("plain text", ["app.py"], ctx)
    assert "plain text" in out  # 本地无 URL,正文不受扰
    assert "Relevant source files" in out or "[​app.py]" in out  # 详情块仍注入
    # local 下链接回退为裸路径(与 deepwiki-open 同)
    assert "app.py" in out


def test_locate_snippet():
    canvas = "line zero\nabc def\nghi\n"
    assert _locate_snippet(canvas, "abc def") == (2, 2)
    assert _locate_snippet(canvas, "def") == (2, 2)  # 首行兜底定位
    assert _locate_snippet(canvas, "zzz") is None


def test_repair_and_extract_json():
    assert _repair_json('{"a": "b",}') == '{"a": "b"}'
    assert _extract_json('```json\n{"x": 1}\n```') == {"x": 1}
    assert _extract_json('prefix {"a": [1, 2,]} suffix') == {"a": [1, 2]}


# ---------------------------------------------------------------------------
# 生成状态落盘与续跑(本地离线:写临时 wikicache,生成函数全部 monkeypatch)
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


def _make_request(owner: str, repo: str) -> WikiTaskRequest:
    return WikiTaskRequest(
        repo_url="/tmp/gh-puller-test-repo", type="local", owner=owner, repo=repo, language="en"
    )


def _digest_of(target: "deepwiki.TargetInput | None") -> str:
    """request target → 稳定摘要(测试与实现共用一个函数)。"""
    return deepwiki._request_digest(target)


def _default_target() -> "deepwiki.TargetInput":
    return deepwiki.TargetInput()


@pytest.mark.asyncio
async def test_wiki_task_state_roundtrip_atomic():
    """状态文件写/读/删往返:原子写无 .tmp 残留,且不被 list_wiki_cache 当成成品。"""
    state = WikiTaskState(
        request=_make_request("state-io", "demo"),
        status=TaskStatus.GENERATING,
        wiki_structure=_make_structure(["p1", "p2"]),
        generated_pages={"p1": _make_page("p1")},
        default_branch="main",
        submitted_at=1234567890,
    )
    assert await write_wiki_task_state(state) is True
    digest = deepwiki._request_digest(state.request.target)
    path = _wiki_state_path("state-io", "demo", "local", "en", digest=digest)
    assert os.path.exists(path)
    assert not os.path.exists(f"{path}.tmp")
    loaded = await read_wiki_task_state("state-io", "demo", "local", "en", digest=digest)
    assert loaded is not None
    assert loaded.model_dump() == state.model_dump()
    # 状态文件以 deepwiki_taskstate_ 前缀命名,不污染成品缓存列表
    assert await list_wiki_cache() == []
    assert await delete_wiki_task_state("state-io", "demo", "local", "en", digest=digest) is True
    assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_agent_text_through_target_dispatcher(monkeypatch):
    """迁移冒烟:deepwiki.agent 调用归一化为 generate_stream(target/options/label/run_id/context 透传)。"""
    calls = []

    async def fake_generate_stream(prompt, *, target=None, options=None, session=None,
                                   session_name=None, run_id=None, context=None, retry=None, meta=None):
        calls.append((target, options, session_name, prompt, run_id, context))
        yield "a"
        yield "b"

    monkeypatch.setattr(deepwiki, "generate_stream", fake_generate_stream)
    ctx = [{"type": "context/inject", "data": {"text": "n"}}]
    out = await deepwiki._agent_text(
        deepwiki.TargetInput(generator="cc"), "sys", "query",
        label="wiki:structure", run_id="r1", context=ctx,
    )
    assert out == "ab"
    target, options, session_name, prompt, run_id, ctx_got = calls[0]
    assert (session_name, prompt, run_id, ctx_got) == ("wiki:structure", "query", "r1", ctx)
    assert target.generator == "cc"
    assert options.system_prompt == "sys"  # cc 选项:仅装配,model/凭证由绑定层
    assert options.model is None  # model 由绑定层注入(ClaudeAgentOptions 恒有该字段)


@pytest.mark.asyncio
async def test_agent_text_through_dsh_stream(monkeypatch):
    """dsh 后端的迁移冒烟:deepwiki.agent 调用归一化为 generate_stream(dsh target);
    options 由 _dsh_options 组装(隔离 + 图 MCP 组合)。"""
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "dsh")
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_DSH_CORDIS", "")
    calls = []

    async def fake_generate_stream(prompt, *, target=None, options=None, session=None,
                                   session_name=None, run_id=None, context=None, retry=None, meta=None):
        calls.append((options, session_name, prompt, run_id, context))
        yield "a"
        yield "b"

    monkeypatch.setattr(deepwiki, "generate_stream", fake_generate_stream)
    ctx = [{"type": "context/inject", "data": {"text": "n"}}]
    out = await deepwiki._agent_text(
        deepwiki.TargetInput(), "sys", "query", label="wiki:structure", run_id="r1", context=ctx
    )
    assert out == "ab"
    options, session_name, prompt, run_id, ctx_got = calls[0]
    assert (session_name, prompt, run_id, ctx_got) == ("wiki:structure", "query", "r1", ctx)
    # 组装面:provider 固定路由、system_prompt 经 env 注入;cordis 未设 → 适配器层
    # 缺省回退隔离组合(dsh_stream 的 fields.setdefault,见 adapters.dsh_cordis_path)
    assert options.provider == "deepseek-official"
    assert options.env == {"DSH_SYSTEM_PROMPT": "sys"}
    assert not hasattr(options, "cordis")
    assert not hasattr(options, "model")  # model 由 target 绑定层注入,装配层不设


def test_dsh_options_config(monkeypatch, tmp_path):
    """_dsh_options 仅做工具/隔离装配:cwd 固定仓库根,runtime_cwd 越过 checkout(.env 加载点);
    model/api_key/base_url 不在装配层(统一经 target 绑定)。"""
    repo = Repo(str(tmp_path), "local")
    opts = deepwiki._dsh_options("sys", repo)
    assert opts.cwd == str(tmp_path)
    assert not hasattr(opts, "model")  # model 交给 target 绑定(显式 > env > SDK 缺省)
    assert not hasattr(opts, "api_key") and not hasattr(opts, "base_url")
    assert opts.session_root.endswith("dsh-sessions")
    assert "dsh-runtime" in opts.runtime_cwd  # 与任务 checkout 隔离(见 envs.DSH_RUNTIME_CWD)
    assert not hasattr(opts, "cordis")  # 默认不传 → 适配器缺省隔离组合
    opts2 = deepwiki._dsh_options("sys", None)
    assert not hasattr(opts2, "cwd")  # repo 空:不固定 cwd(走进程缺省)
    # 显式覆写:envs.DEEPWIKI_DSH_CORDIS 提供即传递(全责在上层组合)
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_DSH_CORDIS", "/custom/cordis.yml")
    assert deepwiki._dsh_options("sys", None).cordis == "/custom/cordis.yml"


def test_dsh_cordis_isolation_and_graphify():
    """内置组合 = 完全隔离(逐项关断本地/用户级配置)+ 显式装载 graphify MCP。

    与 cc 的 setting_sources=[] 同语义:workspaceContext(本地 AGENTS.md 链)/
    skills(用户/项目/捆绑技能)关断;graphify 仅经单服务器 mcp-client 行显式装载。
    """
    from gh_puller.agent import dsh_cordis_path

    text = Path(dsh_cordis_path()).read_text(encoding="utf-8")
    assert "workspaceContext: false" in text
    assert "includeHarnessIdentity: false" in text
    assert "includeRuntimeContext: false" in text
    assert "toolBash: false" in text and "toolJobs: false" in text
    assert "goals: false" in text
    assert "- id: mcp-graphify" in text
    assert "serverName: graphify" in text
    assert "-m" in text and "graphify.serve" in text


def test_wiki_pipeline_dsh_uses_agent(monkeypatch):
    """分派:dsh 与 cc 同为 agent 路(AgentWikiPipeline);llm 路不受扰。"""
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "dsh")
    assert isinstance(deepwiki._wiki_pipeline(), deepwiki.AgentWikiPipeline)
    assert isinstance(deepwiki._service_pipeline(), deepwiki.AgentWikiPipeline)


def test_agent_note_tool_name_by_generator():
    """图工具指引按后端切换(cc 的 graphify_query / dsh 的 mcp__graphify__query_graph)。"""
    assert "graphify_query" in deepwiki._agent_note("cc")
    assert "mcp__graphify__query_graph" not in deepwiki._agent_note("cc")
    assert "mcp__graphify__query_graph" in deepwiki._agent_note("dsh")
    assert "graphify_query" not in deepwiki._agent_note("dsh")
    assert "mcp__graphify__query_graph" in deepwiki._agent_note("codex")


def test_agent_options_cc_setting_sources_isolated():
    """cc 完全隔离本地 claude 配置(setting_sources=[]):用户级 MCP/skills/hooks 不掺入 agent。"""
    opts = deepwiki._agent_options("sys", None)
    assert opts.setting_sources == []


@pytest.mark.asyncio
async def test_chat_stream_context_events(monkeypatch):
    """chat 历史裁剪 → context/modify(trim);agent 注记 → context/inject;run_id 关联会话组。"""
    captured = {}

    async def fake_generate_stream(prompt, *, target=None, options=None, session=None,
                                   session_name=None, run_id=None, context=None, retry=None, meta=None):
        captured.update(session=session_name, run_id=run_id, context=context, prompt=prompt)
        yield "hi"

    monkeypatch.setattr(deepwiki, "generate_stream", fake_generate_stream)
    monkeypatch.setattr(deepwiki.envs, "CHAT_TOKEN_LIMIT_ESTIMATE", 0)  # 估算必超 → 触发裁剪
    request = deepwiki.ChatCompletionRequest(
        repo_url="/tmp/deepwiki-chat-test", type="local", owner="local", repo="demo",
        language="en",
        messages=[deepwiki.ChatMessage(role="user", content="q1"),
                  deepwiki.ChatMessage(role="assistant", content="a1"),
                  deepwiki.ChatMessage(role="user", content="q2")],
    )
    got = [chunk async for chunk in deepwiki.AgentWikiPipeline().chat_stream(request)]
    assert "".join(got) == "hi"
    assert captured["run_id"] == captured["session"]  # 会话组名与监控名同一来源(chat:<repo>)
    assert captured["run_id"].startswith("chat:")
    trim, note = captured["context"]
    assert trim["type"] == "context/modify" and trim["data"]["kind"] == "trim"
    assert trim["data"]["target"] == "chat-history" and trim["data"]["removed"]["n_turns"] == 2
    assert note["type"] == "context/inject" and note["data"]["provenance"] == "deepwiki:note"
    assert "注记全文入事件" not in captured["prompt"]  # 主题:注记仍原样在 prompt 里(非空即错),见下行
    assert "<note>" in captured["prompt"] and "<query>\nq2\n</query>" in captured["prompt"]
    assert "<conversation_history>" not in captured["prompt"]  # 被裁剪


def test_pending_pages_subtracts_done():
    """纯函数:按结构顺序去掉已完成页;done 含不存在 id 时原样返回。"""
    structure = _make_structure(["p1", "p2", "p3"])
    assert [p.id for p in _pending_pages(structure, {"p1": _make_page("p1")})] == ["p2", "p3"]
    assert _pending_pages(structure, {p.id: _make_page(p.id) for p in structure.pages}) == []
    assert [p.id for p in _pending_pages(structure, {"pX": _make_page("pX")})] == ["p1", "p2", "p3"]


@pytest.mark.asyncio
async def test_generate_pages_concurrency_bounded(monkeypatch):
    """页级并发:Semaphore 调用时读模块全局,并发=4 时同时至多 4 个在途页。"""
    active = 0
    max_active = 0

    async def fake_generate(task, page):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return page.model_copy(update={"content": f"content-{page.id}"})

    monkeypatch.setattr(deepwiki, "_generate_page_with_retry", fake_generate)
    monkeypatch.setattr(deepwiki, "_WIKI_PAGE_CONCURRENCY", 4)
    task = WikiTask(request=_make_request("conc-io", "demo"))
    structure = _make_structure([f"p{i}" for i in range(8)])
    pages = await _generate_pages(task, structure)
    assert max_active == 4
    assert len(pages) == 8 and len(task.generated_pages) == 8
    assert task.pages_done == 8
    assert task.current_page_ids == []
    await delete_wiki_task_state("conc-io", "demo", "local", "en")


@pytest.mark.asyncio
async def test_registry_resume_restores_task(monkeypatch):
    """同仓库再次提交:命中落盘状态 → resumed=True,复用结构/进度恢复;state 快照胜出;
    提交明文凭证入请求、落盘状态剥离(仅公开 target),续跑时合并当前提交凭证。"""
    structure = _make_structure(["p1", "p2", "p3"])
    state = WikiTaskState(
        request=_make_request("resume-io", "demo"),
        status=TaskStatus.GENERATING,
        wiki_structure=structure,
        generated_pages={"p1": _make_page("p1"), "p2": _make_page("p2")},
        default_branch="main",
        submitted_at=9876543210,
    )
    assert await write_wiki_task_state(state) is True
    digest = deepwiki._request_digest(state.request.target)

    async def fake_generate(task):
        task.status = TaskStatus.COMPLETED

    monkeypatch.setattr(deepwiki, "generate_repo_wiki", fake_generate)
    monkeypatch.setattr(deepwiki, "_WIKI_TASK_TTL_SECONDS", 0.2)

    key = f"local_resume-io_demo@{digest}"
    fresh = _make_request("resume-io", "demo")
    fresh.comprehensive = False  # 与首次不一致:落盘快照应胜出
    fresh.target = deepwiki.TargetInput(api_key="sk-live-1", base_url="https://custom/v1")
    try:
        res = await deepwiki.registry.submit(WikiTask.from_wiki_request(fresh))
        assert res.created is True
        assert res.resumed is True
        assert res.status == TaskStatus.GENERATING
        task = deepwiki.registry.get(key)
        assert task is not None
        assert [p.id for p in task.wiki_structure.pages] == ["p1", "p2", "p3"]
        assert task.pages_done == 2
        assert task.submitted_at == 9876543210
        assert task.request.comprehensive is True
        # 续跑合并凭证:公开三元组取自落盘快照,凭证来自当前提交
        assert task.request.target.api_key == "sk-live-1"
        assert task.request.target.base_url == "https://custom/v1"
        await task.task
        assert task.status == TaskStatus.COMPLETED
    finally:
        await deepwiki.registry.remove(key)
        await delete_wiki_task_state("resume-io", "demo", "local", "en", digest=digest)
        await asyncio.sleep(0.25)  # 让 TTL 移除计时器自然结束,避免挂起的任务告警


@pytest.mark.asyncio
async def test_resume_isolated_by_target(monkeypatch):
    """续跑按公开 target 摘要隔离:同 repo 切 generator(如 cc → llm)即换状态文件,
    旧快照不复活;新目标全新一代(不进同一队列)。"""
    state = WikiTaskState(
        request=_make_request("gen-switch", "demo"),
        status=TaskStatus.GENERATING,
        wiki_structure=_make_structure(["p1", "p2"]),
        generated_pages={"p1": _make_page("p1")},
        default_branch="main",
        submitted_at=424242,
    )
    assert await write_wiki_task_state(state) is True
    digest_cc = deepwiki._request_digest(deepwiki.TargetInput())  # 空 target → env 缺省 cc

    async def fake_generate(task):
        task.status = TaskStatus.COMPLETED

    monkeypatch.setattr(deepwiki, "generate_repo_wiki", fake_generate)
    monkeypatch.setattr(deepwiki, "_WIKI_TASK_TTL_SECONDS", 0.2)
    key_cc = f"local_gen-switch_demo@{digest_cc}"
    try:
        # 同 target(env 缺省 cc)提交:命中旧快照 → 续跑
        res_ok = await deepwiki.registry.submit(
            WikiTask.from_wiki_request(_make_request("gen-switch", "demo"))
        )
        assert res_ok.resumed is True
        task_ok = deepwiki.registry.get(key_cc)
        await task_ok.task
        assert task_ok.status == TaskStatus.COMPLETED
        await deepwiki.registry.remove(key_cc)

        # 显式 llm target:新摘要 → 全新一代;cc 状态文件仍在(按目标隔离共存)
        res = await deepwiki.registry.submit(
            WikiTask.from_wiki_request(
                _make_request("gen-switch", "demo").model_copy(
                    update={"target": deepwiki.TargetInput(generator="llm")}
                )
            )
        )
        assert res.created is True
        assert res.resumed is False
        assert os.path.exists(_wiki_state_path("gen-switch", "demo", "local", "en", digest=digest_cc))
        key_llm = "local_gen-switch_demo@" + deepwiki._request_digest(
            deepwiki.TargetInput(generator="llm"))
        # 任务键 = repo 键 + target 摘要(与 cc 槽位隔离)
        task = deepwiki.registry.get(key_llm)
        assert task.key != key_cc
        await task.task
        assert task.status == TaskStatus.COMPLETED
    finally:
        await deepwiki.registry.remove(key_cc)
        await deepwiki.registry.remove("local_gen-switch_demo@" + deepwiki._request_digest(
            deepwiki.TargetInput(generator="llm")))
        await delete_wiki_task_state("gen-switch", "demo", "local", "en", digest=digest_cc)
        await asyncio.sleep(0.25)  # 让 TTL 移除计时器自然结束,避免挂起的任务告警


@pytest.mark.asyncio
async def test_cache_hit_respects_target(monkeypatch):
    """成品缓存按公开 target 摘要校验:同轨(generator/provider/model)才命中;
    换 generator(同 repo)即另一份缓存,不否定旧成品、全新重新生成。"""
    cache = WikiCacheData(
        wiki_structure=_make_structure(["p1"]),
        generated_pages={"p1": _make_page("p1")},
        generator="cc",
        provider="anthropic",
        model="",
        repo=RepoInfo(
            owner="gen-cache", repo="demo", type="local", repoUrl="/tmp/gh-puller-test-repo"
        ),
    )
    digest_cc = deepwiki._request_digest(deepwiki.TargetInput())
    assert await save_wiki_cache("gen-cache", "demo", "local", "en", cache, digest=digest_cc) is True

    calls = []

    async def fake_run(task):
        calls.append(task.key)
        task.status = TaskStatus.COMPLETED

    monkeypatch.setattr(deepwiki, "generate_repo_wiki", fake_run)
    monkeypatch.setattr(deepwiki, "_WIKI_TASK_TTL_SECONDS", 0.2)
    key_cc = f"local_gen-cache_demo@{digest_cc}"
    try:
        # 同轨(default cc):缓存命中,不运行
        res_ok = await deepwiki.registry.submit(
            WikiTask.from_wiki_request(_make_request("gen-cache", "demo"))
        )
        assert res_ok.from_cache is True
        assert res_ok.status == TaskStatus.COMPLETED
        assert calls == []

        # 换 target(llm):新摘要 → 不命中,重新生成;旧 cc 成品不被复用也不被删除
        res = await deepwiki.registry.submit(
            WikiTask.from_wiki_request(
                _make_request("gen-cache", "demo").model_copy(
                    update={"target": deepwiki.TargetInput(generator="llm")}
                )
            )
        )
        assert res.from_cache is False
        assert res.created is True
        # llm 任务走自己摘要的注册表槽位(cc 键无新任务)
        assert deepwiki.registry.get(key_cc) is None
        task_llm = deepwiki.registry.get("local_gen-cache_demo@" + deepwiki._request_digest(
            deepwiki.TargetInput(generator="llm")))
        assert task_llm is not None
        await task_llm.task
        assert task_llm.status == TaskStatus.COMPLETED
        assert calls == [task_llm.key]
    finally:
        await deepwiki.registry.remove(key_cc)
        await deepwiki.registry.remove("local_gen-cache_demo@" + deepwiki._request_digest(
            deepwiki.TargetInput(generator="llm")))
        await delete_wiki_cache("gen-cache", "demo", "local", "en", digest=digest_cc)
        await asyncio.sleep(0.25)  # 让 TTL 移除计时器自然结束,避免挂起的任务告警


# ---------------------------------------------------------------------------
# cc/llm 双路径(离线:交付文件预置 / agent 与 llm_stream 全部 monkeypatch)
# ---------------------------------------------------------------------------

_STRUCT_XML = """<wiki_structure>
<title>T</title>
<page id="p1"><title>A</title><file_path>src/a.py</file_path></page>
<page id="p2"><title>B</title><file_path>src/b.py</file_path></page>
<page id="p3"><title>C</title><file_path>src/c.py</file_path></page>
</wiki_structure>"""


def test_agent_cache_naming():
    request = WikiTaskRequest(repo_url="/x", type="local", owner="local", repo="deepwiki-open", language="en")
    pipeline = deepwiki.AgentWikiPipeline()
    prefix = f"local_local_deepwiki-open_{_digest_of(request.target)}"
    assert pipeline._agent_cache_structure_path(request).name == f"{prefix}-structure.md"
    assert pipeline._agent_cache_page_path(request, "page-1").name == f"{prefix}-page-1.md"
    assert pipeline._agent_cache_page_path(request, "overview").name == f"{prefix}-page_overview.md"


def test_sanitize_page_id_no_escape():
    r = _make_request("sanitize-io", "demo")
    pipeline = deepwiki.AgentWikiPipeline()
    out = pipeline._agent_cache_page_path(r, "../../evil")
    assert out.parent == pipeline._agent_cache_dir(r)
    assert out.relative_to(pipeline._agent_cache_dir(r)).parent == Path(".")


def test_agent_options_cc_delivery(tmp_path):
    """写入模式:cwd 固定仓库根,add_dirs 指向交付目录,acceptEdits + 写工具;
    默认模式(chat/codemap):cwd + Read/Grep/Glob 自读代码,无 Write/写目录/acceptEdits;
    model/凭证不在装配层(经 target 绑定)。"""
    repo = Repo(str(tmp_path), "local")
    opts = deepwiki._agent_options(
        "", repo, agent_output_dir=str(tmp_path / "out"), agent_write_mode=True
    )
    assert opts.cwd == str(tmp_path)
    assert opts.add_dirs == [str(tmp_path / "out")]
    assert opts.permission_mode == "acceptEdits"
    assert opts.model is None  # model 由 target 绑定层注入
    for t in ("Read", "Grep", "Glob", "Write", "graphify_query"):
        assert t in opts.allowed_tools, t
    opts2 = deepwiki._agent_options("", repo)
    assert opts2.cwd == str(tmp_path)
    assert opts2.add_dirs == []
    assert opts2.permission_mode is None
    for t in ("Read", "Grep", "Glob", "graphify_query"):
        assert t in opts2.allowed_tools, t
    assert "Write" not in opts2.allowed_tools


@pytest.mark.asyncio
async def test_dispatch_structure_by_generator(monkeypatch):
    """DEEPWIKI_GENERATOR 分派:cc 只走 _determine_structure_cc,llm 只走 _determine_structure_llm。"""
    calls = []

    async def fake_llm(task, repo, files, readme):
        calls.append("llm")
        return _make_structure(["p1"])

    async def fake_cc(task, repo, files, readme=None):
        calls.append("cc")
        return _make_structure(["p1"])

    monkeypatch.setattr(deepwiki.LlmWikiPipeline, "determine_structure", staticmethod(fake_llm))
    monkeypatch.setattr(deepwiki.AgentWikiPipeline, "determine_structure", staticmethod(fake_cc))
    task = WikiTask(request=_make_request("dispatch-io", "demo"))

    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "cc")
    await deepwiki._determine_structure(task)
    assert calls == ["cc"]

    calls.clear()
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "llm")
    await deepwiki._determine_structure(task)
    assert calls == ["llm"]


@pytest.mark.asyncio
async def test_determine_structure_cc_skips_when_file_exists(monkeypatch):
    """cc 结构续跑:structure.md 已存在则直接解析,不启动 agent。"""
    request = _make_request("cc-struct", "demo")
    struct_path = deepwiki.AgentWikiPipeline()._agent_cache_structure_path(request)
    struct_path.parent.mkdir(parents=True, exist_ok=True)
    struct_path.write_text(_STRUCT_XML, encoding="utf-8")

    async def boom(*args, **kwargs):
        raise AssertionError("agent must not be called")

    monkeypatch.setattr(deepwiki, "_agent_write_file", boom)
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "cc")
    repo = Repo("/tmp/gh-puller-test-repo", "local")
    s = await deepwiki.AgentWikiPipeline().determine_structure(
        WikiTask(request=request), repo, ["src/a.py"], ""
    )
    assert [p.id for p in s.pages] == ["p1", "p2", "p3"]


@pytest.mark.asyncio
async def test_determine_structure_cc_calls_agent_no_inline(tmp_path, monkeypatch):
    """cc 结构走 agent 并读回文件;提示词只有文件树路径,不内联任何文件内容。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("SECRET_CODE_BODY", encoding="utf-8")
    (tmp_path / "README.md").write_text("SECRET_README_BODY", encoding="utf-8")
    request = WikiTaskRequest(repo_url=str(tmp_path), type="local", owner="local", repo="demo", language="en")
    captured = {}

    async def fake_write(target, system_prompt, prompt, repo, out_path, label=None,
                         run_id=None, context=None, retry=None):
        captured["prompt"] = prompt
        captured["run_id"] = run_id
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(_STRUCT_XML, encoding="utf-8")
        return _STRUCT_XML

    monkeypatch.setattr(deepwiki, "_agent_write_file", fake_write)
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "cc")
    repo = Repo(str(tmp_path), "local")
    s = await deepwiki.AgentWikiPipeline().determine_structure(
        WikiTask(request=request), repo, ["src/a.py"], ""
    )
    assert [p.id for p in s.pages] == ["p1", "p2", "p3"]
    assert "<file_tree>" in captured["prompt"]
    assert "src/a.py" in captured["prompt"]
    assert "SECRET_CODE_BODY" not in captured["prompt"]
    assert "SECRET_README_BODY" not in captured["prompt"]
    assert captured["run_id"] == request.repo_key  # 任务级会话组关联


@pytest.mark.asyncio
async def test_page_cc_skips_when_file_exists(monkeypatch):
    """cc 页续跑:page_<id>.md 已存在则直接读回(文件为权威),不启动 agent。"""
    request = _make_request("cc-page", "demo")
    out = deepwiki.AgentWikiPipeline()._agent_cache_page_path(request, "p1")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("## PA-REAL\n\nbody\n", encoding="utf-8")

    async def boom(*args, **kwargs):
        raise AssertionError("agent must not be called")

    monkeypatch.setattr(deepwiki, "_agent_write_file", boom)
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "cc")
    task = WikiTask(request=request)
    task.default_branch = "main"
    got = await deepwiki._generate_page(task, _make_page("p1"))
    assert "PA-REAL" in got.content
    assert got.id == "p1"


@pytest.mark.asyncio
async def test_page_cc_calls_agent_and_reads_file(monkeypatch):
    """cc 页走 agent 落盘后读回;提示词只给路径并含交付指令,不内联文件内容。"""
    request = _make_request("cc-page2", "demo")
    captured = {}

    async def fake_write(target, system_prompt, prompt, repo, out_path, label=None,
                         run_id=None, context=None, retry=None):
        captured["prompt"] = prompt
        captured["run_id"] = run_id
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text("## PB-REAL\n\ncontent\n", encoding="utf-8")
        return "## PB-REAL\n\ncontent\n"

    monkeypatch.setattr(deepwiki, "_agent_write_file", fake_write)
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "cc")
    task = WikiTask(request=request)
    task.default_branch = "main"
    got = await deepwiki._generate_page(task, _make_page("p2"))
    assert "PB-REAL" in got.content
    assert "- [src/main.py](src/main.py)" in captured["prompt"]
    assert "DELIVERABLE" in captured["prompt"]
    assert "SECRET" not in captured["prompt"]


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

    async def fake_generate_stream(prompt, *, target=None, options=None, session=None,
                                   session_name=None, run_id=None, context=None, retry=None,
                                   meta=None):
        captured["target"] = target
        captured["session"] = session_name
        captured["options"] = options
        yield "LLM-CONTENT"

    monkeypatch.setattr(deepwiki.graphify, "query", fake_query)
    monkeypatch.setattr(deepwiki, "generate_stream", fake_generate_stream)
    request = _make_request("llm-page", "demo")
    task = WikiTask(request=request)
    task.default_branch = "main"
    page = WikiPage(
        id="p1", title="Page p1", content="", filePaths=["src/a.py"],
        importance="medium", relatedPages=[],
    )
    repo = Repo(str(tmp_path), "local")
    content = await deepwiki.LlmWikiPipeline().generate_page(
        task, repo, page, "- [src/a.py](src/a.py)"
    )
    assert content == "LLM-CONTENT"
    assert captured["session"] == "wiki:page:p1"
    # target 原样透传(解析与绑定在 dispatcher);model/url/api_key 不在上游
    assert captured["target"] is task.request.target
    assert "model" not in captured["options"]
    assert len(captured["options"]["messages"]) == 1
    user_msg = captured["options"]["messages"][0]["content"]
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

    async def fake_generate_stream(prompt, *, target=None, options=None, session=None,
                               session_name=None, run_id=None, context=None, retry=None, meta=None):
        captured["options"] = options
        yield "HUGE"

    monkeypatch.setattr(deepwiki.graphify, "query", fake_query)
    monkeypatch.setattr(deepwiki, "generate_stream", fake_generate_stream)
    task = WikiTask(request=_make_request("llm-huge", "demo"))
    task.default_branch = "main"
    page = WikiPage(
        id="p1", title="x" * 40000, content="", filePaths=[],
        importance="medium", relatedPages=[],
    )
    repo = Repo(str(tmp_path), "local")
    assert await deepwiki.LlmWikiPipeline().generate_page(
        task, repo, page, ""
    ) == "HUGE"
    assert "query" not in captured  # 检索未被调用
    user_msg = captured["options"]["messages"][0]["content"]
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
    request = WikiTaskRequest(
        repo_url=str(repo_dir), type="local", owner="local", repo="demo", language="en",
    )
    # 预置假索引(与 _graph_dir 命名对齐)
    fake_repo = Repo(str(repo_dir), "local")
    graph_dir = deepwiki._graph_dir(fake_repo)
    graph_dir.mkdir(parents=True, exist_ok=True)
    (graph_dir / "graph.json").write_text("{}", encoding="utf-8")
    # 预置结构 + 全部页面交付文件
    pipeline = deepwiki.AgentWikiPipeline()
    struct_path = pipeline._agent_cache_structure_path(request)
    struct_path.parent.mkdir(parents=True, exist_ok=True)
    struct_path.write_text(_STRUCT_XML, encoding="utf-8")
    for pid in ("p1", "p2", "p3"):
        pipeline._agent_cache_page_path(request, pid).write_text(
            f"## {pid}-REAL\n\nbody\n", encoding="utf-8"
        )

    async def boom(*args, **kwargs):
        raise AssertionError("agent must not be called")

    monkeypatch.setattr(deepwiki, "_agent_write_file", boom)
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "cc")
    task = WikiTask(request=request)
    await deepwiki.generate_repo_wiki(task)
    assert task.status == TaskStatus.COMPLETED
    digest = deepwiki._request_digest(request.target)
    cache_path = Path(deepwiki._WIKI_CACHE_DIR) / f"deepwiki_cache_local_local_demo_en_{digest}.json"
    assert cache_path.exists()
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert set(data["generated_pages"]) == {"p1", "p2", "p3"}
    assert data["generator"] == "cc"  # 成品缓存只记公开 target,无凭证字段
    assert data["provider"] == "anthropic"
    assert "api_key" not in json.dumps(data) and "base_url" not in json.dumps(data)
    for pid in ("p1", "p2", "p3"):
        assert f"{pid}-REAL" in data["generated_pages"][pid]["content"]
    assert not Path(deepwiki._wiki_state_path("local", "demo", "local", "en", digest=digest)).exists()
    assert task.pages_done == 3


def test_save_llm_restores_generator_default():
    """env 缺省即 cc(默认走 agent 路径)。"""
    assert deepwiki.envs.DEEPWIKI_GENERATOR == "cc"


@pytest.mark.asyncio
async def test_determine_structure_llm_streams(monkeypatch):
    """llm 路结构(原版同式):结构提示词作为查询经 research_chat 等价流,
    /no_think + SIMPLE 角色 + 无命中时"无检索增强"note,解析 XML。"""
    captured = {}

    async def fake_generate_stream(prompt, *, target=None, options=None, session=None,
                               session_name=None, run_id=None, context=None, retry=None, meta=None):
        captured["session"] = session_name
        captured["options"] = options
        yield _STRUCT_XML

    def fake_query(question, **kw):
        captured["query"] = question
        return {"answer": ""}

    monkeypatch.setattr(deepwiki, "generate_stream", fake_generate_stream)
    monkeypatch.setattr(deepwiki.graphify, "query", fake_query)
    task = WikiTask(request=_make_request("llm-struct", "demo"))
    repo = Repo("/tmp/gh-puller-test-repo", "local")
    s = await deepwiki.LlmWikiPipeline().determine_structure(task, repo, ["app.py"], "")
    assert [p.id for p in s.pages] == ["p1", "p2", "p3"]
    assert captured["session"] == "wiki:structure"
    user_msg = captured["options"]["messages"][0]["content"]
    assert "/no_think " in user_msg and "expert code analyst" in user_msg
    assert "Answering without retrieval augmentation." in user_msg
    assert captured["query"].startswith("Analyze this GitHub repository llm-struct/demo")
    # 输入仅文件树路径(含 <file_tree> 标签),无任何文件内容内联
    assert "<file_tree>" in user_msg
    assert "app.py" in user_msg


# ---------------------------------------------------------------------------
# 子图 → 真实代码上下文(llm 路 chat/codemap 的 RAG 式注入)
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
    pipeline = deepwiki.LlmWikiPipeline()
    hits = pipeline._subgraph_hits(_SUBGRAPH_ANSWER)
    assert hits == {"src/a.py": [5, 12, 40]}
    assert "src/b.py" not in hits and "src/c.py" not in hits
    assert pipeline._subgraph_hits("no matches here\n") == {}


@pytest.mark.asyncio
async def test_subgraph_src_blocks_windows_merge(tmp_path, monkeypatch):
    """相邻命中行合并为一个窗(radius=8):L5/L9 → (1,17);L5/L45 → 两个窗。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "\n".join(f"line {i}" for i in range(1, 101)), encoding="utf-8"
    )
    monkeypatch.setattr(deepwiki.envs, "CHAT_TOKEN_LIMIT_ESTIMATE", 20000)
    pipeline = deepwiki.LlmWikiPipeline()
    blocks, degraded = pipeline._subgraph_src_blocks(
        str(tmp_path), {"src/a.py": [5, 9]}, radius=8
    )
    assert not degraded and len(blocks) == 1
    assert blocks[0]["start_line"] == 1 and blocks[0]["end_line"] == 17
    assert blocks[0]["text"] == "\n".join(f"line {i}" for i in range(1, 18))
    blocks, degraded = pipeline._subgraph_src_blocks(
        str(tmp_path), {"src/a.py": [5, 45]}, radius=8
    )
    assert not degraded and len(blocks) == 2
    assert [b["start_line"] for b in blocks] == [1, 37]
    assert [b["end_line"] for b in blocks] == [13, 53]


def test_subgraph_src_blocks_per_file_cap_and_budget(tmp_path):
    """单文件超 cap 截断;全局超 budget 整体降级([] , True);缺文件跳过。"""
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.py").write_text("\n".join(f"line {i}" for i in range(1, 51)), encoding="utf-8")
    pipeline = deepwiki.LlmWikiPipeline()
    blocks, degraded = pipeline._subgraph_src_blocks(
        str(tmp_path), {"src/a.py": [25]}, radius=8, per_file_cap=30,
    )
    assert not degraded and len(blocks) == 1
    assert len(blocks[0]["text"]) == 30
    assert blocks[0]["end_line"] == blocks[0]["start_line"] + blocks[0]["text"].count("\n")
    blocks, degraded = pipeline._subgraph_src_blocks(
        str(tmp_path), {"src/a.py": [25]}, radius=8, budget_chars=5,
    )
    assert (blocks, degraded) == ([], True)
    blocks, degraded = pipeline._subgraph_src_blocks(str(tmp_path), {"nope.py": [1]})
    assert (blocks, degraded) == ([], False)


def test_format_subgraph_context():
    """原版 _format_context 同式:按文件分组,单文件多窗合并头,文件段以原版
    ("\\n\\n" + "-"*10 + "\\n\\n".join(parts)) 联结;空输入 → ""。"""
    blocks = [
        {"path": "src/a.py", "text": "x = 1", "start_line": 1, "end_line": 1},
        {"path": "src/a.py", "text": "y = 2", "start_line": 10, "end_line": 20},
        {"path": "src/b.py", "text": "z = 3", "start_line": 5, "end_line": 9},
    ]
    pipeline = deepwiki.LlmWikiPipeline()
    text = pipeline._format_subgraph_context(blocks)
    assert text.startswith("\n\n----------## File Path: src/a.py\n\n")  # 原版联结式
    assert text.count("## File Path:") == 2
    assert "[lines 1-1]\nx = 1" in text and "[lines 10-20]\ny = 2" in text
    assert "## File Path: src/b.py\n\n[lines 5-9]\nz = 3" in text
    assert pipeline._format_subgraph_context([]) == ""
    assert pipeline._format_subgraph_context([{"path": "a.py", "text": "t", "start_line": 1, "end_line": 1}]) \
        .startswith("\n\n----------## File Path: a.py")


# ---------------------------------------------------------------------------
# LLM 路 chat / codemap(fake graphify.query + fake llm_stream/llm_complete,全离线)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_chat_stream_prompt_and_context(monkeypatch, tmp_path):
    """llm chat(原版 prompt_builder 同构):单条 user 消息含 /no_think+SIMPLE 角色
    +历史拼接+检索上下文(<START_OF_CONTEXT> + ## File Path + [lines A-B]);查询词为末条。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    captured = {}

    def fake_query(question, **kw):
        captured["query"] = question
        return {"answer": "NODE f [src=src/a.py loc=L1 community=c0]\n"}

    async def fake_generate_stream(prompt, *, target=None, options=None, session=None,
                               session_name=None, run_id=None, context=None, retry=None, meta=None):
        captured.update(session=session_name, options=options)
        yield "HELLO"

    monkeypatch.setattr(deepwiki.graphify, "query", fake_query)
    monkeypatch.setattr(deepwiki, "generate_stream", fake_generate_stream)
    request = deepwiki.ChatCompletionRequest(
        repo_url=str(tmp_path), type="local", language="en",
        messages=[deepwiki.ChatMessage(role="user", content="q1"),
                  deepwiki.ChatMessage(role="assistant", content="a1"),
                  deepwiki.ChatMessage(role="user", content="how does src/a.py work?")],
    )
    got = [c async for c in deepwiki.LlmWikiPipeline().chat_stream(request)]
    assert "".join(got) == "HELLO"
    assert captured["session"] == f"chat:{deepwiki.Repo(str(tmp_path), 'local').name}"
    assert len(captured["options"]["messages"]) == 1  # 原版单条 user 消息(无 system role)
    assert "model" not in captured["options"]  # model 由 dispatcher 注入(target 绑定层)
    msg = captured["options"]["messages"][0]["content"]
    assert msg.startswith("/no_think ")
    assert "expert code analyst" in msg  # SIMPLE 角色模板
    assert "<conversation_history>" in msg and "<turn>" in msg
    assert "<START_OF_CONTEXT>" in msg and "<END_OF_CONTEXT>" in msg
    assert "## File Path: src/a.py" in msg and "[lines 1-1]\nx = 1" in msg
    assert "<file path=\"" not in msg  # 不再 <file> 块(原版格式)
    assert "<query>\nhow does src/a.py work?\n</query>" in msg
    assert "\nAssistant: " in msg
    assert captured["query"] == "how does src/a.py work?"


@pytest.mark.asyncio
async def test_llm_chat_continuation_and_iteration_templates(monkeypatch, tmp_path):
    """continuation 回退(查询词换回首条用户消息);迭代模板按 research_iteration 选。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    captured = {}

    def fake_query(question, **kw):
        captured["query"] = question
        return {"answer": ""}

    async def fake_generate_stream(prompt, *, target=None, options=None, session=None,
                               session_name=None, run_id=None, context=None, retry=None, meta=None):
        captured["options"] = options
        yield ""

    monkeypatch.setattr(deepwiki.graphify, "query", fake_query)
    monkeypatch.setattr(deepwiki, "generate_stream", fake_generate_stream)
    request = deepwiki.ChatCompletionRequest(
        repo_url=str(tmp_path), type="local", language="en", research_iteration=3,
        messages=[deepwiki.ChatMessage(role="user", content="what is this repo about?"),
                  deepwiki.ChatMessage(role="assistant", content="## Research Plan\nx"),
                  deepwiki.ChatMessage(role="user", content="Continue the research",
                                       mode="deep_research")],
    )
    _ = [c async for c in deepwiki.LlmWikiPipeline().chat_stream(request)]
    assert captured["query"] == "what is this repo about?"
    assert "iteration 3" in captured["options"]["messages"][0]["content"]
    request_final = request.model_copy(update={"research_iteration": 5})
    _ = [c async for c in deepwiki.LlmWikiPipeline().chat_stream(request_final)]
    assert "## Final Conclusion" in captured["options"]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_llm_chat_error_fallbacks(monkeypatch, tmp_path):
    """llm chat 失败语义(原版 stream_and_fallback):graph 失败/无命中→"无检索增强"
    note;非 token 错误→"Error with openai API:";token 超限→简化重试;重试也败→致歉。"""
    captured = {}

    def fake_query(question, **kw):
        return {"answer": ""}

    monkeypatch.setattr(deepwiki.graphify, "query", fake_query)

    async def fake_generate_stream(prompt, *, target=None, options=None, session=None,
                               session_name=None, run_id=None, context=None, retry=None, meta=None):
        captured["options"] = options
        yield "ok"

    monkeypatch.setattr(deepwiki, "generate_stream", fake_generate_stream)
    request = deepwiki.ChatCompletionRequest(
        repo_url=str(tmp_path), type="local", language="en",
        messages=[deepwiki.ChatMessage(role="user", content="hi")],
    )
    got = [c async for c in deepwiki.LlmWikiPipeline().chat_stream(request)]
    assert "".join(got) == "ok"  # 图无命中也一样回答
    assert "Answering without retrieval augmentation." in captured["options"]["messages"][0]["content"]
    # 图谱查询失败 → 同样降级 note(不引入任何错误文本)
    def boom(question, **kw):
        raise RuntimeError("graph is gone")

    monkeypatch.setattr(deepwiki.graphify, "query", boom)
    got = [c async for c in deepwiki.LlmWikiPipeline().chat_stream(request)]
    assert "".join(got) == "ok"
    assert "Answering without retrieval augmentation." in captured["options"]["messages"][0]["content"]
    # 非 token 错误 → "Error with openai API: ..." 原版式文本进流
    async def err_stream(*args, **kw):
        raise RuntimeError("llm down")
        yield  # 保持 async generator

    monkeypatch.setattr(deepwiki, "generate_stream", err_stream)
    got = [c async for c in deepwiki.LlmWikiPipeline().chat_stream(request)]
    assert "Error with openai API: llm down" in "".join(got)
    # token 超限 → 简化提示词重试一次
    calls = []

    async def token_stream(*args, **kw):
        calls.append(kw["options"])
        if len(calls) == 1:
            raise RuntimeError("maximum context length exceeded")
        yield "SIMPLIFIED"

    monkeypatch.setattr(deepwiki, "generate_stream", token_stream)
    got = [c async for c in deepwiki.LlmWikiPipeline().chat_stream(request)]
    assert "".join(got) == "SIMPLIFIED"
    assert len(calls) == 2
    assert "due to input size constraints" in calls[1]["messages"][0]["content"]
    # 简化重试也失败 → 原版致歉文本
    async def token_stream2(*args, **kw):
        raise RuntimeError("too many tokens")
        yield

    monkeypatch.setattr(deepwiki, "generate_stream", token_stream2)
    got = [c async for c in deepwiki.LlmWikiPipeline().chat_stream(request)]
    assert "I apologize, but your request is too large for me to process" in "".join(got)


@pytest.mark.asyncio
async def test_llm_codemap_events_sequence(monkeypatch, tmp_path):
    """llm codemap:NDJSON 事件序列与 agent 路同形;一次 graphify 查询两阶段复用;
    _ground_citations 以真实源码覆盖行号。"""
    repo_dir = tmp_path / "repo"
    (repo_dir / "src").mkdir(parents=True)
    (repo_dir / "src" / "a.py").write_text("def f():\n    return 42\n", encoding="utf-8")
    fake_repo = Repo(str(repo_dir), "local")
    graph_dir = deepwiki._graph_dir(fake_repo)
    graph_dir.mkdir(parents=True, exist_ok=True)
    (graph_dir / "graph.json").write_text("{}", encoding="utf-8")

    calls = []

    def fake_query(question, **kw):
        calls.append(("query", question))
        return {"answer": "NODE f [src=src/a.py loc=L1 community=c0]\n"}

    skeleton = {
        "title": "How to f", "summary": "s",
        "sections": [{
            "id": "1", "title": "Do it", "guide": "", "diagram": "",
            "steps": [{"id": "1a", "label": "call f", "code": "f()",
                       "citation": {"file_path": "src/a.py", "start_line": 99,
                                    "end_line": 99, "snippet": "    return 42"}}],
        }],
    }

    async def fake_generate_result(prompt, *, target=None, options=None, session=None,
                                session_name=None, run_id=None, context=None, retry=None, meta=None):
        calls.append(("complete", session_name, options))
        if session_name == "codemap:skeleton":
            return json.dumps(skeleton)
        return json.dumps({**skeleton, "sections": [
            {**skeleton["sections"][0], "guide": "g", "diagram": "graph TD"}]})

    monkeypatch.setattr(deepwiki.graphify, "query", fake_query)
    monkeypatch.setattr(deepwiki, "generate_result", fake_generate_result)
    request = deepwiki.CodeMapRequest(
        repo_url=str(repo_dir), type="local", language="en", question="how to call f?"
    )
    events = [json.loads(ev) async for ev in deepwiki.LlmWikiPipeline().generate_codemap(request)]
    assert [e["type"] for e in events] == ["phase", "phase", "phase", "phase",
                                           "phase", "phase", "codemap", "done"]
    assert events[0] == {"type": "phase", "phase": "analyzing", "status": "start"}
    assert events[1] == {"type": "phase", "phase": "analyzing", "status": "done",
                         "chunk_count": 1}  # analyzing 阶段内完成检索(原版 chunk_count=len(documents))
    assert events[2] == {"type": "phase", "phase": "initial_codemap", "status": "start"}
    assert events[3] == {"type": "phase", "phase": "initial_codemap", "status": "done",
                         "section_count": 1}
    assert events[4] == {"type": "phase", "phase": "diagrams", "status": "start"}
    assert events[5] == {"type": "phase", "phase": "diagrams", "status": "done"}
    assert [c[0] for c in calls].count("query") == 1  # 一次查询、两阶段复用
    data = events[6]["data"]
    assert data["sections"][0]["guide"] == "g"
    cit = data["sections"][0]["steps"][0]["citation"]
    assert (cit["start_line"], cit["end_line"]) == (2, 2)  # 真实行号覆盖 99/99
    skel_payload = [c[2] for c in calls if c[1] == "codemap:skeleton"][0]
    assert "model" not in skel_payload  # model 由 dispatcher 注入(target 绑定层)
    assert len(skel_payload["messages"]) == 1  # 原版单条 user 消息(prompt_builder)
    user = skel_payload["messages"][0]["content"]
    assert user.startswith("/no_think ")
    assert "<START_OF_CONTEXT>" in user
    assert "## File Path: src/a.py" in user and "[lines 1-2]" in user
    assert "expert code analyst" in user and "codemap" in user  # 骨架提示词正文


@pytest.mark.asyncio
async def test_llm_codemap_not_indexed_and_retry_and_degraded(monkeypatch, tmp_path):
    """未索引 → analyzing error 事件且不调补全;骨架 3 次废文本 → initial_codemap error;
    enrich 失败 → degraded=True 且 codemap 事件为骨架内容。"""
    repo_dir = tmp_path / "fresh-repo"
    repo_dir.mkdir()
    complete_calls = []

    async def fake_generate_result(prompt, *, target=None, options=None, session=None,
                                session_name=None, run_id=None, context=None, retry=None, meta=None):
        complete_calls.append(session_name)
        raise RuntimeError("unreachable")  # 不应被调用段在此触发前提供有效返回

    monkeypatch.setattr(deepwiki, "generate_result", fake_generate_result)
    request = deepwiki.CodeMapRequest(
        repo_url=str(repo_dir), type="local", language="en", question="q"
    )
    events = [json.loads(ev) async for ev in deepwiki.LlmWikiPipeline().generate_codemap(request)]
    assert events[1] == {"type": "phase", "phase": "analyzing", "status": "done",
                         "chunk_count": 0}
    assert events[2]["type"] == "error" and events[2]["stage"] == "analyzing"
    assert complete_calls == []
    assert [e["type"] for e in events].count("done") == 0


@pytest.mark.asyncio
async def test_llm_codemap_skeleton_retry_and_enrich_degrade(monkeypatch, tmp_path):
    repo_dir = tmp_path / "repo"
    (repo_dir / "src").mkdir(parents=True)
    (repo_dir / "src" / "a.py").write_text("def f():\n    return 42\n", encoding="utf-8")
    fake_repo = Repo(str(repo_dir), "local")
    graph_dir = deepwiki._graph_dir(fake_repo)
    graph_dir.mkdir(parents=True, exist_ok=True)
    (graph_dir / "graph.json").write_text("{}", encoding="utf-8")

    skeleton = {"title": "How to f", "summary": "s",
                "sections": [{"id": "1", "title": "S", "guide": "", "diagram": "",
                              "steps": [{"id": "1a", "label": "s", "code": "", "citation": None}]}]}
    complete_calls = []

    def fake_query(question, **kw):
        return {"answer": "NODE f [src=src/a.py loc=L1 community=c0]\n"}

    async def fake_generate_result(prompt, *, target=None, options=None, session=None,
                                session_name=None, run_id=None, context=None, retry=None, meta=None):
        complete_calls.append(session_name)
        if session_name == "codemap:skeleton":
            return json.dumps(skeleton)
        return "this is not json"

    monkeypatch.setattr(deepwiki.graphify, "query", fake_query)
    monkeypatch.setattr(deepwiki, "generate_result", fake_generate_result)
    request = deepwiki.CodeMapRequest(
        repo_url=str(repo_dir), type="local", language="en", question="how to call f?"
    )
    events = [json.loads(ev) async for ev in deepwiki.LlmWikiPipeline().generate_codemap(request)]
    assert events[4] == {"type": "phase", "phase": "diagrams", "status": "start"}
    assert events[5]["status"] == "done" and events[5]["degraded"] is True
    assert events[6]["type"] == "codemap" and events[6]["data"]["sections"][0]["guide"] == ""
    assert events[7] == {"type": "done"}
    assert complete_calls == ["codemap:skeleton", "codemap:enrich", "codemap:enrich"]  # 富化重试 2 次(原版)
    # 骨架重试耗尽 → error 事件,无 codemap/done
    async def garbage(prompt, *, target=None, options=None, session_name=None, **kw):
        return "not json"

    monkeypatch.setattr(deepwiki, "generate_result", garbage)
    events = [json.loads(ev) async for ev in deepwiki.LlmWikiPipeline().generate_codemap(request)]
    assert events[3]["type"] == "error" and events[3]["stage"] == "initial_codemap"
    assert events[3]["message"].startswith("Model did not return valid JSON")
    assert [e["type"] for e in events].count("codemap") == 0


# ---------------------------------------------------------------------------
# agent 路现代化(自然历史 + deep 折叠)与分派
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_chat_natural_history(monkeypatch):
    """agent chat:历史自然转写(无 <turn>/<conversation_history> 伪标签)。"""
    captured = {}

    async def fake_generate_stream(prompt, *, target=None, options=None, session=None,
                                   session_name=None, run_id=None, context=None, retry=None, meta=None):
        captured.update(system=options.system_prompt, prompt=prompt, run_id=run_id,
                        session=session_name)
        yield "hi"

    monkeypatch.setattr(deepwiki, "generate_stream", fake_generate_stream)
    request = deepwiki.ChatCompletionRequest(
        repo_url="/tmp/gh-puller-chat-natural", type="local", language="en",
        messages=[deepwiki.ChatMessage(role="user", content="q1"),
                  deepwiki.ChatMessage(role="assistant", content="a1"),
                  deepwiki.ChatMessage(role="user", content="q2")],
    )
    got = [c async for c in deepwiki.AgentWikiPipeline().chat_stream(request)]
    assert "".join(got) == "hi"
    assert captured["session"] == captured["run_id"]
    p = captured["prompt"]
    assert "Previous conversation:" in p and "User: q1" in p and "Assistant: a1" in p
    assert "<turn>" not in p and "<conversation_history>" not in p
    assert "<query>\nq2\n</query>" in p


@pytest.mark.asyncio
async def test_agent_chat_deep_one_shot(monkeypatch):
    """deep 折叠:system 恒 one-shot 模板(含 ## Final Conclusion),不再按迭代选三模板;
    continuation 回退用于查询词。"""
    captured = {}

    async def fake_generate_stream(prompt, *, target=None, options=None, session=None,
                                   session_name=None, run_id=None, context=None, retry=None, meta=None):
        captured["system"] = options.system_prompt
        captured["prompt"] = prompt
        yield "hi"

    monkeypatch.setattr(deepwiki, "generate_stream", fake_generate_stream)
    request = deepwiki.ChatCompletionRequest(
        repo_url="/tmp/gh-puller-chat-natural", type="local", language="en",
        messages=[deepwiki.ChatMessage(role="user", content="what is this repo about?"),
                  deepwiki.ChatMessage(role="assistant", content="half"),
                  deepwiki.ChatMessage(role="user", content="Continue the research",
                                       mode="deep_research")],
    )
    _ = [c async for c in deepwiki.AgentWikiPipeline().chat_stream(request)]
    assert "single-run Deep Research" in captured["system"]
    assert "## Final Conclusion" in captured["system"]
    assert "first iteration of a multi-turn" not in captured["system"]
    assert "<query>\nwhat is this repo about?\n</query>" in captured["prompt"]


@pytest.mark.asyncio
async def test_chat_and_codemap_dispatch_by_generator(monkeypatch):
    """chat/codemap 顶层分派:与 wiki 同开关(DEEPWIKI_GENERATOR)。"""
    calls = []

    async def fake_cc_chat(self, request):
        calls.append("cc")
        yield "c"

    async def fake_llm_chat(self, request):
        calls.append("llm")
        yield "l"

    async def fake_cc_codemap(self, request):
        calls.append("cc-codemap")
        yield "c"

    async def fake_llm_codemap(self, request):
        calls.append("llm-codemap")
        yield "l"

    monkeypatch.setattr(deepwiki.AgentWikiPipeline, "chat_stream", fake_cc_chat)
    monkeypatch.setattr(deepwiki.LlmWikiPipeline, "chat_stream", fake_llm_chat)
    monkeypatch.setattr(deepwiki.AgentWikiPipeline, "generate_codemap", fake_cc_codemap)
    monkeypatch.setattr(deepwiki.LlmWikiPipeline, "generate_codemap", fake_llm_codemap)
    chat_req = deepwiki.ChatCompletionRequest(
        repo_url="/tmp/x", type="local", language="en",
        messages=[deepwiki.ChatMessage(role="user", content="hi")],
    )
    codemap_req = deepwiki.CodeMapRequest(repo_url="/tmp/x", type="local", language="en", question="q")
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "cc")
    assert [c async for c in deepwiki.chat_stream(chat_req)] == ["c"]
    assert [ev async for ev in deepwiki.generate_codemap(codemap_req)] == ["c"]
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "llm")
    assert [c async for c in deepwiki.chat_stream(chat_req)] == ["l"]
    assert [ev async for ev in deepwiki.generate_codemap(codemap_req)] == ["l"]
    assert calls == ["cc", "cc-codemap", "llm", "llm-codemap"]


# ---------------------------------------------------------------------------
# codex 后端分派/选项(镜像 dsh 测试;假 codex_stream,零 SDK/网络/token)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_text_through_codex_stream(monkeypatch):
    """codex 后端镜像 cc/dsh 的迁移冒烟:归一化为 generate_stream(codex target)
    (label/run_id/context 透传);options 由 _codex_options 组装(隔离 home + 图 MCP)。"""
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "codex")
    calls = []

    async def fake_generate_stream(prompt, *, target=None, options=None, session=None,
                                   session_name=None, run_id=None, context=None, retry=None, meta=None):
        calls.append((target, options, session_name, prompt, run_id, context))
        yield "a"
        yield "b"

    monkeypatch.setattr(deepwiki, "generate_stream", fake_generate_stream)
    ctx = [{"type": "context/inject", "data": {"text": "n"}}]
    out = await deepwiki._agent_text(
        deepwiki.TargetInput(), "sys", "query", label="wiki:structure", run_id="r1", context=ctx
    )
    assert out == "ab"
    target, options, session_name, prompt, run_id, ctx_got = calls[0]
    assert (session_name, prompt, run_id, ctx_got) == ("wiki:structure", "query", "r1", ctx)
    assert target.generator == "codex"  # _agent_stream 已解析(env 缺省),透传 resolved target
    # 组装面:system_prompt → 适配器 base_instructions;高自由度缺省(full_access/auto_review);
    # 凭证/home/model 不在装配层(经 target 绑定)
    assert options.system_prompt == "sys"
    assert options.sandbox == "full_access" and options.approval_mode == "auto_review"
    assert not hasattr(options, "codex_home") and not hasattr(options, "token")
    assert not hasattr(options, "model")


def test_codex_options_config(tmp_path):
    """_codex_options 与 cc/dsh 同构:model 只认 target 绑定(envs 层无 CODEX_* 常量;
    凭证/home 零配置在适配器层);cwd 固定仓库根;图目录经 env.GRAPHIFY_OUT 绝对路径注入。"""
    repo = Repo(str(tmp_path), "local")
    opts = deepwiki._codex_options("sys", repo)
    assert not hasattr(opts, "model") and opts.cwd == str(tmp_path)
    assert opts.env == {"GRAPHIFY_OUT": str(deepwiki._graph_dir(repo))}
    opts2 = deepwiki._codex_options("sys", None)
    assert not hasattr(opts2, "cwd") and not hasattr(opts2, "env")  # repo 空:不固定 cwd/图
    assert not hasattr(opts2, "model")  # 缺省交给 SDK 缺省模型
    # 学 cc 凭证面:envs 无 CODEX_* 常量(防误引入"看起来必须配置"的 env 骨架)
    assert not hasattr(deepwiki.envs, "CODEX_HOME")
    assert not hasattr(deepwiki.envs, "CODEX_API_KEY")


def test_codex_home_isolation_and_graphify(tmp_path):
    """codex 隔离 home:config.toml 仅 graphify 单服务器 + env_vars 白名单,无用户配置面
    (与 cc setting_sources=[] / dsh 内置 cordis 同语义)。"""
    from gh_puller.agent.adapters import _codex_home_setup

    home = _codex_home_setup(str(tmp_path / "home"), graphify_command="python3", auth_src=False)
    text = Path(home, "config.toml").read_text(encoding="utf-8")
    assert text.startswith("[mcp_servers.graphify]")
    assert "graphify.serve" in text and "env_vars = [\"GRAPHIFY_OUT\"]" in text
    assert text.count("[mcp_servers.") == 1  # 无第三方服务器/无设置节(隔离边界)
    assert not (Path(home) / "auth.json").exists()  # auth_src=False:纯隔离无凭证态


def test_wiki_pipeline_codex_uses_agent(monkeypatch):
    """分派:codex 与 cc/dsh 同为 agent 路(AgentWikiPipeline);llm 路不受扰。"""
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "codex")
    assert isinstance(deepwiki._wiki_pipeline(), deepwiki.AgentWikiPipeline)
    assert isinstance(deepwiki._service_pipeline(), deepwiki.AgentWikiPipeline)


def test_agent_note_codex_uses_mcp_graphify():
    """图工具指引按后端切换:codex 与 dsh 同款 mcp__graphify__query_graph(隔离 config.toml 装载)。"""
    assert "mcp__graphify__query_graph" in deepwiki._agent_note("codex")
    assert "graphify_query" not in deepwiki._agent_note("codex")
