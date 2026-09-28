"""Exercise resumed native contexts, branch prefixes, evidence safety and model discovery."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from agent_chat.app import create_app
from agent_chat.config import ServerSettings

from .conftest import login, question
from .test_app import create, finished


async def query_export(app, client, identifier, body):
    response = await client.post(f"/api/sessions/{identifier}/questions", json=body)
    assert response.status_code == 202, response.text
    session = await finished(app, identifier)
    events = session.replay(0, 100000)
    assert events[-1]["data"]["status"] == "completed", events[-1]
    assert not events[-1]["data"]["error"]
    return events


@pytest.mark.parametrize("kind", ["github", "gitcode", "web"])
async def test_own_event_stream_restores_native_memory_and_evidence(harness, kind):
    app, client, factory, _ = harness
    await login(client)
    original = await create(client, kind)
    body = question("first evidence", backend="" if kind == "web" else "rest")
    if kind != "web":
        body["settings"]["options"]["tool_result_num_user_query"] = 10
    events = await query_export(app, client, original, body)
    before = app.state.manager.sessions[original]
    messages = before.agent.messages[1:]
    files = {p.name: p.read_bytes() for p in before.storage.root.iterdir() if p.is_file()}
    old_root = before.root
    await app.state.manager.delete(before)
    response = await client.post("/api/sessions", json={"agent": kind, "events": events})
    assert response.status_code == 201, response.text
    assert not response.json()["recovery_warning"]
    resumed = response.json()["id"]
    body = question("second question", request_id="question-next", backend="" if kind == "web" else "rest")
    after = await query_export(app, client, resumed, body)
    assert after[:len(events)] == events
    assert factory.calls[2]["messages"][1:-1] == messages
    assert not old_root.exists()
    session = app.state.manager.sessions[resumed]
    assert all((session.storage.root / name).read_bytes() == data for name, data in files.items())
    if kind == "web":
        ref = next(iter(session.agent.web_tools.resources))
        result = await session.agent.web_tools.web_fetch("read-again", [{"ref": ref}])
        assert "error" not in result["results"][0]
    else:
        result_id = next(iter(session.agent.tools.responses))
        assert session.agent.tools._body(session.agent.tools._saved(result_id)) in files.values()


async def test_cross_agent_roundtrip_keeps_context_without_resurrecting_private_memory(harness):
    app, client, factory, settings = harness
    await login(client)
    settings.max_sessions = 1
    first = await create(client)
    events = await query_export(app, client, first, question("original", tool_result_preview_chars=1))
    before = app.state.manager.sessions[first]
    saved_ids = set(before.agent.tool_results.saved)
    assert saved_ids
    response = await client.post("/api/sessions", json={"agent": "web", "events": events, "source_session": first})
    assert response.status_code == 201, response.text
    assert len(app.state.manager.sessions) == 1 and not before.root.exists()
    second = response.json()["id"]
    body = question("web continuation", request_id="question-second", backend="")
    body["credentials"] = {}
    web_events = await query_export(app, client, second, body)
    assert not app.state.manager.sessions[second].agent.tool_results.saved
    assert web_events[:len(events)] == events
    assert {tool["function"]["name"] for tool in factory.calls[2]["tools"]} == {"web_search", "web_fetch"}
    assert len([message for message in factory.calls[2]["messages"] if message["role"] == "system"]) == 1
    response = await client.post("/api/sessions", json={
        "agent": "github", "events": web_events, "source_session": second})
    third = response.json()["id"]
    await query_export(app, client, third, question("back to GitHub", request_id="question-third"))
    session = app.state.manager.sessions[third]
    assert session.agent.tools.responses
    result = await session.agent.tool_results.get_tool_result("restore-original", sorted(saved_ids))
    assert all("error" in item for item in json.loads(result.content)["results"])
    assert len([message for message in session.agent.messages if message["role"] == "system"]) == 1


async def test_edit_prefix_excludes_replaced_turn_and_keeps_original_history(harness):
    app, client, factory, _ = harness
    await login(client)
    source = await create(client)
    prefix = await query_export(app, client, source, question("first"))
    original = await query_export(app, client, source, question("old second", request_id="question-second"))
    response = await client.post("/api/sessions", json={"agent": "github", "events": prefix, "source_session": source})
    edited = response.json()["id"]
    await query_export(app, client, edited, question("edited second", request_id="question-edit"))
    prompt = factory.calls[4]["messages"]
    assert [message["content"] for message in prompt if message["role"] == "user"] == ["first", "edited second"]
    turn = next(event for event in original
                if event["type"] == "query/start" and event["query_id"] == "question-second")
    assert turn["data"]["prompt"] == "old second"


async def test_legacy_history_restores_answers_without_claiming_evidence(harness):
    app, client, factory, _ = harness
    await login(client)
    events = [{"seq": 1, "type": "query/start", "at": "2026-09-28", "query_id": "old",
               "data": {"prompt": "old question"}},
              {"seq": 2, "type": "query/end", "at": "2026-09-28", "query_id": "old",
               "data": {"answer": "old answer", "old_ref": "0042-web-fetch"}}]
    response = await client.post("/api/sessions", json={"agent": "github", "events": events})
    assert response.json()["recovery_warning"]
    await query_export(app, client, response.json()["id"], question("continue"))
    text = {m["content"] for m in factory.calls[0]["messages"][1:] if isinstance(m.get("content"), str)}
    assert text >= {"old question", "old answer", "continue"}


async def test_legacy_checkpoints_migrate_context_once_and_ignore_private_files(harness):
    app, client, factory, _ = harness
    await login(client)
    messages = [{"role": "user", "content": "old question"}, {"role": "assistant", "content": "old answer"}]
    events = [{"seq": 1, "type": "context/checkpoint", "at": "2026-09-28", "query_id": "old",
               "data": {"messages": messages, "files": {"../../not-used": "not-base64"}}},
              {"seq": 2, "type": "query/end", "at": "2026-09-28", "query_id": "old",
               "data": {"answer": "old answer"}}]
    response = await client.post("/api/sessions", json={"agent": "github", "events": events})
    assert response.status_code == 201 and response.json()["recovery_warning"]
    await query_export(app, client, response.json()["id"], question("continue"))
    assert factory.calls[0]["messages"][1:-1] == messages


@pytest.mark.parametrize("attack", ["path", "context", "sequence"])
async def test_invalid_restore_releases_resources_and_never_reads_host_files(harness, attack):
    app, client, _, settings = harness
    await login(client)
    source = await create(client)
    events = await query_export(app, client, source, question("secret evidence", tool_result_preview_chars=1))
    if attack == "path":
        next(event["data"] for event in events if event["type"] == "artifact/saved")["path"] = "../../outside"
    elif attack == "context":
        next(event["data"] for event in events if event["type"] == "context/append/user")["items"] = "invalid"
    else:
        events[0]["seq"] = 2
    previous = set(app.state.manager.sessions)
    response = await client.post("/api/sessions", json={"agent": "github", "events": events, "source_session": source})
    assert response.status_code == 422
    assert set(app.state.manager.sessions) == previous
    assert len(await asyncio.to_thread(lambda: list(Path(settings.temp_root).iterdir()))) == 1
    assert "fixture-model-secret-value" not in response.text


@pytest.mark.parametrize("kind", ["github/response_saved", "tool_result/saved"])
async def test_tool_owned_restore_rejects_external_files_before_inference(harness, kind, tmp_path):
    app, client, factory, _ = harness
    await login(client)
    source = await create(client)
    events = await query_export(app, client, source, question("evidence", tool_result_preview_chars=1))
    secret = tmp_path / "outside-private-data"
    secret.write_text("must-never-be-read")
    data = next(event["data"] for event in events if event["type"] == kind)
    if kind == "github/response_saved":
        data["metadata"]["body_file"] = str(secret)
    else:
        data["artifact"] = str(secret)
    response = await client.post("/api/sessions", json={"agent": "github", "events": events})
    assert response.status_code == 201
    identifier = response.json()["id"]
    calls = len(factory.calls)
    assert (await client.post(f"/api/sessions/{identifier}/questions", json=question("continue"))).status_code == 202
    session = await finished(app, identifier)
    assert session.replay(0, 100000)[-1]["data"]["status"] == "failed"
    assert session.agent is None and session.context is None
    assert len(factory.calls) == calls
    assert "must-never-be-read" not in json.dumps(session.replay(0, 100000))
    assert (await client.post(f"/api/sessions/{identifier}/questions", json=question(
        "retry", request_id="invalid-retry"))).status_code == 202
    await finished(app, identifier)
    assert session.agent is None and len(factory.calls) == calls
    await app.state.manager.delete(session)
    assert not session.root.exists()


async def test_clone_cannot_copy_another_browser_credentials(harness):
    app, client, _, _ = harness
    await login(client)
    source = await create(client)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://chat.test") as other:
        await login(other)
        assert (await other.post("/api/sessions", json={"agent": "web", "source_session": source})).status_code == 404


async def test_models_endpoint_auth_discovery_and_failure_redaction(tmp_path):
    requests = []

    def models(request):
        requests.append(request)
        if "bad" in request.headers["authorization"]:
            return httpx.Response(401, text=request.headers["authorization"])
        return httpx.Response(200, json={"data": [{"id": "model-b"}, {"id": "model-a"}, {"id": "model-a"}]})

    app = create_app(ServerSettings(password="test-private-passphrase", temp_root=str(tmp_path),
                                   static_dir=tmp_path / "unbuilt-web"),
                     model_transport=httpx.MockTransport(models))
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://chat.test") as client):
        body = {"base_url": "https://model.example/v1", "api_key": "model-secret"}
        assert (await client.post("/api/models", json=body)).status_code == 401
        await login(client)
        response = await client.post("/api/models", json=body)
        assert response.json()["models"] == ["model-a", "model-b"]
        assert str(requests[0].url) == "https://model.example/v1/models"
        assert requests[0].headers["authorization"] == "Bearer model-secret"
        response = await client.post("/api/models", json={**body, "api_key": "bad-secret"})
        assert response.status_code == 422 and "bad-secret" not in response.text
        response = await client.post("/api/models", json={**body, "base_url": "https://secret@example.com/v1"})
        assert response.status_code == 422 and "secret" not in response.text
        assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(("name", "host", "header"), [
    ("github_token", "api.github.com", "authorization"),
    ("gitcode_token", "api.gitcode.com", "authorization"),
    ("brave_api_key", "api.search.brave.com", "x-subscription-token"),
])
async def test_credential_probes_use_fixed_hosts_and_never_echo_tokens(tmp_path, name, host, header):
    calls = []

    def probe(request):
        calls.append(request)
        return httpx.Response(401, text=request.headers.get(header))

    app = create_app(ServerSettings(password="test-private-passphrase", static_dir=tmp_path / "unbuilt-web"),
                     model_transport=httpx.MockTransport(probe))
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://chat.test") as client):
        await login(client)
        response = await client.post("/api/credentials/test", json={"name": name, "value": "private-test-token"})
        assert response.status_code == 422 and "private-test-token" not in response.text
        assert calls[0].url.host == host
        assert "private-test-token" not in str(calls[0].url)
        assert calls[0].headers[header].endswith("private-test-token")
        response = await client.post("/api/credentials/test", json={"name": "unknown", "value": "private-test-token"})
        assert response.status_code == 422 and len(calls) == 1
