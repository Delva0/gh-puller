"""deepwiki 引擎共用 helper(generator 选型/判等/凭证规则簇 + 域内日志 + repo 键
+ 跨功能通用收编:四路装配、llm 传输、llm 路补全协议、Llm 路检索工具簇、
提示词共性常量、索引保障服务)。

规划格局(按功能为主线):wiki/chat/codemap 三个功能模块各自收编本功能专用
helper;跨功能通用 helper 归本模块,由功能模块经本模块**属性调用**
(utils.xxx 调用时取 —— llm_stream/llm_complete 等 monkeypatch 位点打在本模块,
不得 from-import 后裸名调用,测试 patch 活性依赖)。
本模块因 _graphify_server/adapter 需与 claude_agent_sdk 顶层 import,__非__
sdk-free(wiki/cache 保持 sdk-free);被 wiki/chat/codemap 直连,deepwiki 主干
白名单 re-export。

术语:引擎内部一律说 generator(选型 dict = {generator, generator_config} 已
**拆除**:函数签名统一为 generator + generator_config 两个散装参数,wire 字段
"target" 只在 app 层存在,由 apps/deepwiki-webui 拆包传入;解析/判等经
resolve_generator 唯一知识源)。envs/graphify 保持模块对象绑定 + 属性调用
(调用时取;测试 monkeypatch/强刷活性)。对外函数无下划线前缀(常数与
纯内部 helper 除外)。
"""

import asyncio
import hashlib
import os
import sys
from functools import partial
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .. import envs, graphify  # 模块对象绑定:属性一律调用时取(patch/强刷活性)
from ..agent import GENERATORS, RequestFailedError
from ..utils import Repo
from ..utils import _log as _utils_log

# 进度日志走 stderr(同 graphify.py 约定);prefix 固定 [deepwiki]
log = partial(_utils_log, prefix="deepwiki")


# ---------------------------------------------------------------------------
# generator 选型(解析 / 判等身份 / 凭证落盘规则;唯一知识源)
# ---------------------------------------------------------------------------


def _default_get_env(key: str) -> str:
    """env 缺省桥接(envs 模块对象 getattr 调用时取 —— 测试 monkeypatch/pop+delattr 强刷生效)。"""
    return getattr(envs, key, "") or ""


# file 类生成器(它们的 config 是一条配置文件路径)→ config_path 的 env 缺省键;
# 这是本层的契约知识(agent 包不提供任何缺省/元数据假设,见 configs.py 上层自验哲学)。
_FILE_CONFIG_PATH_ENV = {"cc": "DEEPWIKI_CC_CONFIG", "dsh": "DEEPWIKI_DSH_CORDIS",
                         "codex": "DEEPWIKI_CODEX_CONFIG"}


def resolve_generator(generator: str | None = None, generator_config: dict | None = None,
                      get_env=None) -> tuple[str, dict]:
    """generator 选型 → (generator id, 规范化配置);空选型走 env 缺省(与运行期一致)。

    选型 dict({generator, generator_config})在引擎内已拆除为两个散装参数;
    未知 id 报错;file 类(cc/dsh/codex)config_path = 显式 > env 缺省(> 空),
    ~ 展开并绝对化 —— 对象类(llm)透传(api_key/base_url 等由调用方自管)。
    envs 走模块对象 getattr(测试 monkeypatch 与 pop+delattr 强刷均生效)。
    """
    if get_env is None:
        get_env = _default_get_env
    gen_id = generator or get_env("DEEPWIKI_GENERATOR") or "cc"
    if gen_id not in GENERATORS:
        raise ValueError(f"未知 generator: {gen_id!r}(可选 {sorted(GENERATORS)})")
    resolved = dict(generator_config or {})
    env_key = _FILE_CONFIG_PATH_ENV.get(gen_id)
    if env_key:
        config_path = resolved.get("config_path") or get_env(env_key) or ""
        if config_path:
            config_path = os.path.abspath(os.path.expanduser(config_path))
        resolved["config_path"] = config_path
    return gen_id, resolved


def config_kind(generator_id: str) -> str:
    """file/object 配置类别(cache 落盘/凭证处置决策用)。"""
    return "file" if generator_id in _FILE_CONFIG_PATH_ENV else "object"


def generator_identity(generator_id: str, resolved: dict) -> str:
    """判等身份(不含凭证):file 类 = config_path;object 类 = "provider|model"。"""
    if config_kind(generator_id) == "file":
        return resolved.get("config_path", "") or ""
    return f"{resolved.get('provider', '')}|{resolved.get('model', '')}"


def strip_creds(config: dict) -> dict:
    """落盘形态:拷贝并剥离 generator_config 内 api_key/base_url(config_path 非凭证,保留)。"""
    out = dict(config)
    gc = dict(out.get("generator_config") or {})
    gc.pop("api_key", None)
    gc.pop("base_url", None)
    out["generator_config"] = gc
    return out


