"""Provide shared generator infrastructure for the DeepWiki engine.

A generator selection comprises an adapter identifier and its configuration
mapping as supplied. The entire credential-free mapping defines selection
identity; callers remove credentials before using it in persisted state.

Feature modules own feature-specific policy, and application layers inject tool
configuration. This module assumes no tools, imports no generator SDK directly,
and reads environment settings through module bindings so refreshes and test
patches remain visible.
"""

import hashlib
import json
import os
from functools import partial
from typing import Any

from .. import envs  # Keep patches and refreshed environment values visible.
from ..agent import AGENTS, RequestFailedError
from ..utils import Repo
from ..utils import _log as _utils_log

# Human-readable progress uses stderr; callers own machine-readable output.
log = partial(_utils_log, prefix="deepwiki")


# --- Generator selection ---


def resolve_generator(generator: str | None = None,
                      generator_config: dict | None = None,
                      get_env=None) -> tuple[str, dict]:
    """Resolve a generator selection without interpreting its configuration.

    Args:
        generator: Adapter identifier. An empty selection uses the engine's ``cc``
            default.
        generator_config: Configuration to copy with keys, paths, and defaults
            unchanged.
        get_env: Reserved compatibility hook; ignored.

    Returns:
        The resolved adapter identifier and copied configuration.

    Raises:
        ValueError: The adapter identifier is unknown.
    """
    gen_id = generator or "cc"
    if gen_id not in AGENTS:
        raise ValueError(f"未知 generator: {gen_id!r}(可选 {sorted(AGENTS)})")
    return gen_id, dict(generator_config or {})


def generator_identity(generator_id: str, resolved: dict) -> str:
    """Serialize a credential-free configuration for equality checks.

    Args:
        generator_id: Resolved adapter identifier paired with the serialized
            configuration by callers.
        resolved: Configuration whose entire key set and values define identity.

    Returns:
        A deterministic JSON representation of the configuration.
    """
    return json.dumps(resolved, sort_keys=True, ensure_ascii=False)


def repo_key_of(repo_type: str, owner: str, repo: str) -> str:
    """Build the repository key shared by task and cache namespaces.

    Args:
        repo_type: Repository provider or source kind.
        owner: Repository owner.
        repo: Repository name.

    Returns:
        The stable ``type_owner_repo`` key.
    """
    return f"{repo_type}_{owner}_{repo}"


# --- Selection identity ---


def generator_digest(generator: str | None = None,
                     generator_config: dict | None = None,
                     get_env=None) -> str:
    """Build the stable short digest used to isolate selection state.

    Args:
        generator: Adapter identifier following :func:`resolve_generator`
            defaults.
        generator_config: Credential-free selection configuration; see the module
            contract.
        get_env: Reserved compatibility hook forwarded to selection resolution.

    Returns:
        An eight-character hexadecimal fingerprint.
    """
    generator_id, resolved = resolve_generator(generator, generator_config, get_env)
    return _generator_digest_of(generator_id, resolved)


def _generator_digest_of(generator_id: str, resolved: dict) -> str:
    # SHA-1 is a compact cache fingerprint, not a security primitive.
    return hashlib.sha1(  # noqa: S324
        f"{generator_id}|{generator_identity(generator_id, resolved)}".encode(),
    ).hexdigest()[:8]


def cache_identity(cache: dict) -> tuple[str, str]:
    """Read selection identity from a finished cache.

    Args:
        cache: Finished-cache payload containing generator selection fields.

    Returns:
        The adapter identifier and serialized whole-configuration identity.
    """
    generator_id = cache.get("generator") or ""
    resolved = cache.get("generator_config") or {}
    return generator_id, generator_identity(generator_id, resolved)


def cache_generator_matches(cache: dict, generator: str | None = None,
                            generator_config: dict | None = None) -> bool:
    """Check a cache selection after path-level digest isolation.

    Args:
        cache: Finished-cache payload to validate.
        generator: Requested adapter identifier.
        generator_config: Requested credential-free selection configuration.

    Returns:
        Whether the recorded and requested selections are identical.
    """
    generator_id, resolved = resolve_generator(generator, generator_config)
    return cache_identity(cache) == (generator_id, generator_identity(generator_id, resolved))


