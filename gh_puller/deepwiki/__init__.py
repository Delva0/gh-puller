"""DeepWiki 兼容后端:前端契约沿用 deepwiki-open(前端见 apps/deepwiki-webui/web/),运行引擎替换为 Claude Code agent + graphify。

来源与协议:
- 前端契约与提示词来自 deepwiki-open(MIT License, Copyright (c) 2024 Sheing Ng)。
- 原后端 RAG(adalflow + FAISS chunk/embed 检索)整体切除:
  索引 = `graphify.extract(code_only=True)` 的纯本地 AST 建图(输出 <DEEPWIKI_ROOT>/graphify/<repo>/graph.json);
  检索 = `graphify.query()` 封装为 Claude Code agent 的 graphify_query 工具,由 agent 按需调用。
- 双路生成(envs.DEEPWIKI_GENERATOR 统一开关,cc/llm 可选):
  cc(agent)路 = Claude Code agent(自读代码 + graphify_query 工具;wiki 交付件 Write 落盘;
  chat/codemap 每请求一次提问,agent 内部多轮工具调用完成);
  llm 路 = deepwiki-open 原式补全(chat/codemap 的检索上下文 = graphify 子图 →
  真实代码行窗注入,即原 dense RAG 的对应物)。
  提示词(chat / deep_research / codemap / wiki 页面与结构)全部为 deepwiki-open 原文,
  且全部为英文(模型面向文本不出中文;日志/端点消息/注释可为中文)。
  单路生成协议 helper 收进对应 pipeline 类(self._xxx),共用/通道/任务机保持模块级
  (边界注释随实现迁至 gh_puller/deepwiki/pipeline.py 模块 docstring)。

已知简化(v1,详见 gh_puller/envs.py):
- repo 克隆用 git CLI(subprocess,不引 gitpython);远程 URL 的 token 注入沿用原后端三 host 方案。
- token 上限粗略估算(字符数/4,不引 tiktoken)。
- 语言仅 en/zh(与前端裁剪的 messages/{en,zh}.json 同步)。
- 模型:target = {generator, generator_config}(极简,无注册表类);解析/校验在
  agent.resolve_generator 纯函数(显式 > 环境 > 类缺省):file 类(cc/dsh/codex)的
  generator_config = {"config_path": 各 CLI 原生配置文件路径}(服务端原样透传给
  agent SDK,零解析零翻译 —— cc: Claude settings.json;codex: config.toml;
  dsh: cordis.yml);object 类(llm)的 generator_config = {"provider"/"model"/
  "base_url"/"api_key"}。四路:cc+anthropic / dsh+deepseek / codex+openai / llm+openai。
- 文件过滤为内嵌精简规则(与原 repo.json 全量规则有差异)。
- 聊天记忆由单次 agent 会话内承,无持久会话库。

wiki 生成进度中途落盘(deepwiki_taskstate_*,见文末任务状态机):结构确定后与每页
完成后各写一次,进程重启后同仓库再次提交即从落盘状态续跑(结构/已完成页不再重做)。

本模块为引擎+任务层(无 FastAPI 依赖,可被 apps/tui 等 CLI 复用):
HTTP 端点层(FastAPI app/SSE/WS)已迁至 apps/deepwiki-webui/server/app.py。
"""

import asyncio
import contextlib
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Mapping

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from .. import envs, graphify
from ..agent import (
    GENERATORS,
    generate_result,
    generate_stream,
    resolve_generator,
)
from .pipeline import (
    AgentWikiPipeline,
    LlmWikiPipeline,
    WikiPipeline,
    _service_pipeline,
    _wiki_pipeline,
)
from .pipeline import (  # noqa: E402 —— 提示词属生成协议资产
    _LANGUAGE_NAMES,
    _LANGUAGE_NAMES_RAW,
    _SIMPLE_CHAT_SYSTEM_PROMPT,
    _CODEMAP_SKELETON_PROMPT,
    _CODEMAP_ENRICH_PROMPT,
    _build_page_prompt,
    _COMPREHENSIVE_STRUCTURE,
    _CONCISE_STRUCTURE,
    _language_name,
)
from .cache import (  # 缓存/状态/导出 + 判等摘要(详见 cache.py docstring)
    _cache_identity,
    _cache_target_matches,
    _persist_state,
    _request_digest,
    _target_digest,
    _target_digest_of,
    _wiki_cache_path,
    _wiki_state_path,
    delete_wiki_cache,
    delete_wiki_task_state,
    export_wiki,
    list_processed_projects,
    list_wiki_cache,
    read_wiki_cache,
    read_wiki_task_state,
    save_wiki_cache,
    wiki_cache_exists,
    write_wiki_task_state,
)
from ..utils import (
    Repo,
    RepoType,
    Task,
    TaskRegistry,
    TaskStatus,
    TaskSubmitResult,
    _estimate_tokens,
    _event,
    _extract_json,
    _find_readme_path,
    _phase,
    _sanitize_path_seg,
    _strip_markdown_fences,
    detect_default_branch,
    read_repo_file_tree,
)
from ..utils import (
    _log as _utils_log,
)

# ---------------------------------------------------------------------------
# 日志与全局路径
# ---------------------------------------------------------------------------


# 进度日志走 stderr(同 graphify.py 约定);prefix 固定 [deepwiki]
_log = partial(_utils_log, prefix="deepwiki")


# wiki 缓存根目录(克隆根 repos 随 Repo 族已移至 utils._CLONE_ROOT)
_WIKI_CACHE_DIR = os.path.join(envs.DEEPWIKI_ROOT, "wikicache")
_WIKI_PREFIX = "deepwiki_cache_"
# 生成中途状态文件前缀(与 deepwiki_cache_ 区分,避免被 list_wiki_cache 当成成品扫描)
_WIKI_STATE_PREFIX = "deepwiki_taskstate_"
# 状态写锁:并发页生成器的落盘写串行化(asyncio 3.10+ 的 Lock 不再绑定 loop,模块级安全)
_state_write_lock = asyncio.Lock()
os.makedirs(_WIKI_CACHE_DIR, exist_ok=True)

# cc(agent)交付件目录名:wikicache/agent_cache/{proj}-{structure,page_<id>}.md
_AGENT_CACHE_DIRNAME = "agent_cache"



# 页面/结构/图表生成所需的固定并发与重试(原 DEEPWIKI_* 缺省同式,见 envs.py;
# 页并发缺省 4:同时跑 4 个 agent 子进程,受 API 速限与机器内存约束)
# 统一从 envs 读取
_MAX_CONCURRENT_WIKI_TASKS = envs.MAX_CONCURRENT_WIKI_TASKS
_WIKI_PAGE_CONCURRENCY = max(1, envs.WIKI_PAGE_CONCURRENCY)
_WIKI_PAGE_RETRIES = max(0, envs.WIKI_PAGE_RETRIES)
_WIKI_TASK_TTL_SECONDS = envs.WIKI_TASK_TTL_SECONDS

# 提示词(原文移植自 deepwiki-open;_LANGUAGE_NAMES/_language_name/_SIMPLE_CHAT_SYSTEM_PROMPT/
# _CODEMAP_*_PROMPT/_build_page_prompt/_COMPREHENSIVE_STRUCTURE/_CONCISE_STRUCTURE)
# 已迁至 ./pipeline.py(生成协议资产),顶部 re-export 保持对外形状。