def merge_creds(base: dict, other: dict | None) -> dict:
    """落盘形态保持自身;object 类凭证(api_key/base_url 于 generator_config 内)取 other。"""
    out = dict(base)
    oc = dict((other or {}).get("generator_config") or {})
    merged = dict(out.get("generator_config") or {})
    for key in ("base_url", "api_key"):
        if oc.get(key):
            merged[key] = oc[key]
    out["generator_config"] = merged
    return out


def repo_key_of(repo_type: str, owner: str, repo: str) -> str:
    """repo 键(type_owner_repo;与任务注册键/交付件目录前缀同式)。"""
    return f"{repo_type}_{owner}_{repo}"


# ---------------------------------------------------------------------------
# 图产物路径/索引就绪 + 判等摘要族(原 cache.py;图路径属公用基建,cache 消除后
# 归本模块。ready = graph.json 存在;digest 是选型判等身份的 8-hex 摘要,
# 任务 id / 续跑状态 / 成品缓存路径共用同一判等)
# ---------------------------------------------------------------------------


def graph_dir(repo: Repo) -> Path:
    """单仓库图产物根(extract 的 out_dir):graphify/{type}_{name},无日期层,路径稳定以支持已缓存即跳过。"""
    return Path(envs.DEEPWIKI_ROOT) / "graphify" / f"{repo.repo_type}_{repo.name}"


def graph_path(repo: Repo) -> Path:
    """graph.json 规范路径(extract 的 out_dir 即最终目录,无 graphify-out 层)。"""
    return graph_dir(repo) / "graph.json"


def index_ready(repo: Repo) -> bool:
    """索引完成信号 = graph.json 已存在(复用 graphify._load_graph 的存在性语义)。"""
    return graph_path(repo).exists()


def generator_digest(generator: str | None = None, generator_config: dict | None = None,
                     get_env=None) -> str:
    """generator 选型判等身份(不含凭证)的稳定摘要(8 hex)。

    身份 = generator + 配置摘要:file 类 = config_path(路径是身份;内容随文件,
    不读取);object 类 = provider|model。任务 id / 续跑状态 / 成品缓存路径共用:
    同一仓库与语言下不同选型的结果可以并发并存且互不串用。
    """
    generator_id, resolved = resolve_generator(generator, generator_config, get_env)
    return _generator_digest_of(generator_id, resolved)


def _generator_digest_of(generator_id: str, resolved: dict) -> str:
    return hashlib.sha1(
        f"{generator_id}|{generator_identity(generator_id, resolved)}".encode()
    ).hexdigest()[:8]


def cache_identity(cache: dict) -> tuple[str, str]:
    """缓存内记录的公开身份(file 类:generator+config_path;object 类:generator+provider|model)。"""
    generator = cache.get("generator") or ""
    config_path = cache.get("config_path") or ""
    if generator and config_path:
        return (generator, config_path)
    return (generator, f"{cache.get('provider') or ''}|{cache.get('model') or ''}")


def cache_generator_matches(cache: dict, generator: str | None = None,
                            generator_config: dict | None = None) -> bool:
    """成品缓存与公开选型是否同轨(摘要隔离后的二次校验,防手改文件名)。"""
    generator_id, resolved = resolve_generator(generator, generator_config)
    return cache_identity(cache) == (generator_id, generator_identity(generator_id, resolved))


# ---------------------------------------------------------------------------
# 提示词共性常量(跨功能:wiki(llm 路 system)/chat/codemap 共用;
# 语言展示表 _LANGUAGE_NAMES 仅 HTTP 层用时,已在 apps/deepwiki-webui/server/app.py)
# ---------------------------------------------------------------------------

_LANGUAGE_NAMES_RAW = {
    "en": "English",
    "ja": "Japanese (日本語)",
    "zh": "Mandarin Chinese (中文)",
    "zh-tw": "Traditional Chinese (繁體中文)",
    "es": "Spanish (Español)",
    "kr": "Korean (한국어)",
    "vi": "Vietnamese (Tiếng Việt)",
    "pt-br": "Brazilian Portuguese (Português Brasileiro)",
    "fr": "Français (French)",
    "ru": "Русский (Russian)",
}


def language_name(language: str) -> str:
    """语言名(缺省 English;未知语言也回退 English,与原 lang.json 语义一致)。"""
    return _LANGUAGE_NAMES_RAW.get(language, "English")


