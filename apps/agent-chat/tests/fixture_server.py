"""Launch an isolated deterministic app for browser tests; production never imports this module."""

from typing import ClassVar

from gh_puller.agents import WebAgent, register
from gh_puller.configuration import option

from agent_chat.app import create_app
from agent_chat.config import ServerSettings

from .fakes import FakeFactory


@register
class ResearchAgent(WebAgent):
    name = "fixture_research"
    defaults: ClassVar[dict] = {**WebAgent.defaults, "strategy": option("fast", choices=("fast", "deep")),
                               "candidate_count": 20, "follow_links": True}


app = create_app(ServerSettings(password="test-private-passphrase", secure_cookie=False), agent_factory=FakeFactory())
