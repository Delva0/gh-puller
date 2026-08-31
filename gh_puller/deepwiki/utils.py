"""deepwiki 引擎共用 helper(generator 选型/判等/凭证规则簇 + 域内日志 + repo 键)。

规划格局(按功能为主线):wiki/chat/codemap 三个功能模块各自收编本功能专用
helper;跨功能通用 helper 归本模块,由功能模块经本模块**属性调用**(调用时取;
monkeypatch 位点打在本模块,不得 from-import 后裸名调用)。

本模块 sdk-free / 零工具假设:生成器装配只做选型收敛 + 白名单透传,工具配置/
工具指引文本全部由上层经 generator_config 覆盖构造参数注入,本层不假设
任何工具由上层提供(图知识在 webui 组装层,见包 docstring)。生成器契约类型
(GENERATORS/RequestFailedError)保留(生成器依赖的定义来源)。

术语:引擎内部一律说 generator;函数签名统一为 generator + generator_config
两个散装参数;wire 字段 "target" 只在 app 层,拆分/解析经 resolve_generator
唯一知识源。envs 保持模块对象绑定 + 属性调用(调用时取;测试 monkeypatch/
强刷活性)。对外函数无下划线前缀(常数与纯内部 helper 除外)。
"""

import hashlib
import json
import os
from functools import partial
from typing import Any

from .. import envs  # 模块对象绑定:属性一律调用时取(patch/强刷活性)
from ..agent import GENERATORS, RequestFailedError
from ..utils import Repo
from ..utils import _log as _utils_log

# 进度日志走 stderr(人类可读诊断,机器结果走调用方);prefix 固定 [deepwiki]
log = partial(_utils_log, prefix="deepwiki")


# ---------------------------------------------------------------------------
# generator 选型(解析 / 判等身份 / 凭证落盘规则;唯一知识源)
# ---------------------------------------------------------------------------


def resolve_generator(generator: str | None = None, generator_config: dict | None = None,
                      get_env=None) -> tuple[str, dict]:
    """Generator selection → (generator id, config as-given); empty selection = engine default cc.

    Selection is this function's only job: generator_config passes through
    untouched (key names, path spelling and defaults are the upper-layer boundary,
    see webui runtime_config); unknown ids raise. "Default generator" is an
    upper-layer (webui) policy injected at the apps/deepwiki-webui/server/app.py
    boundary (DEEPWIKI_GENERATOR) — the engine never reads env for selection.
    """
    gen_id = generator or "cc"
    if gen_id not in GENERATORS:
        raise ValueError(f"未知 generator: {gen_id!r}(可选 {sorted(GENERATORS)})")
    return gen_id, dict(generator_config or {})


def generator_identity(generator_id: str, resolved: dict) -> str:
    """Equality identity (no credentials): the generator_config as-given.

    The engine never picks a key out of generator_config — the whole dict is the
    selection (credentials stripped by the caller); key naming and the
    public/native forms are upper-layer knowledge (see webui runtime_config).
    """
    return json.dumps(resolved, sort_keys=True, ensure_ascii=False)


def repo_key_of(repo_type: str, owner: str, repo: str) -> str:
    """repo 键(type_owner_repo;与任务注册键/生成器缓存目录前缀同式)。"""
    return f"{repo_type}_{owner}_{repo}"


# ---------------------------------------------------------------------------
# 判等摘要族(digest 是选型判等身份的 8-hex 摘要,
# 任务 id / 续跑状态 / 成品缓存路径共用同一判等。图产物路径与索引就绪
# 属图知识 — 在 apps/deepwiki-webui/server/generators.py)
# ---------------------------------------------------------------------------


def generator_digest(generator: str | None = None, generator_config: dict | None = None,
                     get_env=None) -> str:
    """Stable digest (8 hex) of the identity above (no credentials).

    Identity = generator + generator_config as-given (whole-dict equality, no
    field-type knowledge). Shared by task ids / resume state / finished-cache
    paths: different selections under the same repo and language can coexist and
    never cross-use each other.
    """
    generator_id, resolved = resolve_generator(generator, generator_config, get_env)
    return _generator_digest_of(generator_id, resolved)


def _generator_digest_of(generator_id: str, resolved: dict) -> str:
    # sha1 仅作生成器身份指纹(缓存摘要),非安全用途
    return hashlib.sha1(  # noqa: S324
        f"{generator_id}|{generator_identity(generator_id, resolved)}".encode(),
    ).hexdigest()[:8]


