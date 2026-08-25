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
    Repo,
    RepoUrlContext,
    TaskStatus,
    WikiPage,
    WikiStructureModel,
    WikiTask,
    WikiTaskRequest,
    WikiTaskState,
    _clone_url_with_token,
    _extract_json,
    _generate_pages,
    _locate_snippet,
    _path_is_url,
    _pending_pages,
    _repair_json,
    _wiki_state_path,
    delete_wiki_task_state,
    list_wiki_cache,
    parse_wiki_structure,
    post_process_wiki_content,
    read_wiki_task_state,
    write_wiki_task_state,
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
    path = _wiki_state_path("state-io", "demo", "local", "en")
    assert os.path.exists(path)
    assert not os.path.exists(f"{path}.tmp")
    loaded = await read_wiki_task_state("state-io", "demo", "local", "en")
    assert loaded is not None
    assert loaded.model_dump() == state.model_dump()
    # 状态文件以 deepwiki_taskstate_ 前缀命名,不污染成品缓存列表
    assert await list_wiki_cache() == []
    assert await delete_wiki_task_state("state-io", "demo", "local", "en") is True
    assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_agent_text_through_cc_stream(monkeypatch):
    """迁移冒烟:deepwiki.agent 调用归一化为 agent.cc_stream(label 透传)."""
    calls = []

    async def fake_cc_stream(options, prompt, *, session=None, session_name=None, meta=None):
        calls.append((session_name, prompt))
        yield "a"
        yield "b"

    monkeypatch.setattr(deepwiki, "cc_stream", fake_cc_stream)
    out = await deepwiki._agent_text("sys", "query", label="wiki:structure")
    assert out == "ab"
    assert calls == [("wiki:structure", "query")]


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
    """同仓库再次提交:命中落盘状态 → resumed=True,复用结构/进度恢复;state 快照胜出。"""
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

    async def fake_generate(task):
        task.status = TaskStatus.COMPLETED

    monkeypatch.setattr(deepwiki, "generate_repo_wiki", fake_generate)
    monkeypatch.setattr(deepwiki, "_WIKI_TASK_TTL_SECONDS", 0.2)

    key = "local_resume-io_demo"
    fresh = _make_request("resume-io", "demo")
    fresh.comprehensive = False  # 与首次不一致:落盘快照应胜出
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
        await task.task
        assert task.status == TaskStatus.COMPLETED
    finally:
        await deepwiki.registry.remove(key)
        await delete_wiki_task_state("resume-io", "demo", "local", "en")
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
    assert deepwiki._agent_cache_structure_path(request).name == "local_local_deepwiki-open-structure.md"
    assert deepwiki._agent_cache_page_path(request, "page-1").name == "local_local_deepwiki-open-page-1.md"
    assert deepwiki._agent_cache_page_path(request, "overview").name == "local_local_deepwiki-open-page_overview.md"


def test_sanitize_page_id_no_escape():
    r = _make_request("sanitize-io", "demo")
    out = deepwiki._agent_cache_page_path(r, "../../evil")
    assert out.parent == deepwiki._agent_cache_dir(r)
    assert out.relative_to(deepwiki._agent_cache_dir(r)).parent == Path(".")


def test_agent_options_cc_delivery(tmp_path):
    """写入模式:cwd 固定仓库根,add_dirs 指向交付目录,acceptEdits + 读工具放开;
    默认模式只有 cwd(chat/codemap 场景),无写权限。"""
    repo = Repo(str(tmp_path), "local")
    opts = deepwiki._agent_options(
        "", repo, model="m", agent_output_dir=str(tmp_path / "out"), agent_write_mode=True
    )
    assert opts.cwd == str(tmp_path)
    assert opts.add_dirs == [str(tmp_path / "out")]
    assert opts.permission_mode == "acceptEdits"
    for t in ("Read", "Grep", "Glob", "Write", "graphify_query"):
        assert t in opts.allowed_tools, t
    opts2 = deepwiki._agent_options("", repo, model="m")
    assert opts2.cwd == str(tmp_path)
    assert opts2.add_dirs == []
    assert opts2.permission_mode is None


