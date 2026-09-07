"""Check explicit experimental model selection without a production model environment variable."""
# ruff: noqa: S101 - Research tests use pytest assertions.

import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from playground.graphub.v1 import pilot

from .agent import MinimalAgent, Tool
from .code import READ_TOOLS
from .test_agent import packet, scripted


@pytest.mark.parametrize("legacy_model", [None, "stale-model"])
def test_cli_defaults_ignore_the_legacy_model_environment(monkeypatch, legacy_model):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    if legacy_model:
        monkeypatch.setenv("LLM_MODEL", legacy_model)
    monkeypatch.setattr(sys, "argv", ["pilot", "--cases", "cases.json", "--number", "1", "--github", "facts.db",
                                     "--git", "repo.git", "--out", "run"])
    captured = []

    async def run(args):
        captured.append(args)
        return True

    monkeypatch.setattr(pilot, "run", run)
    with pytest.raises(SystemExit) as stopped:
        pilot.main()
    assert stopped.value.code == 0
    assert captured[0].model == "glm-5.3-flash"
    assert captured[0].max_tokens_field == "max_tokens"
    assert captured[0].reasoning_effort is None
    assert captured[0].group == "A"


@pytest.mark.asyncio
async def test_runner_reaches_source_verification_without_a_model_environment(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret")
    monkeypatch.setattr(pilot, "load_dotenv", lambda: None)

    def verify(*args):
        raise ValueError("source verification reached")

    monkeypatch.setattr(pilot, "verified_case", verify)
    args = SimpleNamespace(model="glm-5.3-flash", max_tokens_field="max_tokens", reasoning_effort=None,
                           cases=None, number=1, github=None)
    with pytest.raises(ValueError, match="source verification reached"):
        await pilot.run(args)


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["A", "B", "C"])
async def test_each_group_keeps_the_common_tools_prompt_and_model_contract(monkeypatch, tmp_path, group):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret")
    monkeypatch.setattr(pilot, "load_dotenv", lambda: None)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"scope": {"archive": "graphs.kga"}}))
    case = {"repository": "example/widgets", "ref": "v1", "commit": "a" * 40, "log": {"text": "original log"}}
    monkeypatch.setattr(pilot, "verified_case", lambda *args: case)
    mounts = []

    async def enter(subject):
        if isinstance(subject, pilot.Workspace):
            mounts.append(dict(subject.mounts))
        return subject

    async def leave(subject, *args):
        return None

    async def process(*args):
        return {"exit_code": 0, "stdout": {"text": "fixture-image"}}

    async def checked(workspace, command):
        return case["commit"] if command.endswith("rev-parse HEAD") else ""

    for cls in (pilot.Workspace, pilot.Web):
        monkeypatch.setattr(cls, "__aenter__", enter)
        monkeypatch.setattr(cls, "__aexit__", leave)
    monkeypatch.setattr(pilot, "process", process)
    monkeypatch.setattr(pilot, "checked", checked)

    @asynccontextmanager
    async def extra(names):
        yield SimpleNamespace(tools=lambda: [Tool(name, name, {"type": "object"}, process) for name in names],
                              records=[], directory=tmp_path / "snapshot-sources", coverage=[], change_meta={})

    monkeypatch.setattr(pilot, "Code", lambda *args: extra(sorted(READ_TOOLS)))
    github_names = {"GitHubSearch", "GitHubChanges", "GitHubRead"}
    monkeypatch.setattr(pilot, "GitHub", lambda *args: extra(sorted(github_names)))
    transport, requests = scripted(packet("done"))
    monkeypatch.setattr(pilot, "MinimalAgent", lambda config, tools, **kwargs:
                        MinimalAgent(config, tools, transport=transport, **kwargs))
    args = SimpleNamespace(model="glm-5.3-flash", max_tokens_field="max_tokens", reasoning_effort=None,
                           cases=cases, number=1, github=tmp_path / "source", git=tmp_path / "git", group=group,
                           out=tmp_path / "run", image="fixture", steps=64, tool_calls=192, output_tokens=8192,
                           tool_chars=40000, seconds=1800, text_index=tmp_path / "text",
                           change_index=tmp_path / "changes", archive=None)
    assert await pilot.run(args)
    request, = requests
    expected = {"Bash", "Grep", "Glob", "Read", "Write", "Edit", "WebSearch", "WebFetch"}
    if group in {"B", "C"}:
        expected |= READ_TOOLS
    if group == "C":
        expected |= github_names
    assert {tool["function"]["name"] for tool in request["tools"]} == expected
    assert request["model"] == "glm-5.3-flash" and request["max_tokens"] == 8192
    assert request["messages"][0]["content"] == pilot.SYSTEM
    assert request["messages"][1]["content"] == (
        f"仓库：example/widgets，代码版本：v1（{'a' * 40}）。\n帮我分析并解决下面日志中的问题。\n\noriginal log"
    )
    assert len(mounts[0]) == (1 if group == "A" else 2)