def cache_identity(cache: dict) -> tuple[str, str]:
    """Identity recorded in a finished cache (generator + whole generator_config, same semantics as generator_identity)."""
    generator_id = cache.get("generator") or ""
    resolved = cache.get("generator_config") or {}
    return generator_id, generator_identity(generator_id, resolved)


def cache_generator_matches(cache: dict, generator: str | None = None,
                            generator_config: dict | None = None) -> bool:
    """Whether a finished cache matches the given selection (second check after digest isolation; guards hand-renamed files)."""
    generator_id, resolved = resolve_generator(generator, generator_config)
    return cache_identity(cache) == (generator_id, generator_identity(generator_id, resolved))


# ---------------------------------------------------------------------------
# 提示词共性常量(跨功能:wiki/chat/codemap 共用;
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
# 装配(适配器构造入口 adapter;上层经 generator_config 注入工具配置,
# 本层零 SDK/零工具假设)
# ---------------------------------------------------------------------------


def adapt_generator(generator: str | None = None, *, generator_config: dict | None = None,
            system_prompt: str = "", repo: Repo | None = None,
            generator_cache_dir: str | None = None, generator_cache_write_mode: bool = False):
    """generator → adapter instance (converged construction entry; ≈ GENERATORS[gid](config)).

    generator_config passes through untouched; this layer only adds engine-base
    keys (system_prompt / cwd / tool-desk assembly) — key-set selection belongs
    to the generator files (config contract: the key set IS the parse-layer
    whitelist semantics). Engine-layer injection notes:
    - cc: cwd pinned to the repo root when repo is given (SDK default = process
      cwd, once caused the exec process to write docs into gh-puller);
      generator_cache_write_mode (generator-cache persistence, wiki structure/
      pages) adds Write/add_dirs/acceptEdits, default opens only Read/Grep/Glob.
    - dsh: session_root/runtime_cwd + system_prompt → composed persona.

    One instance = one conversation (fresh construction per retry/stage; the SDK
    object is assembled at construction time).
    """
    gid, resolved = resolve_generator(generator, generator_config)
    if gid == "llm":
        return GENERATORS["llm"](resolved)
    # 拼接:generator_config 传入的 system_prompt(用户级)在前,参数 system_prompt(task 级)追加在后
    if resolved.get("system_prompt"):
        system_prompt = f"{resolved['system_prompt']}\n\n{system_prompt}" if system_prompt else resolved["system_prompt"]
    if gid == "dsh":
        options: dict[str, Any] = dict(resolved)
        options.update({
            "session_root": envs.DSH_SESSION_ROOT,
            "runtime_cwd": envs.DSH_RUNTIME_CWD,  # .env 加载点越过任务 checkout(见 envs)
            "system_prompt": system_prompt,  # → 组合 persona(dsh_fields 映射,空则缺省)
        })
        if repo is not None:
            options["cwd"] = os.path.abspath(repo.save_path)
    elif gid == "codex":
        options = dict(resolved)
        options.update({
            "system_prompt": system_prompt,
            "sandbox": "full_access",  # 高自由度缺省(镜像 dsh danger-full-access;可覆写)
            "approval_mode": "auto_review",
        })
        if repo is not None:
            options["cwd"] = os.path.abspath(repo.save_path)
    elif gid == "opencode":
        options = dict(resolved)
        options.update({
            "system_prompt": system_prompt,
            "auto": True,  # 无头缺省:权限未明拒即自动批准(防 resolved auto=False 静默关)
        })
        if repo is not None:
            options["cwd"] = os.path.abspath(repo.save_path)
    else:  # cc
        options = dict(resolved)
        options.update({
            "system_prompt": system_prompt,
            "include_partial_messages": True,
            "setting_sources": [],  # 完全隔离本地 claude 配置(用户级 MCP/skills/hooks 不掺入生成会话)
        })
        if repo is not None:
            options["cwd"] = os.path.abspath(repo.save_path)
            tools = ["Read", "Grep", "Glob", *list(resolved.get("allowed_tools") or [])]  # app 注入工具名
            if generator_cache_write_mode:
                if generator_cache_dir:
                    options["add_dirs"] = [os.path.abspath(generator_cache_dir)]
                options["permission_mode"] = "acceptEdits"
                tools = ["Write", *tools]
            options["allowed_tools"] = tools
    return GENERATORS[gid](options)


def failure(exc: Exception) -> Exception:
    """RequestFailedError → RuntimeError("generator 执行失败: ...") (public message); everything else is returned as-is."""

    if isinstance(exc, RequestFailedError):
        return RuntimeError(f"generator 执行失败: {exc.detail}")
    return exc