@pytest.mark.asyncio
async def test_dispatch_structure_by_generator(monkeypatch):
    """DEEPWIKI_GENERATOR 分派:cc 只走 _determine_structure_cc,llm 只走 _determine_structure_llm。"""
    calls = []

    async def fake_llm(task, repo, files, readme):
        calls.append("llm")
        return _make_structure(["p1"])

    async def fake_cc(task, repo, files):
        calls.append("cc")
        return _make_structure(["p1"])

    monkeypatch.setattr(deepwiki, "_determine_structure_llm", fake_llm)
    monkeypatch.setattr(deepwiki, "_determine_structure_cc", fake_cc)
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
    struct_path = deepwiki._agent_cache_structure_path(request)
    struct_path.parent.mkdir(parents=True, exist_ok=True)
    struct_path.write_text(_STRUCT_XML, encoding="utf-8")

    async def boom(*args, **kwargs):
        raise AssertionError("agent must not be called")

    monkeypatch.setattr(deepwiki, "_agent_write_file", boom)
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "cc")
    repo = Repo("/tmp/gh-puller-test-repo", "local")
    s = await deepwiki._determine_structure_cc(WikiTask(request=request), repo, ["src/a.py"])
    assert [p.id for p in s.pages] == ["p1", "p2", "p3"]


@pytest.mark.asyncio
async def test_determine_structure_cc_calls_agent_no_inline(tmp_path, monkeypatch):
    """cc 结构走 agent 并读回文件;提示词只有文件树路径,不内联任何文件内容。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("SECRET_CODE_BODY", encoding="utf-8")
    (tmp_path / "README.md").write_text("SECRET_README_BODY", encoding="utf-8")
    request = WikiTaskRequest(repo_url=str(tmp_path), type="local", owner="local", repo="demo", language="en")
    captured = {}

    async def fake_write(system_prompt, prompt, repo, model, out_path, label=None):
        captured["prompt"] = prompt
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(_STRUCT_XML, encoding="utf-8")
        return _STRUCT_XML

    monkeypatch.setattr(deepwiki, "_agent_write_file", fake_write)
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "cc")
    repo = Repo(str(tmp_path), "local")
    s = await deepwiki._determine_structure_cc(WikiTask(request=request), repo, ["src/a.py"])
    assert [p.id for p in s.pages] == ["p1", "p2", "p3"]
    assert "<file_tree>" in captured["prompt"]
    assert "src/a.py" in captured["prompt"]
    assert "SECRET_CODE_BODY" not in captured["prompt"]
    assert "SECRET_README_BODY" not in captured["prompt"]


@pytest.mark.asyncio
async def test_page_cc_skips_when_file_exists(monkeypatch):
    """cc 页续跑:page_<id>.md 已存在则直接读回(文件为权威),不启动 agent。"""
    request = _make_request("cc-page", "demo")
    out = deepwiki._agent_cache_page_path(request, "p1")
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

    async def fake_write(system_prompt, prompt, repo, model, out_path, label=None):
        captured["prompt"] = prompt
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
async def test_page_llm_inlines_files(tmp_path, monkeypatch):
    """llm 页:内联相关文件内容入 payload(无 agent/无工具),单次流式。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("SECRET_CODE_BODY", encoding="utf-8")
    captured = {}

    async def fake_llm_stream(*, url, payload, api_key=None, session_name=None, **kw):
        captured["url"] = url
        captured["api_key"] = api_key
        captured["session"] = session_name
        captured["payload"] = payload
        yield "LLM-CONTENT"

    monkeypatch.setattr(deepwiki, "llm_stream", fake_llm_stream)
    request = _make_request("llm-page", "demo")
    task = WikiTask(request=request)
    task.default_branch = "main"
    page = WikiPage(
        id="p1", title="Page p1", content="", filePaths=["src/a.py"],
        importance="medium", relatedPages=[],
    )
    repo = Repo(str(tmp_path), "local")
    content = await deepwiki._generate_page_llm(task, repo, page, "- [src/a.py](src/a.py)")
    assert content == "LLM-CONTENT"
    assert captured["session"] == "wiki:page:p1"
    user_msg = captured["payload"]["messages"][0]["content"]
    assert '<file path="src/a.py">' in user_msg
    assert "SECRET_CODE_BODY" in user_msg
    assert captured["payload"]["model"] == deepwiki.envs.DEEPWIKI_LLM_MODEL


