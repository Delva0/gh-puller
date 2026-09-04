"""Test DeepWiki chat history, dispatch, and failure degradation."""

import contextlib

import pytest

from gh_puller import deepwiki
from gh_puller.agent import AGENTS, RequestFailedError
from tests.deepwiki._support import _chat, _gen_kwargs, _repo_of


@pytest.mark.asyncio
async def test_chat_stream_history_trim_no_context(monkeypatch):
    """Pass only the current query after trimming history, without session context.

    The adapter publishes monitoring events and ties the run ID to the session group.
    """
    captured = {}

    @contextlib.asynccontextmanager
    async def fake_session(self, **kw):  # Avoid an SDK client; metadata belongs to the session.
        captured.update(session=kw.get("session_name"), run_id=kw.get("run_id"))
        yield self

    async def fake_stream(self, prompt):
        captured.update(prompt=prompt)
        yield "hi"

    monkeypatch.setattr(AGENTS["cc"], "session", fake_session)  # Patch the registered agent adapter.
    monkeypatch.setattr(AGENTS["cc"], "stream", fake_stream)
    monkeypatch.setattr(deepwiki.envs, "CHAT_TOKEN_LIMIT_ESTIMATE", 0)  # Force history trimming.
    request = {
        "repo_url": "/tmp/deepwiki-chat-test", "type": "local", "language": "en",
        "target": {}, "token": None,
        "messages": [{"role": "user", "content": "q1"},
                     {"role": "assistant", "content": "a1"},
                     {"role": "user", "content": "q2"}],
    }
    got = await _chat(request)
    assert "".join(got) == "hi"
    assert captured["run_id"] == captured["session"]  # Both names use the chat repository key.
    assert captured["run_id"].startswith("chat:")
    assert "context" not in captured
    assert captured["prompt"] == "q2"
    assert "Previous conversation:" not in captured["prompt"]  # History was trimmed.
    assert "<conversation_history>" not in captured["prompt"]

@pytest.mark.asyncio
async def test_agent_chat_natural_history(monkeypatch):
    """Render agent chat history naturally without synthetic XML tags."""
    captured = {}

    @contextlib.asynccontextmanager
    async def fake_session(self, *, session_name=None, run_id=None, **kw):
        captured.update(system=self.config["system_prompt"], session=session_name, run_id=run_id)
        yield self

    async def fake_stream(self, prompt):
        captured.update(prompt=prompt)
        yield "hi"

    monkeypatch.setattr(AGENTS["cc"], "session", fake_session)  # Patch the registered adapter instance.
    monkeypatch.setattr(AGENTS["cc"], "stream", fake_stream)
    request = {
        "repo_url": "/tmp/gh-puller-chat-natural", "type": "local", "language": "en",
        "target": {}, "token": None,
        "messages": [{"role": "user", "content": "q1"},
                     {"role": "assistant", "content": "a1"},
                     {"role": "user", "content": "q2"}],
    }
    got = await _chat(request)
    assert "".join(got) == "hi"
    assert captured["session"] == captured["run_id"]
    p = captured["prompt"]
    assert "Previous conversation:" in p and "User: q1" in p and "Assistant: a1" in p
    assert "<turn>" not in p and "<conversation_history>" not in p and "<query>" not in p
    assert p.endswith("\n\nq2")

@pytest.mark.asyncio
async def test_agent_chat_deep_one_shot(monkeypatch):
    """Use the one-shot deep-research system prompt regardless of iteration count.

    A continuation request falls back to the preceding user query.
    """
    captured = {}

    @contextlib.asynccontextmanager
    async def fake_session(self, **kw):  # Avoid constructing an SDK client.
        yield self

    async def fake_stream(self, prompt):
        captured["system"] = self.config["system_prompt"]
        captured["prompt"] = prompt
        yield "hi"

    monkeypatch.setattr(AGENTS["cc"], "session", fake_session)  # Patch the registered adapter instance.
    monkeypatch.setattr(AGENTS["cc"], "stream", fake_stream)
    request = {
        "repo_url": "/tmp/gh-puller-chat-natural", "type": "local", "language": "en",
        "target": {}, "token": None,
        "messages": [{"role": "user", "content": "what is this repo about?"},
                     {"role": "assistant", "content": "half"},
                     {"role": "user", "content": "Continue the research",
                      "mode": "deep_research"}],
    }
    _ = await _chat(request)
    assert "single-run Deep Research" in captured["system"]
    assert "## Final Conclusion" in captured["system"]
    assert "first iteration of a multi-turn" not in captured["system"]
    assert captured["prompt"].endswith("\n\nwhat is this repo about?")

@pytest.mark.asyncio
async def test_chat_and_codemap_single_pipeline(monkeypatch):
    """Route chat and codemap through one generator pipeline for CC and LLM."""
    calls = []

    async def fake_chat(**kwargs):
        calls.append("chat")
        yield "c"

    async def fake_codemap(**kwargs):
        calls.append("codemap")
        yield "c"

    monkeypatch.setattr(deepwiki.chat, "_chat", fake_chat)
    monkeypatch.setattr(deepwiki.codemap, "_codemap", fake_codemap)
    chat_req = {
        "repo_url": "/tmp/x", "type": "local", "language": "en",
        "target": {}, "token": None,
        "messages": [{"role": "user", "content": "hi"}],
    }
    codemap_req = {
        "repo_url": "/tmp/x", "type": "local", "language": "en",
        "target": {}, "token": None, "question": "q",
    }
    for gid in ("cc", "llm"):  # Adapter contracts handle failures without another dispatch path.
        assert [c async for c in deepwiki.chat_stream(
            **_gen_kwargs({"generator": gid}), repo=_repo_of(chat_req), messages=chat_req["messages"],
            language="en",
        )] == ["c"]
        assert [ev async for ev in deepwiki.generate_codemap(
            **_gen_kwargs({"generator": gid}), repo=_repo_of(codemap_req),
            question=codemap_req["question"], language="en",
        )] == ["c"]
    assert calls == ["chat", "codemap", "chat", "codemap"]

@pytest.mark.asyncio
async def test_agent_chat_wraps_request_failure_in_degrade(monkeypatch):
    """Wrap request failures before returning the exact client-visible fallback."""
    @contextlib.asynccontextmanager
    async def fake_session(self, **kw):  # Avoid constructing an SDK client.
        yield self

    async def boom_stream(self, prompt, **kw):
        raise RequestFailedError("sdk exploded")
        yield  # Keep the stub an async generator.

    monkeypatch.setattr(AGENTS["cc"], "session", fake_session)  # Patch the registered adapter instance.
    monkeypatch.setattr(AGENTS["cc"], "stream", boom_stream)
    request = {
        "repo_url": "/tmp/gh-puller-chat-wrap", "type": "local", "language": "en",
        "target": {}, "token": None,
        "messages": [{"role": "user", "content": "hi"}],
    }
    got = "".join(await _chat(request))
    # Punctuation is part of the client-visible wire contract.
    assert "(抱歉,本次请求处理失败: generator 执行失败: sdk exploded)" in got
