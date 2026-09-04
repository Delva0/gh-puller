"""Test the WebUI generator assembly layer without model calls.

This module owns index readiness, MCP server descriptions, index assurance, and
runtime configuration injection. Fake registered adapters avoid SDK construction,
while a temporary CBM cache makes filesystem readiness checks deterministic.
"""

from pathlib import Path

import pytest
from gh_puller.agent import AGENTS
from gh_puller.agent.adapters.codex import codex_home_setup
from gh_puller.agent.adapters.dsh import dsh_cordis_path
from gh_puller.deepwiki.utils import adapt_generator
from gh_puller.utils import Repo

import generators


class _FakeGenerator:
    """Capture assembled configuration without SDK construction side effects.

    The constructor mirrors the agent contract by accepting the complete configuration.
    """

    generator = "cc"  # Individual cases replace this with the selected generator ID.

    def __init__(self, config):
        self.config = dict(config)


# ---------------------------------------------------------------------------
# Runtime configuration injection
# ---------------------------------------------------------------------------


def test_runtime_config_injects_tooltable_by_backend(tmp_path, monkeypatch):
    """Inject backend-specific MCP server configuration for tool-capable adapters.

    CC receives its stdio configuration and allowed tools; DSH, Codex, and OpenCode
    receive subprocess descriptions. CBM paths are forwarded only where supported,
    and the direct HTTP LLM adapter receives no MCP server.
    """
    monkeypatch.setenv("CBM_CACHE_DIR", "/tmp/cbm-cache")
    monkeypatch.setenv("CBM_RUNTIME_DIR", "/tmp/cbm-runtime")
    repo = Repo(str(tmp_path), "local")

    cc_gc = generators.runtime_config("cc", {"config_path": "/tmp/settings.json"}, repo=repo)
    assert cc_gc["settings"] == "/tmp/settings.json"  # Public config_path maps to native settings.
    assert "gh_puller" in cc_gc["mcp_servers"]
    cfg = cc_gc["mcp_servers"]["gh_puller"]  # The SDK consumes the TypedDict as a stdio process.
    assert cfg["command"] == "uv" and "gh_puller_mcp" in cfg["args"]
    assert cc_gc["allowed_tools"] == [
        *generators._SCOUT_TOOLS, *[f"mcp__gh_puller__{n}" for n in generators._SCOUT_TOOLS],
    ]
    assert "tool_note" not in cc_gc and "codemap_note" not in cc_gc  # Prompts carry no tool hints.

    dsh_gc = generators.runtime_config("dsh", {}, repo=repo)
    assert dsh_gc["mcp_servers"] == generators._gh_puller_mcp("dsh")
    assert "env" not in dsh_gc

    codex_gc = generators.runtime_config("codex", {}, repo=repo)
    assert codex_gc["mcp_servers"] == generators._gh_puller_mcp("codex")
    assert codex_gc["env"] == {"CBM_CACHE_DIR": "/tmp/cbm-cache",
                               "CBM_RUNTIME_DIR": "/tmp/cbm-runtime"}  # Forward explicitly set roots.

    opencode_gc = generators.runtime_config("opencode", {}, repo=repo)
    assert opencode_gc["mcp_servers"] == generators._gh_puller_mcp("opencode")  # Same subprocess shape as Codex.
    assert opencode_gc["env"] == {"CBM_CACHE_DIR": "/tmp/cbm-cache",
                                  "CBM_RUNTIME_DIR": "/tmp/cbm-runtime"}

    monkeypatch.delenv("CBM_CACHE_DIR", raising=False)
    monkeypatch.delenv("CBM_RUNTIME_DIR", raising=False)
    codex_gc = generators.runtime_config("codex", {}, repo=repo)
    assert "env" not in codex_gc  # Both processes use defaults when CBM roots are unset.

    dsh_gc = generators.runtime_config("dsh", {}, repo=repo)
    assert "env" not in dsh_gc  # DSH inherits its subprocess environment.

    llm_gc = generators.runtime_config("llm", {"model": "m1"}, repo=repo)
    assert llm_gc["model"] == "m1"
    assert "mcp_servers" not in llm_gc and "env" not in llm_gc
    assert "tool_note" not in llm_gc and "codemap_note" not in llm_gc  # Prompts carry no tool hints.

    bare = generators.runtime_config("cc", {}, repo=None)
    assert "mcp_servers" not in bare  # MCP tools require repository context.


