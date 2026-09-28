"""Verify native readiness checks and browser-isolated credential validation."""

import httpx
import pytest

from agent_chat.app import create_app
from agent_chat.config import ServerSettings

from .conftest import login, question
from .fakes import FakeFactory


@pytest.fixture
async def validation_client(tmp_path):
    calls = []

    def probe(request):
        calls.append(request)
        value = request.headers.get("X-Subscription-Token", request.headers.get("Authorization", ""))
        return httpx.Response(401 if value.endswith("rejected-secret") else 200, text=value)

    app = create_app(ServerSettings(password="test-private-passphrase", temp_root=str(tmp_path),
                                    static_dir=tmp_path / "unbuilt-web"), agent_factory=FakeFactory(),
                     model_transport=httpx.MockTransport(probe))
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://chat.test") as client):
        yield app, client, calls


async def validate(client, **body):
    response = await client.post("/api/configuration/validate", json=body)
    assert response.status_code == 200
    for secret in body.get("credentials", {}).values():
        assert secret not in response.text
    return response.json()


async def test_readiness_requires_exact_credential_check_and_does_not_probe_while_editing(validation_client, tmp_path):
    _, client, calls = validation_client
    assert (await client.post("/api/configuration/validate", json={})).status_code == 401
    await login(client)
    report = await validate(client)
    tools = {item["id"]: item for item in report["tools"]}
    assert not report["agents"]["web"]["valid"] and not report["agents"]["code"]["valid"]
    assert not tools["web_search"]["valid"] and tools["web_fetch"]["valid"]
    assert "web" not in tools and "tool_results" not in tools
    for value in ("accepted-secret", "rejected-secret", "edited-secret"):
        report = await validate(client, credentials={"brave_api_key": value})
        assert report["fields"]["brave_api_key"]["pending"]
    assert not calls
    for value, valid in (("rejected-secret", False), ("accepted-secret", True)):
        result = await client.post("/api/credentials/test", json={"name": "brave_api_key", "value": value})
        assert result.status_code == (200 if valid else 422) and value not in result.text
        report = await validate(client, credentials={"brave_api_key": value})
        assert report["agents"]["web"]["valid"] == valid
    assert len(calls) == 2
    changed = await validate(client, credentials={"brave_api_key": "edited-secret"})
    assert changed["fields"]["brave_api_key"]["pending"]
    inactive = await validate(client, tools={"web_search_backend": "duckduckgo"},
                              credentials={"brave_api_key": "rejected-secret"})
    assert inactive["agents"]["web"]["valid"] and not inactive["fields"]["brave_api_key"]["active"]
    assert len(calls) == 2 and not list(tmp_path.iterdir())


async def test_configuration_graph_and_native_errors_follow_current_agent_options(validation_client):
    _, client, _ = validation_client
    await login(client)
    report = await validate(client, agents={"github": {"backend": "split"}},
                            tools={"web_search_backend": "duckduckgo", "web_search_concurrency": 0})
    tools = {item["id"]: item for item in report["tools"]}
    assert len(tools) == len(report["tools"])
    assert tools["github_rest"]["configuration"] == tools["github_graphql"]["configuration"]
    assert "github_token" in tools["github_rest"]["configuration"]
    assert tools["github_rest"]["valid"] and tools["web_fetch"]["valid"]
    assert set(tools["web_search"]["issues"]) == {"web_search_concurrency"}
    assert set(report["agents"]["github"]["issues"]) == {"web_search_concurrency"}
    updated = await validate(client, agents={"github": {"backend": "dsl"}, "gitcode": {"backend": "dsl"}})
    for name in ("github", "gitcode"):
        installed = {item["id"] for item in updated["agents"][name]["tools"]}
        assert name + "_dsl" in installed and name + "_rest" not in installed and name not in installed


async def test_validated_session_keys_are_private_and_logout_discards_checks(validation_client):
    app, client, _ = validation_client
    await login(client)
    value = "accepted-secret"
    await client.post("/api/credentials/test", json={"name": "brave_api_key", "value": value})
    session_id = (await client.post("/api/sessions", json={"agent": "web"})).json()["id"]
    body = question(backend=None, web_search_backend="brave")
    body["credentials"]["brave_api_key"] = value
    assert (await client.post(f"/api/sessions/{session_id}/questions", json=body)).status_code == 202
    await app.state.manager.sessions[session_id].task
    report = await validate(client, session_id=session_id)
    assert report["agents"]["web"]["valid"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://chat.test") as other:
        await login(other)
        report = await validate(other, credentials={"brave_api_key": value})
        assert report["fields"]["brave_api_key"]["pending"]
        assert (await other.post("/api/configuration/validate", json={"session_id": session_id})).status_code == 404
    await client.post("/api/auth/logout", json={})
    await login(client)
    report = await validate(client, credentials={"brave_api_key": value})
    assert report["fields"]["brave_api_key"]["pending"]
