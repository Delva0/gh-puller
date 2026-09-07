"""Check exact snapshot projection and native query routing on repository-independent graphs."""
# ruff: noqa: S101 - Research tests use pytest assertions.

import asyncio
import json
import os
from hashlib import sha256

import pytest
from jsonschema import Draft202012Validator

from gh_puller.codebase import resolve_cbm_binary
from gh_puller.codebase.archive import Archive, ArchiveWriter
from gh_puller.codebase.git_tree import materialize_full
from gh_puller.codebase.store import GraphRows
from playground.graphub import test_change_git
from playground.graphub.stack_search import restore
from tests.codebase.test_archive import commit_rows

from .code import READ_TOOLS, Code, frozen_archive, verify_source
from .workspace import Workspace

repository = test_change_git.repository


@pytest.fixture
def snapshots(repository, tmp_path):
    identifiers, rows = [], []
    for index in range(3):
        content = f"def code():\n    return {index}\n\ndef caller():\n    return code()\n"
        identifiers.append(test_change_git.commit(repository, {b"m.py": (b"100644", content.encode())},
                                                  tuple(identifiers[-1:])))
        nodes = {name: {"label": "Function", "name": name.split(".")[-1], "file_path": "m.py",
                        "start_line": start, "end_line": start + 1, "properties": {"callee": "code"}}
                 for name, start in (("m.code", 1), ("m.caller", 4))}
        edges = {("m.caller", "m.code", "CALLS", ""): {"properties": {"callee": "code", "line": 5}},
                 ("m.code", "unavailable", "CALLS", ""): {"properties": {}}}
        rows.append(GraphRows(nodes, edges))
    path = tmp_path / "graphs.kga"
    writer = ArchiveWriter(path)
    for index, (identifier, graph) in enumerate(zip(identifiers, rows, strict=True)):
        commit_rows(writer, identifier, identifiers[index - 1:index] if index else [], graph)
    writer.finalize()
    archive = Archive(path)
    identities = [(identifier, archive.manifest(identifier)["graph_digest"], archive.manifest(identifier)["parents"])
                  for identifier in sorted(identifiers)]
    scope = {"graph_count": len(identifiers),
             "graph_identity_digest": sha256(json.dumps(identities).encode()).hexdigest(),
             "cbm_sha256": resolve_cbm_binary().sha256}
    return path, scope, identifiers, rows


def test_projection_detects_generic_project_name_collisions(snapshots, tmp_path):
    _, _, identifiers, rows = snapshots
    with pytest.raises(ValueError, match="normalized graph rows"):
        restore(rows[0], tmp_path, "code")
    result = restore(rows[0], tmp_path, "snapshot_" + identifiers[0])
    assert result["retained_round_trip"] is True and result["source_dangling_edges"] == 1


def test_frozen_prefix_rejects_missing_or_changed_identity(snapshots):
    path, scope, identifiers, _ = snapshots
    assert frozen_archive(path, scope)[1] == frozenset(identifiers)
    with pytest.raises(ValueError, match="unavailable"):
        frozen_archive(path, scope | {"graph_count": 4})
    with pytest.raises(ValueError, match="frozen identity"):
        frozen_archive(path, scope | {"graph_identity_digest": "wrong"})


def structured(response):
    native = response["native_result"]
    assert not native.get("isError"), native
    return native.get("structuredContent") or json.loads(native["content"][0]["text"])


def test_export_attribute_substitution_cannot_masquerade_as_original_source(repository, tmp_path):
    commit = test_change_git.commit(repository, {b".gitattributes": (b"100644", b"m.py export-subst\n"),
                                                b"m.py": (b"100644", b"REVISION = '$Format:%H$'\n")})
    tree = tmp_path / "source"
    materialize_full(repository, commit, tree)
    with pytest.raises(ValueError, match="differs from Git blob"):
        verify_source(repository, commit, tree)


@pytest.mark.asyncio
async def test_native_tools_query_matching_source_and_evict_only_owned_snapshots(snapshots, repository, tmp_path):
    path, scope, identifiers, _ = snapshots
    before = sha256(path.read_bytes()).hexdigest()
    async with Code(path, repository, scope, tmp_path / "projections", identifiers[0], capacity=1) as code:
        tools = {tool.name: tool for tool in code.tools()}
        assert set(tools) == READ_TOOLS
        for tool in tools.values():
            Draft202012Validator.check_schema(tool.parameters)
            assert "project" not in tool.parameters["properties"]
        for index, identifier in enumerate(identifiers):
            args = {"qualified_name": "m.code", "revision": identifier}
            response = json.loads(await tools["get_code_snippet"].invoke(args))
            snippet = structured(response)
            assert f"return {index}" in json.dumps(snippet)
            assert response["snapshot"]["commit"] == identifier
            assert response["snapshot"]["dangling_edges_omitted"] == 1
            assert len(code.snapshots) == 1
        assert len(code.records) == 3
        current = code.snapshots[identifiers[-1]]
        await tools["get_graph_schema"].invoke({"revision": identifiers[-1]})
        assert len(code.records) == 3
        with pytest.raises(ValueError, match="static analysis"):
            await code._work(code.query, "delete_project", {})
        unknown = test_change_git.commit(repository, {})
        with pytest.raises(ValueError, match="No graph"):
            await tools["get_graph_schema"].invoke({"revision": unknown})
    assert current.engine.process.poll() is not None
    assert not current.cache.exists() and not current.runtime.exists()
    assert not list((tmp_path / "projections").iterdir())
    assert sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.asyncio
async def test_cancelled_native_worker_is_finished_before_resources_close(snapshots, repository, tmp_path):
    path, scope, identifiers, _ = snapshots
    async with Code(path, repository, scope, tmp_path / "cancel", identifiers[0]) as code:
        entered, release = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()

        def work():
            loop.call_soon_threadsafe(entered.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)

        task = asyncio.create_task(code._work(work))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert code.closed and not code.snapshots


@pytest.mark.asyncio
async def test_native_source_paths_are_readable_in_the_agent_workspace(snapshots, repository, tmp_path):
    image = os.environ.get("GRAPHUB_TEST_IMAGE")
    if not image:
        pytest.skip("GRAPHUB_TEST_IMAGE enables the native-CBM/Docker boundary check")
    path, scope, identifiers, _ = snapshots
    async with Code(path, repository, scope, tmp_path / "sources", identifiers[0]) as code, Workspace(
        tmp_path / "work", image=image, mounts={str(code.directory): code.directory},
    ) as workspace:
        tools = {tool.name: tool for tool in workspace.tools()}
        response = json.loads(await code._work(code.query, "get_code_snippet", {"qualified_name": "m.code"}))
        native = structured(response)
        assert native["file_path"].startswith(str(code.directory))
        read = json.loads(await tools["Read"].invoke({"path": native["file_path"], "limit": 2}))
        assert read["exit_code"] == 0 and "return 0" in read["stdout"]["text"]
        write = json.loads(await tools["Write"].invoke({"path": native["file_path"], "content": "tampered"}))
        assert write["exit_code"] != 0
