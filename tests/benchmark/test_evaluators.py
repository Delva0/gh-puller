"""Verify benchmark evaluators without network access or paid tokens.

Minimal subclasses provide the abstract extension points. Fake ClaudeCode and HTTP
backends cover successful coercion, failure degradation, retry nudges, and exact
request payload forwarding.
"""

import asyncio
import contextlib
import json
import types
from typing import ClassVar

import httpx
import pytest
import pytest_asyncio

from gh_puller import agent
from gh_puller.benchmark.evaluators import ClaudeEvaluator, LLMEvaluator


@pytest_asyncio.fixture(autouse=True)
async def _monitor_cleanup():
    """Clear monitoring endpoints after evaluator calls use the real result wrappers."""
    yield
    agent.configure(ws_urls=[], otel_urls=[])
    await asyncio.sleep(0.01)


class _MiniClaude(ClaudeEvaluator):
    def make_options(self, question, ref, answer):
        return types.SimpleNamespace(model="", system_prompt="sys")

    def user_prompt(self, question, ref, answer):
        return f"judge {question}"

    def coerce(self, data):
        return {"dims": data.get("dimensions", {}), "overall": data.get("overall")}


class _MiniLLM(LLMEvaluator):
    def user_prompt(self, question, ref, answer):
        return question

    def coerce(self, data):
        return {"dims": data.get("dimensions", {}), "overall": data.get("overall")}


# ---------------------------------------------------------------------------
# Claude evaluator
# ---------------------------------------------------------------------------

GOOD_VERDICT = '{"dimensions": {"code_essence": 8}, "overall": 7, "reason": "ok"}'


@pytest.mark.asyncio
async def test_claude_judge_success(monkeypatch):
    calls = []

    @contextlib.asynccontextmanager
    async def fake_session(self, *, session_name=None, **kw):
        calls.append((self.config.get("model"), None, session_name))
        yield self

    async def fake_result(self, prompt, **kw):
        calls.append((self.config.get("model"), prompt, ""))
        return GOOD_VERDICT

    monkeypatch.setattr(agent.ClaudeCode, "session", fake_session)
    monkeypatch.setattr(agent.ClaudeCode, "result", fake_result)
    judge = _MiniClaude()
    r = await judge.evaluate("q", "ref", "ans")
    assert r["overall"] == 7
    assert calls == [("", None, "judge:claude"), ("", "judge q", "")]  # Session owns the name; result owns the payload.


@pytest.mark.asyncio
async def test_claude_judge_degrades_on_error_and_bad_json(monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_session(self, **kw):
        yield self

    async def fake_result(self, prompt, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent.ClaudeCode, "session", fake_session)
    monkeypatch.setattr(agent.ClaudeCode, "result", fake_result)
    judge = _MiniClaude()
    r = await judge.evaluate("q", "ref", "ans")
    assert r == {"dimensions": {}, "overall": 0, "reason": "评测失败: RuntimeError: boom"}

    async def fake_result_json(self, prompt, **kw):
        return "not a json"

    monkeypatch.setattr(agent.ClaudeCode, "result", fake_result_json)
    r2 = await judge.evaluate("q", "ref", "ans")
    assert r2["overall"] == 0 and "评测失败" in r2["reason"]


# ---------------------------------------------------------------------------
# LLM evaluator
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeSSERes:
    """Wrap a complete canned response in one SSE delta for the streaming adapter."""

    def __init__(self, resp):
        self._resp = resp

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        # The adapter consumes delta content, while fixtures store complete messages.
        content = self._resp.json()["choices"][0]["message"]["content"]
        yield "data: " + json.dumps({"choices": [{"delta": {"content": content}}]})
        yield "data: [DONE]"


class _FakeHttpClient:
    """Capture request bodies and return canned responses like httpx.AsyncClient."""

    posts: ClassVar[list[tuple[str, dict, dict]]] = []  # Each test replaces this shared request log.
    responses: ClassVar[list[_FakeResponse]] = []

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json, headers, timeout=None):  # noqa: ASYNC109 - Match the httpx contract.
        _FakeHttpClient.posts.append((url, json, headers))
        return _FakeHttpClient.responses.pop(0)

    def stream(self, method, url, json, headers, timeout=None):  # Return an async context manager like httpx.
        type(self).posts.append((url, json, headers))
        resp = type(self).responses.pop(0)

        class _CM:
            async def __aenter__(self):
                return _FakeSSERes(resp)

            async def __aexit__(self, *exc):
                return False

        return _CM()


def _fake_httpx(monkeypatch):
    posts = _FakeHttpClient.posts = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeHttpClient)
    return posts


@pytest.mark.asyncio
async def test_llm_judge_success(monkeypatch):
    from gh_puller.benchmark import evaluators as llm_mod

    monkeypatch.setattr(llm_mod, "LLM_JUDGE_API_KEY", "sk-test")
    posts = _fake_httpx(monkeypatch)
    _FakeHttpClient.responses = [_FakeResponse({"choices": [{"message": {"content": GOOD_VERDICT}}]})]
    judge = _MiniLLM(url="http://j", model="m")
    r = await judge.evaluate("q", "ref", "ans")
    assert r["overall"] == 7
    url, body, headers = posts[0]
    assert url == "http://j/chat/completions"
    # The result wrapper adds only the stream flag to the forwarded payload.
    assert body == {"model": "m", "messages": [{"role": "user", "content": "q"}], "stream": True}
    assert headers["Authorization"].startswith("Bearer")


@pytest.mark.asyncio
async def test_llm_judge_retry_nudge_then_success(monkeypatch):
    """Append the retry nudge after the first parse failure, then succeed."""
    posts = _fake_httpx(monkeypatch)
    _FakeHttpClient.responses = [
        _FakeResponse({"choices": [{"message": {"content": "garbage"}}]}),
        _FakeResponse({"choices": [{"message": {"content": GOOD_VERDICT}}]}),
    ]
    judge = _MiniLLM(url="http://j", model="m")
    r = await judge.evaluate("q", "ref", "ans")
    assert r["overall"] == 7
    assert len(posts) == 2
    assert posts[1][1]["messages"][-1] == {
        "role": "user", "content": f"q\n\n{judge.retry_nudge}",
    }


@pytest.mark.asyncio
async def test_llm_judge_exhausted_degrades(monkeypatch):
    posts = _fake_httpx(monkeypatch)
    _FakeHttpClient.responses = [
        _FakeResponse({"choices": [{"message": {"content": "garbage"}}]}),
        _FakeResponse({"choices": [{"message": {"content": "garbage"}}]}),
    ]
    judge = _MiniLLM(url="http://j", model="m")
    r = await judge.evaluate("q", "ref", "ans")
    assert r["overall"] == 0 and "评测失败" in r["reason"]
    assert len(posts) == 2  # Exhaustion degrades without issuing a third request.
