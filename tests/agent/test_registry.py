"""Verify configuration discovery stays consistent with native agent construction."""

from typing import ClassVar

import pytest

from gh_puller.agents import AGENTS, GitCodeAgent, GitHubAgent, WebAgent, register
from gh_puller.configuration import option
from gh_puller.tools.storage import ToolStorage


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
    assert "tool_result_preview_chars" in tools[name]
    assert ("github_graphql" in tools) == (backend in {"split", "graphql"})
    installed = next(item for item in GitHubAgent.configuration_tools(options) if item["id"] == name)
    assert installed["call_name"] == "github"
    if backend == "split":
        assert tools["github_rest"] == tools["github_graphql"]
    ptc = {item["id"]: item["configuration"]
           for item in GitHubAgent.configuration_tools({**options, "ptc": "A"})}
    assert {"github_token", "brave_api_key"} <= set(ptc["run_code"])


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