# ---------------------------------------------------------------------------
# Pydantic 契约模型(与 deepwiki-open api/schemas 同形)
# ---------------------------------------------------------------------------


# target 请求形态(极简):{"generator": id, "generator_config": {...}}
# 校验/规范化/缺省在 agent.resolve_generator(纯函数,运行前 ValueError)。


class RepoRequestBase(BaseModel):
    repo_url: str = Field(..., description="URL or local path of the repository")
    type: RepoType = Field("github", description="Repository type")
    token: str | None = Field(None, description="PAT for private repositories")
    target: dict[str, Any] = Field(
        default_factory=dict,
        description="target 请求形态:generator + generator_config(file 类:config_path;"
                    "object 类:provider/model/凭证;api_key/base_url 仅请求态不落盘)",
    )
    language: str = Field("en", description="Language for content generation")
    excluded_dirs: list[str] = Field(
        default_factory=list,
        description="List or newline-separated string of directories to exclude from processing",
    )
    excluded_files: list[str] = Field(
        default_factory=list,
        description="List or newline-separated string of file patterns to exclude from processing",
    )
    included_dirs: list[str] = Field(
        default_factory=list,
        description="List or newline-separated string of directories to include exclusively",
    )
    included_files: list[str] = Field(
        default_factory=list,
        description="List or newline-separated string of file patterns to include exclusively",
    )

    @field_validator(
        "excluded_dirs",
        "excluded_files",
        "included_dirs",
        "included_files",
        mode="before",
    )
    @classmethod
    def validate_path(cls, value: list[str] | str) -> list[str]:
        """list 或换行分隔字符串(原 schemas 同式,保留换行分隔兼容)。"""
        if isinstance(value, str):
            value = [p.strip() for p in value.split("\n") if p.strip()]
        return value


class ChatMessage(BaseModel):
    role: str  # 'user' or 'assistant'
    content: str
    mode: Literal["normal", "deep_research"] = Field(default="normal")


class ChatCompletionRequest(RepoRequestBase):
    messages: list[ChatMessage] = Field(..., description="List of chat messages")
    research_iteration: int = Field(
        default=1,
        ge=1,
        description="Current deep research iteration (1-based). Only used when the request is in deep_research mode.",
    )


class RepoPrepareRequest(RepoRequestBase):
    """POST /repo/prepare 的请求体(索引预热,无消息)。"""


class CodeMapCitation(BaseModel):
    file_path: str = Field(..., description="Repository-relative path of the source file")
    start_line: int | None = Field(None, description="1-based line range start")
    end_line: int | None = Field(None, description="1-based line range end")
    snippet: str = Field(
        "", description="Verbatim excerpt copied from the source (used to locate the range)"
    )


class CodeMapStep(BaseModel):
    id: str = Field(..., description="Human-facing id such as '1a', '1b', '2a'")
    label: str = Field(..., description="Short title of the step")
    code: str = Field("", description="Example code snippet illustrating the step")
    citation: CodeMapCitation | None = Field(None, description="Where this step's code comes from")


class CodeMapSection(BaseModel):
    id: str = Field(..., description="Section id such as '1', '2'")
    title: str = Field(..., description="Section title")
    guide: str = Field("", description="Prose guide for the section (filled in phase 2)")
    diagram: str = Field("", description="Mermaid diagram source (filled in phase 2)")
    steps: list[CodeMapStep] = Field(default_factory=list)


class CodeMap(BaseModel):
    title: str = Field(..., description="Overall codemap title")
    summary: str = Field("", description="Introductory summary")
    sections: list[CodeMapSection] = Field(default_factory=list)


class CodeMapRequest(RepoRequestBase):
    question: str = Field(..., description="The user's how-to / usage question")


class WikiTaskRequest(RepoRequestBase):
    owner: str
    repo: str
    comprehensive: bool = Field(True, description="Comprehensive vs concise wiki")

    @property
    def repo_key(self) -> str:
        return f"{self.type}_{self.owner}_{self.repo}"


# 通用化:直接别名(task_id/status/from_cache/resumed 语义同 utils.TaskSubmitResult)
WikiTaskSubmitResult = TaskSubmitResult


class RepoInfo(BaseModel):
    owner: str
    repo: str
    type: str
    token: str | None = None
    localPath: str | None = None
    repoUrl: str | None = None


class WikiPage(BaseModel):
    id: str
    title: str
    content: str
    filePaths: list[str]
    importance: str  # 'high' | 'medium' | 'low'
    relatedPages: list[str]


class WikiSection(BaseModel):
    id: str
    title: str
    pages: list[str]
    subsections: list[str] | None = None


class WikiStructureModel(BaseModel):
    id: str
    title: str
    description: str
    pages: list[WikiPage]
    sections: list[WikiSection] | None = None
    rootSections: list[str] | None = None


class WikiCacheData(BaseModel):
    wiki_structure: WikiStructureModel
    generated_pages: dict[str, WikiPage]
    repo_url: str | None = None  # compatible for old cache
    repo: RepoInfo | None = None
    provider: str | None = None  # object 类:provider id
    model: str | None = None  # object 类:模型标识
    generator: str | None = None  # 生成模式快照(cc/dsh/codex/llm);缓存命中时校验,防跨模式混用
    config_path: str | None = None  # file 类:公开身份(config 文件路径;凭证不进缓存)


class WikiTaskState(BaseModel):
    """生成中途落盘状态:同仓库再次提交时据此续跑(结构与已完成页不再重生成)。"""

    version: int = 1
    request: WikiTaskRequest  # 全量快照:续跑沿用首次输入(comprehensive/model/filters 等)
    status: TaskStatus  # 仅审计;恢复时按 wiki_structure 有无重新映射
    wiki_structure: WikiStructureModel | None = None
    generated_pages: dict[str, WikiPage] = Field(default_factory=dict)
    default_branch: str = "main"
    submitted_at: int  # 保留原始提交时间
    error: str | None = None


class WikiExportRequest(BaseModel):
    repo_url: str = Field(..., description="URL of the repository")
    pages: list[WikiPage] = Field(..., description="List of wiki pages to export")
    format: Literal["markdown", "json"] = Field(..., description="Export format")


class ProcessedProjectEntry(BaseModel):
    id: str  # Filename
    owner: str
    repo: str
    name: str  # owner/repo
    repo_type: str
    submittedAt: int
    language: str
    digest: str = ""


class WikiTaskSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    owner: str
    repo: str
    repo_type: str
    language: str
    status: TaskStatus
    # 列尾公开 target 摘要(同一仓库多 target 并存;缺省无摘要=旧格式兼容)
    digest: str = "" 
    pages_done: int = Field(default=0, ge=0)
    pages_total: int = Field(default=0, ge=0)
    current_page_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    submitted_at: int = Field(..., ge=0, validation_alias="submittedAt")

    @computed_field
    @property
    def name(self) -> str:
        return f"{self.owner}/{self.repo}"


class WikiTaskStatus(WikiTaskSummary):
    wiki_structure: WikiStructureModel | None = None


class Model(BaseModel):
    id: str
    name: str


