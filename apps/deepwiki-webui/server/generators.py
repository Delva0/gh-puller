"""Assemble WebUI-specific generator runtimes and gh-puller MCP tooling.

The DeepWiki engine receives only generic Agent configuration. This boundary owns the
codebase-memory index, MCP process descriptions, tool allowlists, and per-adapter
configuration. Importing it has no side effects.
"""

import asyncio
import json
import os
import re
from pathlib import Path

from claude_agent_sdk.types import McpStdioServerConfig
from gh_puller.deepwiki.utils import resolve_generator
from gh_puller.utils import Repo
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# --- Index state ---

_CBM_DEFAULT_CACHE = "~/.cache/codebase-memory-mcp"


def _cbm_cache_dir() -> Path:
    """Return the codebase-memory cache root, resolving its environment at call time."""
    return Path(os.environ.get("CBM_CACHE_DIR") or _CBM_DEFAULT_CACHE).expanduser()


def project_name(repo: Repo) -> str:
    """Return a C-compatible ASCII project name for an index database."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", f"{repo.repo_type}_{repo.name}")


def index_ready(repo: Repo) -> bool:
    """Return whether the repository index database exists."""
    return (_cbm_cache_dir() / f"{project_name(repo)}.db").exists()


# --- MCP tool desk ---

_MCP_ENTRY = ["run", "python", "-m", "gh_puller_mcp"]
# Keep this read-only set aligned with ``gh_puller_mcp.manifest.SCOUT_TOOLS``.
_SCOUT_TOOLS = ("search_graph", "trace_path", "get_code_snippet", "get_architecture",
                "list_projects", "index_status", "check_index_coverage")
_INDEX_TIMEOUT_SEC: float | None = None

# The backend stages writes, so serialize the rare index operations.
_INDEX_LOCK = asyncio.Lock()


def _mcp_project_root() -> Path:
    """Resolve the gh-puller-mcp project root from the monorepo or environment."""
    root = Path(os.environ.get("DEEPWIKI_CBM_MCP_ROOT")
                or Path(__file__).resolve().parents[2] / "gh-puller-mcp")
    if not (root / "gh_puller_mcp" / "__main__.py").exists():
        raise RuntimeError(f"gh-puller-mcp 未找到: {root}(可设 DEEPWIKI_CBM_MCP_ROOT 覆盖)")
    return root.resolve()


def _gh_puller_mcp(backend: str):
    """Build the backend-specific process description for the scout tool profile."""
    args = ["--directory", str(_mcp_project_root()), *_MCP_ENTRY, "--tool-profile", "scout"]
    if backend == "dsh":
        return [{"id": "mcp-gh-puller", "serverName": "gh_puller", "command": "uv", "args": args}]
    if backend in ("codex", "opencode"):
        return [{"id": "gh_puller", "command": "uv", "args": args,
                 "env_vars": ["CBM_CACHE_DIR", "CBM_RUNTIME_DIR"]}]
    return McpStdioServerConfig(command="uv", args=args)


async def _call_mcp_tool(tool: str, arguments: dict, *, timeout: float | None = None) -> dict:  # noqa: ASYNC109
    """Call one tool over a short-lived stdio connection.

    The server exits at EOF. MCP failures raise ``RuntimeError``; successful envelopes
    prefer structured content and fall back to JSON text. Only explicit CBM settings and
    the test binary override cross the MCP SDK's restricted environment boundary.
    """
    args = ["--directory", str(_mcp_project_root()), *_MCP_ENTRY]
    if timeout is not None:
        args += ["--timeout", str(timeout)]
    # The C binary requires explicit cache and runtime roots to exist beforehand.
    for _key in ("CBM_CACHE_DIR", "CBM_RUNTIME_DIR"):
        _dir = os.environ.get(_key)
        if _dir:
            os.makedirs(_dir, exist_ok=True)
    _env: dict[str, str] = {
        k: v for k, v in os.environ.items()
        if k in ("CBM_CACHE_DIR", "CBM_RUNTIME_DIR", "GH_PULLER_MCP_BINARY")
    }
    params = StdioServerParameters(command="uv", args=args, env=_env or None)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool(tool, arguments)
    if result.is_error:
        text = result.content[0].text if result.content else ""
        raise RuntimeError(text or f"{tool} failed")
    if result.structured_content is not None:
        return dict(result.structured_content)
    text = result.content[0].text if result.content else ""
    return json.loads(text) if text else {}

# --- Index preparation ---


async def _run_index(repo: Repo) -> None:
    """Build a structural index through ``index_repository`` in fast mode."""
    await _call_mcp_tool(
        "index_repository",
        {"repo_path": repo.save_path, "mode": "fast", "name": project_name(repo)},
        timeout=_INDEX_TIMEOUT_SEC,
    )


async def ensure_index(repo: Repo) -> None:
    """Ensure both checkout and serialized codebase-memory index exist.

    A missing checkout is restored even when the index remains. Index failures propagate
    so the endpoint or task runtime can report them.
    """
    async with _INDEX_LOCK:
        if not repo.downloaded and not repo.is_local:
            await asyncio.to_thread(repo.download)
        if not index_ready(repo):
            await _run_index(repo)


# --- Runtime configuration ---

# Each adapter maps this instruction into its native system-prompt field.
_GRAPH_FIRST_SYSTEM_PROMPT = (
    "Prefer the gh_puller_mcp(codebase graph tools) for code queries: locate definitions, call chains "
    "and dependencies directly by symbol name — more precise and efficient than Grep/Glob/Read."
)


def runtime_config(generator: str | None = None, generator_config: dict | None = None,
                   *, repo: Repo | None = None) -> dict:
    """Selection → runtime config (public config_path adapted per generator + tool-desk injection).

    The wire form (frontend/HTTP) carries config_path; per-generator adaptation:
    cc → settings; opencode → config; codex keeps config_path (no SDK-native
    parameter, the generator layer converts it into the home config.toml symlink);
    dsh → no adaptation (no such concept, the isolated composition covers it).
    Empty values drop (SDK default isolation). gh-puller-mcp tool desk injected
    per backend: cc gets McpStdioServerConfig (scout tier) + graph tool names;
    dsh/codex/opencode get child-process descriptions (+ codex/opencode env
    passthrough of CBM_* for a consistent index root); llm gets no tool desk
    (direct HTTP). repo=None skips the injection (same semantics as the original
    adapter "no mcp without repo").
    """
    result = dict(generator_config or {})
    gid, _ = resolve_generator(generator, generator_config)
    if gid == "cc" and (cfg := result.pop("config_path", None)):
        result["settings"] = cfg
    elif gid == "opencode" and (cfg := result.pop("config_path", None)):
        result["config"] = cfg
    # codex keeps config_path; dsh has no config_path concept — nothing to adapt.
    if repo is None:
        return result
    if gid == "dsh":
        result["mcp_servers"] = _gh_puller_mcp("dsh")
    elif gid in ("codex", "opencode"):
        result["mcp_servers"] = _gh_puller_mcp(gid)
        env = {k: os.environ[k] for k in ("CBM_CACHE_DIR", "CBM_RUNTIME_DIR") if k in os.environ}
        if env:
            result["env"] = env
    elif gid == "llm":
        pass  # The direct HTTP backend has no MCP tool desk.
    else:  # cc
        result["mcp_servers"] = {"gh_puller": _gh_puller_mcp("cc")}
        result["allowed_tools"] = [*_SCOUT_TOOLS, *[f"mcp__gh_puller__{n}" for n in _SCOUT_TOOLS]]
    return result
