"""Verify authentication, replay, isolation, idempotency, cancellation and cleanup."""

import asyncio
import base64
import hashlib
import json
import threading
import time

import httpx
import pytest
from gh_puller.agent.events import fold_state

from agent_chat.app import COOKIE
from agent_chat.config import Question

from .conftest import login, question


async def create(client, kind="github"):
    response = await client.post("/api/sessions", json={"agent": kind})
    assert response.status_code == 201
    return response.json()["id"]


async def finished(app, session_id):
    session = app.state.manager.sessions[session_id]
    if session.task:
        await session.task
    return session


async def test_auth_cookie_csrf_and_validation_redaction(harness):
    _, client, _, _ = harness
    assert (await client.get("/api/health")).status_code == 200
    assert (await client.get("/api/sessions")).status_code == 401
    assert (await client.post("/api/auth/login", json={"password": "bad"})).status_code == 401
    response = await login(client)
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert (await client.get("/api/auth/me")).status_code == 200
    response = await client.post("/api/sessions", json={}, headers={"Origin": "https://evil.test"})
    assert response.status_code == 403
    response = await client.post("/api/auth/login", json={"password": {"secret": "never-echo-me"}})
    assert response.status_code == 422 and "never-echo-me" not in response.text
    assert (await client.post("/api/sessions", content="{}")).status_code == 415


async def test_rate_limit_login(harness):
    _, client, _, _ = harness
    for _ in range(8):
        assert (await client.post("/api/auth/login", json={"password": "bad"})).status_code == 401
    assert (await login(client)).status_code == 429