class Provider(BaseModel):
    id: str
    name: str
    models: list[Model] = Field(default_factory=list)
    supportsCustomModel: bool = Field(False, description="Whether this provider supports custom models")


class ModelConfig(BaseModel):
    providers: list[Provider] = Field(..., description="Available model providers")
    defaultProvider: str = Field(..., description="ID of the default provider")


class AuthorizationConfig(BaseModel):
    code: str = Field(..., description="Authorization code")





def _locate_snippet(text: str, snippet: str) -> tuple[int, int] | None:
    """在文本中定位 snippet 的 1-based 行号范围(LLM 给的行号不可靠,snippet 为权威)。"""
    snippet = snippet.strip("\n")
    if not snippet:
        return None
    pos = text.find(snippet)
    if pos != -1:
        start = text.count("\n", 0, pos) + 1
        return start, start + snippet.count("\n")
    first = next((ln.strip() for ln in snippet.splitlines() if ln.strip()), "")
    if first:
        idx = text.find(first)
        if idx != -1:
            start = text.count("\n", 0, idx) + 1
            return start, start + snippet.count("\n")
    return None


def _ground_citations(codemap: CodeMap, repo_dir: str) -> None:
    """用真实源码里的 snippet 位置覆盖每条引用的行号范围。"""
    file_cache: dict[str, str | None] = {}
    for section in codemap.sections:
        for step in section.steps:
            cit = step.citation
            if not cit or not cit.snippet or not cit.file_path:
                continue
            if cit.file_path not in file_cache:
                path = os.path.join(repo_dir, cit.file_path)
                try:
                    with open(path, encoding="utf-8") as f:
                        file_cache[cit.file_path] = f.read()
                except (OSError, UnicodeDecodeError):
                    file_cache[cit.file_path] = None
            text = file_cache[cit.file_path]
            if not text:
                continue
            loc = _locate_snippet(text, cit.snippet)
            if loc:
                cit.start_line, cit.end_line = loc


# ---------------------------------------------------------------------------
# graphify 对接:索引(extract)与 graphify_query 工具
# ---------------------------------------------------------------------------


def _graph_dir(repo: Repo) -> Path:
    """单仓库图产物根(extract 的 out_dir):graphify/{type}_{name},无日期层,路径稳定以支持已缓存即跳过。"""
    return Path(envs.DEEPWIKI_ROOT) / "graphify" / f"{repo.repo_type}_{repo.name}"


def _graph_path(repo: Repo) -> Path:
    """graph.json 规范路径(extract 的 out_dir 即最终目录,无 graphify-out 层)。"""
    return _graph_dir(repo) / "graph.json"


def _index_ready(repo: Repo) -> bool:
    """索引完成信号 = graph.json 已存在(复用 graphify._load_graph 的存在性语义)。"""
    return _graph_path(repo).exists()


async def _run_extract(repo: Repo, request: RepoRequestBase) -> dict:
    """graphify.extract 建图(code_only 纯本地 AST,无 key 可跑);失败返回错误态 dict,不抛。"""
    extra_excludes: list[str] | None = None
    if request.excluded_dirs or request.excluded_files:
        extra_excludes = [*request.excluded_dirs, *request.excluded_files]
    return await asyncio.to_thread(
        graphify.extract,
        path=repo.save_path,
        code_only=True,
        out_dir=_graph_dir(repo),
        extra_excludes=extra_excludes,
    )




def _graphify_mcp(backend: str) -> list[dict]:
    """引擎图工具桌(装配层概念:graphify 属本模块) → 适配层通用 mcp_servers 描述。

    backend="dsh":组合 mcp-client 行(id mcp-graphify / serverName graphify);
    backend="codex":config.toml 段(id graphify + env_vars 白名单透传 GRAPHIFY_OUT)。
    """
    command = os.environ.get("GRAPHIFY_MCP_PYTHON") or sys.executable
    if backend == "dsh":
        return [{"id": "mcp-graphify", "serverName": "graphify",
                 "command": command, "args": ["-m", "graphify.serve"]}]
    return [{"id": "graphify", "command": command, "args": ["-m", "graphify.serve"],
             "env_vars": ["GRAPHIFY_OUT"]}]


def _graphify_server(repo: Repo):
    """进程内 MCP server:把 graphify.query 封装为 graphify_query 工具(闭包绑定图路径)。"""

    graph_path = _graph_path(repo)

    @tool(
        "graphify_query",
        "Query this repository's code graph and return the related code subgraph "
        "(functions/classes/call relationships as text), with `Source: <file path> L<line> "
        "markers. Use it to get code structure and line-number references.",
        {"question": str},
    )
    async def graphify_query(args: dict) -> dict:
        try:
            question = (args.get("question") or "").strip()
            result = graphify.query(question, graph_path=graph_path)
            text = result.get("answer") or ""
        except Exception as exc:
            text = f"Graph query failed: {type(exc).__name__}: {exc}"
        return {"content": [{"type": "text", "text": text.strip() or "(No matching results in code graph)"}]}

    return create_sdk_mcp_server("graphify", tools=[graphify_query])


# ---------------------------------------------------------------------------
# Claude agent 封装(SDK 进程内 MCP 工具 + 流式/整收)
# ---------------------------------------------------------------------------


def _resolve_target(target: Mapping | None = None, get_env=None):
    """target dict → (generator id, 规范化配置);空 target 走 env 缺省(与运行期一致)。

    get_env 缺省桥接 envs 快照常量(envs.py 单点;测试 monkeypatch 生效)。
    """
    if get_env is None:
        get_env = lambda key: getattr(envs, key, "") or ""
    t = dict(target or {})
    gen, resolved = resolve_generator(t.get("generator"), t.get("generator_config"), get_env)
    return gen.id, resolved


def _target_identity(generator_id: str, resolved: Mapping) -> str:
    """判等身份(不含凭证):file 类 = config_path;object 类 = "provider|model"。"""
    if GENERATORS[generator_id].config_kind == "file":
        return resolved.get("config_path", "") or ""
    return f"{resolved.get('provider', '')}|{resolved.get('model', '')}"


def _strip_creds(config: Mapping) -> dict:
    """落盘形态:拷贝并剥离 generator_config 内 api_key/base_url(config_path 非凭证,保留)。"""
    out = dict(config)
    gc = dict(out.get("generator_config") or {})
    gc.pop("api_key", None)
    gc.pop("base_url", None)
    out["generator_config"] = gc
    return out


def _merge_creds(base: Mapping, other: Mapping) -> dict:
    """落盘形态保持自身;object 类凭证(api_key/base_url 于 generator_config 内)取 other。"""
    out = dict(base)
    oc = dict((other or {}).get("generator_config") or {})
    merged = dict(out.get("generator_config") or {})
    for key in ("base_url", "api_key"):
        if oc.get(key):
            merged[key] = oc[key]
    out["generator_config"] = merged
    return out





