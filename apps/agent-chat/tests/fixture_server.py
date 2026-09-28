"""Launch an isolated deterministic app for browser tests; production never imports this module."""

from typing import ClassVar

import httpx
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


def connection(request):
    if "invalid-tool-key" in (request.headers.get("Authorization", "").removeprefix("Bearer "),
                              request.headers.get("X-Subscription-Token", "")):
        return httpx.Response(401)
    return httpx.Response(200, json={"data": [
        {"id": "fixture-model"}, {"id": "provider/very-long-model-name-for-research-2026-09-preview"},
    ]})


app = create_app(ServerSettings(password="test-private-passphrase", secure_cookie=False), agent_factory=FakeFactory(),
                 model_transport=httpx.MockTransport(connection))
