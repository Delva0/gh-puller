"""Check common tool semantics, process cleanup and lossless web request forwarding."""
# ruff: noqa: S101 - Research tests use pytest assertions.

import asyncio
import json
import os
from pathlib import Path

import pytest

from .web import Web
from .workspace import Workspace, file_operation, process


def test_file_tools_preserve_content_and_require_unambiguous_edits(tmp_path, capsys):
    path = tmp_path / "nested" / "a.py"
    file_operation("write", {"path": str(path), "content": "α\nbeta\nbeta\n"})
    assert path.read_text() == "α\nbeta\nbeta\n"
    capsys.readouterr()
    file_operation("read", {"path": str(path), "offset": 2, "limit": 1})
    assert capsys.readouterr().out == "2: beta\n"
    with pytest.raises(ValueError, match="unambiguous"):
        file_operation("edit", {"path": str(path), "old_string": "beta", "new_string": "γ"})
    assert path.read_text() == "α\nbeta\nbeta\n"
    file_operation("edit", {"path": str(path), "old_string": "beta", "new_string": "γ", "replace_all": True})
    assert path.read_text() == "α\nγ\nγ\n"


@pytest.mark.asyncio
async def test_process_drains_large_outputs_but_bounds_captured_memory():
    result = await process(["bash", "-c", "head -c 50000 /dev/zero; printf warning >&2; exit 7"], capture_bytes=16)
    assert result["exit_code"] == 7
    assert result["stdout"] == {"text": "\0" * 16, "bytes": 50000, "truncated": True}
    assert result["stderr"] == {"text": "warning", "bytes": 7, "truncated": False}


@pytest.mark.asyncio
async def test_all_common_tools_use_the_same_unfiltered_workspace(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path, image="test")
    calls = []

    async def execute(argv, **kwargs):
        calls.append((argv, kwargs))
        return "result"

    monkeypatch.setattr(workspace, "execute", execute)
    tools = {tool.name: tool for tool in workspace.tools()}
    assert set(tools) == {"Bash", "Grep", "Glob", "Read", "Write", "Edit"}
    command = "git log -5; gh issue view 1; uv run analysis.py | jq ."
    assert await tools["Bash"].invoke({"command": command, "cwd": "/work/repo", "timeout_seconds": 600}) == "result"
    assert calls[-1] == (["bash", "-c", command], {"cwd": "/work/repo", "seconds": 600})
    await tools["Grep"].invoke({"pattern": "--not-a-flag", "path": "/work", "glob": "*.py"})
    assert calls[-1][0][-3:] == ["--", "--not-a-flag", "/work"]
    await tools["Glob"].invoke({"pattern": "**/*.py"})
    assert calls[-1][0] == ["rg", "--files", "--hidden", "--no-ignore", "--glob", "**/*.py", "--", "/work"]
    await tools["Write"].invoke({"path": "/work/x", "content": "$(arbitrary text)"})
    assert calls[-1][0][:5] == ["uv", "run", "--offline", "--no-project", "python"]
    assert json.loads(calls[-1][1]["data"])["content"] == "$(arbitrary text)"


@pytest.mark.asyncio
async def test_workspace_does_not_disable_network_or_mount_host_home(tmp_path, monkeypatch):
    from . import workspace as module

    calls = []

    async def fake_process(argv, **kwargs):
        calls.append((argv, kwargs))
        return {"exit_code": 0, "stdout": {"text": ""}, "stderr": {"text": ""}}

    monkeypatch.setattr(module, "process", fake_process)
    async with Workspace(tmp_path, image="test", mounts={"/facts/git": tmp_path / "facts"}):
        pass
    argv = calls[0][0]
    assert "--network" not in argv and "--privileged" not in argv
    assert f"type=bind,src={tmp_path / 'facts'},dst=/facts/git,readonly" in argv
    assert not any(str(Path.home()) == item or "/var/run/docker.sock" in item for item in argv)
    assert calls[-1][0][:3] == ["docker", "rm", "--force"]