def _agent_options(
    resolved: dict,
    system_prompt: str,
    repo: Repo | None,
    *,
    agent_output_dir: str | None = None,
    agent_write_mode: bool = False,
) -> ClaudeAgentOptions:
    """组装 cc 选项(工具隔离 + 配置文件装配;模型/凭证随所选 settings 文件):
    repo 非空时挂 graphify 工具并把 cwd 固定到仓库根(SDK 缺省 = 进程 cwd,曾导致
    agent 串到 gh-puller 并把 docs 写入其中)。默认模式开放 Read/Grep/Glob
    (chat/codemap 的 agent 自读代码,无落盘);agent_write_mode(cc 交付件落盘,
    wiki 结构/页面)在此基础上追加 Write 并提供 agent_cache 写目录(add_dirs)
    与 acceptEdits,成品经 Write 落盘。

    resolved(file 类规范化配置):config_path → ClaudeAgentOptions.settings
    (SDK 传 --settings,flag 层最高优先,仅装载所选文件;setting_sources 仍为
    [] —— 机器上其它 claude 配置不掺入 agent)。"""
    kwargs: dict[str, Any] = {
        "system_prompt": system_prompt,
        "include_partial_messages": True,
        "setting_sources": [],  # 完全隔离本地 claude 配置(用户级 MCP/skills/hooks 不掺入 agent)
    }
    if resolved.get("config_path"):
        kwargs["settings"] = resolved["config_path"]  # 纯透传:SDK 经 --settings 装载,本层不读文件
    if repo is not None:
        kwargs["cwd"] = os.path.abspath(repo.save_path)
        kwargs["mcp_servers"] = {"graphify": _graphify_server(repo)}
        tools = ["Read", "Grep", "Glob", "graphify_query", "mcp__graphify__graphify_query"]
        if agent_write_mode:
            if agent_output_dir:
                kwargs["add_dirs"] = [os.path.abspath(agent_output_dir)]
            kwargs["permission_mode"] = "acceptEdits"
            tools = ["Write", *tools]
        kwargs["allowed_tools"] = tools
    return ClaudeAgentOptions(**kwargs)


def _dsh_options(
    resolved: dict,
    system_prompt: str,
    repo: Repo | None,
    *,
    agent_output_dir: str | None = None,
    agent_write_mode: bool = False,
) -> SimpleNamespace:
    """组装 dsh 选项(鸭子类型,适配器按 attr 取;与 cc 的 _agent_options 同构):

    - file 类契约:组合置为所选配置(经 resolve 解析的 config_path —— 原
      envs.DEEPWIKI_DSH_CORDIS 语义迁移:同名 env 成为 config_path 缺省);
      config_path 空 → 适配器回退隔离默认组合(dsh_stream 对未提供的 cordis
      兜底;graphify 工具经组合内置 mcp-client 装载,对 agent 为
      mcp__graphify__query_graph,见 _agent_note)—— 上层给配置 = 全责,
      隔离与否由该配置保证;
    - system_prompt 经 env.DSH_SYSTEM_PROMPT 注入组合 persona(cc 同构);
    - model/凭证随组合配置(file 类请求无凭证字段;SDK 读进程环境兜底);
    - cwd 固定仓库根(与 cc 同:防 agent 串到 gh-puller 工作区)。
    """
    kwargs: dict[str, Any] = {
        "provider": "deepseek-official",  # dsh SDK 原生 provider 路由名(gh provider id 是 deepseek)
        "session_root": envs.DSH_SESSION_ROOT,
        "runtime_cwd": envs.DSH_RUNTIME_CWD,  # .env 加载点越过任务 checkout(见 envs)
        "env": {"DSH_SYSTEM_PROMPT": system_prompt},
    }
    if resolved.get("config_path"):
        kwargs["cordis"] = resolved["config_path"]
    if repo is not None:
        kwargs["cwd"] = os.path.abspath(repo.save_path)
        kwargs["mcp_servers"] = _graphify_mcp("dsh")  # 引擎工具桌经通用描述注入(适配层零工具名)
    return SimpleNamespace(**kwargs)


def _codex_options(
    resolved: dict,
    system_prompt: str,
    repo: Repo | None,
    *,
    agent_output_dir: str | None = None,
    agent_write_mode: bool = False,
) -> SimpleNamespace:
    """组装 codex 选项(鸭子类型,适配器按 attr 取;与 cc/dsh 的 options 构建同构):

    - file 类契约:config_path(经 resolve 解析)经 options.config_path 交给适配器,
      作隔离 home config.toml 的基底(预留 graphify MCP 段合并,见
      _codex_home_setup config_path 说明);空 → 适配器内置隔离缺省
      (config.toml 仅 graphify MCP + 符号链接引用本地凭证);
    - system_prompt 经 options.system_prompt → 适配器 thread_start.base_instructions
      (cc 的 system_prompt 同位);
    - model/凭证随配置文件(file 类请求无凭证字段;零配置缺省符号链接复用本地登录);
    - cwd 固定仓库根(与 cc 同:防 agent 串到 gh-puller 工作区);
    - sandbox 缺省 full_access(镜像 dsh 组合 danger-full-access 的高自由度;
      可经 options.sandbox 覆写 read_only/workspace_write);
    - repo 非空时经 env.GRAPHIFY_OUT 注入图目录(绝对路径 —— 隔离 config.toml 的
      env_vars 白名单透传给 graphify MCP 子进程,定位与 cwd/并发无关)。
    """
    kwargs: dict[str, Any] = {
        "system_prompt": system_prompt,
        "sandbox": "full_access",  # 高自由度缺省(镜像 dsh danger-full-access;可覆写)
        "approval_mode": "auto_review",
    }
    if resolved.get("config_path"):
        kwargs["config_path"] = resolved["config_path"]
    if repo is not None:
        kwargs["cwd"] = os.path.abspath(repo.save_path)
        kwargs["env"] = {"GRAPHIFY_OUT": str(_graph_dir(repo))}
    return SimpleNamespace(**kwargs)


async def _agent_stream(
    target: "Mapping | None", system_prompt: str, prompt: str,
    repo: Repo | None = None,
    label: str | None = None, *, run_id: str | None = None,
    context: list[dict] | None = None, retry: dict | None = None,
    agent_output_dir: str | None = None, agent_write_mode: bool = False,
):
    """agent 流式应答:统一经 generate_stream 分派(按 target.generator 选
    cc/dsh/codex 适配器,工具/隔离面 + file 类 config_path 经 _target_options
    装配);label 作为监控会话名(chat:/codemap:/wiki: 前缀区分用途);
    run_id 关联任务级会话组;context = 上下文注入/修改说明事件;
    retry = 重试元数据(见 events.py)。"""
    generator_id, resolved = _resolve_target(target)
    options = _target_options(generator_id, resolved, system_prompt, repo,
                              agent_output_dir=agent_output_dir,
                              agent_write_mode=agent_write_mode)
    async for chunk in generate_stream(prompt, target=target, options=options,
                                       session_name=label, run_id=run_id,
                                       context=context, retry=retry):
        yield chunk