async def test_body_limit_stops_reading_chunked_upload(harness):
    _, client, _, _ = harness
    chunks = []

    async def upload():
        for index in range(10):
            chunks.append(index)
            yield b"x" * (64 * 1024)

    response = await client.post("/api/auth/login", content=upload(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413 and len(chunks) == 3


async def test_ownership_and_capabilities(harness):
    app, client, _, _ = harness
    await login(client)
    session_id = await create(client)
    catalog = (await client.get("/api/catalog")).json()
    code = next(item for item in catalog["agents"] if item["id"] == "code")
    assert not code["available"] and code["reason"]
    assert (await client.post("/api/sessions", json={"agent": "code"})).status_code == 422
    github = next(item for item in catalog["agents"] if item["id"] == "github")
    assert github["defaults"]["concurrency"] == 8
    assert github["defaults"]["backend"] == "rest"
    assert "max_steps" not in github["defaults"] and catalog["defaults"]["max_tokens"] == 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://chat.test") as other:
        await login(other)
        assert (await other.get("/api/sessions")).json() == []
        for path in (f"/api/sessions/{session_id}", f"/api/sessions/{session_id}/events",
                     f"/api/sessions/{session_id}/export"):
            assert (await other.get(path)).status_code == 404
        assert (await other.post(f"/api/sessions/{session_id}/stop", json={})).status_code == 404
        response = await other.delete(f"/api/sessions/{session_id}", headers={"Content-Type": "application/json"})
        assert response.status_code == 404
    web = await create(client, "web")
    assert (await client.post(f"/api/sessions/{web}/questions", json=question(backend="", ptc="A"))).status_code == 422
    response = await client.post(f"/api/sessions/{session_id}/questions", json=question(backend="gh-cli"))
    assert response.status_code == 422


@pytest.mark.parametrize(("kind", "backend"), [("github", "rest"), ("gitcode", "rest"), ("web", "")])
async def test_real_agents_replay_export_and_secret_free_files(harness, kind, backend):
    app, client, factory, _ = harness
    await login(client)
    session_id = await create(client, kind)
    assert (await client.get(f"/api/sessions/{session_id}")).json()["configured_credentials"] == []
    response = await client.post(f"/api/sessions/{session_id}/questions",
                                 json=question("secret evidence", backend=backend))
    assert response.status_code == 202
    session = await finished(app, session_id)
    view = (await client.get(f"/api/sessions/{session_id}")).json()
    assert view["configured_credentials"] == ["api_key"]
    assert "fixture-model-secret-value" not in json.dumps(view)
    events = session.replay(0, 10000)
    assert events[-1]["data"]["status"] == "completed"
    assert len(factory.calls) == 2
    assert {event["type"] for event in events} >= {"tool/start", "tool/end", "model/response", "query/end"}
    data = (await client.get(f"/api/sessions/{session_id}/export")).json()
    assert data["events"] == events
    assert data["version"] == 3 and data["artifacts"]
    assert f"events_{session_id}.json" in (await client.get(
        f"/api/sessions/{session_id}/export")).headers["content-disposition"]
    for digest, content in data["artifacts"].items():
        decoded = base64.b64decode(content)
        assert hashlib.sha256(decoded).hexdigest() == digest
        assert b"fixture-model-secret-value" not in decoded
    assert all("content" not in e["data"] for e in events if e["type"] == "artifact/saved")
    assert "fixture-model-secret-value" not in json.dumps(data)
    files = [path for path in session.root.rglob("*") if path.is_file()]
    assert all(b"fixture-model-secret-value" not in path.read_bytes() for path in files)
    assert not any(path.name in {"manifest.json", "source.tar", "timeline.jsonl"} for path in session.root.rglob("*"))
    after = events[3]["seq"]
    response = await client.get(f"/api/sessions/{session_id}/events?after=1", headers={"Last-Event-ID": str(after)})
    packets = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
    replay = [packet for packet in packets if "seq" in packet]
    assert replay == events[after:]
    assert {packet["sha256"]: packet["content"] for packet in packets if "sha256" in packet} == data["artifacts"]
    assert "event: idle" in response.text
    assert (await client.get(f"/api/sessions/{session_id}/events?after=999999")).status_code == 409


async def test_duplicate_submission_and_busy_session(harness):
    app, client, factory, _ = harness
    await login(client)
    session_id = await create(client)
    url = f"/api/sessions/{session_id}/questions"
    body = question()
    assert (await client.post(url, json=body)).status_code == 202
    assert (await client.post(url, json=body)).json()["duplicate"]
    assert (await client.post(url, json=question(request_id="request-0002"))).status_code == 409
    assert (await client.post(url, json=question("different prompt"))).status_code == 409
    await finished(app, session_id)
    assert (await client.post(url, json=body)).json()["duplicate"]
    assert len(factory.calls) == 2
    assert (await client.post(url, json=question("continue context", request_id="request-0002"))).status_code == 202
    session = await finished(app, session_id)
    assert "已有上下文" in session.replay(0, 10000)[-1]["data"]["answer"]
    assert len(factory.agents) == 1
    assert (await client.post(url, json=question(request_id="request-0003", concurrency=3))).status_code == 409


async def test_reused_instance_observes_changed_model_and_request_controls(harness):
    app, client, factory, _ = harness
    await login(client)
    identifier = await create(client)
    url = f"/api/sessions/{identifier}/questions"
    await client.post(url, json=question())
    session = await finished(app, identifier)
    body = question("next model", request_id="request-0002")
    body["settings"].update(model="next-model", reasoning_effort="provider-custom", thinking=True)
    await client.post(url, json=body)
    await finished(app, identifier)
    assert len(factory.agents) == 1 and factory.calls[2]["model"] == "next-model"
    assert factory.calls[2]["reasoning_effort"] == "provider-custom"
    events = session.replay(0, 10000)
    assert fold_state(events)["agent"]["config"] == session.agent.config
    assert len([event for event in events if event["type"] == "session/start"]) == 1
    unchanged = len(events)
    body["request_id"] = "request-0003"
    await client.post(url, json=body)
    await finished(app, identifier)
    assert not any(e["type"].startswith("agent/set/") for e in session.replay(unchanged, 10000))


@pytest.mark.parametrize("variant", ["A", "B"])
async def test_ptc_dispatches_nested_tools_and_releases_files(harness, variant):
    app, client, _, _ = harness
    await login(client)
    session_id = await create(client)
    await client.post(f"/api/sessions/{session_id}/questions", json=question("secret evidence", ptc=variant))
    session = await finished(app, session_id)
    events = session.replay(0, 10000)
    assert events[-1]["data"]["status"] == "completed"
    starts = [event["data"] for event in events if event["type"] == "tool/start"]
    assert {event["name"] for event in starts} == {"github", "run_code"}
    assert next(event for event in starts if event["name"] == "github")["parentCallId"]
    assert not any(event["type"] == "tool/end" and "error" in event["data"] for event in events)
    assert "fixture-model-secret-value" not in json.dumps(events)
    assert not list(session.root.rglob("*.ts"))
    root = session.root
    await app.state.manager.delete(session)
    assert not root.exists()


async def test_schema_initialization_keeps_http_and_cancellation_responsive(harness, monkeypatch):
    app, client, _, _ = harness
    started, release = threading.Event(), threading.Event()
    builds = []

    def initialize():
        builds.append(True)
        started.set()
        release.wait(3)

    monkeypatch.setattr("gh_puller.agents.agent_search.query_schema", initialize)
    await login(client)
    first, second = await create(client), await create(client)
    manager = app.state.manager
    session = manager.sessions[first]
    try:
        manager.submit(session, Question.model_validate(question(backend="dsl")))
        assert await asyncio.to_thread(started.wait, 1)
        async with asyncio.timeout(1):
            assert (await client.get("/api/health")).status_code == 200
            await client.post(f"/api/sessions/{second}/questions", json=question(backend="dsl"))
            await manager.delete(session)
        assert not session.root.exists()
        assert not manager.preparations["github_schema"].done()
    finally:
        release.set()
    remaining = await finished(app, second)
    assert remaining.replay(0, 10000)[-1]["data"]["status"] == "completed"
    assert len(builds) == 1


async def test_stop_and_delete_release_storage(harness):
    app, client, _, _ = harness
    await login(client)
    session_id = await create(client)
    await client.post(f"/api/sessions/{session_id}/questions", json=question("slow tool"))
    session = app.state.manager.sessions[session_id]
    async with asyncio.timeout(3):
        while not any(e["type"] == "tool/start" for e in session.replay(0, 10000)):
            session.changed.clear()
            await session.changed.wait()
    assert (await client.post(f"/api/sessions/{session_id}/stop", json={})).status_code == 200
    assert session.replay(0, 10000)[-1]["data"]["status"] == "cancelled"
    assert not session.running
    await client.post(f"/api/sessions/{session_id}/questions", json=question("continue", request_id="request-0002"))
    await finished(app, session_id)
    assert session.replay(0, 10000)[-1]["data"]["status"] == "completed"
    root = session.root
    response = await client.request("DELETE", f"/api/sessions/{session_id}", json={})
    assert response.status_code == 200
    assert not root.exists() and not session.credentials and session.agent is None
    assert (await client.get(f"/api/sessions/{session_id}")).status_code == 404


async def test_cancel_before_runner_starts(harness):
    app, client, _, _ = harness
    await login(client)
    session_id = await create(client)
    manager = app.state.manager
    session = manager.sessions[session_id]
    manager.submit(session, Question.model_validate(question("slow")))
    await manager.stop(session)
    assert session.replay(0, 10000)[-1]["type"] == "query/end"


async def test_expiry_preserves_running_and_logout_cleans(harness):
    app, client, _, _ = harness
    await login(client)
    first, second = await create(client), await create(client)
    manager = app.state.manager
    await client.post(f"/api/sessions/{second}/questions", json=question("slow"))
    for session in manager.sessions.values():
        session.touched = time.monotonic() - 3601
    expired_root = manager.sessions[first].root
    live_root = manager.sessions[second].root
    await manager.expire()
    assert first not in manager.sessions and not expired_root.exists()
    assert second in manager.sessions and manager.sessions[second].running
    assert (await client.post("/api/auth/logout", json={})).status_code == 200
    assert not manager.sessions and not live_root.exists()
    assert COOKIE not in client.cookies


async def test_failure_timeout_and_history_rename(harness):
    app, client, _, settings = harness
    await login(client)
    session_id = await create(client)
    url = f"/api/sessions/{session_id}"
    await client.post(url + "/questions", json=question("failure"))
    session = await finished(app, session_id)
    assert session.replay(0, 10000)[-1]["data"]["status"] == "failed"
    response = await client.patch(url, json={"title": "研究记录"})
    assert response.json()["title"] == "研究记录"
    settings.run_seconds = 0.03
    await client.post(url + "/questions", json=question("slow", request_id="request-0002"))
    await finished(app, session_id)
    assert "超时" in session.replay(0, 10000)[-1]["data"]["error"]


async def test_limits_and_credentials_needed(harness):
    app, client, _, settings = harness
    await login(client)
    session_id = await create(client)
    body = question()
    body["credentials"] = {}
    assert (await client.post(f"/api/sessions/{session_id}/questions", json=body)).status_code == 422
    body = question(web_search_backend="brave")
    assert (await client.post(f"/api/sessions/{session_id}/questions", json=body)).status_code == 422
    settings.max_sessions = 1
    assert (await client.post("/api/sessions", json={})).status_code == 429
    settings.event_bytes = 1200
    await client.post(f"/api/sessions/{session_id}/questions", json=question())
    session = await finished(app, session_id)
    assert session.view()["readonly"]
    assert session.replay(0, 10000)[-1]["data"]["status"] == "failed"


async def test_memory_credentials_reused_without_browser_resending(harness):
    app, client, _, _ = harness
    await login(client)
    session_id = await create(client)
    url = f"/api/sessions/{session_id}/questions"
    await client.post(url, json=question())
    await finished(app, session_id)
    followup = question("followup", request_id="request-0002")
    followup["credentials"] = {}
    assert (await client.post(url, json=followup)).status_code == 202
    session = await finished(app, session_id)
    assert session.replay(0, 10000)[-1]["data"]["status"] == "completed"
    followup["request_id"] = "request-0003"
    followup["settings"]["base_url"] = "https://other.example/v1"
    assert (await client.post(url, json=followup)).status_code == 422


async def test_event_limit_at_submission_keeps_readonly_terminal_state(harness):
    app, client, factory, settings = harness
    await login(client)
    session_id = await create(client)
    settings.event_bytes = 1
    response = await client.post(f"/api/sessions/{session_id}/questions", json=question())
    assert response.status_code == 409
    session = app.state.manager.sessions[session_id]
    assert session.view()["readonly"] and not session.running and not factory.calls
    assert session.replay(0)[-1]["data"]["status"] == "failed"