@pytest.mark.asyncio
async def test_page_llm_degrades_over_limit(tmp_path, monkeypatch):
    """llm 页超限降级:内联累计超过 token 上限 → 无内联块,仅文件链接 + 降级 note。"""
    for i in range(5):
        d = tmp_path / "src"
        d.mkdir(exist_ok=True)
        (d / f"f{i}.py").write_text("x" * 15000, encoding="utf-8")
    captured = {}

    async def fake_llm_stream(*, url, payload, api_key=None, session_name=None, **kw):
        captured["payload"] = payload
        yield "DEGRADED"

    monkeypatch.setattr(deepwiki, "llm_stream", fake_llm_stream)
    task = WikiTask(request=_make_request("llm-deg", "demo"))
    task.default_branch = "main"
    page = WikiPage(
        id="p1", title="P", content="", filePaths=[f"src/f{i}.py" for i in range(5)],
        importance="medium", relatedPages=[],
    )
    repo = Repo(str(tmp_path), "local")
    assert await deepwiki._generate_page_llm(task, repo, page, "- [src/f0.py](src/f0.py)") == "DEGRADED"
    user_msg = captured["payload"]["messages"][0]["content"]
    assert "<file path=" not in user_msg
    assert "输入超限" in user_msg


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
    struct_path = deepwiki._agent_cache_structure_path(request)
    struct_path.parent.mkdir(parents=True, exist_ok=True)
    struct_path.write_text(_STRUCT_XML, encoding="utf-8")
    for pid in ("p1", "p2", "p3"):
        deepwiki._agent_cache_page_path(request, pid).write_text(
            f"## {pid}-REAL\n\nbody\n", encoding="utf-8"
        )

    async def boom(*args, **kwargs):
        raise AssertionError("agent must not be called")

    monkeypatch.setattr(deepwiki, "_agent_write_file", boom)
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_GENERATOR", "cc")
    task = WikiTask(request=request)
    await deepwiki.generate_repo_wiki(task)
    assert task.status == TaskStatus.COMPLETED
    cache_path = Path(deepwiki._WIKI_CACHE_DIR) / "deepwiki_cache_local_local_demo_en.json"
    assert cache_path.exists()
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert set(data["generated_pages"]) == {"p1", "p2", "p3"}
    for pid in ("p1", "p2", "p3"):
        assert f"{pid}-REAL" in data["generated_pages"][pid]["content"]
    assert not Path(deepwiki._wiki_state_path("local", "demo", "local", "en")).exists()
    assert task.pages_done == 3


def test_save_llm_restores_generator_default():
    """env 缺省即 cc(默认走 agent 路径)。"""
    assert deepwiki.envs.DEEPWIKI_GENERATOR == "cc"


@pytest.mark.asyncio
async def test_determine_structure_llm_streams(monkeypatch):
    """llm 路结构:单次 llm_stream 补全(无 agent/无工具),解析 XML。"""
    captured = {}

    async def fake_llm_stream(*, url, payload, api_key=None, session_name=None, **kw):
        captured["session"] = session_name
        captured["payload"] = payload
        yield _STRUCT_XML

    monkeypatch.setattr(deepwiki, "llm_stream", fake_llm_stream)
    task = WikiTask(request=_make_request("llm-struct", "demo"))
    repo = Repo("/tmp/gh-puller-test-repo", "local")
    s = await deepwiki._determine_structure_llm(task, repo, ["app.py"], "")
    assert [p.id for p in s.pages] == ["p1", "p2", "p3"]
    assert captured["session"] == "wiki:structure"
    # 输入仅文件树路径(含 <file_tree> 标签),无任何文件内容内联
    assert "<file_tree>" in captured["payload"]["messages"][0]["content"]
    assert "app.py" in captured["payload"]["messages"][0]["content"]