_SIMPLE_CHAT_SYSTEM_PROMPT = """<role>
You are an expert code analyst examining the {repo_type} repository: {repo_url} ({repo_name}).
You provide direct, concise, and accurate information about code repositories.
You NEVER start responses with markdown headers or code fences.
IMPORTANT:You MUST respond in {language_name} language.
</role>

<guidelines>
- Answer the user's question directly without ANY preamble or filler phrases
- DO NOT include any rationale, explanation, or extra comments.
- DO NOT start with preambles like "Okay, here's a breakdown" or "Here's an explanation"
- DO NOT start with markdown headers like "## Analysis of..." or any file path references
- DO NOT start with ```markdown code fences
- DO NOT end your response with ``` closing fences
- DO NOT start by repeating or acknowledging the question
- JUST START with the direct answer to the question

<example_of_what_not_to_do>
```markdown
## Analysis of `adalflow/adalflow/datasets/gsm8k.py`

This file contains...
```
</example_of_what_not_to_do>

- Format your response with proper markdown including headings, lists, and code blocks WITHIN your answer
- For code analysis, organize your response with clear sections
- Think step by step and structure your answer logically
- Start with the most relevant information that directly addresses the user's query
- Be precise and technical when discussing code
- Your response language should be in the same language as the user's query
</guidelines>

<style>
- Use concise, direct language
- Prioritize accuracy over verbosity
- When showing code, include line numbers and file paths when relevant
- Use markdown formatting to improve readability
</style>"""


def agent_note(generator: str) -> str:
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


def prompt_fmt(repo: Repo, *, language: str = "en") -> dict:
    """提示词格式化用的公共字段(repo 域对象 + 语言散装)。"""
    return {
        "repo_type": repo.repo_type,
        "repo_url": repo.repo_url,
        "repo_name": repo.name,
        "language_name": language_name(language or "en"),
    }


# ---------------------------------------------------------------------------
# 四路装配(适配器构造入口 adapter + graphify MCP 工具桌 + llm 传输)
# ---------------------------------------------------------------------------


