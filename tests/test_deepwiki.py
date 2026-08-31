"""gh_puller.deepwiki 引擎层的本地测试。

不调 Claude agent(不依赖 API key / CLI):
- 环境变量要求:ANTHROPIC_API_KEY 不设;DEEPWIKI_ROOT/CC 配置钉临时目录(见 tests/conftest.py)。
- 覆盖:纯函数层(结构解析 / 引用后处理 / snippet 定位 / JSON 修复)、Repo 克隆语义、
  缓存与状态文件 IO 原语(离线:真实写临时 DEEPWIKI_ROOT,按 <repo_key>/ 项目文件夹布局)。
- wiki 任务 runtime(注册表/主流程/进度落盘投影)测试见
  apps/deepwiki-webui/server/tests/test_tasks.py;HTTP 端点契约测试见同目录 test_app.py。
"""

import contextlib
import dataclasses
import json
import os
from pathlib import Path

import pytest

from gh_puller import deepwiki
from gh_puller.agent import GENERATORS, RequestFailedError
from gh_puller.deepwiki import (
    WikiPage,
    WikiStructureModel,
    delete_resume_state,
    delete_wiki_cache,
    list_wiki_cache,
    read_resume_state,
    save_wiki_cache,
    write_resume_state,
)
from gh_puller.deepwiki import utils as deepwiki_utils
from gh_puller.deepwiki.codemap import _locate_snippet
from gh_puller.deepwiki.utils import adapt_generator, generator_digest
from gh_puller.deepwiki.wiki import (
    RepoUrlContext,
    _generator_cache_dir,
    _generator_cache_page_path,
    _generator_cache_structure_path,
    _produce_file,
    parse_wiki_structure,
    post_process_wiki_content,
    render_file_links,
    resume_state_path,
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
    secret = "ghp_SECRET_TOKEN_123"  # noqa: S105 - 测试假 token,非真实凭证
    repo = Repo("https://127.0.0.1:1/foo/bar.git", "github", access_token=secret)
    with pytest.raises(ValueError, match="unable to access") as ei:
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
    with pytest.raises(ValueError, match="No valid <wiki_structure> XML found"):
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
    assert "Relevant source files" in out or "[\u200bapp.py]" in out  # 详情块仍注入
    # local 下链接回退为裸路径(与 deepwiki-open 同)
    assert "app.py" in out


def test_render_file_links_canonical_escaped():
    """规范式文件链接行:label 转义 '[' / ']',URL 原样(github blob 路由)。"""
    ctx = RepoUrlContext(type="github", repo_url="https://github.com/foo/bar", default_branch="main")
    assert render_file_links(["docs/[x].md", "src/a.py"], ctx) == (
        "- [docs/\\[x\\].md](https://github.com/foo/bar/blob/main/docs/[x].md)\n"
        "- [src/a.py](https://github.com/foo/bar/blob/main/src/a.py)"
    )
    local_ctx = RepoUrlContext(type="local", repo_url="", default_branch="main")
    assert render_file_links(["src/a.py"], local_ctx) == "- [src/a.py](src/a.py)"


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
        id="wiki", title="T", description="", pages=[_make_page(p) for p in page_ids],
    )


def _make_request(owner: str, repo: str) -> dict:
    return {
        "repo_url": "/tmp/gh-puller-test-repo", "type": "local", "owner": owner,
        "repo": repo, "language": "en", "target": {}, "token": None,
    }


def _digest_of(choice: "dict | None") -> str:
    """选型 dict → 稳定摘要(测试与实现共用一个函数)。"""
    return generator_digest((choice or {}).get("generator"), (choice or {}).get("generator_config"))


def _repo_of(req: dict) -> Repo:
    """散装参数测试辅助:请求 dict → Repo(域对象携带 repo_url/type/token)。"""
    return Repo(req["repo_url"], req["type"], access_token=req.get("token"))


def _gen_kwargs(choice: dict | None) -> dict:
    """散装参数测试辅助:选型 dict(wire target)→ generator/generator_config 拆分 kwargs。"""
    c = choice or {}
    return {"generator": c.get("generator"), "generator_config": dict(c.get("generator_config") or {})}