# --- Shared prompt context ---

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
    """Resolve the display name for a prompt language.

    Args:
        language: Language code from the request boundary.

    Returns:
        The display name, or English for an unknown code.
    """
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
    """Build the common template context for feature prompts.

    Args:
        repo: Repository domain object supplying identity and location fields.
        language: Requested response-language code; an empty value uses English.

    Returns:
        Repository and language fields for prompt formatting.
    """
    return {
        "repo_type": repo.repo_type,
        "repo_url": repo.repo_url,
        "repo_name": repo.name,
        "language_name": language_name(language or "en"),
    }


# --- Adapter assembly ---


def adapt_generator(generator: str | None = None, *, generator_config: dict | None = None,
            system_prompt: str = "", repo: Repo | None = None,
            generator_cache_dir: str | None = None, generator_cache_write_mode: bool = False):
    """Construct a fresh adapter conversation with engine-owned defaults.

    The configured user prompt precedes the task prompt. Repository context pins
    adapter working directories. Claude sessions ignore local settings, and cache
    persistence grants their engine-owned write access. DSH runtime locations
    come from the environment contract.

    Args:
        generator: Adapter identifier following :func:`resolve_generator`
            defaults.
        generator_config: Adapter-specific configuration copied before common
            engine fields are applied.
        system_prompt: Task prompt appended after any configured user prompt.
        repo: Repository context used to pin the adapter working directory.
        generator_cache_dir: Directory exposed to Claude cache-writing sessions.
        generator_cache_write_mode: Whether Claude may persist generated cache
            artifacts.

    Returns:
        A newly constructed adapter for one conversation.

    Raises:
        ValueError: The adapter identifier is unknown.
    """
    gid, resolved = resolve_generator(generator, generator_config)
    # Preserve user-prompt precedence over the task-specific prompt.
    if resolved.get("system_prompt"):
        system_prompt = (
            f"{resolved['system_prompt']}\n\n{system_prompt}"
            if system_prompt else resolved["system_prompt"]
        )
    if gid == "llm":
        options = {**resolved, "system_prompt": system_prompt}
    elif gid == "dsh":
        options: dict[str, Any] = dict(resolved)
        options.setdefault("dsh_home", envs.DSH_HOME)
        if envs.DSH_BIN:
            options.setdefault("dsh_bin", envs.DSH_BIN)
        options.update({
            "session_root": envs.DSH_SESSION_ROOT,
            "runtime_cwd": envs.DSH_RUNTIME_CWD,  # Keep .env discovery outside task checkouts; see envs.
            "system_prompt": system_prompt,
        })
        if repo is not None:
            options["cwd"] = os.path.abspath(repo.save_path)
    elif gid == "codex":
        options = dict(resolved)
        options.update({
            "system_prompt": system_prompt,
            "sandbox": "full_access",  # Match the high-autonomy DSH default.
            "approval_mode": "auto_review",
        })
        if repo is not None:
            options["cwd"] = os.path.abspath(repo.save_path)
    elif gid == "opencode":
        options = dict(resolved)
        options.update({
            "system_prompt": system_prompt,
            "auto": True,  # Prevent headless sessions from stalling on unresolved permissions.
        })
        if repo is not None:
            options["cwd"] = os.path.abspath(repo.save_path)
    else:
        options = dict(resolved)
        options.update({
            "system_prompt": system_prompt,
            "include_partial_messages": True,
            "setting_sources": [],  # Isolate local Claude MCP, skill, and hook settings.
        })
        if repo is not None:
            options["cwd"] = os.path.abspath(repo.save_path)
            tools = ["Read", "Grep", "Glob", *list(resolved.get("allowed_tools") or [])]
            if generator_cache_write_mode:
                if generator_cache_dir:
                    options["add_dirs"] = [os.path.abspath(generator_cache_dir)]
                options["permission_mode"] = "acceptEdits"
                tools = ["Write", *tools]
            options["allowed_tools"] = tools
    return AGENTS[gid](options)


def failure(exc: Exception) -> Exception:
    """Normalize adapter failures for public callers.

    Args:
        exc: Error raised by an adapter operation.

    Returns:
        A public RuntimeError for RequestFailedError, or the original error.
    """
    if isinstance(exc, RequestFailedError):
        return RuntimeError(f"generator 执行失败: {exc.detail}")
    return exc