def test_adapter_chain_gets_injected_graphify_config(tmp_path, monkeypatch):
    """Carry allowlisted runtime configuration into the assembled adapter config."""
    repo = Repo(str(tmp_path), "local")
    monkeypatch.setitem(AGENTS, "dsh", _FakeGenerator)
    monkeypatch.setattr(_FakeGenerator, "generator", "dsh")
    gc = generators.runtime_config("dsh", {}, repo=repo)
    cfg = adapt_generator("dsh", generator_config=gc, system_prompt="sys", repo=repo).config
    assert cfg["mcp_servers"] == generators._gh_puller_mcp("dsh")
    assert "tool_note" not in cfg and "codemap_note" not in cfg  # Tool hints never enter SDK config.


# ---------------------------------------------------------------------------
# gh-puller-mcp isolation in agent configuration
# ---------------------------------------------------------------------------


def test_dsh_cordis_isolation_and_graphify():
    """Isolate the built-in DSH profile and inject tools only through this layer.

    Workspace context, skills, and local tools remain disabled. The default profile
    contains no tool server; ``_gh_puller_mcp`` is the sole injection point.
    """
    text = Path(dsh_cordis_path()).read_text(encoding="utf-8")
    assert "workspaceContext: false" in text
    assert "includeHarnessIdentity: false" in text
    assert "includeRuntimeContext: false" in text
    assert "toolBash: false" in text and "toolJobs: false" in text
    assert "goals: false" in text
    assert "mcp-gh-puller" not in text  # runtime_config owns MCP injection.
    with_mcp = Path(dsh_cordis_path(generators._gh_puller_mcp("dsh"))).read_text(encoding="utf-8")
    assert "- id: mcp-gh-puller" in with_mcp
    assert "serverName: gh_puller" in with_mcp
    assert "gh_puller_mcp" in with_mcp and "--tool-profile" in with_mcp


def test_codex_home_isolation_and_graphify(tmp_path):
    """Limit an isolated Codex home to one server and allowlisted environment keys.

    This provides the same user-configuration boundary as CC and DSH isolation.
    """
    home = codex_home_setup(str(tmp_path / "graph"), auth_src=False,
                            mcp_servers=generators._gh_puller_mcp("codex"))
    text = Path(home, "config.toml").read_text(encoding="utf-8")
    assert text.startswith("[mcp_servers.gh_puller]")
    assert "gh_puller_mcp" in text
    assert 'env_vars = ["CBM_CACHE_DIR", "CBM_RUNTIME_DIR"]' in text
    assert text.count("[mcp_servers.") == 1  # No third-party servers cross the boundary.
    assert not (Path(home) / "auth.json").exists()  # auth_src=False keeps credentials isolated.


# ---------------------------------------------------------------------------
# Index readiness and failure propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_index_skips_when_ready(tmp_path, monkeypatch):
    """Return without running the indexer when an index is ready."""
    monkeypatch.setenv("CBM_CACHE_DIR", str(tmp_path / "cache"))
    repo = Repo(str(tmp_path), "local")
    cdir = generators._cbm_cache_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / f"{generators.project_name(repo)}.db").touch()

    async def boom(repo):
        raise AssertionError("index_ready 已 True,不得调用 _run_index")

    monkeypatch.setattr(generators, "_run_index", boom)
    await generators.ensure_index(repo)  # The stub proves _run_index was not called.


@pytest.mark.asyncio
async def test_ensure_index_calls_index_repository(tmp_path, monkeypatch):
    """Run repository indexing when no ready database exists."""
    repo = Repo(str(tmp_path), "local")
    calls = []

    async def fake_run_index(repo):
        calls.append(repo)

    monkeypatch.setattr(generators, "_run_index", fake_run_index)
    await generators.ensure_index(repo)
    assert calls == [repo]


@pytest.mark.asyncio
async def test_ensure_index_raises_on_index_error(tmp_path, monkeypatch):
    """Propagate index failures so callers can report them or fail the task."""
    repo = Repo(str(tmp_path), "local")

    async def fake_run_index(repo):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(generators, "_run_index", fake_run_index)
    with pytest.raises(RuntimeError, match="index exploded"):
        await generators.ensure_index(repo)
