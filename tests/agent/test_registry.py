"""Verify configuration discovery stays consistent with native agent construction."""

from typing import ClassVar

import pytest

from gh_puller.agents import AGENTS, GitCodeAgent, GitHubAgent, WebAgent, register
from gh_puller.configuration import option
from gh_puller.tools.storage import ToolStorage
from gh_puller.tools.tool_offload import OffloadPolicy


def test_added_fields_and_inherited_metadata_need_no_application_schema(monkeypatch, tmp_path):
    monkeypatch.setattr("gh_puller.agents.registry.AGENTS", dict(AGENTS))

    @register
    class ResearchAgent(WebAgent):
        name = "research"
        defaults: ClassVar[dict] = {**WebAgent.defaults, "strategy": option("fast", choices=("fast", "deep")),
                                   "candidate_count": 4096, "follow_links": True}

    config = ResearchAgent.configuration
    catalog = config.catalog({})
    fields = {field["key"]: field for field in catalog["fields"]}
    assert fields["candidate_count"]["type"] == "integer"
    assert fields["follow_links"]["type"] == "boolean"
    assert fields["strategy"]["choices"] == [{"value": value, "reason": ""} for value in ("fast", "deep")]
    assert "max_steps" not in fields
    tools = {tool["id"]: tool["configuration"] for tool in catalog["tools"]}
    assert set(tools) == {"web_search", "web_fetch"}
    assert "brave_api_key" in tools["web_search"] and not tools["web_fetch"]
    assert catalog["credentials"]["brave_api_key"]["required_when"] == {"web_search_backend": "brave"}
    assert catalog["credentials"]["brave_api_key"]["testable"]
    assert ResearchAgent.defaults["strategy"] == "fast"
    native = config.resolve({"candidate_count": 100000, "strategy": "deep"}, {})
    subject = ResearchAgent({"model": "arbitrary", "base_url": "https://example.org", **native},
                            ToolStorage(tmp_path), api_key="")
    assert subject.options["candidate_count"] == 100000 and subject.options["strategy"] == "deep"
    assert subject.config["max_steps"] == 0
    with pytest.raises(ValueError, match="strategy"):
        config.resolve({"strategy": "invented"}, {})


def test_shared_tools_defaults_and_operator_only_bindings():
    for agent in (GitHubAgent, GitCodeAgent):
        assert agent.defaults["backend"] == "rest"
        assert agent.defaults["web_search_backend"] == WebAgent.defaults["web_search_backend"]
        config = agent.configuration
        assert config.owners["web_search_backend"] == "web"
        assert "ptc" not in config.owners
        assert "brave_api_key" not in config.public_defaults()
        assert "brave_api_key" in agent.credential_names
        with pytest.raises(ValueError, match="node"):
            config.resolve({"ptc": "A"}, {})
        assert config.resolve({"ptc": "A"}, {"node": True})["agent_options"]["ptc"] == "A"
    with pytest.raises(ValueError, match="mcp_config"):
        GitHubAgent.configuration.resolve({"mcp_config": "/private/file"}, {})
    with pytest.raises(ValueError, match="brave_api_key"):
        WebAgent.configuration.validate_credentials(WebAgent.defaults, {})


@pytest.mark.parametrize("backend", ["rest", "graphql", "dsl", "split"])
def test_tool_discovery_preserves_registered_identity_under_runtime_aliases(backend):
    options = {**GitHubAgent.defaults, "backend": backend}
    tools = {item["id"]: item["configuration"] for item in GitHubAgent.configuration_tools(options)}
    name = "github_" + ("rest" if backend == "split" else backend)
    assert not {"github", "web", "tool_results"} & tools.keys()
    assert "github_token" in tools[name]
    assert name + ".tool_result_preview_chars" in tools[name]
    assert ("github_graphql" in tools) == (backend in {"split", "graphql"})
    installed = next(item for item in GitHubAgent.configuration_tools(options) if item["id"] == name)
    assert installed["call_name"] == "github"
    if backend == "split":
        assert set(tools["github_rest"]) & set(tools["github_graphql"]) == {"github_token"}
    ptc = {item["id"]: item["configuration"]
           for item in GitHubAgent.configuration_tools({**options, "ptc": "A"})}
    assert {"github_token", "brave_api_key"} <= set(ptc["run_code"])


