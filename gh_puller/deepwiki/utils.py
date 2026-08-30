"""deepwiki 引擎共用 helper(generator 选型/判等/凭证规则簇 + 域内日志 + repo 键

+ 跨功能通用收编:四路装配、llm 传输、llm 路补全协议、提示词共性常量)。

规划格局(按功能为主线):wiki/chat/codemap 三个功能模块各自收编本功能专用
helper;跨功能通用 helper 归本模块,由功能模块经本模块**属性调用**
(utils.xxx 调用时取 —— llm_stream/llm_complete 等 monkeypatch 位点打在本模块,
不得 from-import 后裸名调用,测试 patch 活性依赖)。

本模块 sdk-free / 零工具假设:生成器构造(adapter)只做 GENERATORS[id](options)
四路收敛 + 白名单透传;工具配置(mcp_servers/env/工具名)、工具指引文本
(tool_note/codemap_note)与图服务(generator_config["graph"]:ready/context)全部
由上层经覆盖构造参数注入,本层不假设任何工具由上层提供。装配在
apps/deepwiki-webui/server/generators.py。agent 契约类型(GENERATORS/
RequestFailedError)保留(生成器依赖的定义来源)。

术语:引擎内部一律说 generator(选型 dict = {generator, generator_config} 已
**拆除**:函数签名统一为 generator + generator_config 两个散装参数,wire 字段
"target" 只在 app 层存在,由 apps/deepwiki-webui 拆包传入;解析/判等经
resolve_generator 唯一知识源)。envs 保持模块对象绑定 + 属性调用
(调用时取;测试 monkeypatch/强刷活性)。对外函数无下划线前缀(常数与
纯内部 helper 除外)。
"""

import hashlib
import os
from functools import partial
from typing import Any

from .. import envs  # 模块对象绑定:属性一律调用时取(patch/强刷活性)
from ..agent import GENERATORS, RequestFailedError
from ..utils import Repo, _estimate_tokens
from ..utils import _log as _utils_log

# 进度日志走 stderr(人类可读诊断,机器结果走调用方);prefix 固定 [deepwiki]
log = partial(_utils_log, prefix="deepwiki")


# ---------------------------------------------------------------------------
# generator 选型(解析 / 判等身份 / 凭证落盘规则;唯一知识源)
# ---------------------------------------------------------------------------


def _default_get_env(key: str) -> str:
    """env 缺省桥接(envs 模块对象 getattr 调用时取 —— 测试 monkeypatch/pop+delattr 强刷生效)。"""
    return getattr(envs, key, "") or ""


# file 类生成器(它们的 config 是一条配置文件路径)→ config_path 的 env 缺省键;
# 这是本层的契约知识(agent 包不提供任何缺省/元数据假设,上层自验哲学见 generators/ 各文件 config 契约)。
_FILE_CONFIG_PATH_ENV = {"cc": "DEEPWIKI_CC_CONFIG", "dsh": "DEEPWIKI_DSH_CORDIS",
                         "codex": "DEEPWIKI_CODEX_CONFIG"}


