"""Opt-in real-backend tests for every Agent adapter and its persisted event flow.

Run ``GH_PULLER_REAL_TESTS=1 uv run pytest -q -m real tests/real`` for checks that may
spawn local agents or consume provider tokens. Monitor data stays under pytest's
temporary path.
"""
import asyncio
import json
import os
import shutil

import httpx
import pytest
import pytest_asyncio

from gh_puller import agent
from gh_puller.agent.events import fold_state

DSH_MODEL = os.environ.get("DSH_MODEL", "deepseek-v4-flash")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
MODEL_CODEX = "gpt-5.6-luna"

# The OpenAI-compatible route defaults to the low-cost DeepSeek endpoint.
LLM_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1")

pytestmark = [
    pytest.mark.real,
    pytest.mark.skipif(
        os.environ.get("GH_PULLER_REAL_TESTS") != "1",
        reason="real backends require GH_PULLER_REAL_TESTS=1 and may consume tokens",
    ),
]


@pytest_asyncio.fixture(autouse=True)
async def _monitor_cleanup():
    """Restore monitor outputs after each isolated real-backend test."""
    yield
    agent.configure(ws_urls=[], otel_urls=[])
    await asyncio.sleep(0.01)


async def _collect(stream) -> list[str]:
    """Collect visible deltas and yield once for asynchronous sink draining."""
    parts = [p async for p in stream]
    await asyncio.sleep(0.05)
    return parts


def _text_of(content) -> str:
    """Flatten text carried by canonical Items and content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return _text_of(
            content.get("text") or content.get("output") or content.get("content")
            or content.get("items") or "",
        )
    if isinstance(content, list):
        return "".join(_text_of(item) for item in content)
    return ""


async def _read_single_session(tmp_path) -> list[dict]:
    """Wait for and read the only complete FileSink session in the test directory."""
    sess = tmp_path
    for _ in range(40):
        files = sorted(sess.glob("*.jsonl"))
        if len(files) == 1:
            lines = files[0].read_text(encoding="utf-8").splitlines()
            if lines and '"type": "session/end"' in lines[-1]:
                return [json.loads(line) for line in lines]
        await asyncio.sleep(0.05)
    files = sorted(sess.glob("*.jsonl"))
    assert len(files) == 1, f"expected one JSONL file, got {[p.name for p in files]}"
    return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]


def _assert_flow(events: list[dict], agent_name: str) -> None:
    """Assert compact persistence, correlation, and replayable context."""
    assert events, "expected persisted events"
    assert events[0]["type"] == "session/start", events[0]
    types = [e["type"] for e in events]
    assert not {"model/delta/text", "model/delta/reasoning", "model/delta/tool-call"} & set(types)
    agent_event = next(event for event in events if event["type"] == "agent/set")
    assert agent_event["data"]["agent"] == agent_name
    if "api_key" in agent_event["data"]["config"]:
        assert agent_event["data"]["config"]["api_key"] == "<redacted>"
    request_ids = [
        event["data"]["requestId"] for event in events if event["type"] == "model/request"
    ]
    assert request_ids
    responses = [event for event in events if event["type"] == "model/response"]
    assert [event["data"]["requestId"] for event in responses] == request_ids
    assert all(isinstance(event["data"]["output"], list) for event in responses)
    context = fold_state(events)["context"]
    user = next(item for item in context
                if item["type"] == "message" and item.get("role") == "user")
    assert "你好" in _text_of(user), user
    assistants = [item for item in context
                  if item["type"] == "message" and item.get("role") == "assistant"]
    assert assistants
    end = events[-1]
    assert end["type"] == "session/end", end
    assert end["data"]["outcome"] == "completed", end
    assert any(_text_of(item).strip() for item in assistants)


@pytest.mark.asyncio
async def test_cc_stream_real(tmp_path):
    """Run cc through the local Claude CLI and its own credentials."""
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    config = {
        "cwd": str(tmp_path),
        "allowed_tools": [],
        "max_turns": 1,
        "include_partial_messages": True,
    }
    if model := os.environ.get("CC_MODEL"):
        config["model"] = model
    subject = agent.ClaudeCode(config)
    async with subject.session(session_name="real:cc", run_id="r-cc"):
        first = await _collect(subject.stream("你好，请记住代号“蓝鲸”。"))
        second = await _collect(subject.stream("刚才让你记住的代号是什么？"))
    print("cc responses:", "".join(first), "|", "".join(second))
    assert "".join(first) != "", "cc should emit text deltas"
    assert "蓝鲸" in "".join(second), "cc should retain the native conversation"
    _assert_flow(await _read_single_session(tmp_path), "cc")


@pytest.mark.asyncio
async def test_llm_stream_real(tmp_path):
    """Run the OpenAI adapter against an OpenAI-compatible endpoint."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        pytest.skip("OPENAI_API_KEY is not configured")

    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    config = {
        "model": LLM_MODEL,
        "base_url": LLM_BASE_URL,
        "api_key": key,
        "system_prompt": "简短回答用户。",
        "parameters": {"max_tokens": 256, "temperature": 0},
    }
    timeout = httpx.Timeout(connect=10.0, read=180.0, write=10.0, pool=10.0)
    subject = agent.OpenAI(config)
    async with subject.session(session_name="real:llm", run_id="r-llm"):
        first = await _collect(subject.stream("你好，请记住代号“蓝鲸”。", timeout=timeout))
        second = await _collect(subject.stream("刚才让你记住的代号是什么？", timeout=timeout))
    assert "".join(first) != "", "OpenAI-compatible backend should emit text deltas"
    assert "蓝鲸" in "".join(second), "OpenAI-compatible history should reach the backend"
    print("llm responses:", "".join(first), "|", "".join(second))
    _assert_flow(await _read_single_session(tmp_path), "llm")


