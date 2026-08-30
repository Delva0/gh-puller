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
from gh_puller.deepwiki.utils import adapter, generator_digest
from gh_puller.deepwiki.wiki import (
    RepoUrlContext,
    _wiki_pipeline,
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


class _FakeGraph:
    """图服务假类(注入 generator_config['graph']);解析/窗口属 webui generators 模块,假件只回块。

    ready 可钉(未索引路径);context 记录 question(替代旧 deepwiki.graphify.query 捕获),
    可钉 blocks(行窗块,供 format_subgraph_context / chunk_count)或 error(→
    "代码图谱不可用: ..." 同式包装,检索失败须 raise 语义)。
    """

    def __init__(self, *, ready: bool = True, blocks=None, error: str = ""):
        self.ready_value = ready
        self.blocks = list(blocks or [])
        self.error = error
        self.questions: list[str] = []

    def ready(self, repo) -> bool:
        return self.ready_value

    async def context(self, repo, question) -> dict:
        self.questions.append(question)
        if self.error:
            raise RuntimeError(f"代码图谱不可用: {self.error}")
        return {"hits": {}, "blocks": self.blocks}


def _gen_kwargs(choice: dict | None, *, graph: _FakeGraph | None = None, note: str = "") -> dict:
    """散装参数测试辅助:选型 dict(wire target)→ generator/generator_config 拆分 kwargs。

    llm 路须注入图服务(graph 假件;引擎经 generator_config['graph'] 取用);
    note = 上层注入的工具指引文本(引擎零工具假设,指引由上层传)。
    """
    c = choice or {}
    gc = dict(c.get("generator_config") or {})
    if graph is not None:
        gc["graph"] = graph
    if note:
        gc["tool_note"] = note
    return {"generator": c.get("generator"), "generator_config": gc}


async def _chat(req: dict, graph: _FakeGraph | None = None, note: str = "") -> list[str]:
    """散装参数测试辅助:调用模块级 chat_stream(请求为 dict;agent/llm 分派随 target)。"""
    return [c async for c in deepwiki.chat_stream(
        **_gen_kwargs(req["target"], graph=graph, note=note), repo=_repo_of(req), messages=req["messages"],
        language=req.get("language", "en"),
        research_iteration=req.get("research_iteration", 1),
    )]


async def _codemap(req: dict, graph: _FakeGraph | None = None) -> list[dict]:
    """散装参数测试辅助:调用模块级 generate_codemap 并解析 NDJSON 事件(请求为 dict)。"""
    return [json.loads(ev) async for ev in deepwiki.generate_codemap(
        **_gen_kwargs(req["target"], graph=graph), repo=_repo_of(req), question=req["question"],
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
    d2 = _digest_of({"generator": "llm"})
    record = {"wiki_structure": dataclasses.asdict(_make_structure(["p1"])), "generated_pages": {}}
    assert await save_wiki_cache("layout-io", "demo", "local", "en", record, digest=d1) is True
    assert await save_wiki_cache("layout-io", "demo", "local", "zh", record, digest=d2) is True
    proj_dir = root / "wiki" / "local_layout-io_demo"
    assert (proj_dir / f"cache_local_layout-io_demo_en_{d1}.json").exists()
    assert (proj_dir / f"cache_local_layout-io_demo_zh_{d2}.json").exists()
    # 旁支干扰:repos/graphify(根下,与 wiki/ 平级)与 agent_cache 交付件(md)均不计数
    (root / "repos" / "cache_local_layout-io_demo_en_zzzzzzzz.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "repos" / "cache_local_layout-io_demo_en_zzzzzzzz.json").write_text("{}")
    (root / "graphify").mkdir(exist_ok=True)
    (proj_dir / "agent_cache").mkdir(parents=True, exist_ok=True)
    (proj_dir / "agent_cache" / "local_layout-io_demo_00000000-structure.md").write_text("x")
    entries = await list_wiki_cache()
    assert len(entries) == 2, [e["id"] for e in entries]
    by_lang = {(e["owner"], e["repo"], e["repo_type"], e["language"]): e for e in entries}
    assert by_lang[("layout-io", "demo", "local", "en")]["digest"] == d1
    assert by_lang[("layout-io", "demo", "local", "zh")]["digest"] == d2


@pytest.mark.asyncio
async def test_cache_delete_removes_whole_project():
    """删除缓存 = 删整个项目目录(json + agent_cache 全清;同项目连删,用户语义)。"""
    request = _make_request("layout-io", "demo")
    d1 = generator_digest(request["target"])
    record = {"wiki_structure": dataclasses.asdict(_make_structure(["p1"])), "generated_pages": {}}
    assert await save_wiki_cache("layout-io", "demo", "local", "en", record, digest=d1) is True
    proj_dir = Path(deepwiki.envs.DEEPWIKI_ROOT) / "wiki" / "local_layout-io_demo"
    (proj_dir / "agent_cache").mkdir(parents=True, exist_ok=True)
    (proj_dir / "agent_cache" / "local_layout-io_demo_x-structure.md").write_text("x")
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
    for gid in ("cc", "dsh", "codex"):
        monkeypatch.setitem(GENERATORS, gid, _FakeGenerator)
        monkeypatch.setattr(_FakeGenerator, "generator", gid)
        inst = adapter(**_gen_kwargs({"generator": gid}), system_prompt="s")
        assert inst.generator == gid and isinstance(inst, _FakeGenerator)
        assert inst.config["system_prompt"] == "s"  # 构造期注入(config 整体)
    monkeypatch.setattr(_FakeGenerator, "generator", "cc")
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_CC_CONFIG", "")  # conftest 钉了 tmp,清掉以测"无配置"
    inst = adapter(**_gen_kwargs({}))  # 空选型 = 内建缺省 cc(缺省政策在 webui,引擎不读 env)
    assert inst.generator == "cc" and isinstance(inst, _FakeGenerator)
    assert "config_path" not in inst.config  # 无配置:概念键缺失 → SDK 缺省隔离


def test_dsh_options_config(monkeypatch, tmp_path):
    """经 _adapter 的 dsh 装配:cwd 固定仓库根,runtime_cwd 越过 checkout(.env 加载点);

    model/api_key/base_url 不在装配层(file 类配置随组合文件);system_prompt 为概念键;
    mcp_servers 白名单透传(app 经 generator_config 注入图工具桌,适配层零工具名)。
    """
    repo = Repo(str(tmp_path), "local")
    monkeypatch.setitem(GENERATORS, "dsh", _FakeGenerator)
    monkeypatch.setattr(_FakeGenerator, "generator", "dsh")
    fake_mcp = [{"id": "fake", "command": "x"}]
    cfg = adapter(
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
    assert "config_path" not in cfg  # 默认不传 → agent 缺省隔离组合
    cfg2 = adapter(**_gen_kwargs({"generator": "dsh"}), system_prompt="sys").config
    assert "cwd" not in cfg2  # repo 空:不固定 cwd(走进程缺省)
    assert "mcp_servers" not in cfg2  # repo 空:不注入图工具桌(同"repo 非空才落 mcp")
    # file 类契约:config_path(经 resolve 解析;env DEEPWIKI_DSH_CORDIS 为 env 缺省)即 cordis
    cordis = tmp_path / "cordis.yml"
    cordis.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(deepwiki.envs, "DEEPWIKI_DSH_CORDIS", str(cordis))
    assert adapter(**_gen_kwargs({"generator": "dsh"})).config["config_path"] == str(cordis)


def test_wiki_pipeline_dsh_uses_agent():
    """分派:dsh 与 cc 同为 agent 路(AgentWikiPipeline);llm 路不受扰。"""
    assert isinstance(_wiki_pipeline("dsh"), deepwiki.AgentWikiPipeline)


def test_agent_options_cc_setting_sources_isolated(monkeypatch):
    """cc 完全隔离本地 claude 配置(setting_sources=[]):用户级 MCP/skills/hooks 不掺入 agent;

    config_path 概念键纯透传(路径直传 SDK,不读文件)。
    """
    monkeypatch.setitem(GENERATORS, "cc", _FakeGenerator)
    cfg = adapter(**_gen_kwargs({}), system_prompt="sys").config
    assert cfg["setting_sources"] == []
    assert cfg["config_path"] == os.path.abspath(deepwiki.envs.DEEPWIKI_CC_CONFIG)  # env 缺省
    cfg2 = adapter(
        **_gen_kwargs({"generator": "cc", "generator_config": {"config_path": "/tmp/dw-test-settings.json"}}),
        system_prompt="sys",
    ).config
    assert cfg2["setting_sources"] == []
    assert cfg2["config_path"] == "/tmp/dw-test-settings.json"  # 显式 > env;纯透传(无任何文件读取)


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
    got = await _chat(request, note="<note>tool hint</note>\n\n")
    assert "".join(got) == "hi"
    assert captured["run_id"] == captured["session"]  # 会话组名与监控名同一来源(chat:<repo>)
    assert captured["run_id"].startswith("chat:")
    assert "context" not in captured  # 引擎不再传 context
    # 工具指引文本经 generator_config 注入(引擎零工具假设;未注入 → 无 note)
    assert "<note>tool hint</note>" in captured["prompt"] and "<query>\nq2\n</query>" in captured["prompt"]
    assert "Previous conversation:" not in captured["prompt"]  # 被裁剪
    assert "<conversation_history>" not in captured["prompt"]


# ---------------------------------------------------------------------------
# cc/llm 双路径(离线:交付文件预置 / agent 与 llm_stream 全部 monkeypatch)
# ---------------------------------------------------------------------------


def test_agent_cache_naming():
    request = {"repo_url": "/x", "type": "local", "owner": "local",
               "repo": "deepwiki-open", "language": "en", "target": {}}
    pipeline = deepwiki.AgentWikiPipeline()
    project_key = deepwiki.repo_key_of("local", "local", "deepwiki-open")
    prefix = f"{project_key}_{_digest_of(request['target'])}"
    gen_kw = _gen_kwargs(request["target"])
    assert pipeline._agent_cache_structure_path(project_key, **gen_kw).name == f"{prefix}-structure.md"
    assert pipeline._agent_cache_page_path(project_key, "page-1", **gen_kw).name == f"{prefix}-page-1.md"
    assert pipeline._agent_cache_page_path(project_key, "overview", **gen_kw).name == \
        f"{prefix}-page_overview.md"


def test_sanitize_page_id_no_escape():
    r = _make_request("sanitize-io", "demo")
    project_key = deepwiki.repo_key_of(r["type"], r["owner"], r["repo"])
    pipeline = deepwiki.AgentWikiPipeline()
    out = pipeline._agent_cache_page_path(project_key, "../../evil", **_gen_kwargs(r["target"]))
    assert out.parent == pipeline._agent_cache_dir(project_key, **_gen_kwargs(r["target"]))
    assert out.relative_to(pipeline._agent_cache_dir(project_key, **_gen_kwargs(r["target"]))).parent == Path(".")


def test_agent_options_cc_delivery(monkeypatch, tmp_path):
    """写入模式:cwd 固定仓库根,add_dirs 指向交付目录,acceptEdits + 写工具;

    默认模式(chat/codemap):cwd + Read/Grep/Glob 自读代码,无 Write/写目录/acceptEdits;
    model/凭证不在装配层(经 target 绑定);allowed_tools 图工具名经 generator_config
    注入(app 侧 runtime_config 组装),引擎并入基座读工具。
    """
    repo = Repo(str(tmp_path), "local")
    monkeypatch.setitem(GENERATORS, "cc", _FakeGenerator)
    graph_tools = ["graphify_query", "mcp__graphify__graphify_query"]
    cfg = adapter(
        **_gen_kwargs({"generator_config": {"allowed_tools": graph_tools}}),
        system_prompt="", repo=repo,
        agent_output_dir=str(tmp_path / "out"), agent_write_mode=True,
    ).config
    assert cfg["cwd"] == str(tmp_path)
    assert cfg["add_dirs"] == [str(tmp_path / "out")]
    assert cfg["permission_mode"] == "acceptEdits"
    assert "model" not in cfg  # model 随配置文件(file 类不在装配层注入)
    assert cfg["config_path"] == os.path.abspath(deepwiki.envs.DEEPWIKI_CC_CONFIG)  # env 缺省
    for t in ("Read", "Grep", "Glob", "Write", *graph_tools):
        assert t in cfg["allowed_tools"], t
    cfg2 = adapter(
        **_gen_kwargs({"generator_config": {"allowed_tools": graph_tools}}),
        system_prompt="", repo=repo,
    ).config
    assert cfg2["cwd"] == str(tmp_path)
    assert "add_dirs" not in cfg2 and "permission_mode" not in cfg2  # 缺键 → SDK 缺省
    for t in ("Read", "Grep", "Glob", *graph_tools):
        assert t in cfg2["allowed_tools"], t
    assert "Write" not in cfg2["allowed_tools"]


def test_save_llm_restores_generator_default():
    """空选型默认 = 引擎内建 cc(缺省政策已迁 webui,引擎不再读 env)。"""
    assert deepwiki_utils.resolve_generator()[0] == "cc"


# ---------------------------------------------------------------------------
# 检索上下文格式化(llm 路 chat/codemap 的 RAG 式注入;图查询与子图解析属
# webui 组装层 generators 模块,测试在 apps/deepwiki-webui/server/tests/test_generators.py)
# ---------------------------------------------------------------------------


def test_format_subgraph_context():
    r"""原版 _format_context 同式:按文件分组,单文件多窗合并头,文件段以原版

    ("\n\n" + "-"*10 + "\n\n".join(parts)) 联结;空输入 → ""。
    """
    blocks = [
        {"path": "src/a.py", "text": "x = 1", "start_line": 1, "end_line": 1},
        {"path": "src/a.py", "text": "y = 2", "start_line": 10, "end_line": 20},
        {"path": "src/b.py", "text": "z = 3", "start_line": 5, "end_line": 9},
    ]
    text = deepwiki.utils.format_subgraph_context(blocks)
    assert text.startswith("\n\n----------## File Path: src/a.py\n\n")  # 原版联结式
    assert text.count("## File Path:") == 2
    assert "[lines 1-1]\nx = 1" in text and "[lines 10-20]\ny = 2" in text
    assert "## File Path: src/b.py\n\n[lines 5-9]\nz = 3" in text
    assert deepwiki.utils.format_subgraph_context([]) == ""
    assert deepwiki.utils.format_subgraph_context([{"path": "a.py", "text": "t", "start_line": 1, "end_line": 1}]) \
        .startswith("\n\n----------## File Path: a.py")


# ---------------------------------------------------------------------------
# LLM 路 chat / codemap(fake graphify.query + fake llm_stream/llm_complete,全离线)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_chat_stream_prompt_and_context(monkeypatch, tmp_path):
    """llm chat(原版 prompt_builder 同构):单条 user 消息含 /no_think+SIMPLE 角色

    +历史拼接+检索上下文(<START_OF_CONTEXT> + ## File Path + [lines A-B]);查询词为末条。
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    captured = {}
    graph = _FakeGraph(blocks=[{"path": "src/a.py", "text": "x = 1", "start_line": 1, "end_line": 1}])

    async def fake_llm_stream(prompt, *, generator=None, generator_config=None,
                              session_name=None, run_id=None, **kw):
        captured.update(session=session_name, prompt=prompt, generator=generator)
        yield "HELLO"

    monkeypatch.setattr(deepwiki_utils, "llm_stream", fake_llm_stream)
    request = {
        "repo_url": str(tmp_path), "type": "local", "language": "en",
        "target": {"generator": "llm"}, "token": None,
        "messages": [{"role": "user", "content": "q1"},
                     {"role": "assistant", "content": "a1"},
                     {"role": "user", "content": "how does src/a.py work?"}],
    }
    got = await _chat(request, graph)
    assert "".join(got) == "HELLO"
    assert captured["session"] == f"chat:{Repo(str(tmp_path), 'local').name}"
    assert captured["generator"] == "llm"  # llm 路:model/url/api_key 由选型注入(经 adapter)
    msg = captured["prompt"]  # 模块级 llm_stream 只收单条 user 消息(payload 为内部形态)
    assert msg.startswith("/no_think ")
    assert "expert code analyst" in msg  # SIMPLE 角色模板
    assert "<conversation_history>" in msg and "<turn>" in msg
    assert "<START_OF_CONTEXT>" in msg and "<END_OF_CONTEXT>" in msg
    assert "## File Path: src/a.py" in msg and "[lines 1-1]\nx = 1" in msg
    assert '<file path="' not in msg  # 不再 <file> 块(原版格式)
    assert "<query>\nhow does src/a.py work?\n</query>" in msg
    assert "\nAssistant: " in msg
    assert graph.questions == ["how does src/a.py work?"]


@pytest.mark.asyncio
async def test_llm_chat_continuation_and_iteration_templates(monkeypatch, tmp_path):
    """continuation 回退(查询词换回首条用户消息);迭代模板按 research_iteration 选。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    captured = {}
    graph = _FakeGraph(blocks=[])

    async def fake_llm_stream(prompt, *, generator=None, generator_config=None,
                              session_name=None, run_id=None, **kw):
        captured["prompt"] = prompt
        yield ""

    monkeypatch.setattr(deepwiki_utils, "llm_stream", fake_llm_stream)
    request = {
        "repo_url": str(tmp_path), "type": "local", "language": "en",
        "target": {"generator": "llm"}, "token": None, "research_iteration": 3,
        "messages": [{"role": "user", "content": "what is this repo about?"},
                     {"role": "assistant", "content": "## Research Plan\nx"},
                     {"role": "user", "content": "Continue the research",
                      "mode": "deep_research"}],
    }
    _ = await _chat(request, graph)
    assert graph.questions == ["what is this repo about?"]
    assert "iteration 3" in captured["prompt"]
    request_final = {**request, "research_iteration": 5}
    _ = await _chat(request_final, graph)
    assert "## Final Conclusion" in captured["prompt"]


@pytest.mark.asyncio
async def test_llm_chat_error_fallbacks(monkeypatch, tmp_path):
    """llm chat 失败语义(原版 stream_and_fallback):graph 失败/无命中→"无检索增强"

    note;非 token 错误→"Error with openai API:";token 超限→简化重试;重试也败→致歉。
    """
    captured = {}
    graph = _FakeGraph(blocks=[])

    async def fake_llm_stream(prompt, *, generator=None, generator_config=None,
                              session_name=None, run_id=None, **kw):
        captured["prompt"] = prompt
        yield "ok"

    monkeypatch.setattr(deepwiki_utils, "llm_stream", fake_llm_stream)
    request = {
        "repo_url": str(tmp_path), "type": "local", "language": "en",
        "target": {"generator": "llm"}, "token": None,
        "messages": [{"role": "user", "content": "hi"}],
    }
    got = await _chat(request, graph)
    assert "".join(got) == "ok"  # 图无命中也一样回答(无检索增强 note)
    assert "Answering without retrieval augmentation." in captured["prompt"]
    # 图服务检索失败 → 直接 raise(检索失败不得带病继续;图服务经 generator_config 注入)
    with pytest.raises(RuntimeError, match="代码图谱不可用"):
        _ = await _chat(request, _FakeGraph(blocks=[], error="graph is gone"))
    # 非 token 错误 → "Error with openai API: ..." 原版式文本进流
    async def err_stream(prompt, **kw):
        raise RuntimeError("llm down")
        yield  # 保持 async generator

    monkeypatch.setattr(deepwiki_utils, "llm_stream", err_stream)
    got = await _chat(request, graph)
    assert "Error with openai API: llm down" in "".join(got)
    # token 超限 → 简化提示词重试一次
    calls = []

    async def token_stream(prompt, **kw):
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("maximum context length exceeded")
        yield "SIMPLIFIED"

    monkeypatch.setattr(deepwiki_utils, "llm_stream", token_stream)
    got = await _chat(request, graph)
    assert "".join(got) == "SIMPLIFIED"
    assert len(calls) == 2
    assert "due to input size constraints" in calls[1]
    # 简化重试也失败 → 原版致歉文本
    async def token_stream2(prompt, **kw):
        raise RuntimeError("too many tokens")
        yield

    monkeypatch.setattr(deepwiki_utils, "llm_stream", token_stream2)
    got = await _chat(request, graph)
    assert "I apologize, but your request is too large for me to process" in "".join(got)


@pytest.mark.asyncio
async def test_llm_codemap_events_sequence(monkeypatch, tmp_path):
    """llm codemap:NDJSON 事件序列与 agent 路同形;一次 graphify 查询两阶段复用;

    _ground_citations 以真实源码覆盖行号。
    """
    repo_dir = tmp_path / "repo"
    (repo_dir / "src").mkdir(parents=True)
    (repo_dir / "src" / "a.py").write_text("def f():\n    return 42\n", encoding="utf-8")
    graph = _FakeGraph(blocks=[
        {"path": "src/a.py", "text": "def f():\n    return 42\n",
         "start_line": 1, "end_line": 2},
    ])

    calls = []

    skeleton = {
        "title": "How to f", "summary": "s",
        "sections": [{
            "id": "1", "title": "Do it", "guide": "", "diagram": "",
            "steps": [{"id": "1a", "label": "call f", "code": "f()",
                       "citation": {"file_path": "src/a.py", "start_line": 99,
                                    "end_line": 99, "snippet": "    return 42"}}],
        }],
    }

    async def fake_llm_complete(prompt, *, generator=None, generator_config=None,
                                session_name=None, run_id=None, **kw):
        calls.append(("complete", session_name, prompt))
        if session_name == "codemap:skeleton":
            return json.dumps(skeleton)
        return json.dumps({**skeleton, "sections": [
            {**skeleton["sections"][0], "guide": "g", "diagram": "graph TD"}]})

    monkeypatch.setattr(deepwiki_utils, "llm_complete", fake_llm_complete)
    request = {
        "repo_url": str(repo_dir), "type": "local", "language": "en",
        "target": {"generator": "llm"}, "token": None, "question": "how to call f?",
    }
    events = await _codemap(request, graph)
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
    assert len(graph.questions) == 1  # 一次检索、两阶段复用
    data = events[6]["data"]
    assert data["sections"][0]["guide"] == "g"
    cit = data["sections"][0]["steps"][0]["citation"]
    assert (cit["start_line"], cit["end_line"]) == (2, 2)  # 真实行号覆盖 99/99
    skel_prompt = next(c[2] for c in calls if c[1] == "codemap:skeleton")
    user = skel_prompt  # 模块级 llm_complete 只收单条 user 消息(payload 为内部形态)
    assert user.startswith("/no_think ")
    assert "<START_OF_CONTEXT>" in user
    assert "## File Path: src/a.py" in user and "[lines 1-2]" in user
    assert "expert code analyst" in user and "codemap" in user  # 骨架提示词正文


@pytest.mark.asyncio
async def test_llm_codemap_not_indexed_and_retry_and_degraded(monkeypatch, tmp_path):
    """未索引 → analyzing error 事件且不调补全;骨架 3 次废文本 → initial_codemap error;

    enrich 失败 → degraded=True 且 codemap 事件为骨架内容。
    """
    repo_dir = tmp_path / "fresh-repo"
    repo_dir.mkdir()
    complete_calls = []
    graph = _FakeGraph(ready=False, blocks=[])

    async def fake_llm_complete(prompt, *, generator=None, generator_config=None,
                                session_name=None, run_id=None, **kw):
        complete_calls.append(session_name)
        raise RuntimeError("unreachable")  # 不应被调用段在此触发前提供有效返回

    monkeypatch.setattr(deepwiki_utils, "llm_complete", fake_llm_complete)
    request = {
        "repo_url": str(repo_dir), "type": "local", "language": "en",
        "target": {"generator": "llm"}, "token": None, "question": "q",
    }
    events = await _codemap(request, graph)
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
    graph = _FakeGraph(blocks=[
        {"path": "src/a.py", "text": "def f():\n    return 42\n",
         "start_line": 1, "end_line": 2},
    ])

    skeleton = {"title": "How to f", "summary": "s",
                "sections": [{"id": "1", "title": "S", "guide": "", "diagram": "",
                              "steps": [{"id": "1a", "label": "s", "code": "", "citation": None}]}]}
    complete_calls = []

    async def fake_llm_complete(prompt, *, generator=None, generator_config=None,
                                session_name=None, run_id=None, **kw):
        complete_calls.append(session_name)
        if session_name == "codemap:skeleton":
            return json.dumps(skeleton)
        return "this is not json"

    monkeypatch.setattr(deepwiki_utils, "llm_complete", fake_llm_complete)
    request = {
        "repo_url": str(repo_dir), "type": "local", "language": "en",
        "target": {"generator": "llm"}, "token": None, "question": "how to call f?",
    }
    events = await _codemap(request, graph)
    assert events[4] == {"type": "phase", "phase": "diagrams", "status": "start"}
    assert events[5]["status"] == "done" and events[5]["degraded"] is True
    assert events[6]["type"] == "codemap" and events[6]["data"]["sections"][0]["guide"] == ""
    assert events[7] == {"type": "done"}
    assert complete_calls == ["codemap:skeleton", "codemap:enrich", "codemap:enrich"]  # 富化重试 2 次(原版)
    # 骨架重试耗尽 → error 事件,无 codemap/done
    async def garbage(prompt, *, generator=None, generator_config=None, session_name=None, **kw):
        return "not json"

    monkeypatch.setattr(deepwiki_utils, "llm_complete", garbage)
    events = await _codemap(request, graph)
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
async def test_chat_and_codemap_dispatch_by_generator(monkeypatch):
    """chat/codemap 顶层分派:与 wiki 同开关(显式选型分派;空选型 = 内建 cc)。"""
    calls = []

    async def fake_cc_chat(**kwargs):
        calls.append("cc")
        yield "c"

    async def fake_llm_chat(**kwargs):
        calls.append("llm")
        yield "l"

    async def fake_cc_codemap(**kwargs):
        calls.append("cc-codemap")
        yield "c"

    async def fake_llm_codemap(**kwargs):
        calls.append("llm-codemap")
        yield "l"

    monkeypatch.setattr(deepwiki.chat, "_agent_chat", fake_cc_chat)
    monkeypatch.setattr(deepwiki.chat, "_llm_chat", fake_llm_chat)
    monkeypatch.setattr(deepwiki.codemap, "_agent_codemap", fake_cc_codemap)
    monkeypatch.setattr(deepwiki.codemap, "_llm_codemap", fake_llm_codemap)
    chat_req = {
        "repo_url": "/tmp/x", "type": "local", "language": "en",
        "target": {}, "token": None,
        "messages": [{"role": "user", "content": "hi"}],
    }
    codemap_req = {
        "repo_url": "/tmp/x", "type": "local", "language": "en",
        "target": {}, "token": None, "question": "q",
    }
    assert [c async for c in deepwiki.chat_stream(
        **_gen_kwargs({"generator": "cc"}), repo=_repo_of(chat_req), messages=chat_req["messages"],
        language="en",
    )] == ["c"]
    assert [ev async for ev in deepwiki.generate_codemap(
        **_gen_kwargs({"generator": "cc"}), repo=_repo_of(codemap_req),
        question=codemap_req["question"], language="en",
    )] == ["c"]
    assert [c async for c in deepwiki.chat_stream(
        **_gen_kwargs({"generator": "llm"}), repo=_repo_of(chat_req), messages=chat_req["messages"],
        language="en",
    )] == ["l"]
    assert [ev async for ev in deepwiki.generate_codemap(
        **_gen_kwargs({"generator": "llm"}), repo=_repo_of(codemap_req),
        question=codemap_req["question"], language="en",
    )] == ["l"]
    assert calls == ["cc", "cc-codemap", "llm", "llm-codemap"]


# ---------------------------------------------------------------------------
# codex 后端分派/选项(镜像 dsh 测试;假 codex_stream,零 SDK/网络/token)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_deliver_wraps_request_failure(monkeypatch, tmp_path):
    """RequestFailedError → RuntimeError("agent 执行失败: ...")(原 generate_* file 分支

    的包装收进 AgentWikiPipeline._failure;__cause__ 保留原始 SDK 失败)。
    """

    @contextlib.asynccontextmanager
    async def fake_session(self, **kw):  # 零 SDK 客户端进入占位
        yield self

    async def boom_stream(self, prompt, **kw):
        raise RequestFailedError("sdk exploded")
        yield  # 保持 async generator

    monkeypatch.setattr(GENERATORS["cc"], "session", fake_session)  # 适配器单例直连
    monkeypatch.setattr(GENERATORS["cc"], "stream", boom_stream)
    pipe = deepwiki.AgentWikiPipeline()
    out_path = tmp_path / "out" / "page.md"
    with pytest.raises(RuntimeError) as ei:
        await pipe._deliver(GENERATORS["cc"]({}), "", out_path,
                            label="wiki:page:p1", run_id="r1")
    assert str(ei.value) == "agent 执行失败: sdk exploded"
    assert isinstance(ei.value.__cause__, RequestFailedError)
    assert not out_path.exists()  # 失败即无交付文件


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
    assert "(抱歉,本次请求处理失败: agent 执行失败: sdk exploded)" in got


def test_codex_options_config(monkeypatch, tmp_path):
    """经 _adapter 的 codex 装配:file 类配置随 config_path(纯透传,不读文件);

    cwd 固定仓库根;env.GRAPHIFY_OUT / mcp_servers 图工具桌经 generator_config
    白名单透传(app 侧 runtime_config 注入;repo 空时无 cwd/图位)。
    """
    repo = Repo(str(tmp_path), "local")
    monkeypatch.setitem(GENERATORS, "codex", _FakeGenerator)
    monkeypatch.setattr(_FakeGenerator, "generator", "codex")
    fake_mcp = [{"id": "fake", "command": "x"}]
    cfg = adapter(
        **_gen_kwargs({"generator": "codex", "generator_config": {
            "mcp_servers": fake_mcp, "env": {"GRAPHIFY_OUT": "/tmp/g"}}}),
        system_prompt="sys", repo=repo,
    ).config
    assert "model" not in cfg and cfg["cwd"] == str(tmp_path)
    assert cfg["env"] == {"GRAPHIFY_OUT": "/tmp/g"}
    assert cfg["mcp_servers"] == fake_mcp
    assert cfg["sandbox"] == "full_access" and cfg["approval_mode"] == "auto_review"
    cfg2 = adapter(**_gen_kwargs({"generator": "codex"}), system_prompt="sys").config
    assert "cwd" not in cfg2 and "env" not in cfg2 and "mcp_servers" not in cfg2  # repo 空:不固定 cwd/图
    assert "model" not in cfg2  # 缺省交给 SDK 缺省模型
    # 学 cc 凭证面:envs 无 CODEX_* 常量(防误引入"看起来必须配置"的 env 骨架)
    assert not hasattr(deepwiki.envs, "CODEX_HOME")
    assert not hasattr(deepwiki.envs, "CODEX_API_KEY")


def test_wiki_pipeline_codex_uses_agent():
    """分派:codex 与 cc/dsh 同为 agent 路(AgentWikiPipeline);llm 路不受扰。"""
    assert isinstance(_wiki_pipeline("codex"), deepwiki.AgentWikiPipeline)