def _target_options(
    generator_id: str, resolved: dict, system_prompt: str, repo: Repo | None, *,
    agent_output_dir: str | None = None, agent_write_mode: bool = False,
):
    """按 resolved 装配适配器选项(工具/隔离面 + file 类 config_path)。

    三条 agent 路的工具桌面与运行隔离各有差异(cc:进程内 MCP + --settings 文件;
    dsh:隔离组合内置 mcp-client;codex:隔离 config.toml),各自 options 构建
    函数承接;llm 路无此层(直接以 payload messages 走 dispatcher)。
    """
    if generator_id == "dsh":
        return _dsh_options(resolved, system_prompt, repo, agent_output_dir=agent_output_dir,
                            agent_write_mode=agent_write_mode)
    if generator_id == "codex":
        return _codex_options(resolved, system_prompt, repo, agent_output_dir=agent_output_dir,
                              agent_write_mode=agent_write_mode)
    return _agent_options(resolved, system_prompt, repo, agent_output_dir=agent_output_dir,
                          agent_write_mode=agent_write_mode)


def _agent_note(generator: str) -> str:
    """注入到 user 消息的指引段(供 agent 知道用图工具获取带行号的代码上下文)。

    dsh/codex 后端的图工具名不同(dsh 经组合内置 mcp-client、codex 经隔离
    config.toml 装载 graphify,对 agent 均为 mcp__graphify__query_graph;cc 为
    graphify_query)—— 指引按后端切换,防 agent 找错工具名而放弃图语境。
    """
    if generator in ("dsh", "codex"):
        return (
            "<note>You may use the mcp__graphify__query_graph tool to inspect this "
            "repository's code graph whenever you need code context or exact file/line "
            "references for citations. "
            "Its results mark sources as `Source: <file path> L<line number>`.</note>\n\n"
        )
    return (
        "<note>You may use the graphify_query tool to inspect this repository's code graph "
        "whenever you need code context or exact file/line references for citations. "
        "Its results mark sources as `Source: <file path> L<line number>`.</note>\n\n"
    )


async def _agent_text(
    target: "Mapping | None", system_prompt: str, prompt: str,
    repo: Repo | None = None,
    label: str | None = None, *, run_id: str | None = None,
    context: list[dict] | None = None, retry: dict | None = None,
) -> str:
    """agent 整收应答(用于 wiki 结构/页面、codemap 两阶段)。"""
    parts: list[str] = []
    async for chunk in _agent_stream(target, system_prompt, prompt, repo, label,
                                     run_id=run_id, context=context, retry=retry):
        parts.append(chunk)
    return "".join(parts)


