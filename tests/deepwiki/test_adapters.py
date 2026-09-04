"""Test DeepWiki generator selection and adapter configuration."""

import pytest

from gh_puller import deepwiki
from gh_puller.agent import AGENTS
from gh_puller.deepwiki import utils as deepwiki_utils
from gh_puller.deepwiki.utils import adapt_generator
from gh_puller.utils import Repo
from tests.deepwiki._support import _gen_kwargs


class _FakeGenerator:
    """Capture adapter configuration without constructing a real SDK client.

    Real DSH and Codex construction writes isolated runtime directories, which
    configuration-only tests must avoid.
    """

    generator = "cc"  # Individual cases replace this with the selected generator.

    def __init__(self, config):
        self.config = dict(config)


@pytest.mark.asyncio
async def test_agent_dispatch_by_generator(monkeypatch):
    """Dispatch wire targets through the registered generator constructor."""
    for gid in ("cc", "dsh", "codex", "opencode", "llm"):
        monkeypatch.setitem(AGENTS, gid, _FakeGenerator)
        monkeypatch.setattr(_FakeGenerator, "generator", gid)
        inst = adapt_generator(**_gen_kwargs({"generator": gid}), system_prompt="s")
        assert inst.generator == gid and isinstance(inst, _FakeGenerator)
        assert inst.config["system_prompt"] == "s"  # Configuration is injected at construction.
    monkeypatch.setattr(_FakeGenerator, "generator", "cc")
    inst = adapt_generator(**_gen_kwargs({}))  # The engine owns the empty-target CC fallback.
    assert inst.generator == "cc" and isinstance(inst, _FakeGenerator)


def test_dsh_options_config(monkeypatch, tmp_path):
    """Keep DSH runtime state outside the checkout while passing graph configuration.

    File-backed model and credential configuration stays outside this assembly
    layer, while the repository and allowed MCP server settings are injected.
    """
    repo = Repo(str(tmp_path), "local")
    monkeypatch.setitem(AGENTS, "dsh", _FakeGenerator)
    monkeypatch.setattr(_FakeGenerator, "generator", "dsh")
    fake_mcp = [{"id": "fake", "command": "x"}]
    cfg = adapt_generator(
        **_gen_kwargs({"generator": "dsh", "generator_config": {"mcp_servers": fake_mcp}}),
        system_prompt="sys", repo=repo,
    ).config
    assert cfg["cwd"] == str(tmp_path)
    assert "model" not in cfg  # File-backed models are not part of the request contract.
    assert "api_key" not in cfg and "base_url" not in cfg
    assert cfg["session_root"].endswith("dsh-sessions")
    assert "dsh-runtime" in cfg["runtime_cwd"]  # Runtime state stays outside the task checkout.
    assert cfg["system_prompt"] == "sys"  # The adapter maps this concept to DSH_SYSTEM_PROMPT.
    assert cfg["mcp_servers"] == fake_mcp
    cfg2 = adapt_generator(**_gen_kwargs({"generator": "dsh"}), system_prompt="sys").config
    assert "cwd" not in cfg2  # No repository means the process default remains authoritative.
    assert "mcp_servers" not in cfg2  # Graph tools are injected only for a repository-scoped run.