def resolve_generator(generator: str | None = None, generator_config: dict | None = None,
                      get_env=None) -> tuple[str, dict]:
    """generator 选型 → (generator id, 规范化配置);空选型 = 引擎内建缺省 cc。

    选型 dict({generator, generator_config})在引擎内已拆除为两个散装参数;
    未知 id 报错;file 类(cc/dsh/codex)config_path = 显式 > env 缺省(> 空),
    ~ 展开并绝对化 —— 对象类(llm)透传(api_key/base_url 等由调用方自管)。
    "缺省生成器"是上层(webui)政策:由 apps/deepwiki-webui/server/app.py 边界注入
    (DEEPWIKI_GENERATOR),引擎本身不读 env 选型;get_env 仅服务 file 类
    config_path 的 env 缺省,走模块对象 getattr(测试 monkeypatch 与
    pop+delattr 强刷均生效)。
    """
    if get_env is None:
        get_env = _default_get_env
    gen_id = generator or "cc"
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
# 判等摘要族(原 cache.py;digest 是选型判等身份的 8-hex 摘要,
# 任务 id / 续跑状态 / 成品缓存路径共用同一判等。图产物路径与索引就绪
# 属图知识 — 在 apps/deepwiki-webui/server/generators.py)
# ---------------------------------------------------------------------------


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
    # sha1 仅作生成器身份指纹(缓存摘要),非安全用途
    return hashlib.sha1(  # noqa: S324
        f"{generator_id}|{generator_identity(generator_id, resolved)}".encode(),
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


def prompt_fmt(repo: Repo, *, language: str = "en") -> dict:
    """提示词格式化用的公共字段(repo 域对象 + 语言散装)。"""
    return {
        "repo_type": repo.repo_type,
        "repo_url": repo.repo_url,
        "repo_name": repo.name,
        "language_name": language_name(language or "en"),
    }


# ---------------------------------------------------------------------------
# 四路装配(适配器构造入口 adapter;上层经 generator_config 注入工具配置,
# 本层零 SDK/零工具假设)
# ---------------------------------------------------------------------------


# 引擎私有键(由上层 generator_config 注入、仅供引擎内部消费,不得落 SDK 配置):
# graph = 图服务(ready/context);tool_note/codemap_note = 上层工具指引文本,
# 装配在 apps/deepwiki-webui/server/generators.py
_ENGINE_KEYS = ("graph", "tool_note", "codemap_note")

# 各生成器透传白名单(镜像 agent/generators/ 各文件 TypedDict 键集;app 经
# generator_config 传入的覆盖构造参数,仅白名单键透传 —— 未知键不落 SDK:
# cc 的 ClaudeAgentOptions(**config) 对未知键会 TypeError)
_CC_PASSTHROUGH: tuple[str, ...] = ("model", "max_turns", "config_path")
_DSH_PASSTHROUGH: tuple[str, ...] = ("provider", "model", "max_tokens", "config_path",
                                     "base_url", "api_key", "runtime_bin",
                                     "launch_args_override", "request_timeout_seconds",
                                     "shutdown_timeout_seconds")
_CODEX_PASSTHROUGH: tuple[str, ...] = ("model", "config_path", "token", "timeout_seconds",
                                       "allowed_tools", "effort", "output_schema",
                                       "config_overrides", "launch_args_override",
                                       "base_instructions", "service_tier", "summary",
                                       "web_search")


def _merge(dest: dict, src: dict, keys: tuple[str, ...]) -> None:
    """白名单透传(src 键值非空才落;上层经 generator_config 注入的覆盖构造参数)。

    空值语义与原适配器同式(config_path 空串/None 不落键 → SDK 缺省隔离)。
    """
    for key in keys:
        if src.get(key):
            dest[key] = src[key]


def adapter(generator: str | None = None, *, generator_config: dict | None = None,
            system_prompt: str = "", repo: Repo | None = None,
            agent_output_dir: str | None = None, agent_write_mode: bool = False):
    """generator → 适配器实例(四路收敛构造入口;≈ GENERATORS[gid](config) 一行)。

    gid 经 resolve_generator(file 类 config_path 规范化);llm 路 resolved 即
    OpenAIConfig(概念键透传)。cc/dsh/codex 按 agent/generators/ 各文件 TypedDict 键集
    组装 config dict —— 引擎基座与阶段专属键在本层,sdk 映射全在 agent 侧:
    - cc:工具隔离(setting_sources=[]),repo 非空时 cwd 固定仓库根(SDK 缺省 =
      进程 cwd,曾导致 agent 串到 gh-puller 把 docs 写入其中);agent_write_mode
      (交付件落盘,wiki 结构/页面)追加 Write/add_dirs/acceptEdits,默认模式只开放
      Read/Grep/Glob(chat/codemap 的 agent 自读代码,无落盘);
    - dsh:provider/session_root/runtime_cwd + system_prompt → 组合 persona
      (非空才注入,空回退缺省);
    - codex:system_prompt → thread_start.base_instructions;config_path 纯透传
      (home config.toml 符号链接);sandbox 缺省 full_access,镜像 dsh
      danger-full-access,可覆写。

    上层注入的工具配置(mcp_servers/env/工具名)经 generator_config 覆盖构造参数
    白名单透传(见 _CC_PASSTHROUGH 等;_ENGINE_KEYS 剥离,工具指引文本
    tool_note/codemap_note 同理不落 SDK 配置)。

    一个实例 = 一次对话(重试/每阶段刷新构造;构造期即装配 SDK 对象)。
    """
    gid, resolved = resolve_generator(generator, generator_config)
    resolved = {k: v for k, v in resolved.items() if k not in _ENGINE_KEYS}
    if gid == "llm":
        return GENERATORS["llm"](resolved)
    if gid == "dsh":
        options: dict[str, Any] = {
            "provider": "deepseek-official",  # dsh SDK 原生 provider 路由名(gh provider id 是 deepseek)
            "session_root": envs.DSH_SESSION_ROOT,
            "runtime_cwd": envs.DSH_RUNTIME_CWD,  # .env 加载点越过任务 checkout(见 envs)
            "system_prompt": system_prompt,  # → 组合 persona(agent dsh_fields 映射,空则缺省)
        }
        _merge(options, resolved, _DSH_PASSTHROUGH)
        if repo is not None:
            options["cwd"] = os.path.abspath(repo.save_path)
            if resolved.get("mcp_servers"):
                options["mcp_servers"] = resolved["mcp_servers"]  # app 注入工具桌(适配层零工具名)
    elif gid == "codex":
        options = {
            "system_prompt": system_prompt,
            "sandbox": "full_access",  # 高自由度缺省(镜像 dsh danger-full-access;可覆写)
            "approval_mode": "auto_review",
        }
        _merge(options, resolved, _CODEX_PASSTHROUGH)
        if repo is not None:
            options["cwd"] = os.path.abspath(repo.save_path)
            if resolved.get("env"):
                options["env"] = resolved["env"]  # app 注入环境(隔离 home 工具桌)
            if resolved.get("mcp_servers"):
                options["mcp_servers"] = resolved["mcp_servers"]  # app 注入工具桌(隔离 config.toml 装载)
    else:  # cc
        options = {
            "system_prompt": system_prompt,
            "include_partial_messages": True,
            "setting_sources": [],  # 完全隔离本地 claude 配置(用户级 MCP/skills/hooks 不掺入 agent)
        }
        _merge(options, resolved, _CC_PASSTHROUGH)
        if repo is not None:
            options["cwd"] = os.path.abspath(repo.save_path)
            if resolved.get("mcp_servers"):
                options["mcp_servers"] = resolved["mcp_servers"]  # app 注入进程内 MCP server
            tools = ["Read", "Grep", "Glob", *list(resolved.get("allowed_tools") or [])]  # app 注入工具名
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

    内部经 adapter 构造(模型/url/凭证随所选 generator);一次会话 =
    async with inst.session(...)(监控与客户端同寿);本模块是测试
    monkeypatch 位点(经 utils.llm_stream 属性替换)。
    """
    inst = adapter(generator, generator_config=generator_config)
    payload = {"messages": [{"role": "user", "content": prompt}], "model": inst.config.get("model")}
    async with inst.session(session_name=session_name, run_id=run_id):
        async for chunk in inst.stream(payload):
            yield chunk


async def llm_complete(prompt: str, *, generator: str | None, generator_config: dict | None,
                       session_name: str, run_id: str) -> str:
    """llm 路统一整收补全口(单条 user 消息;同 llm_stream 装配)。"""
    inst = adapter(generator, generator_config=generator_config)
    payload = {"messages": [{"role": "user", "content": prompt}], "model": inst.config.get("model")}
    async with inst.session(session_name=session_name, run_id=run_id):
        return await inst.result(payload)


def failure(exc: Exception) -> Exception:
    """RequestFailedError → RuntimeError("agent 执行失败: ...")(对外文案,

    原 agent.generate_* file 类分支的包装);其余异常原样返回。
    链式追溯由唯一 raise 点(_deliver)以 raise ... from 补上。
    """
    if isinstance(exc, RequestFailedError):
        return RuntimeError(f"agent 执行失败: {exc.detail}")
    return exc


# ---------------------------------------------------------------------------
# llm 路补全协议(原 chat.py 收编;chat/wiki 的 llm 路单次补全复用)+ 图服务接入
# (检索工具簇属 app 侧组装层 generators.py,引擎经 graph_service 取用;
# llm 路专用,失败即 raise,不许"带病继续")
# ---------------------------------------------------------------------------


def build_service_prompt(
    system_prompt: str, query: str, *, conversation_history: str = "", context: str = "",
    simplify: bool = False,
) -> str:
    """原版 api/chat/_prompts.py prompt_builder 逐字移植(单条 user 消息)。

    结构:/no_think + 系统提示词 → <conversation_history> → 检索上下文
    (<START_OF_CONTEXT> 包裹;为空注"无检索增强"note)或简化 note(输入超限时)
    → <query>…</query> + Assistant:。
    """
    prompt = f"/no_think {system_prompt}\n\n"
    if conversation_history:
        prompt += f"<conversation_history>\n{conversation_history}</conversation_history>\n\n"
    if not simplify:
        if context.strip():
            prompt += f"<START_OF_CONTEXT>\n{context}\n<END_OF_CONTEXT>\n\n"
        else:
            prompt += "<note>Answering without retrieval augmentation.</note>\n\n"
    else:
        prompt += "<note>Answering without retrieval augmentation due to input size constraints.</note>\n\n"
    return prompt + f"<query>\n{query}\n</query>\n\nAssistant: "


def _is_token_limit_error(exc: Exception) -> bool:
    """原版 api/chat/__init__.py is_token_limit_error 的判断子串(大小写不敏感)。"""
    error_message = str(exc).lower()
    return any(k in error_message for k in (
        "maximum context length", "token limit", "too many tokens",
    ))


async def llm_research_chat(
    system: str, query: str, *, generator: str | None, generator_config: dict | None,
    repo: Repo, session_name: str, run_id: str,
    conversation_history: str = "",
):
    """原版 research_chat 语义(流式整口):

    - 最后一问估算超 CHAT_TOKEN_LIMIT_ESTIMATE(原 MAX_INPUT_TOKENS=7500)→ 跳过检索;
    - 检索上下文 = 图谱子图→真实代码窗(原 RAG 的适配点),经 prompt_builder 同式
      拼装(<START_OF_CONTEXT> 包裹;为空注"无检索增强"note);图谱失败/超预算
      直接 raise(检索失败不继续);
    - stream_and_fallback:token 超限 → 简化提示词重试(去掉检索上下文)→ 致歉;
      其余异常 → "Error with openai API: {e}" 文本进流。
    """
    context_text = ""
    if _estimate_tokens(query) > envs.CHAT_TOKEN_LIMIT_ESTIMATE:
        log(f"输入过大(估算 {_estimate_tokens(query)} tokens),跳过检索上下文(原版 MAX_INPUT_TOKENS 语义)")
    else:
        # 图服务经 generator_config 注入(webui generators.py 装配);图谱失败/超预算 → raise
        ctx = await graph_service(generator_config).context(repo, query)
        context_text = format_subgraph_context(ctx["blocks"])

    prompt = build_service_prompt(
        system, query, conversation_history=conversation_history, context=context_text,
    )
    simplified = build_service_prompt(
        system, query, conversation_history=conversation_history, simplify=True,
    )
    try:
        async for chunk in llm_stream(
            prompt, generator=generator, generator_config=generator_config,
            session_name=session_name, run_id=run_id,
        ):
            yield chunk
    except Exception as e:
        if _is_token_limit_error(e):
            log("token 超限,简化为无检索上下文重试")
            try:
                async for chunk in llm_stream(
                    simplified, generator=generator, generator_config=generator_config,
                    session_name=session_name, run_id=run_id,
                ):
                    yield chunk
            except Exception as e2:
                log(f"简化重试失败: {e2}")
                yield (
                    "\nI apologize, but your request is too large for me to process. "
                    "Please try a shorter query or break it into smaller parts."
                )
        else:
            log(f"chat llm 错误: {e}")
            yield f"\nError with openai API: {e}"


def format_subgraph_context(blocks: list[dict[str, Any]]) -> str:
    r"""代码窗 → 原版 _format_context 同式文本(chat/codemap 共用)。

    按文件分组:每组 `## File Path: {path}` 头 + 每窗 `[lines A-B]\n<code>`
    (窗间空行);文件段以原版同式(`"\n\n" + "-"*10 + "\n\n".join(parts)`)
    联结。空输入 → ""(上层 prompt_builder 会注入"无检索增强"note)。
    """
    if not blocks:
        return ""  # 原版由调用方兜底(无文档时不调用);空输入保持 "" 供 prompt_builder 注 note
    groups: dict[str, list[dict[str, Any]]] = {}
    for b in blocks:
        groups.setdefault(b["path"], []).append(b)
    context_parts: list[str] = []
    for path, blks in groups.items():
        chunk_texts = [f"[lines {b['start_line']}-{b['end_line']}]\n{b['text']}" for b in blks]
        context_parts.append(f"## File Path: {path}\n\n" + "\n\n".join(chunk_texts))
    return "\n\n" + ("-" * 10) + "\n\n".join(context_parts)


def graph_service(generator_config: dict | None = None):
    """app 注入的图服务(generator_config["graph"]):ready(repo)/async context(repo, question)。

    图服务(建图/查询/子图检索/MCP 装配)全部在 apps/deepwiki-webui/server/
    generators.py(webui 组装层),引擎只经本接口取用:就绪门与 llm 路检索共用;
    本层零图假设,未注入(直接调引擎而未过 runtime_config)即报错。
    """
    service = (generator_config or {}).get("graph")
    if service is None:
        raise RuntimeError(
            "代码图谱服务未注入(generator_config['graph'];由 webui generators.runtime_config 注入)",
        )
    return service