async def _chat(req: dict) -> list[str]:
    """散装参数测试辅助:调用模块级 chat_stream(请求为 dict;单一生成器管道随 target)。"""
    return [c async for c in deepwiki.chat_stream(
        **_gen_kwargs(req["target"]), repo=_repo_of(req), messages=req["messages"],
        language=req.get("language", "en"),
        research_iteration=req.get("research_iteration", 1),
    )]


async def _codemap(req: dict) -> list[dict]:
    """散装参数测试辅助:调用模块级 generate_codemap 并解析 NDJSON 事件(请求为 dict)。"""
    return [json.loads(ev) async for ev in deepwiki.generate_codemap(
        **_gen_kwargs(req["target"]), repo=_repo_of(req), question=req["question"],
        language=req.get("language", "en"),
    )]



@pytest.mark.asyncio
async def test_wiki_task_state_roundtrip_atomic():
    """续跑状态(纯 dict)写/读/删往返:原子写无 .tmp 残留,且不被 list_wiki_cache 当成成品。"""
    request = _make_request("state-io", "demo")
    state = {
        "version": 1,
        "request": request,
        "status": TaskStatus.GENERATING,
        "wiki_structure": dataclasses.asdict(_make_structure(["p1", "p2"])),
        "generated_pages": {"p1": dataclasses.asdict(_make_page("p1"))},
        "default_branch": "main",
        "submitted_at": 1234567890,
        "error": None,
    }
    digest = generator_digest(request["target"])
    assert await write_resume_state("state-io", "demo", "local", "en", state, digest=digest) is True
    path = resume_state_path("state-io", "demo", "local", "en", digest=digest)
    assert os.path.exists(path)  # noqa: ASYNC240 - 测试内同步 stat 断言,微秒级阻塞无碍
    assert not os.path.exists(f"{path}.tmp")  # noqa: ASYNC240 - 测试内同步 stat 断言,微秒级阻塞无碍
    loaded = await read_resume_state("state-io", "demo", "local", "en", digest=digest)
    assert loaded is not None
    assert loaded == state  # 纯 dict json 往返(Enum 为 str 子类,判等成立)
    # 状态文件以 resume_ 前缀命名,不污染成品缓存列表
    assert await list_wiki_cache() == []
    assert await delete_resume_state("state-io", "demo", "local", "en", digest=digest) is True
    assert not os.path.exists(path)  # noqa: ASYNC240 - 测试内同步 stat 断言,微秒级阻塞无碍