@pytest.mark.skip(
    reason="the DSH SDK stdio runtime is unavailable in this development environment",
)
@pytest.mark.asyncio
async def test_dsh_stream_real(tmp_path):
    """Run DSH through its SDK-managed runtime and credentials."""
    work = tmp_path / "work"
    work.mkdir()
    sessions = tmp_path / "dsh-sessions"
    sessions.mkdir()
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    config = {
        "provider": "deepseek-official",
        "model": DSH_MODEL,
        "cwd": str(work),
        "session_root": str(sessions),
        "max_tokens": 1024,
    }
    subject = agent.Dsh(config)
    async with subject.session(session_name="real:dsh", run_id="r-dsh"):
        parts = await _collect(subject.stream("你好"))
    print("dsh response:", "".join(parts))
    assert "".join(parts) != "", "DSH should emit text deltas"
    _assert_flow(await _read_single_session(tmp_path), "dsh")


@pytest.mark.asyncio
async def test_codex_stream_real(tmp_path):
    """Run Codex through an isolated SDK home linked to local authentication."""
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    config = {
        "codex_home": str(tmp_path / "codex-home"),
        "sandbox": "full_access",
        "approval_mode": "auto_review",
        "model": MODEL_CODEX,
        "cwd": str(tmp_path),
        "timeout_seconds": 300,
    }
    subject = agent.Codex(config)
    async with subject.session(session_name="real:codex", run_id="r-codex"):
        first = await _collect(subject.stream("你好，请记住代号“蓝鲸”。"))
        second = await _collect(subject.stream("刚才让你记住的代号是什么？"))
    print("codex responses:", "".join(first), "|", "".join(second))
    assert "".join(first) != "", "Codex should emit text deltas"
    assert "蓝鲸" in "".join(second), "Codex should retain the native thread"
    _assert_flow(await _read_single_session(tmp_path), "codex")


@pytest.mark.asyncio
async def test_opencode_stream_real(tmp_path):
    """Run OpenCode through its CLI-managed routing and credentials."""
    binpath = shutil.which("opencode")
    if not binpath:
        pytest.skip("opencode executable is unavailable")
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    config = {
        "opencode_bin": binpath,
        "cwd": str(tmp_path),
        "auto": True,
        "timeout_seconds": 300,
    }
    subject = agent.OpenCode(config)
    async with subject.session(session_name="real:opencode", run_id="r-opencode"):
        first = await _collect(subject.stream("你好，请记住代号“蓝鲸”。"))
        second = await _collect(subject.stream("刚才让你记住的代号是什么？"))
    print("opencode responses:", "".join(first), "|", "".join(second))
    assert "".join(first) != "", "OpenCode should emit text deltas"
    assert "蓝鲸" in "".join(second), "OpenCode should retain the native session"
    _assert_flow(await _read_single_session(tmp_path), "opencode")
