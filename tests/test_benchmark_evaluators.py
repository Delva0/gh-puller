"""benchmark evaluator 迁移后的本地测试(零网络 / 零 token)。

两个评测器本体是半抽象基类,这里用最小子类挂接扩展点,并把后端调用
(cc_result / httpx)全部置换为假实现:
- claude judge:成功路径(cc_result → coerce)与降级路径(RuntimeError / JSON 解析失败);
- llm judge:HTTP 成功、nudge 重试(第二次追加提示)、耗尽降级;并断言请求体逐字节直连一致。
"""

import asyncio
import types

import httpx
import pytest
import pytest_asyncio

from gh_puller import agent
from gh_puller.benchmark.evaluators import ClaudeEvaluator
from gh_puller.benchmark.evaluators import LLMEvaluator


@pytest_asyncio.fixture(autouse=True)
async def _monitor_cleanup():
    """评测器走真实 llm_complete/cc_result:监控需停用,避免写用户真实 monitor 目录。"""
    yield
    agent.configure(file=False, ws_url="")
    await asyncio.sleep(0.01)


class _MiniClaude(ClaudeEvaluator):
    def make_options(self, question, ref, answer):
        return types.SimpleNamespace(model="", system_prompt="sys")

    def user_prompt(self, question, ref, answer):
        return f"judge {question}"

    def coerce(self, data):
        return {"dims": data.get("dimensions", {}), "overall": data.get("overall")}


class _MiniLLM(LLMEvaluator):
    def make_payload(self, question, ref, answer):
        return {"model": self.model, "messages": [{"role": "user", "content": question}]}

    def coerce(self, data):
        return {"dims": data.get("dimensions", {}), "overall": data.get("overall")}


# ---------------------------------------------------------------------------
# Claude 评测器
# ---------------------------------------------------------------------------

GOOD_VERDICT = '{"dimensions": {"code_essence": 8}, "overall": 7, "reason": "ok"}'


@pytest.mark.asyncio
async def test_claude_judge_success(monkeypatch):
    calls = []

    async def fake_cc_result(options, prompt, *, session=None, session_name=None, meta=None):
        calls.append((options.model, prompt, session_name))
        return GOOD_VERDICT

    from gh_puller.benchmark import evaluators as claude_mod

    monkeypatch.setattr(claude_mod, "cc_result", fake_cc_result)
    judge = _MiniClaude()
    r = await judge.evaluate("q", "ref", "ans")
    assert r["overall"] == 7
    assert calls == [("", "judge q", "judge:claude")]


@pytest.mark.asyncio
async def test_claude_judge_degrades_on_error_and_bad_json(monkeypatch):
    from gh_puller.benchmark import evaluators as claude_mod

    async def fake_cc_result(options, prompt, *, session=None, session_name=None, meta=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(claude_mod, "cc_result", fake_cc_result)
    judge = _MiniClaude()
    r = await judge.evaluate("q", "ref", "ans")
    assert r == {"dimensions": {}, "overall": 0, "reason": "评测失败: RuntimeError: boom"}

    async def fake_cc_result_json(options, prompt, **kw):
        return "not a json"

    monkeypatch.setattr(claude_mod, "cc_result", fake_cc_result_json)
    r2 = await judge.evaluate("q", "ref", "ans")
    assert r2["overall"] == 0 and "评测失败" in r2["reason"]


# ---------------------------------------------------------------------------
# LLM 评测器
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeHttpClient:
    """形似 httpx.AsyncClient 的假客户端:捕获请求体,按 index 返回预设响应。"""

    posts: list[tuple[str, dict, dict]] = []
    responses: list[_FakeResponse] = []

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json, headers):
        _FakeHttpClient.posts.append((url, json, headers))
        return _FakeHttpClient.responses.pop(0)


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
    assert body == {"model": "m", "messages": [{"role": "user", "content": "q"}]}  # body 与 payload 逐字节一致
    assert headers["Authorization"].startswith("Bearer")


@pytest.mark.asyncio
async def test_llm_judge_retry_nudge_then_success(monkeypatch):
    """首次解析失败 → 第二次调用追加 nudge 提示后成功。"""
    posts = _fake_httpx(monkeypatch)
    _FakeHttpClient.responses = [
        _FakeResponse({"choices": [{"message": {"content": "garbage"}}]}),
        _FakeResponse({"choices": [{"message": {"content": GOOD_VERDICT}}]}),
    ]
    judge = _MiniLLM(url="http://j", model="m")
    r = await judge.evaluate("q", "ref", "ans")
    assert r["overall"] == 7
    assert len(posts) == 2
    assert posts[1][1]["messages"][-1] == {"role": "user", "content": judge.retry_nudge}


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
    assert len(posts) == 2  # 耗尽后降级,不再发第三次请求
