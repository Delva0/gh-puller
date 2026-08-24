"""gh_puller.deepwiki 引擎/任务层的本地测试。

不调 Claude agent(不依赖 API key / CLI):
- 环境变量要求:ANTHROPIC_API_KEY 不设;DEEPWIKI_ROOT 指向临时目录(见文件头)。
- 覆盖:纯函数层(结构解析 / 引用后处理 / snippet 定位 / JSON 修复)、Repo 克隆语义,
  与生成状态落盘/续跑(离线:生成函数全部 monkeypatch,真实写临时 wikicache)。
- HTTP 端点契约测试(TestClient)在 apps/webui/tests/test_app.py(独立项目)。
"""

import asyncio
import os
import tempfile

# envs 在模块导入时单点读取 —— 必须在 import gh_puller.deepwiki 前把产物根指向临时目录
os.environ.setdefault("DEEPWIKI_ROOT", tempfile.mkdtemp(prefix="deepwiki-test-"))

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


def test_pending_pages_subtracts_done():
    """纯函数:按结构顺序去掉已完成页;done 含不存在 id 时原样返回。"""
    structure = _make_structure(["p1", "p2", "p3"])
    assert [p.id for p in _pending_pages(structure, {"p1": _make_page("p1")})] == ["p2", "p3"]
    assert _pending_pages(structure, {p.id: _make_page(p.id) for p in structure.pages}) == []
    assert [p.id for p in _pending_pages(structure, {"pX": _make_page("pX")})] == ["p1", "p2", "p3"]


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