def _graphify_mcp(backend: str) -> list[dict]:
    """引擎图工具桌(graphify 属引擎内建)→ 适配层通用 mcp_servers 描述。

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

    gp = graph_path(repo)

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
            result = graphify.query(question, graph_path=gp)
            text = result.get("answer") or ""
        except Exception as exc:
            text = f"Graph query failed: {type(exc).__name__}: {exc}"
        return {"content": [{"type": "text", "text": text.strip() or "(No matching results in code graph)"}]}

    return create_sdk_mcp_server("graphify", tools=[graphify_query])


def adapter(generator: str | None = None, *, generator_config: dict | None = None,
            system_prompt: str = "", repo: Repo | None = None,
            agent_output_dir: str | None = None, agent_write_mode: bool = False):
    """generator → 适配器实例(四路收敛构造入口;≈ GENERATORS[gid](config) 一行)。

    gid 经 resolve_generator(file 类 config_path 规范化);llm 路 resolved 即
    OpenAIConfig(概念键透传)。cc/dsh/codex 按 agent.configs.py TypedDict 键集
    组装 config dict(空/None 不落键;SDK 映射全在 agent 侧 —— 本层零 SDK 字段名;
    config_path 纯透传,本层不读文件;模型/凭证随所选配置):
    - cc:工具隔离(setting_sources=[] + 内置 graphify 进程内 server),repo 非空时
      cwd 固定仓库根(SDK 缺省 = 进程 cwd,曾导致 agent 串到 gh-puller 把 docs
      写入其中);agent_write_mode(交付件落盘,wiki 结构/页面)追加 Write/add_dirs/
      acceptEdits,默认模式只开放 Read/Grep/Glob(chat/codemap 的 agent 自读代码,
      无落盘);
    - dsh:provider/session_root/runtime_cwd + system_prompt → 组合 persona
      (非空才注入,空回退缺省);mcp_servers 通用描述注入图工具桌
      (适配层零工具名;对 agent 为 mcp__graphify__query_graph,见 agent_note);
    - codex:system_prompt → thread_start.base_instructions;config_path 纯透传
      (home config.toml 符号链接);mcp_servers 通用描述 + env.GRAPHIFY_OUT 注入
      隔离 home 工具桌(零配置缺省即带图;sandbox 缺省 full_access,镜像 dsh
      danger-full-access,可覆写)。

    一个实例 = 一次对话(重试/每阶段刷新构造;构造期即装配 SDK 对象)。
    """
    gid, resolved = resolve_generator(generator, generator_config)
    if gid == "llm":
        return GENERATORS["llm"](resolved)
    if gid == "dsh":
        options: dict[str, Any] = {
            "provider": "deepseek-official",  # dsh SDK 原生 provider 路由名(gh provider id 是 deepseek)
            "session_root": envs.DSH_SESSION_ROOT,
            "runtime_cwd": envs.DSH_RUNTIME_CWD,  # .env 加载点越过任务 checkout(见 envs)
            "system_prompt": system_prompt,  # → 组合 persona(agent dsh_fields 映射,空则缺省)
        }
        if resolved.get("config_path"):
            options["config_path"] = resolved["config_path"]
        if repo is not None:
            options["cwd"] = os.path.abspath(repo.save_path)
            options["mcp_servers"] = _graphify_mcp("dsh")  # 引擎工具桌经通用描述注入(适配层零工具名)
    elif gid == "codex":
        options = {
            "system_prompt": system_prompt,
            "sandbox": "full_access",  # 高自由度缺省(镜像 dsh danger-full-access;可覆写)
            "approval_mode": "auto_review",
        }
        if resolved.get("config_path"):
            options["config_path"] = resolved["config_path"]
        if repo is not None:
            options["cwd"] = os.path.abspath(repo.save_path)
            options["env"] = {"GRAPHIFY_OUT": str(graph_dir(repo))}
            options["mcp_servers"] = _graphify_mcp("codex")  # 零配置缺省隔离 config.toml 带图工具
    else:  # cc
        options = {
            "system_prompt": system_prompt,
            "include_partial_messages": True,
            "setting_sources": [],  # 完全隔离本地 claude 配置(用户级 MCP/skills/hooks 不掺入 agent)
        }
        if resolved.get("config_path"):
            options["config_path"] = resolved["config_path"]  # 纯透传:SDK 经 --settings 装载,本层不读文件
        if repo is not None:
            options["cwd"] = os.path.abspath(repo.save_path)
            options["mcp_servers"] = {"graphify": _graphify_server(repo)}
            tools = ["Read", "Grep", "Glob", "graphify_query", "mcp__graphify__graphify_query"]
            if agent_write_mode:
                if agent_output_dir:
                    options["add_dirs"] = [os.path.abspath(agent_output_dir)]
                options["permission_mode"] = "acceptEdits"
                tools = ["Write", *tools]
            options["allowed_tools"] = tools
    return GENERATORS[gid](options)


async def llm_stream(prompt: str, *, generator: str | None, generator_config: dict | None,
                     session_name: str, run_id: str):
    """llm 路统一流式补全口(单条 user 消息;payload 独立于 config 运行时传入)。

    内部经 adapter 构造(模型/url/凭证随所选 generator);本模块是测试
    monkeypatch 位点(经 utils.llm_stream 属性替换)。
    """
    inst = adapter(generator, generator_config=generator_config)
    payload = {"messages": [{"role": "user", "content": prompt}], "model": inst.config.get("model")}
    async for chunk in inst.stream(payload, session_name=session_name, run_id=run_id):
        yield chunk


async def llm_complete(prompt: str, *, generator: str | None, generator_config: dict | None,
                       session_name: str, run_id: str) -> str:
    """llm 路统一整收补全口(单条 user 消息;同 llm_stream 装配)。"""
    inst = adapter(generator, generator_config=generator_config)
    payload = {"messages": [{"role": "user", "content": prompt}], "model": inst.config.get("model")}
    return await inst.result(payload, session_name=session_name, run_id=run_id)


def failure(exc: Exception) -> Exception:
    """RequestFailedError → RuntimeError("agent 执行失败: ...")(对外文案,
    原 agent.generate_* file 类分支的包装);其余异常原样返回。
    链式追溯由唯一 raise 点(_deliver)以 raise ... from 补上。"""
    if isinstance(exc, RequestFailedError):
        return RuntimeError(f"agent 执行失败: {exc.detail}")
    return exc


# ---------------------------------------------------------------------------
# 索引保障服务(clone + 建图;/repo/prepare 与 wiki 任务主流程共用。
# 未索引前置校验属端点守卫,在 apps/deepwiki-webui/server/app.py)
# ---------------------------------------------------------------------------


async def _run_extract(repo: Repo, *, extra_excludes: list[str] | None = None) -> dict:
    """graphify.extract 建图(code_only 纯本地 AST,无 key 可跑);失败返回错误态 dict,不抛。"""
    return await asyncio.to_thread(
        graphify.extract,
        path=repo.save_path,
        code_only=True,
        out_dir=graph_dir(repo),
        extra_excludes=extra_excludes,
    )


async def ensure_index(repo: Repo, *, extra_excludes: list[str] | None = None) -> None:
    """索引保障(克隆 + 建图):已 ready 直接返回。

    克隆判断独立于 ready 与否 —— graph.json 在但克隆目录被删时补克隆
    (防文件树静默退化为空);建图失败(extract 错误态)→ RuntimeError 上抛,
    由调用方决定上报/置任务 FAILED。
    """
    if not repo.downloaded and not repo.is_local:
        await asyncio.to_thread(repo.download)
    if not index_ready(repo):
        result = await _run_extract(repo, extra_excludes=extra_excludes)
        if result.get("error"):
            raise RuntimeError(result["error"])
