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
import os
import re
import sys
from functools import partial
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .. import envs, graphify  # 模块对象绑定:属性一律调用时取(patch/强刷活性)
from ..agent import GENERATORS, RequestFailedError
from ..utils import Repo, _estimate_tokens
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

    from .cache import _graph_path  # lazy:cache 反向依赖本模块(see circular import)

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
    from .cache import _graph_dir  # lazy:cache 反向依赖本模块(see circular import)

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
            options["env"] = {"GRAPHIFY_OUT": str(_graph_dir(repo))}
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
# Llm 路检索工具簇(graphify 子图 → 真实代码行窗;chat/codemap/wiki(llm 路)共用。
# 检索失败 → raise:管道不作无检索降级,不许"带病继续")
# ---------------------------------------------------------------------------


# 纯 LLM 路径单文件内联截断(字符)
_FILE_INLINE_CAP = 8000


def subgraph_hits(answer: str) -> dict[str, list[int]]:
    """解析 graphify.query 的 answer 标注 → {file: [行号...]}。

    NODE 行形如 `NODE <label> [src=<file> loc=L<line> community=<cid>]`,
    EDGE 行以 ` at=<file>:L<line>` 结尾;容忍 [i] TRUNCATED / over-budget
    前缀行与 ... (truncated 尾行;非匹配行忽略,loc 缺失/为空的文件跳过)。
    """
    hits: dict[str, list[int]] = {}
    for raw in answer.splitlines():
        m = re.match(r"^NODE\s+.+?\s+\[([^\]]*)\]$", raw)
        if m:
            src = re.search(r"\bsrc=(\S+)\b", m.group(1))
            loc = re.search(r"\bloc=(L?\d+)\b", m.group(1))
            if src and loc:
                hits.setdefault(src.group(1), []).append(int(loc.group(1).lstrip("L")))
            continue
        m = re.search(r"\bat=([^:\s]+):(L\d+)$", raw)
        if m:
            hits.setdefault(m.group(1), []).append(int(m.group(2)[1:]))
    return {p: sorted(set(lines)) for p, lines in hits.items()}