@pytest.mark.asyncio
async def test_shell_cancellation_closes_the_owned_container(tmp_path, monkeypatch):
    from . import workspace as module

    calls, entered = [], asyncio.Event()

    async def fake_process(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "exec":
            entered.set()
            await asyncio.Event().wait()
        return {"exit_code": 0, "stdout": {"text": ""}, "stderr": {"text": ""}}

    monkeypatch.setattr(module, "process", fake_process)
    async with Workspace(tmp_path, image="test") as workspace:
        task = asyncio.create_task(workspace.execute(["bash", "-c", "sleep 1000"]))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len([argv for argv in calls if argv[1] == "rm"]) == 1


@pytest.mark.asyncio
async def test_web_queries_and_pages_are_agent_selected_and_unmodified(monkeypatch):
    web, calls = Web(), []

    async def exchange(arguments):
        calls.append(arguments)
        return "original backend evidence"

    monkeypatch.setattr(web, "exchange", exchange)
    tools = {tool.name: tool for tool in web.tools()}
    assert set(tools) == {"WebSearch", "WebFetch"}
    query = '"some error" unrelated-package'
    assert await tools["WebSearch"].invoke({"query": query}) == "original backend evidence"
    assert calls[-1] == {"search_query": [{"q": query}], "response_length": "long"}
    await tools["WebFetch"].invoke({"url": "https://example.com/source", "line": 30})
    assert calls[-1] == {"open": [{"ref_id": "https://example.com/source", "lineno": 30}], "response_length": "long"}


@pytest.mark.asyncio
async def test_web_transport_retains_full_result_and_rejects_mismatched_reply(capsys):
    web = Web()
    content = "evidence:" + "字" * 20000
    web.reader.feed_data((json.dumps({"id": 1, "result": content}) + "\n").encode())
    assert await web.exchange({"open": [{"ref_id": "https://example.com"}]}) == content
    assert json.loads(capsys.readouterr().out)["graphub_web"] == 1
    web.reader.feed_data(b'{"id":3,"result":"wrong"}\n')
    with pytest.raises(ValueError, match="does not match"):
        await web.exchange({})


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("GRAPHUB_TEST_IMAGE"), reason="Set GRAPHUB_TEST_IMAGE to a built local image")
async def test_real_workspace_supports_coding_tools_and_protects_mounted_facts(tmp_path):
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "original.txt").write_text("immutable")
    async with Workspace(tmp_path / "work", image=os.environ["GRAPHUB_TEST_IMAGE"],
                         mounts={"/facts": facts}, environment={"GH_TOKEN": "fixture-secret"}) as workspace:
        tools = {tool.name: tool for tool in workspace.tools()}

        async def invoke(name, arguments):
            return json.loads(await tools[name].invoke(arguments))

        written = await invoke("Write", {"path": "/work/a.py", "content": "value = 1\r\n"})
        assert written["exit_code"] == 0, written
        assert (await invoke("Edit", {"path": "/work/a.py", "old_string": "1", "new_string": "2"}))["exit_code"] == 0
        assert (await invoke("Read", {"path": "/work/a.py"}))["stdout"]["text"] == "1: value = 2\r\n"
        assert "/work/a.py" in (await invoke("Glob", {"pattern": "*.py"}))["stdout"]["text"]
        assert "/work/a.py:1:value = 2" in (await invoke("Grep", {"pattern": "value"}))["stdout"]["text"]
        result = await invoke("Bash", {"command": "uv run --offline --no-project python /work/a.py"})
        assert result["exit_code"] == 0
        result = await invoke("Bash", {"command": "command -v git gh rg jq sqlite3 curl uv"})
        assert result["exit_code"] == 0 and len(result["stdout"]["text"].splitlines()) == 7
        result = await invoke("Bash", {"command": "printf changed > /facts/original.txt"})
        assert result["exit_code"] != 0 and (facts / "original.txt").read_text() == "immutable"
        result = await invoke("Bash", {"command": "printf '%s' \"$GH_TOKEN\""})
        assert result["stdout"]["text"] == "<redacted>"
    assert (tmp_path / "work" / "a.py").read_bytes() == b"value = 2\r\n"