async def _agent_write_file(
    target: "Mapping | None", system_prompt: str, prompt: str, repo: Repo,
    out_path: Path,
    label: str | None = None, *, run_id: str | None = None,
    context: list[dict] | None = None, retry: dict | None = None,
) -> str:
    """agent 交付件统一落盘口:提示词只给路径,agent 用自身工具读码并把成品写入 out_path;
    产生以文件为准(流式文本仅作监控/错误检测),未产出文件即任务失败。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)  # add_dirs 指向目录须先存在(agent Write 可直接落)
    generator_id, resolved = _resolve_target(target)
    options = _target_options(generator_id, resolved, system_prompt, repo,
                              agent_output_dir=str(out_path.parent), agent_write_mode=True)
    async for _ in generate_stream(prompt, target=target, options=options,
                                   session_name=label, run_id=run_id,
                                   context=context, retry=retry):
        pass
    text = await asyncio.to_thread(out_path.read_text, encoding="utf-8")
    if not text.strip():
        raise RuntimeError(f"agent 未产出交付文件: {out_path}")
    return text


# ---------------------------------------------------------------------------
# 引用后处理(移植 api/services/wiki/content.py)
# ---------------------------------------------------------------------------


class RepoUrlContext:
    """把仓库相对路径转成 web URL 所需的一切(local/无 URL → 返回裸路径)。"""

    def __init__(self, type: str, repo_url: str | None, default_branch: str):
        self.type = type
        self.repo_url = repo_url
        self.default_branch = default_branch


def generate_file_url(file_path: str, ctx: RepoUrlContext) -> str:
    if ctx.type == "local" or not ctx.repo_url:
        return file_path
    if ctx.type == "github":
        return f"{ctx.repo_url}/blob/{ctx.default_branch}/{file_path}"
    if ctx.type == "gitlab":
        return f"{ctx.repo_url}/-/blob/{ctx.default_branch}/{file_path}"
    if ctx.type == "bitbucket":
        return f"{ctx.repo_url}/src/{ctx.default_branch}/{file_path}"
    return file_path


def _escape_label(s: str) -> str:
    """转义 '[' / ']' 使路径能作为 Markdown 链接普通文本渲染。"""
    return re.sub(r"([\[\]])", r"\\\1", s)


def _line_anchor(repo_type: str, start: str | None, end: str | None) -> str:
    if not start:
        return ""
    if repo_type == "github":
        return f"#L{start}-L{end}" if end else f"#L{start}"
    if repo_type == "gitlab":
        return f"#L{start}-{end}" if end else f"#L{start}"
    if repo_type == "bitbucket":
        return f"#lines-{start}:{end}" if end else f"#lines-{start}"
    return ""


def _citation_link(path: str, start: str | None, end: str | None, ctx: RepoUrlContext) -> str | None:
    """把 `path[:start[-end]]` 解析为 Markdown 链接;local/未知 host 返回 None。"""
    url = generate_file_url(path, ctx)
    if url == path:
        return None
    line_part = (f":{start}-{end}" if end else f":{start}") if start else ""
    anchor = _line_anchor(ctx.type, start, end)
    return f"[{_escape_label(path)}{line_part}]({url}{anchor})"


_DETAILS_RE = re.compile(
    r"<details>\s*<summary>\s*Relevant source files\s*</summary>[\s\S]*?</details>",
    re.IGNORECASE,
)
_GENERIC_RE = re.compile(r"\[([^\[\]\s()]+?\.[A-Za-z0-9]+)(?::(\d+)(?:-(\d+))?)?\]\(\)")
_PREFIXED_RE = re.compile(
    r"\[(Sources?|Source):\s*([^\[\]\s():]+?)(?::(\d+)(?:-(\d+))?)?\]\(\)",
    re.IGNORECASE,
)
_STRAY_PARENS_RE = re.compile(r"(\]\([^)\s]+\))\(\)")


def post_process_wiki_content(content: str, file_paths: list[str], ctx: RepoUrlContext) -> str:
    """后处理模型产出的 wiki markdown:重建 <details> 块、解析各种空括号引用为真实链接。"""
    processed = content

    # 1. 用已知文件列表重建 <details> 块
    if file_paths:
        links = "\n".join(
            f"- [{_escape_label(p)}]({generate_file_url(p, ctx)})" for p in file_paths
        )
        details_block = (
            "<details>\n"
            "<summary>Relevant source files</summary>\n\n"
            "The following files were used as context for generating this wiki page:\n\n"
            f"{links}\n"
            "</details>"
        )
        if _DETAILS_RE.search(processed):
            processed = _DETAILS_RE.sub(lambda _m: details_block, processed)
        else:
            processed = f"{details_block}\n\n{processed}"

    # 2. 按已知 filePaths 解析空引用(最长优先)
    if file_paths:
        alternation = "|".join(re.escape(p) for p in sorted(file_paths, key=len, reverse=True))
        citation_re = re.compile(r"\[(" + alternation + r")(?::(\d+)(?:-(\d+))?)?\]\(\)")

        def _repl_known(m: re.Match) -> str:
            link = _citation_link(m.group(1), m.group(2), m.group(3), ctx)
            return link if link is not None else m.group(0)

        processed = citation_re.sub(_repl_known, processed)

    # 3. 剩余形如文件路径的空引用
    def _repl_generic(m: re.Match) -> str:
        link = _citation_link(m.group(1), m.group(2), m.group(3), ctx)
        return link if link is not None else m.group(0)

    processed = _GENERIC_RE.sub(_repl_generic, processed)

    # 4. `[Sources: 裸文件名:行]()` 通过 basename 查回全路径
    if file_paths:
        by_basename: dict[str, str] = {}
        for p in file_paths:
            by_basename.setdefault(p.rsplit("/", 1)[-1], p)

        def _repl_prefixed(m: re.Match) -> str:
            prefix, token, start, end = m.group(1), m.group(2), m.group(3), m.group(4)
            full_path = token if "/" in token else by_basename.get(token)
            if not full_path:
                return m.group(0)
            link = _citation_link(full_path, start, end, ctx)
            if link is None:
                return m.group(0)
            return f"{prefix}: {link}"

        processed = _PREFIXED_RE.sub(_repl_prefixed, processed)

    # 5. 去掉完成链接后的冗余空 "()"
    processed = _STRAY_PARENS_RE.sub(r"\1", processed)
    return processed


# ---------------------------------------------------------------------------
# wiki 结构解析(移植 api/services/wiki/structure.py)
# ---------------------------------------------------------------------------


def _normalize_importance(value: str | None) -> str:
    v = (value or "").strip().lower()
    return v if v in ("high", "medium", "low") else "medium"


def _page_from_element(el: ET.Element, index: int) -> WikiPage:
    return WikiPage(
        id=el.get("id") or f"page-{index + 1}",
        title=(el.findtext("title") or "").strip(),
        content="",
        filePaths=[e.text.strip() for e in el.iter("file_path") if e.text and e.text.strip()],
        importance=_normalize_importance(el.findtext("importance")),
        relatedPages=[e.text.strip() for e in el.iter("related") if e.text and e.text.strip()],
    )


def _pages_via_regex(xml_text: str) -> list[WikiPage]:
    """严格 XML 解析失败或零页面时的正则兜底。"""
    pages: list[WikiPage] = []
    for i, block in enumerate(re.findall(r"<page\b[\s\S]*?</page>", xml_text)):
        pid = re.search(r'<page\s+id="([^"]+)"', block)
        title = re.search(r"<title>([\s\S]*?)</title>", block)
        importance = re.search(r"<importance>([\s\S]*?)</importance>", block)
        file_paths = [m.strip() for m in re.findall(r"<file_path>([\s\S]*?)</file_path>", block) if m.strip()]
        related = [m.strip() for m in re.findall(r"<related>([\s\S]*?)</related>", block) if m.strip()]
        pages.append(
            WikiPage(
                id=pid.group(1) if pid else f"page-{i + 1}",
                title=title.group(1).strip() if title else "",
                content="",
                filePaths=file_paths,
                importance=_normalize_importance(importance.group(1) if importance else None),
                relatedPages=related,
            )
        )
    return pages


def _parse_sections(root: ET.Element) -> tuple[list[WikiSection], list[str]]:
    sections: list[WikiSection] = []
    referenced: set[str] = set()
    for i, el in enumerate(root.iter("section")):
        sid = el.get("id") or f"section-{i + 1}"
        subs = [e.text.strip() for e in el.iter("section_ref") if e.text and e.text.strip()]
        sections.append(
            WikiSection(
                id=sid,
                title=(el.findtext("title") or "").strip(),
                pages=[e.text.strip() for e in el.iter("page_ref") if e.text and e.text.strip()],
                subsections=subs or None,
            )
        )
        referenced.update(subs)
    root_sections = [s.id for s in sections if s.id not in referenced]
    return sections, root_sections


def _first_group(pattern: str, text: str) -> str:
    m = re.search(pattern, text)
    return m.group(1).strip() if m else ""


def _sections_via_regex(xml_text: str) -> tuple[list[WikiSection], list[str]]:
    """严格解析失败时恢复完整 <section> 块(镜像 _parse_sections)。"""
    sections: list[WikiSection] = []
    referenced: set[str] = set()
    for i, block in enumerate(re.findall(r"<section\b[\s\S]*?</section>", xml_text)):
        sid = re.search(r'<section\s+id="([^"]+)"', block)
        title = re.search(r"<title>([\s\S]*?)</title>", block)
        page_refs = [m.strip() for m in re.findall(r"<page_ref>([\s\S]*?)</page_ref>", block) if m.strip()]
        subs = [m.strip() for m in re.findall(r"<section_ref>([\s\S]*?)</section_ref>", block) if m.strip()]
        sections.append(
            WikiSection(
                id=sid.group(1) if sid else f"section-{i + 1}",
                title=title.group(1).strip() if title else "",
                pages=page_refs,
                subsections=subs or None,
            )
        )
        referenced.update(subs)
    root_sections = [s.id for s in sections if s.id not in referenced]
    return sections, root_sections


def parse_wiki_structure(text: str, comprehensive: bool) -> WikiStructureModel:
    """解析模型产出的 XML 结构;容错:剥 markdown fence、转义裸 &、正则兜底;无 <wiki_structure> 抛 ValueError。"""
    text = re.sub(r"^```(?:xml)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"```\s*$", "", text)

    match = re.search(r"<wiki_structure>[\s\S]*?</wiki_structure>", text)
    if match:
        xml_text = match.group(0)
    else:
        # 截断响应:从开标签救取到文末(补合成闭合),让下方正则兜底恢复完整块
        open_match = re.search(r"<wiki_structure>[\s\S]*", text)
        if not open_match:
            raise ValueError("No valid <wiki_structure> XML found in response")
        _log("响应疑似被截断(缺 </wiki_structure>),按完整块救取")
        xml_text = f"{open_match.group(0)}\n</wiki_structure>"

    xml_text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", xml_text)
    xml_text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", xml_text)

    root: ET.Element | None = None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        _log(f"严格 XML 解析失败,用正则兜底: {e}")

    if root is not None:
        title = root.findtext("title") or ""
        description = root.findtext("description") or ""
        pages = [_page_from_element(el, i) for i, el in enumerate(root.iter("page"))]
    else:
        # 头版 <title>/<description> 最先出现;页面级同名标签在后面
        title = _first_group(r"<title>([\s\S]*?)</title>", xml_text)
        description = _first_group(r"<description>([\s\S]*?)</description>", xml_text)
        pages = []

    if not pages:
        _log("XML 解析无页面,用正则兜底")
        pages = _pages_via_regex(xml_text)

    sections: list[WikiSection] = []
    root_sections: list[str] = []
    if comprehensive:
        if root is not None:
            sections, root_sections = _parse_sections(root)
        else:
            sections, root_sections = _sections_via_regex(xml_text)

    return WikiStructureModel(
        id="wiki",
        title=title.strip(),
        description=description.strip(),
        pages=pages,
        sections=sections,
        rootSections=root_sections,
    )


# ---------------------------------------------------------------------------
# wiki 缓存与导出(含判等摘要族)已迁至 ./cache.py;顶部 import 系值绑定仅供本模块
# 内部继续以裸名调用(对外形状经 same import 保持)。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# chat 服务(双路;协议/错误语义与原 research.py 对齐)
# ---------------------------------------------------------------------------


class RepoNotIndexedError(ValueError):
    """chat/codemap 请求到达时仓库尚未建图。"""


def _require_indexed(request: RepoRequestBase) -> Repo:
    """前置校验:仓库必须已建图;返回 Repo 句柄(端点层调用,失败在进生成器前即抛)。"""
    repo = Repo(request.repo_url, request.type, access_token=request.token)
    if not _index_ready(repo):
        raise RepoNotIndexedError(
            f"仓库尚未索引: {repo.name}。请先通过 /repo/prepare 建立代码图谱。"
        )
    return repo


def _format_request_fmt(request: RepoRequestBase) -> dict:
    """提示词格式化用的公共字段。"""
    return {
        "repo_type": request.type,
        "repo_url": request.repo_url,
        "repo_name": Repo(request.repo_url, request.type).name,
        "language_name": _language_name(request.language or "en"),
    }


def _resolve_chat_continuation(last: ChatMessage, messages: list[ChatMessage]) -> None:
    """continuation 回退(移植 research.py):末条含 continue+research 时换回首个用户消息(就地改 last.content)。"""
    if "continue" in last.content.lower() and "research" in last.content.lower():
        for msg in messages:
            if msg.role == "user" and "continue" not in msg.content.lower():
                last.content = msg.content.strip()
                break


async def chat_stream(request: ChatCompletionRequest):
    """一次 chat 请求的流式应答(纯文本 chunk 序列,前后端协议同原 research_chat);按 target.generator 分派双路。"""
    async for chunk in _service_pipeline(request.target).chat_stream(request):
        yield chunk



# ---------------------------------------------------------------------------
# codemap 服务(双路两阶段;NDJSON 事件协议同原)
# ---------------------------------------------------------------------------

async def generate_codemap(request: CodeMapRequest):
    """两阶段 codemap 生成(骨架 → 指南/图),NDJSON 事件流;阶段失败语义与原相同;按 target.generator 分派双路。"""
    async for ev in _service_pipeline(request.target).generate_codemap(request):
        yield ev


# ---------------------------------------------------------------------------
# wiki 任务状态机(移植 api/services/wiki/tasks.py;RAG → agent + graphify)
# 状态机/去重/并发/TTL 基类在 utils.TaskRegistry,此处仅注入 wiki 特化钩子
# ---------------------------------------------------------------------------


class WikiTask(Task):
    """单个仓库生成任务的内存态状态(状态/错误/提交时间/运行时任务由 Task 基类承担)。"""

    request: WikiTaskRequest
    pages_done: int = 0
    current_page_ids: list[str] = Field(default_factory=list)
    generated_pages: dict[str, WikiPage] = Field(default_factory=dict)  # 完成页就地累积(续跑=已生成页)
    wiki_structure: WikiStructureModel | None = None
    default_branch: str = "main"  # 确定结构时设置,用于文件 URL

    @computed_field
    @property
    def pages_total(self) -> int:
        if self.wiki_structure is not None:
            return len(self.wiki_structure.pages)
        return 0

    @classmethod
    def from_wiki_request(cls, request: WikiTaskRequest) -> "WikiTask":
        return cls(request=request)

    @property
    def repo_key(self) -> str:
        return self.request.repo_key

    @property
    def key(self) -> str:
        """注册表去重键 = repo 键 + target 判等摘要:同一仓库/语言下
        不同 target 的任务可并发并存(隔离生成产物与续跑状态)。"""
        return f"{self.repo_key}@{_request_digest(self.request.target)}"

    def to_status(self) -> WikiTaskStatus:
        r = self.request
        return WikiTaskStatus(
            id=self.key,
            owner=r.owner,
            repo=r.repo,
            repo_type=r.type,
            language=r.language,
            status=self.status,
            pages_done=self.pages_done,
            pages_total=self.pages_total,
            current_page_ids=self.current_page_ids,
            wiki_structure=self.wiki_structure,
            error=self.error,
            submitted_at=self.submitted_at,
        )

    def to_summary(self) -> WikiTaskSummary:
        r = self.request
        return WikiTaskSummary(
            id=self.key,
            owner=r.owner,
            repo=r.repo,
            repo_type=r.type,
            language=r.language,
            status=self.status,
            pages_done=self.pages_done,
            pages_total=self.pages_total,
            current_page_ids=self.current_page_ids,
            error=self.error,
            submitted_at=self.submitted_at,
        )


class WikiTaskRegistry(TaskRegistry):
    """wiki 专属提交语义(缓存胜/续跑/生成器执行)经钩子注入;TTL 读模块全局供测试 monkeypatch。"""

    async def run(self, task: WikiTask) -> None:
        await generate_repo_wiki(task)  # 调用时经模块全局解析(monkeypatch 生效)

    async def is_cached(self, task: WikiTask) -> bool:
        r = task.request
        cache = await read_wiki_cache(
            r.owner, r.repo, r.type, r.language, digest=_request_digest(r.target)
        )
        if cache is None:
            return False
        # 判等身份与缓存内记录对齐(旧缓存字段缺失/旧契约 → 判不匹配,重新生成)
        if _cache_target_matches(cache, r.target):
            return True
        _log(
            f"成品缓存 target 不匹配({_cache_identity(cache)!r} vs "
            f"{_resolve_target(r.target)!r}),忽略并重新生成: {r.owner}/{r.repo}"
        )
        return False

    async def on_cache_hit(self, task: WikiTask) -> None:
        r = task.request
        await delete_wiki_task_state(
            r.owner, r.repo, r.type, r.language, digest=_request_digest(r.target)
        )

    async def load_resume(self, task: WikiTask) -> WikiTask | None:
        r = task.request
        state = await read_wiki_task_state(
            r.owner, r.repo, r.type, r.language, digest=_request_digest(r.target)
        )
        if state is None:
            return None
        # 状态文件按 target 摘要隔离(同仓库不同 target 并存);凭证从当前提交合并
        # (落盘状态只存公开三元组,见 _persist_state)
        merged = state.request.model_copy(
            update={"target": _merge_creds(state.request.target, r.target)}
        )
        return WikiTask(
            request=merged,
            status=(
                TaskStatus.GENERATING
                if state.wiki_structure is not None
                else TaskStatus.DETERMINING_STRUCTURE
            ),
            pages_done=len(state.generated_pages),
            wiki_structure=state.wiki_structure,
            default_branch=state.default_branch,
            submitted_at=state.submitted_at,  # 保留原始提交时间
            generated_pages=state.generated_pages,
        )

    def _ttl_seconds(self) -> float:
        # call-time 读模块全局:tests monkeypatch deepwiki._WIKI_TASK_TTL_SECONDS
        return _WIKI_TASK_TTL_SECONDS


registry = WikiTaskRegistry(
    max_concurrent=_MAX_CONCURRENT_WIKI_TASKS,
    ttl_seconds=_WIKI_TASK_TTL_SECONDS,
)


# ---------------------------------------------------------------------------
# 双路包装类(WikiPipeline / AgentWikiPipeline / LlmWikiPipeline 与分派函数
# _wiki_pipeline / _service_pipeline)已迁至 ./pipeline.py;顶部 re-export
# 保持对外形状(边界约定见其模块 docstring)。
# ---------------------------------------------------------------------------


async def generate_repo_wiki(task: WikiTask) -> None:
    """驱动一个任务走完状态机(索引 → 结构 → 页面 → 缓存),失败置 FAILED。

    进度中途落盘(deepwiki_taskstate_*):结构确定后与每页完成后各写一次,
    失败/取消也尽力写;同仓库再次提交时从落盘状态续跑(见 TaskRegistry.submit)。
    """
    r = task.request
    try:
        await _persist_state(task)  # 入口即落盘:中断于索引/结构阶段的也能续跑
        pipeline = _wiki_pipeline(r.target)
        repo = Repo(r.repo_url, r.type, access_token=r.token)
        # 索引:只建一次(v1 无增量;已存在即跳过)
        if not _index_ready(repo):
            task.status = TaskStatus.INDEXING
            _log(f"索引中: {task.repo_key}")
            if not repo.downloaded and not repo.is_local:
                await asyncio.to_thread(repo.download)
            result = await _run_extract(repo, r)
            if result.get("error"):
                raise RuntimeError(result["error"])

        if task.wiki_structure is None or pipeline.needs_structure_regenerate(task):
            # 续跑:结构已落盘(cc 下以交付文件为准,被删则强制重生成)则跳过 agent 调用
            task.status = TaskStatus.DETERMINING_STRUCTURE
            task.wiki_structure = await _determine_structure(task)
            await _persist_state(task)

        task.status = TaskStatus.GENERATING
        await pipeline.hydrate_pages(task)  # cc 以文件为权威覆盖落盘 state 旧文本;llm no-op
        pages = await _generate_pages(task, task.wiki_structure)

        if not await _save(task, pages):
            raise RuntimeError("写 wiki 缓存失败")  # 不删状态:再提交仅重试写缓存
        await delete_wiki_task_state(
            r.owner, r.repo, r.type, r.language, digest=_request_digest(r.target)
        )
        task.status = TaskStatus.COMPLETED
        _log(f"wiki 任务完成: {task.repo_key}")
    except asyncio.CancelledError:  # Ctrl+C/停机:尽力持久化一次后重新抛出
        await _persist_state(task)
        raise
    except Exception as e:
        task.status = TaskStatus.FAILED
        task.error = str(e)
        await _persist_state(task)  # FAILED 也落盘,后续提交可续跑
        _log(f"wiki 任务失败: {task.repo_key} - {e}")


async def _determine_structure(task: WikiTask) -> WikiStructureModel:
    """确定 wiki 结构(按 target.generator 分派 cc/dsh/codex/llm);失败上抛使任务 FAILED。"""
    r = task.request
    repo = Repo(r.repo_url, r.type, access_token=r.token)
    if not repo.is_local and not repo.downloaded:
        await asyncio.to_thread(repo.download)

    task.default_branch = await asyncio.to_thread(detect_default_branch, repo.save_path)
    file_tree, readme = await asyncio.to_thread(
        read_repo_file_tree,
        repo.save_path,
        r.included_files,
        r.included_dirs,
        r.excluded_files,
        r.excluded_dirs,
    )
    return await _wiki_pipeline(task.request.target).determine_structure(task, repo, file_tree, readme)


async def _generate_page(task: WikiTask, page: WikiPage) -> WikiPage:
    """生成单个页面:cc 落盘读回 / llm 单次补全 → 剥 fence → 引用后处理。"""
    r = task.request
    repo = Repo(r.repo_url, r.type, access_token=r.token)
    ctx = RepoUrlContext(type=r.type, repo_url=r.repo_url, default_branch=task.default_branch)
    file_links = "\n".join(f"- [{p}]({generate_file_url(p, ctx)})" for p in page.filePaths)
    content = await _wiki_pipeline(task.request.target).generate_page(task, repo, page, file_links)
    content = _strip_markdown_fences(content)
    content = post_process_wiki_content(content, list(page.filePaths), ctx)
    return page.model_copy(update={"content": content})


async def _generate_page_with_retry(task: WikiTask, page: WikiPage) -> WikiPage:
    last_error: Exception | None = None
    for attempt in range(_WIKI_PAGE_RETRIES + 1):
        try:
            return await _generate_page(task, page)
        except Exception as e:  # noqa: BLE001 - 瞬时/永久错误统一由重试预算兜底
            last_error = e
            _log(f"页面 {page.id} 生成失败(尝试 {attempt + 1}/{_WIKI_PAGE_RETRIES + 1}): {e}")
    # 重试耗尽:回退错误占位页,保证整个 wiki 仍能完成
    content = f"Error generating content: {last_error}"
    _wiki_pipeline(task.request.target).write_error_page(task, page, content)
    return page.model_copy(update={"content": content})


def _pending_pages(structure: WikiStructureModel, done: dict[str, WikiPage]) -> list[WikiPage]:
    """按结构顺序返回尚未生成的页面(done: 已完成页 id → 页)。"""
    return [p for p in structure.pages if p.id not in done]


async def _generate_pages(task: WikiTask, structure: WikiStructureModel) -> dict[str, WikiPage]:
    """有界并发 + 每页重试地生成所有页面;续跑跳过已落盘的页,每页完成后立即落盘。"""
    sema = asyncio.Semaphore(_WIKI_PAGE_CONCURRENCY)
    task.pages_done = len(task.generated_pages)  # 续跑:从恢复的完成数起步
    pending = _pending_pages(structure, task.generated_pages)

    async def one(page: WikiPage) -> None:
        async with sema:
            task.current_page_ids.append(page.id)
            try:
                task.generated_pages[page.id] = await _generate_page_with_retry(task, page)
            finally:
                with contextlib.suppress(ValueError):
                    task.current_page_ids.remove(page.id)
                task.pages_done += 1
            await _persist_state(task)  # 每页完成即落盘(锁内串行写)

    await asyncio.gather(*(one(page) for page in pending))
    return task.generated_pages


async def _save(task: WikiTask, pages: dict[str, WikiPage]) -> bool:
    assert task.wiki_structure is not None
    generator_id, resolved = _resolve_target(task.request.target)
    identity = _target_identity(generator_id, resolved)  # file 类:config_path;object:"provider|model"
    object_parts = identity.split("|", 1)
    return await save_wiki_cache(
        owner=task.request.owner,
        repo=task.request.repo,
        repo_type=task.request.type,
        language=task.request.language,
        digest=_target_digest_of(generator_id, resolved),
        wiki_cache=WikiCacheData(
            wiki_structure=task.wiki_structure,
            generated_pages=pages,
            generator=generator_id,  # 成品缓存记判等身份,cache 命中时校验(见 is_cached)
            provider=object_parts[0] if len(object_parts) > 1 else None,  # object 类才落
            model=object_parts[1] if len(object_parts) > 1 else "",  # 旧缓存兼容字段
            config_path=identity if GENERATORS[generator_id].config_kind == "file" else None,
            repo=RepoInfo(
                owner=task.request.owner,
                repo=task.request.repo,
                type=task.request.type,
                token=None,  # 缓存文件不落 token
                repoUrl=task.request.repo_url,
            ),
        ),
    )


