"""Provide authenticated application clients without external services."""

import httpx
import pytest

from agent_chat.app import create_app
from agent_chat.config import ServerSettings

from .fakes import FakeFactory


@pytest.fixture
async def harness(tmp_path):
    factory = FakeFactory()
    settings = ServerSettings(password="test-private-passphrase", temp_root=str(tmp_path))
    app = create_app(settings, agent_factory=factory)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://chat.test") as client,
    ):
        yield app, client, factory, settings


async def login(client):
    return await client.post("/api/auth/login", json={"password": "test-private-passphrase"})


def question(prompt="Find evidence", *, request_id="request-0001", backend="rest", **settings):
    return {"request_id": request_id, "prompt": prompt,
            "settings": {"base_url": "https://model.example/v1", "model": "fixture-model", "backend": backend,
                         "web_search_backend": "duckduckgo", "multimodal": False, **settings},
            "credentials": {"api_key": "fixture-model-secret-value"}}