def subgraph_src_blocks(
    save_path: str,
    hits: dict[str, list[int]],
    *,
    radius: int = 12,
    per_file_cap: int = _FILE_INLINE_CAP,
    budget_chars: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """命中行窗提取:{file: [行号]} → [{path,text,start_line,end_line}...], (blocks, degraded)。

    每文件:命中行 ±radius 展开、相邻窗(间距 ≤ 2*radius)合并、夹到文件行界;
    单文件累计字符封顶 per_file_cap(溢出窗截断,后续窗丢弃);
    全量累计超 budget_chars(缺省 CHAT_TOKEN_LIMIT_ESTIMATE*4)即整组降级
    (返回 ([], True));调用方(graphify_context)据此 raise。
    OSError/解码失败的文件跳过(其余文件正常)。
    """
    budget = budget_chars if budget_chars is not None else envs.CHAT_TOKEN_LIMIT_ESTIMATE * 4
    blocks: list[dict[str, Any]] = []
    total = 0
    for path in sorted(hits):
        try:
            full_text = Path(save_path, path).read_text(encoding="utf-8")
        except OSError:
            continue
        lines = full_text.splitlines()
        n_lines = len(lines)
        # 命中行 → 合并后的窗区间
        windows: list[tuple[int, int]] = []
        for line in sorted(set(hits[path])):
            start = max(1, line - radius)
            end = min(n_lines, line + radius)
            if windows and start <= windows[-1][1] + 1:
                windows[-1] = (windows[-1][0], max(windows[-1][1], end))
            else:
                windows.append((start, end))
        file_chars = 0
        for start, end in windows:
            seg_text = "\n".join(lines[start - 1:end])
            if not seg_text.strip():
                continue
            remain = per_file_cap - file_chars
            if len(seg_text) > remain:
                if remain <= 0:
                    break
                seg_text = seg_text[:remain]
                end = start + seg_text.count("\n")
            file_chars += len(seg_text)
            if total + len(seg_text) > budget:  # 先按单文件截断,再整体预算判断
                return [], True
            blocks.append({"path": path, "text": seg_text, "start_line": start, "end_line": end})
            total += len(seg_text)
    return blocks, False


def format_subgraph_context(blocks: list[dict[str, Any]]) -> str:
    """代码窗 → 原版 _format_context 同式文本(chat/codemap 共用)。

    按文件分组:每组 `## File Path: {path}` 头 + 每窗 `[lines A-B]\n<code>`
    (窗间空行);文件段以原版同式(`"\n\n" + "-"*10 + "\n\n".join(parts)`)
    联结。空输入 → ""(上层 prompt_builder 会注入"无检索增强"note)。"""
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


async def graphify_context(repo: Repo, question: str) -> dict[str, Any]:
    """graphify.query + 子图 → 真实代码行窗。

    检索是正确作答的前提:图谱查询失败与超预算降级均直接 raise(不再以
    "无检索增强"降级继续)。
    """
    from .cache import _graph_path  # lazy:cache 反向依赖本模块(see circular import)

    try:
        result = await asyncio.to_thread(
            graphify.query, question, graph_path=str(_graph_path(repo))
        )
        answer = result.get("answer") or ""
    except Exception as exc:
        raise RuntimeError(f"代码图谱不可用: {type(exc).__name__}: {exc}") from exc
    hits = subgraph_hits(answer)
    if not hits:
        return {"hits": {}, "blocks": []}
    blocks, degraded = subgraph_src_blocks(repo.save_path, hits)
    if degraded:
        raise RuntimeError("检索上下文超出预算")
    return {"hits": hits, "blocks": blocks}


# ---------------------------------------------------------------------------
# llm 路补全协议(research_chat 等价:检索上下文注入 + token 超限简化重试;
# wiki(llm 路 structure/page)/chat 共用;context 事件已全部清除 —— 引擎不向
# agent 传输层传 context,失败即 raise/流错误文本,不做"假日志"事件)
# ---------------------------------------------------------------------------


def build_service_prompt(
    system_prompt: str, query: str, *, conversation_history: str = "", context: str = "",
    simplify: bool = False,
) -> str:
    """原版 api/chat/_prompts.py prompt_builder 逐字移植(单条 user 消息)。

    结构:/no_think + 系统提示词 → <conversation_history> → 检索上下文
    (<START_OF_CONTEXT> 包裹;为空注"无检索增强"note)或简化 note(输入超限时)
    → <query>…</query> + Assistant:。"""
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
    """原版 research_chat 语义(流式整口;已合并原 _llm_research_chat/_stream):

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
        ctx = await graphify_context(repo, query)  # 图谱失败/超预算 → raise
        context_text = format_subgraph_context(ctx["blocks"])

    prompt = build_service_prompt(
        system, query, conversation_history=conversation_history, context=context_text
    )
    simplified = build_service_prompt(
        system, query, conversation_history=conversation_history, simplify=True
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
            except Exception as e2:  # noqa: BLE001 - 简化重试失败 → 致歉文本(原版同式)
                log(f"简化重试失败: {e2}")
                yield (
                    "\nI apologize, but your request is too large for me to process. "
                    "Please try a shorter query or break it into smaller parts."
                )
        else:
            log(f"chat llm 错误: {e}")
            yield f"\nError with openai API: {e}"


# ---------------------------------------------------------------------------
# 索引保障服务(clone + 建图;/repo/prepare 与 wiki 任务主流程共用。
# 未索引前置校验属端点守卫,在 apps/deepwiki-webui/server/app.py)
# ---------------------------------------------------------------------------


async def _run_extract(repo: Repo, *, extra_excludes: list[str] | None = None) -> dict:
    """graphify.extract 建图(code_only 纯本地 AST,无 key 可跑);失败返回错误态 dict,不抛。"""
    from .cache import _graph_dir  # lazy:cache 反向依赖本模块(see circular import)

    return await asyncio.to_thread(
        graphify.extract,
        path=repo.save_path,
        code_only=True,
        out_dir=_graph_dir(repo),
        extra_excludes=extra_excludes,
    )


async def ensure_index(repo: Repo, *, extra_excludes: list[str] | None = None) -> None:
    """索引保障(克隆 + 建图):已 ready 直接返回。

    克隆判断独立于 ready 与否 —— graph.json 在但克隆目录被删时补克隆
    (防文件树静默退化为空);建图失败(extract 错误态)→ RuntimeError 上抛,
    由调用方决定上报/置任务 FAILED。
    """
    from .cache import _index_ready  # lazy:cache 反向依赖本模块(see circular import)

    if not repo.downloaded and not repo.is_local:
        await asyncio.to_thread(repo.download)
    if not _index_ready(repo):
        result = await _run_extract(repo, extra_excludes=extra_excludes)
        if result.get("error"):
            raise RuntimeError(result["error"])