def test_opencode_options_config(monkeypatch, tmp_path):
    """Pin OpenCode to the repository and pass only supported runtime settings.

    Headless mode is owned by the engine, while prompt, MCP, environment, and
    model settings cross the generator configuration boundary unchanged.
    """
    repo = Repo(str(tmp_path), "local")
    monkeypatch.setitem(AGENTS, "opencode", _FakeGenerator)
    monkeypatch.setattr(_FakeGenerator, "generator", "opencode")
    fake_mcp = [{"id": "fake", "command": "x"}]
    cfg = adapt_generator(
        **_gen_kwargs({"generator": "opencode",
                       "generator_config": {"mcp_servers": fake_mcp,
                                            "env": {"GRAPHIFY_OUT": "/g"},
                                            "model": "deepseek/deepseek-chat"}}),
        system_prompt="sys", repo=repo,
    ).config
    assert cfg["cwd"] == str(tmp_path)  # The repository root is the execution root.
    assert cfg["auto"] is True  # The engine owns the headless default.
    assert cfg["system_prompt"] == "sys"
    assert cfg["mcp_servers"] == fake_mcp
    assert cfg["env"] == {"GRAPHIFY_OUT": "/g"}
    assert cfg["model"] == "deepseek/deepseek-chat"  # Explicit model selection passes through.
    cfg2 = adapt_generator(**_gen_kwargs({"generator": "opencode"}), system_prompt="sys").config
    assert "cwd" not in cfg2 and "mcp_servers" not in cfg2  # Repository scope gates both fields.


def test_agent_options_cc_setting_sources_isolated(monkeypatch):
    """Isolate CC from user-level MCP servers, skills, and hooks."""
    monkeypatch.setitem(AGENTS, "cc", _FakeGenerator)
    cfg = adapt_generator(**_gen_kwargs({}), system_prompt="sys").config
    assert cfg["setting_sources"] == []
    assert cfg["system_prompt"] == "sys"

def test_options_cc_cache_write_mode(monkeypatch, tmp_path):
    """Grant CC cache writes only while producing persisted wiki files.

    Interactive chat and codemap runs keep read-only repository tools. Models,
    credentials, and graph tools remain governed by their target contracts.
    """
    repo = Repo(str(tmp_path), "local")
    monkeypatch.setitem(AGENTS, "cc", _FakeGenerator)
    graph_tools = ["graphify_query", "mcp__graphify__graphify_query"]
    cfg = adapt_generator(
        **_gen_kwargs({"generator_config": {"allowed_tools": graph_tools}}),
        system_prompt="", repo=repo,
        generator_cache_dir=str(tmp_path / "out"), generator_cache_write_mode=True,
    ).config
    assert cfg["cwd"] == str(tmp_path)
    assert cfg["add_dirs"] == [str(tmp_path / "out")]
    assert cfg["permission_mode"] == "acceptEdits"
    assert "model" not in cfg  # File-backed model selection stays outside this layer.
    for t in ("Read", "Grep", "Glob", "Write", *graph_tools):
        assert t in cfg["allowed_tools"], t
    cfg2 = adapt_generator(
        **_gen_kwargs({"generator_config": {"allowed_tools": graph_tools}}),
        system_prompt="", repo=repo,
    ).config
    assert cfg2["cwd"] == str(tmp_path)
    assert "add_dirs" not in cfg2 and "permission_mode" not in cfg2  # The SDK retains its defaults.
    for t in ("Read", "Grep", "Glob", *graph_tools):
        assert t in cfg2["allowed_tools"], t
    assert "Write" not in cfg2["allowed_tools"]

def test_resolve_generator_default_cc():
    """Use the engine's CC default without consulting environment selection."""
    assert deepwiki_utils.resolve_generator()[0] == "cc"

def test_codex_options_config(monkeypatch, tmp_path):
    """Pass object-backed Codex settings without reading a configuration file.

    Repository-scoped runs receive the checkout, graph environment, and MCP
    servers; unscoped runs leave those fields absent.
    """
    repo = Repo(str(tmp_path), "local")
    monkeypatch.setitem(AGENTS, "codex", _FakeGenerator)
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
    assert "cwd" not in cfg2 and "env" not in cfg2 and "mcp_servers" not in cfg2  # Scope gates graph fields.
    assert "model" not in cfg2  # The SDK remains responsible for its model default.
    # Codex follows CC's credential surface and must not imply mandatory environment settings.
    assert not hasattr(deepwiki.envs, "CODEX_HOME")
    assert not hasattr(deepwiki.envs, "CODEX_API_KEY")
