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