@pytest.mark.asyncio
async def test_cache_new_layout_by_project_dir():
    """成品缓存落 deepwiki 根下 <repo_key>/ 文件夹:json 文件名带完整段(list 反解

    language/digest 的唯一无歧义来源);根下旁支目录(repos/graphify/agent_cache 的 md)
    不参与列表。
    """
    root = Path(deepwiki.envs.DEEPWIKI_ROOT)
    d1 = _digest_of({"generator": "cc"})
    d2 = _digest_of({"generator": "dsh"})
    record = {"wiki_structure": dataclasses.asdict(_make_structure(["p1"])), "generated_pages": {}}
    assert await save_wiki_cache("layout-io", "demo", "local", "en", record, digest=d1) is True
    assert await save_wiki_cache("layout-io", "demo", "local", "zh", record, digest=d2) is True
    proj_dir = root / "wiki" / "local_layout-io_demo"
    assert (proj_dir / f"cache_local_layout-io_demo_en_{d1}.json").exists()
    assert (proj_dir / f"cache_local_layout-io_demo_zh_{d2}.json").exists()
    # 旁支干扰:repos/graphify(根下,与 wiki/ 平级)与 generator_cache 缓存文件(md)均不计数
    (root / "repos" / "cache_local_layout-io_demo_en_zzzzzzzz.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "repos" / "cache_local_layout-io_demo_en_zzzzzzzz.json").write_text("{}")
    (root / "graphify").mkdir(exist_ok=True)
    (proj_dir / "generator_cache").mkdir(parents=True, exist_ok=True)
    (proj_dir / "generator_cache" / "local_layout-io_demo_00000000-structure.md").write_text("x")
    entries = await list_wiki_cache()
    assert len(entries) == 2, [e["id"] for e in entries]
    by_lang = {(e["owner"], e["repo"], e["repo_type"], e["language"]): e for e in entries}
    assert by_lang[("layout-io", "demo", "local", "en")]["digest"] == d1
    assert by_lang[("layout-io", "demo", "local", "zh")]["digest"] == d2


@pytest.mark.asyncio
async def test_cache_delete_removes_whole_project():
    """删除缓存 = 删整个项目目录(json + generator_cache 全清;同项目连删,用户语义)。"""
    request = _make_request("layout-io", "demo")
    d1 = generator_digest(request["target"])
    record = {"wiki_structure": dataclasses.asdict(_make_structure(["p1"])), "generated_pages": {}}
    assert await save_wiki_cache("layout-io", "demo", "local", "en", record, digest=d1) is True
    proj_dir = Path(deepwiki.envs.DEEPWIKI_ROOT) / "wiki" / "local_layout-io_demo"
    (proj_dir / "generator_cache").mkdir(parents=True, exist_ok=True)
    (proj_dir / "generator_cache" / "local_layout-io_demo_x-structure.md").write_text("x")
    assert await delete_wiki_cache("layout-io", "demo", "local", "en") is True
    assert not proj_dir.exists()
    assert await delete_wiki_cache("layout-io", "demo", "local", "en") is False


class _FakeGenerator:
    """适配器假类:零 SDK 构造副作用(真 Dsh/Codex 构造会写隔离目录,禁用);

    构造期捕获 config 供装配断言(与 agent 契约同形:config 整体注入)。
    """

    generator = "cc"  # 测试按 gid monkeypatch 覆盖

    def __init__(self, config):
        self.config = dict(config)


@pytest.mark.asyncio
async def test_agent_dispatch_by_generator(monkeypatch):
    """分派冒烟(原 generate_* 迁移测试接替):模块级 adapter 按 generator

    (wire target 拆包)经 GENERATORS[gid](config) 收敛构造;空选型 = 引擎内建 cc。
    """
    for gid in ("cc", "dsh", "codex", "opencode"):
        monkeypatch.setitem(GENERATORS, gid, _FakeGenerator)
        monkeypatch.setattr(_FakeGenerator, "generator", gid)
        inst = adapt_generator(**_gen_kwargs({"generator": gid}), system_prompt="s")
        assert inst.generator == gid and isinstance(inst, _FakeGenerator)
        assert inst.config["system_prompt"] == "s"  # 构造期注入(config 整体)
    monkeypatch.setattr(_FakeGenerator, "generator", "cc")
    inst = adapt_generator(**_gen_kwargs({}))  # 空选型 = 内建缺省 cc(缺省政策在 webui,引擎不读 env)
    assert inst.generator == "cc" and isinstance(inst, _FakeGenerator)


def test_dsh_options_config(monkeypatch, tmp_path):
    """经 _adapter 的 dsh 装配:cwd 固定仓库根,runtime_cwd 越过 checkout(.env 加载点);

    model/api_key/base_url 不在装配层(file 类配置随组合文件);system_prompt 为概念键;
    mcp_servers 白名单透传(app 经 generator_config 注入图工具桌,适配层零工具名)。
    """
    repo = Repo(str(tmp_path), "local")
    monkeypatch.setitem(GENERATORS, "dsh", _FakeGenerator)
    monkeypatch.setattr(_FakeGenerator, "generator", "dsh")
    fake_mcp = [{"id": "fake", "command": "x"}]
    cfg = adapt_generator(
        **_gen_kwargs({"generator": "dsh", "generator_config": {"mcp_servers": fake_mcp}}),
        system_prompt="sys", repo=repo,
    ).config
    assert cfg["cwd"] == str(tmp_path)
    assert "model" not in cfg  # model 随组合配置(file 类不在请求面)
    assert "api_key" not in cfg and "base_url" not in cfg
    assert cfg["session_root"].endswith("dsh-sessions")
    assert "dsh-runtime" in cfg["runtime_cwd"]  # 与任务 checkout 隔离(见 envs.DSH_RUNTIME_CWD)
    assert cfg["system_prompt"] == "sys"  # 契约键(agent dsh_fields 映射 DSH_SYSTEM_PROMPT)
    assert cfg["mcp_servers"] == fake_mcp
    cfg2 = adapt_generator(**_gen_kwargs({"generator": "dsh"}), system_prompt="sys").config
    assert "cwd" not in cfg2  # repo 空:不固定 cwd(走进程缺省)
    assert "mcp_servers" not in cfg2  # repo 空:不注入图工具桌(同"repo 非空才落 mcp")


def test_opencode_options_config(monkeypatch, tmp_path):
    """经 adapter 的 opencode 装配:cwd 固定仓库根,auto 恒 True(generator_config 无此轴),

    system_prompt 概念键透传(→ 生成器临时 instructions 文件),mcp_servers/env 白名单
    透传(app 经 generator_config 注入图工具桌);repo 空不注入。
    """
    repo = Repo(str(tmp_path), "local")
    monkeypatch.setitem(GENERATORS, "opencode", _FakeGenerator)
    monkeypatch.setattr(_FakeGenerator, "generator", "opencode")
    fake_mcp = [{"id": "fake", "command": "x"}]
    cfg = adapt_generator(
        **_gen_kwargs({"generator": "opencode",
                       "generator_config": {"mcp_servers": fake_mcp,
                                            "env": {"GRAPHIFY_OUT": "/g"},
                                            "model": "deepseek/deepseek-chat"}}),
        system_prompt="sys", repo=repo,
    ).config
    assert cfg["cwd"] == str(tmp_path)  # 仓库根固定
    assert cfg["auto"] is True  # 无头缺省(引擎置位;与 generator_config 传入无关)
    assert cfg["system_prompt"] == "sys"
    assert cfg["mcp_servers"] == fake_mcp
    assert cfg["env"] == {"GRAPHIFY_OUT": "/g"}
    assert cfg["model"] == "deepseek/deepseek-chat"  # 原样随传
    cfg2 = adapt_generator(**_gen_kwargs({"generator": "opencode"}), system_prompt="sys").config
    assert "cwd" not in cfg2 and "mcp_servers" not in cfg2  # repo 空:不固定 cwd/不注入工具桌


def test_agent_options_cc_setting_sources_isolated(monkeypatch):
    """cc 完全隔离本地 claude 配置(setting_sources=[]):用户级 MCP/skills/hooks 不掺入 agent。

    """
    monkeypatch.setitem(GENERATORS, "cc", _FakeGenerator)
    cfg = adapt_generator(**_gen_kwargs({}), system_prompt="sys").config
    assert cfg["setting_sources"] == []
    assert cfg["system_prompt"] == "sys"


@pytest.mark.asyncio
async def test_chat_stream_history_trim_no_context(monkeypatch):
    """chat 历史裁剪仍发生(输入过大省略历史);引擎不再传 context(假日志事件已清除,

    监控由适配器内 EventRecorder 发布);run_id 关联会话组。
    """
    captured = {}

    @contextlib.asynccontextmanager
    async def fake_session(self, **kw):  # 零 SDK 客户端进入占位(会话元数据在 session)
        captured.update(session=kw.get("session_name"), run_id=kw.get("run_id"))
        yield self

    async def fake_stream(self, prompt):
        captured.update(prompt=prompt)
        yield "hi"

    monkeypatch.setattr(GENERATORS["cc"], "session", fake_session)  # 适配器单例直连(agent 路)
    monkeypatch.setattr(GENERATORS["cc"], "stream", fake_stream)
    monkeypatch.setattr(deepwiki.envs, "CHAT_TOKEN_LIMIT_ESTIMATE", 0)  # 估算必超 → 触发裁剪
    request = {
        "repo_url": "/tmp/deepwiki-chat-test", "type": "local", "language": "en",
        "target": {}, "token": None,
        "messages": [{"role": "user", "content": "q1"},
                     {"role": "assistant", "content": "a1"},
                     {"role": "user", "content": "q2"}],
    }
    got = await _chat(request)
    assert "".join(got) == "hi"
    assert captured["run_id"] == captured["session"]  # 会话组名与监控名同一来源(chat:<repo>)
    assert captured["run_id"].startswith("chat:")
    assert "context" not in captured  # 引擎不再传 context
    assert "<query>\nq2\n</query>" in captured["prompt"]
    assert "Previous conversation:" not in captured["prompt"]  # 被裁剪
    assert "<conversation_history>" not in captured["prompt"]


# ---------------------------------------------------------------------------
# 生成器管道(离线:缓存文件预置 / 执行全部 monkeypatch)
# ---------------------------------------------------------------------------


def test_generator_cache_path_naming():
    request = {"repo_url": "/x", "type": "local", "owner": "local",
               "repo": "deepwiki-open", "language": "en", "target": {}}
    project_key = deepwiki.repo_key_of("local", "local", "deepwiki-open")
    prefix = f"{project_key}_{_digest_of(request['target'])}"
    gen_kw = _gen_kwargs(request["target"])
    assert _generator_cache_structure_path(project_key, **gen_kw).name == f"{prefix}-structure.md"
    assert _generator_cache_page_path(project_key, "page-1", **gen_kw).name == f"{prefix}-page-1.md"
    assert _generator_cache_page_path(project_key, "overview", **gen_kw).name == \
        f"{prefix}-page_overview.md"


def test_sanitize_page_id_no_escape():
    r = _make_request("sanitize-io", "demo")
    project_key = deepwiki.repo_key_of(r["type"], r["owner"], r["repo"])
    out = _generator_cache_page_path(project_key, "../../evil", **_gen_kwargs(r["target"]))
    assert out.parent == _generator_cache_dir(project_key, **_gen_kwargs(r["target"]))
    assert out.relative_to(_generator_cache_dir(project_key, **_gen_kwargs(r["target"]))).parent == Path(".")


def test_options_cc_cache_write_mode(monkeypatch, tmp_path):
    """写入模式:cwd 固定仓库根,add_dirs 指向生成器缓存目录,acceptEdits + 写工具;

    默认模式(chat/codemap):cwd + Read/Grep/Glob 自读代码,无 Write/写目录/acceptEdits;
    model/凭证不在装配层(经 target 绑定);allowed_tools 图工具名经 generator_config
    注入(app 侧 runtime_config 组装),引擎并入基座读工具。
    """
    repo = Repo(str(tmp_path), "local")
    monkeypatch.setitem(GENERATORS, "cc", _FakeGenerator)
    graph_tools = ["graphify_query", "mcp__graphify__graphify_query"]
    cfg = adapt_generator(
        **_gen_kwargs({"generator_config": {"allowed_tools": graph_tools}}),
        system_prompt="", repo=repo,
        generator_cache_dir=str(tmp_path / "out"), generator_cache_write_mode=True,
    ).config
    assert cfg["cwd"] == str(tmp_path)
    assert cfg["add_dirs"] == [str(tmp_path / "out")]
    assert cfg["permission_mode"] == "acceptEdits"
    assert "model" not in cfg  # model 随配置文件(file 类不在装配层注入)
    for t in ("Read", "Grep", "Glob", "Write", *graph_tools):
        assert t in cfg["allowed_tools"], t
    cfg2 = adapt_generator(
        **_gen_kwargs({"generator_config": {"allowed_tools": graph_tools}}),
        system_prompt="", repo=repo,
    ).config
    assert cfg2["cwd"] == str(tmp_path)
    assert "add_dirs" not in cfg2 and "permission_mode" not in cfg2  # 缺键 → SDK 缺省
    for t in ("Read", "Grep", "Glob", *graph_tools):
        assert t in cfg2["allowed_tools"], t
    assert "Write" not in cfg2["allowed_tools"]


def test_resolve_generator_default_cc():
    """空选型默认 = 引擎内建 cc(缺省政策已迁 webui,引擎不再读 env)。"""
    assert deepwiki_utils.resolve_generator()[0] == "cc"


# ---------------------------------------------------------------------------
# 生成器管道(自然历史 + deep 折叠)与分派
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_chat_natural_history(monkeypatch):
    """agent chat:历史自然转写(无 <turn>/<conversation_history> 伪标签)。"""
    captured = {}

    @contextlib.asynccontextmanager
    async def fake_session(self, *, session_name=None, run_id=None, **kw):
        captured.update(system=self.config["system_prompt"], session=session_name, run_id=run_id)
        yield self

    async def fake_stream(self, prompt):
        captured.update(prompt=prompt)
        yield "hi"

    monkeypatch.setattr(GENERATORS["cc"], "session", fake_session)  # 适配器单例直连
    monkeypatch.setattr(GENERATORS["cc"], "stream", fake_stream)
    request = {
        "repo_url": "/tmp/gh-puller-chat-natural", "type": "local", "language": "en",
        "target": {}, "token": None,
        "messages": [{"role": "user", "content": "q1"},
                     {"role": "assistant", "content": "a1"},
                     {"role": "user", "content": "q2"}],
    }
    got = await _chat(request)
    assert "".join(got) == "hi"
    assert captured["session"] == captured["run_id"]
    p = captured["prompt"]
    assert "Previous conversation:" in p and "User: q1" in p and "Assistant: a1" in p
    assert "<turn>" not in p and "<conversation_history>" not in p
    assert "<query>\nq2\n</query>" in p


@pytest.mark.asyncio
async def test_agent_chat_deep_one_shot(monkeypatch):
    """deep 折叠:system 恒 one-shot 模板(含 ## Final Conclusion),不再按迭代选三模板;

    continuation 回退用于查询词。
    """
    captured = {}

    @contextlib.asynccontextmanager
    async def fake_session(self, **kw):  # 零 SDK 客户端进入占位
        yield self

    async def fake_stream(self, prompt):
        captured["system"] = self.config["system_prompt"]
        captured["prompt"] = prompt
        yield "hi"

    monkeypatch.setattr(GENERATORS["cc"], "session", fake_session)  # 适配器单例直连
    monkeypatch.setattr(GENERATORS["cc"], "stream", fake_stream)
    request = {
        "repo_url": "/tmp/gh-puller-chat-natural", "type": "local", "language": "en",
        "target": {}, "token": None,
        "messages": [{"role": "user", "content": "what is this repo about?"},
                     {"role": "assistant", "content": "half"},
                     {"role": "user", "content": "Continue the research",
                      "mode": "deep_research"}],
    }
    _ = await _chat(request)
    assert "single-run Deep Research" in captured["system"]
    assert "## Final Conclusion" in captured["system"]
    assert "first iteration of a multi-turn" not in captured["system"]
    assert "<query>\nwhat is this repo about?\n</query>" in captured["prompt"]


@pytest.mark.asyncio
async def test_chat_and_codemap_single_pipeline(monkeypatch):
    """chat/codemap 顶层恒走单一生成器管道:不再按 generator 分辨(cc/llm 同路)。"""
    calls = []

    async def fake_chat(**kwargs):
        calls.append("chat")
        yield "c"

    async def fake_codemap(**kwargs):
        calls.append("codemap")
        yield "c"

    monkeypatch.setattr(deepwiki.chat, "_chat", fake_chat)
    monkeypatch.setattr(deepwiki.codemap, "_codemap", fake_codemap)
    chat_req = {
        "repo_url": "/tmp/x", "type": "local", "language": "en",
        "target": {}, "token": None,
        "messages": [{"role": "user", "content": "hi"}],
    }
    codemap_req = {
        "repo_url": "/tmp/x", "type": "local", "language": "en",
        "target": {}, "token": None, "question": "q",
    }
    for gid in ("cc", "llm"):  # llm 亦然(自然失败在管道内适配器契约,不另设分派)
        assert [c async for c in deepwiki.chat_stream(
            **_gen_kwargs({"generator": gid}), repo=_repo_of(chat_req), messages=chat_req["messages"],
            language="en",
        )] == ["c"]
        assert [ev async for ev in deepwiki.generate_codemap(
            **_gen_kwargs({"generator": gid}), repo=_repo_of(codemap_req),
            question=codemap_req["question"], language="en",
        )] == ["c"]
    assert calls == ["chat", "codemap", "chat", "codemap"]


# ---------------------------------------------------------------------------
# codex 后端分派/选项(镜像 dsh 测试;假 codex_stream,零 SDK/网络/token)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_produce_file_wraps_request_failure(monkeypatch, tmp_path):
    """RequestFailedError → RuntimeError("generator 执行失败: ...")(原 generate_* file 分支

    的包装收进 _produce_file;__cause__ 保留原始 SDK 失败)。
    """

    @contextlib.asynccontextmanager
    async def fake_session(self, **kw):  # 零 SDK 客户端进入占位
        yield self

    async def boom_stream(self, prompt, **kw):
        raise RequestFailedError("sdk exploded")
        yield  # 保持 async generator

    monkeypatch.setattr(GENERATORS["cc"], "session", fake_session)  # 适配器单例直连
    monkeypatch.setattr(GENERATORS["cc"], "stream", boom_stream)
    out_path = tmp_path / "out" / "page.md"
    with pytest.raises(RuntimeError) as ei:
        await _produce_file(GENERATORS["cc"]({}), "", out_path,
                            label="wiki:page:p1", run_id="r1")
    assert str(ei.value) == "generator 执行失败: sdk exploded"
    assert isinstance(ei.value.__cause__, RequestFailedError)
    assert not out_path.exists()  # 失败即无产出文件


@pytest.mark.asyncio
async def test_agent_chat_wraps_request_failure_in_degrade(monkeypatch):
    """chat 降级路径同样先包装再降级(客户端可见串原样:"(抱歉，本次请求处理失败： ...)")。"""
    @contextlib.asynccontextmanager
    async def fake_session(self, **kw):  # 零 SDK 客户端进入占位
        yield self

    async def boom_stream(self, prompt, **kw):
        raise RequestFailedError("sdk exploded")
        yield  # 保持 async generator

    monkeypatch.setattr(GENERATORS["cc"], "session", fake_session)  # 适配器单例直连
    monkeypatch.setattr(GENERATORS["cc"], "stream", boom_stream)
    request = {
        "repo_url": "/tmp/gh-puller-chat-wrap", "type": "local", "language": "en",
        "target": {}, "token": None,
        "messages": [{"role": "user", "content": "hi"}],
    }
    got = "".join(await _chat(request))
    # 客户端可见降级串逐字节(wire 契约):半角逗号/冒号
    assert "(抱歉,本次请求处理失败: generator 执行失败: sdk exploded)" in got


def test_codex_options_config(monkeypatch, tmp_path):
    """经 _adapter 的 codex 装配:config 原样随传(不读文件);

    cwd 固定仓库根;env.GRAPHIFY_OUT / mcp_servers 图工具桌经 generator_config
    原样透传(app 侧 runtime_config 注入;repo 空时无 cwd/图位)。
    """
    repo = Repo(str(tmp_path), "local")
    monkeypatch.setitem(GENERATORS, "codex", _FakeGenerator)
    monkeypatch.setattr(_FakeGenerator, "generator", "codex")
    fake_mcp = [{"id": "fake", "command": "x"}]
    cfg = adapt_generator(
        **_gen_kwargs({"generator": "codex", "generator_config": {
            "mcp_servers": fake_mcp, "env": {"GRAPHIFY_OUT": "/tmp/g"}}}),
        system_prompt="sys", repo=repo,
    ).config
    assert "model" not in cfg and cfg["cwd"] == str(tmp_path)
    assert cfg["env"] == {"GRAPHIFY_OUT": "/tmp/g"}
    assert cfg["mcp_servers"] == fake_mcp
    assert cfg["sandbox"] == "full_access" and cfg["approval_mode"] == "auto_review"
    cfg2 = adapt_generator(**_gen_kwargs({"generator": "codex"}), system_prompt="sys").config
    assert "cwd" not in cfg2 and "env" not in cfg2 and "mcp_servers" not in cfg2  # repo 空:不固定 cwd/图
    assert "model" not in cfg2  # 缺省交给 SDK 缺省模型
    # 学 cc 凭证面:envs 无 CODEX_* 常量(防误引入"看起来必须配置"的 env 骨架)
    assert not hasattr(deepwiki.envs, "CODEX_HOME")
    assert not hasattr(deepwiki.envs, "CODEX_API_KEY")