def test_complete_catalog_and_inactive_tool_settings_are_independent_of_agent_selection():
    config = GitHubAgent.configuration
    catalog = config.catalog({})
    fields = {field["key"]: field for field in catalog["fields"]}
    assert "tool_result_preview_chars" not in fields
    assert fields["github_dsl.tool_result_preview_chars"]["effective_default"] == 2000
    expected = {item["id"] for item in catalog["tool_catalog"]}
    assert {"github_rest", "github_graphql", "github_dsl", "run_code"} <= expected
    options = {"web_search_backend": "duckduckgo", "github_dsl.tool_result_preview_chars": 0}
    report = config.validate(options, {}, {})
    assert report["valid"]
    assert {item["id"] for item in report["tool_catalog"]} == expected
    assert not next(item for item in report["tool_catalog"] if item["id"] == "github_dsl")["valid"]
    config.resolve(options, {})
    options["backend"] = "dsl"
    assert not config.validate(options, {}, {})["valid"]
    with pytest.raises(ValueError, match="positive"):
        config.resolve(options, {})
    with pytest.raises(ValueError, match="backend"):
        config.resolve({"backend": "unknown"}, {})
    with pytest.raises(ValueError, match="Unsupported"):
        config.resolve({"max_steps": 1}, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["rest", "graphql", "dsl", "split"])
async def test_tool_result_overrides_follow_registered_tool_identity_under_aliases(tmp_path, backend):
    options = {"backend": backend, "web_search_backend": "duckduckgo",
               "tool_result_preview_lines": 3, "github_rest.tool_result_preview_chars": 37,
               "github_graphql.tool_result_preview_chars": 59, "github_dsl.tool_result_preview_chars": 83,
               "web_fetch.tool_result_preview_lines": 7}
    native = GitHubAgent.configuration.resolve(options, {})
    agent = GitHubAgent({"model": "test", "base_url": "https://model.example/v1", **native},
                        ToolStorage(tmp_path), api_key="")
    async with agent.session():
        expected = {"rest": 37, "graphql": 59, "dsl": 83, "split": 37}[backend]
        assert agent.offload_policies["github"].preview_chars == expected
        assert agent.offload_policies["github"].preview_lines == 3
        assert agent.offload_policies["web_fetch"].preview_chars == 2000
        assert agent.offload_policies["web_fetch"].preview_lines == 7
        assert agent.offload_policies["web_search"].preview_lines == 3
        if backend == "split":
            assert agent.offload_policies["github_graphql"].preview_chars == 59
        agent.install_tools((agent.tools, "github"), offload=OffloadPolicy(preview_lines=17))
        assert agent.offload_policies["github"].preview_lines == 17
        assert agent.offload_policies["github"].preview_chars == expected


def test_native_validators_control_tools_and_agent_without_network_calls():
    config = WebAgent.configuration
    missing = config.validate({}, {}, {})
    tools = {item["id"]: item for item in missing["tools"]}
    assert not missing["valid"] and not tools["web_search"]["valid"]
    assert tools["web_fetch"]["valid"]
    assert missing["issues"]["brave_api_key"]["reason"] == "Required credential"
    supplied = {"brave_api_key": "memory-only"}
    pending = config.validate({}, supplied, {})
    assert pending["issues"]["brave_api_key"]["pending"]
    rejected = {"brave_api_key": {"valid": False, "reason": "Rejected by provider"}}
    assert not config.validate({}, supplied, {}, rejected)["valid"]
    assert config.validate({}, supplied, {}, {"brave_api_key": {"valid": True}})["valid"]
    inactive = config.validate({"web_search_backend": "duckduckgo"}, supplied, {}, rejected)
    assert inactive["valid"] and not inactive["fields"]["brave_api_key"]["active"]
    assert config.validate({"web_search_backend": "auto"}, {}, {})["valid"]
    invalid = config.validate({"web_search_backend": "duckduckgo", "web_search_interval": -1}, {}, {})
    assert set(invalid["issues"]) == {"web_search_interval"}
    assert not next(item for item in invalid["tools"] if item["id"] == "web_search")["valid"]
    with pytest.raises(ValueError, match="non-negative"):
        config.resolve({"web_search_interval": -1}, {})
