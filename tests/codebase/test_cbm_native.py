import json
import os
from pathlib import Path

import pytest

from gh_puller.codebase import (
    Archive,
    ArchiveWriter,
    CBMBinaryError,
    CBMClient,
    CBMTransportError,
    IncrementalConfig,
)
from gh_puller.codebase.archive import (
    COVERAGE_FIDELITY_VERSION,
    GRAPH_FIDELITY_VERSION,
    RadixTree,
    coverage_digest,
    graph_digest,
    materialization_digest,
)
from gh_puller.codebase.cbm._native import NativeArchiveTransport
from gh_puller.codebase.store import GraphRows, load_coverage, load_rows


def write_fake_helper(
    path: Path,
    *,
    hang_on_query: bool = False,
    indexing: bool = False,
) -> None:
    protocol = 8 if indexing else 6
    capabilities = ["archive-load", "tool-call", "graph-compare"]
    if indexing:
        capabilities.extend(
            [
                "repository-index",
                "project-open",
                "project-list",
                "project-delete",
                "granular-delta-controls",
                "force-full-route",
            ],
        )
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import struct
import sys
import time

if sys.argv[1:] == ["--version"]:
    print("gh-puller-cbm-helper {protocol}")
    raise SystemExit(0)

def read_exact(size):
    value = b""
    while len(value) < size:
        block = sys.stdin.buffer.read(size - len(value))
        if not block:
            raise SystemExit(0)
        value += block
    return value

def respond(value):
    raw = json.dumps(value, separators=(",", ":")).encode()
    sys.stdout.buffer.write(struct.pack(">I", len(raw)) + raw)
    sys.stdout.buffer.flush()

while True:
    request = json.loads(read_exact(struct.unpack(">I", read_exact(4))[0]))
    ident = request["id"]
    method = request["method"]
    params = request.get("params", {{}})
    if log := os.environ.get("NATIVE_REQUEST_LOG"):
        with Path(log).open("a") as stream:
            stream.write(json.dumps(request) + "\\n")
    if method == "hello":
        result = {{"protocol": {protocol}, "kga_format": 5, "graph_fidelity": 2,
                  "coverage_fidelity": 1, "store_format": 1, "sdk_abi": 2,
                  "capabilities": {capabilities!r},
                  "tools": ["query_graph", "get_graph_schema", "index_status"]}}
    elif method == "load":
        Path(params["database_path"]).touch()
        result = {{"project": params["project"], "graph_digest": params["graph_digest"],
                  "materialization_digest": params["materialization_digest"],
                  "nodes": params["node_count"], "edges": params["edge_count"],
                  "graph_fidelity": params["graph_fidelity"],
                  "coverage_rows": params["coverage_count"],
                  "coverage_fidelity": params["coverage_fidelity"],
                  "materialized": not params["reuse"]}}
    elif method == "call":
        name = params["name"]
        arguments = params["arguments"]
        if {hang_on_query!r} and name == "query_graph":
            time.sleep(60)
        if name == "query_graph":
            result = {{"columns": ["backend", "project"],
                      "rows": [["native", arguments["project"]]], "total": 1,
                      "pid": os.getpid()}}
        elif name == "index_status":
            result = {{"project": arguments["project"], "nodes": 3, "edges": 3,
                      "status": "ready", "parse_partial": {{"files": [], "count": 0,
                      "truncated": False}}, "skipped": {{"files": [], "count": 0,
                      "truncated": False}}, "not_indexed": {{"dirs": [],
                      "dirs_count": 0, "files": [], "files_count": 0,
                      "truncated": False}}}}
        else:
            result = {{"node_labels": [{{"label": "Function", "count": 2}}],
                      "edge_types": []}}
    elif method == "compare":
        result = {{"schema_version": 1, "base": params["base"],
                  "target": params["target"], "limit": params["limit"],
                  "scan_limit": params["scan_limit"]}}
    elif method == "index":
        route = "full" if params["force_full"] else "closure_repair"
        result = {{"project": params["project"], "status": "indexed",
                  "incremental_controls": params["incremental_controls"],
                  "index_execution": {{"route": route}}}}
    elif method == "open":
        result = {{"project": params["project"], "nodes": 3, "edges": 2}}
    elif method == "list":
        result = {{"projects": [{{"name": "native-build"}}], "total": 1, "returned": 1}}
    elif method == "delete":
        result = {{"project": params["project"], "deleted": True}}
    elif method == "shutdown":
        respond({{"id": ident, "ok": True, "result": {{}}}})
        break
    else:
        respond({{"id": ident, "ok": False,
                 "error": {{"code": "unsupported", "message": method}}}})
        continue
    respond({{"id": ident, "ok": True, "result": result}})
""",
    )
    path.chmod(0o755)


def write_archive(
    path: Path,
    project: str = "native-test",
    *,
    with_coverage: bool = False,
    coverage_generation: str = "original-cbm-generation",
    extra_node: str | None = None,
) -> tuple[dict, GraphRows]:
    newline = f"{project}.mod.unit.a\n"
    bang = f"{project}.mod.unit.a!"
    nodes = {
        project: {
            "label": "Project",
            "name": project,
            "file_path": "",
            "start_line": 0,
            "end_line": 0,
            "properties": {},
        },
        newline: {
            "label": "Function",
            "name": "newline",
            "file_path": "newline.py",
            "start_line": 3,
            "end_line": 7,
            "properties": {"docstring": "native needle", "nested": {"unicode": "图"}},
        },
        bang: {
            "label": "Function",
            "name": "bang",
            "file_path": "bang.py",
            "start_line": 11,
            "end_line": 13,
            "properties": {"ratio": 1.25},
        },
    }
    edges = {
        (project, newline, "CONTAINS", ""): {"properties": {}},
        (project, bang, "CONTAINS", ""): {"properties": {}},
        (newline, bang, "CALLS", ""): {"properties": {}},
    }
    if extra_node is not None:
        qualified_name = f"{project}.mod.unit.{extra_node}"
        nodes[qualified_name] = {
            "label": "Function",
            "name": extra_node,
            "file_path": f"{extra_node}.py",
            "start_line": 1,
            "end_line": 2,
            "properties": {},
        }
        edges[(project, qualified_name, "CONTAINS", "")] = {"properties": {}}
    rows = GraphRows(nodes, edges)
    writer = ArchiveWriter(path)
    node_root = RadixTree(writer, "nodes").build(rows.nodes.items())
    edge_root = RadixTree(writer, "edges").build(rows.edges.items())
    manifest = {
        "sha": "commit-one",
        "parents": [],
        "node_root": node_root.to_json(),
        "edge_root": edge_root.to_json(),
        "nodes": len(rows.nodes),
        "edges": len(rows.edges),
        "graph_digest": graph_digest(node_root, edge_root),
        "graph_fidelity_version": GRAPH_FIDELITY_VERSION,
        "cbm_project": project,
    }
    if with_coverage:
        coverage_rows = {
            ("newline.py", "parse_partial"): "3-4, 6-6",
            ("vendor", "not_indexed_dir"): "excluded subtree",
        }
        coverage_metadata = {
            "project": project,
            "generation": coverage_generation,
            "index_mode": "delta",
            "recorded_at": "2026-09-08T00:00:00Z",
            "recording_status": "complete",
            "ignored_files_stored": 1,
            "ignored_files_total": 1,
            "coverage_version": 3,
            "hash_records_complete": True,
        }
        coverage_root = RadixTree(writer, "coverage").build(coverage_rows.items())
        coverage_identity = coverage_digest(coverage_root, coverage_metadata)
        manifest.update(
            {
                "coverage_root": coverage_root.to_json(),
                "coverage_rows": len(coverage_rows),
                "coverage_metadata": coverage_metadata,
                "coverage_digest": coverage_identity,
                "materialization_digest": materialization_digest(
                    manifest["graph_digest"],
                    coverage_identity,
                ),
                "coverage_fidelity_version": COVERAGE_FIDELITY_VERSION,
            },
        )
    writer.commit(manifest)
    writer.finalize()
    return manifest, rows


def write_missharded_archive(path: Path) -> None:
    writer = ArchiveWriter(path)
    entry = [
        "__project__",
        {
            "label": "Project",
            "name": "__project__",
            "file_path": "",
            "start_line": 0,
            "end_line": 0,
            "properties": {},
        },
    ]
    leaf = writer.store_page(
        {"tree": "nodes", "kind": "leaf", "depth": 1, "count": 1, "entries": [entry]},
        {"tree": "nodes", "kind": "leaf", "depth": 1, "entries": [entry]},
    )
    node_root = writer.store_page(
        {
            "tree": "nodes",
            "kind": "branch",
            "depth": 0,
            "count": 1,
            "children": [["wrong", {"logical_hash": leaf.logical_hash, "count": 1}]],
        },
        {
            "tree": "nodes",
            "kind": "branch",
            "depth": 0,
            "children": [["wrong", leaf.to_json()]],
        },
    )
    writer.commit(
        {
            "sha": "missharded",
            "parents": [],
            "node_root": node_root.to_json(),
            "edge_root": None,
            "nodes": 1,
            "edges": 0,
            "graph_digest": graph_digest(node_root, None),
            "graph_fidelity_version": GRAPH_FIDELITY_VERSION,
            "cbm_project": "__project__",
        },
    )
    writer.finalize()


def requests(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_native_transport_keeps_graph_rows_out_of_python(tmp_path, monkeypatch):
    helper = tmp_path / "fake-helper"
    log = tmp_path / "requests.jsonl"
    archive_path = tmp_path / "archive.kga"
    manifest, _ = write_archive(archive_path)
    write_fake_helper(helper)
    monkeypatch.setattr(Archive, "load_rows", lambda *_args, **_kwargs: pytest.fail("loaded graph rows"))

    with NativeArchiveTransport(
        helper,
        tmp_path / "cache",
        5,
        {"NATIVE_REQUEST_LOG": str(log)},
    ) as transport:
        loaded = transport.load_archive(archive_path)
        queried = transport.query_graph(project="native-test", query="MATCH (n) RETURN n")

    load_request = next(item for item in requests(log) if item["method"] == "load")
    assert loaded["materialized"] is True
    assert loaded["graph_digest"] == manifest["graph_digest"]
    assert loaded["materialization_digest"] == manifest["graph_digest"]
    assert Path(loaded["database_path"]).is_file()
    assert load_request["params"]["node_root"] == manifest["node_root"]
    assert load_request["params"]["edge_root"] == manifest["edge_root"]
    assert load_request["params"]["coverage_fidelity"] == 0
    assert load_request["params"]["coverage_root"] is None
    assert load_request["params"]["coverage_metadata"] is None
    assert queried["rows"] == [["native", "native-test"]]


def test_native_cache_is_reused_by_a_new_helper_process(tmp_path):
    helper = tmp_path / "fake-helper"
    log = tmp_path / "requests.jsonl"
    archive_path = tmp_path / "archive.kga"
    write_fake_helper(helper)
    write_archive(archive_path)
    environment = {"NATIVE_REQUEST_LOG": str(log)}

    with NativeArchiveTransport(helper, tmp_path / "cache", 5, environment) as first:
        assert first.load_archive(archive_path)["materialized"] is True
    with NativeArchiveTransport(helper, tmp_path / "cache", 5, environment) as second:
        assert second.load_archive(archive_path)["materialized"] is False

    loads = [item for item in requests(log) if item["method"] == "load"]
    assert [item["params"]["reuse"] for item in loads] == [False, True]


def test_native_identity_includes_coverage_snapshot(tmp_path):
    helper = tmp_path / "fake-helper"
    first_archive = tmp_path / "first.kga"
    second_archive = tmp_path / "second.kga"
    write_fake_helper(helper)
    first_manifest, _ = write_archive(
        first_archive,
        with_coverage=True,
        coverage_generation="generation-one",
    )
    second_manifest, _ = write_archive(
        second_archive,
        with_coverage=True,
        coverage_generation="generation-two",
    )
    assert first_manifest["graph_digest"] == second_manifest["graph_digest"]
    assert first_manifest["materialization_digest"] != second_manifest["materialization_digest"]

    with CBMClient(native_helper=helper, cache_root=tmp_path / "cache", timeout=5) as client:
        first = client.load_archive(first_archive)
        second = client.load_archive(second_archive)

        assert first.database_path != second.database_path
        assert second.coverage_rows == 2
        assert second.coverage_fidelity == COVERAGE_FIDELITY_VERSION
        with pytest.raises(CBMTransportError, match="no longer loaded"):
            client.query_graph(first, query="MATCH (n) RETURN n")


def test_client_native_query_does_not_resolve_or_start_mcp(tmp_path):
    helper = tmp_path / "fake-helper"
    archive_path = tmp_path / "archive.kga"
    write_fake_helper(helper)
    write_archive(archive_path)

    with CBMClient(
        tmp_path / "missing-cbm",
        native_helper=helper,
        cache_root=tmp_path / "cache",
        timeout=5,
    ) as client:
        assert client._daemon_backend is None
        graph = client.load_archive(archive_path)
        result = client.query_graph(graph, query="MATCH (n) RETURN n")
        schema = client.get_graph_schema(graph)
        status = client.index_status(graph)
        assert result["rows"] == [["native", graph.project]]
        assert result["pid"] == client.native_pid
        assert schema["node_labels"][0]["label"] == "Function"
        assert status["nodes"] == 3
        with pytest.raises(CBMTransportError, match="requires a daemon-backed graph"):
            client.index_status(graph, verbose=True)
        assert client._daemon_backend is None

        with pytest.raises(CBMBinaryError, match="does not exist"):
            client.query_graph(client.daemon_graph(graph.project), query="MATCH (n) RETURN n")
        with pytest.raises(CBMTransportError, match="not supported for this archive graph"):
            client.call_json_tool("trace_path", {"function_name": "main"}, target=graph)
        assert client._daemon_backend is None


def test_client_native_index_uses_full_helper_without_resolving_mcp(tmp_path, monkeypatch):
    helper = tmp_path / "fake-index-helper"
    log = tmp_path / "requests.jsonl"
    write_fake_helper(helper, indexing=True)
    controls = {
        "closure_overflow": "repair",
        "closure_cost_percent": 20,
        "dependent_scope": "symbol",
        "new_surface": "bounded",
        "reference_fanout_cap": 64,
        "pair_outputs": "lazy",
        "pair_refresh_budget": 10000,
        "pair_input_missing": "skip",
    }
    tree = tmp_path / "tree"
    tree.mkdir()
    monkeypatch.setenv("NATIVE_REQUEST_LOG", str(log))

    with CBMClient(
        tmp_path / "missing-cbm",
        native_index_helper=helper,
        cache_root=tmp_path / "cache",
        index_backend="native",
        timeout=5,
    ) as client:
        assert {"repository-index", "granular-delta-controls", "force-full-route"} <= client.capabilities()
        execution = client.index_repository(
            tree,
            "native-build",
            "full",
            force_full=True,
            incremental_controls=controls,
        )
        cross_execution = client.index_repository(
            tree,
            "native-build",
            "cross-repo-intelligence",
            incremental_controls=controls,
            target_projects=["dependency"],
        )
        graph = client.project_graph("native-build", source_root=tree)
        queried = client.query_graph(graph, query="MATCH (n) RETURN n")
        projects = client.list_projects(include_details=True)
        deleted, _detail = client.delete_project("native-build")
        assert execution["route"] == "full"
        assert cross_execution["route"] == "closure_repair"
        assert graph.nodes == 3
        assert queried["rows"] == [["native", "native-build"]]
        assert projects["projects"] == [{"name": "native-build"}]
        assert deleted is True
        assert client._daemon_backend is None

    index_request = next(item for item in requests(log) if item["method"] == "index")
    assert index_request["params"] == {
        "repo_path": str(tree),
        "database_path": str(tmp_path / "cache" / "native-build.db"),
        "cache_directory": str(tmp_path / "cache"),
        "project": "native-build",
        "mode": "full",
        "persistence": False,
        "force_full": True,
        "incremental_controls": controls,
        "target_projects": [],
    }
    cross_request = [item for item in requests(log) if item["method"] == "index"][1]
    assert cross_request["params"]["incremental_controls"] is None
    assert cross_request["params"]["target_projects"] == ["dependency"]


def test_client_rejects_archive_handle_after_loading_another_generation(tmp_path):
    helper = tmp_path / "fake-helper"
    first_archive = tmp_path / "first.kga"
    second_archive = tmp_path / "second.kga"
    write_fake_helper(helper)
    write_archive(first_archive, "first")
    write_archive(second_archive, "second")

    with CBMClient(native_helper=helper, cache_root=tmp_path / "cache", timeout=5) as client:
        first = client.load_archive(first_archive)
        second = client.load_archive(second_archive)
        compared = client.compare_graphs(first, second, limit=4, scan_limit=100)

        assert client.query_graph(second, query="MATCH (n) RETURN n")["rows"] == [
            ["native", "second"],
        ]
        assert compared["base"] == {
            "database_path": str(first.database_path),
            "project": "first",
        }
        assert compared["target"] == {
            "database_path": str(second.database_path),
            "project": "second",
        }
        assert compared["limit"] == 4
        assert compared["scan_limit"] == 100
        with pytest.raises(CBMTransportError, match="no longer loaded"):
            client.query_graph(first, query="MATCH (n) RETURN n")
        with pytest.raises(CBMTransportError, match="cannot compare native and daemon"):
            client.compare_graphs(first, client.daemon_graph("second"))


def test_native_query_timeout_terminates_helper(tmp_path):
    helper = tmp_path / "fake-helper"
    archive_path = tmp_path / "archive.kga"
    write_fake_helper(helper, hang_on_query=True)
    write_archive(archive_path)
    transport = NativeArchiveTransport(helper, tmp_path / "cache", 0.1)
    transport.load_archive(archive_path)

    with pytest.raises(CBMTransportError, match="timed out"):
        transport.query_graph(project="native-test", query="MATCH (n) RETURN n")

    assert transport.process.poll() is not None
    transport.close()


def test_native_project_binding_rejects_cache_path_escape(tmp_path):
    with (
        CBMClient(index_backend="native", cache_root=tmp_path) as client,
        pytest.raises(ValueError, match="invalid CBM project"),
    ):
        client.project_graph("../outside")


@pytest.mark.integration
def test_real_native_index_helper_publishes_and_deletes_graph(tmp_path):
    configured = os.environ.get("GH_PULLER_TEST_CBM_NATIVE_INDEX_HELPER")
    if configured is None:
        pytest.skip("real native index helper not configured")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "main.py").write_text("def native_symbol():\n    return 42\n")
    cache = tmp_path / "cache"

    with CBMClient(
        tmp_path / "unused-cbm",
        native_index_helper=Path(configured),
        cache_root=cache,
        index_backend="native",
        timeout=120,
    ) as client:
        execution = client.index_repository(
            tree,
            "native-index",
            "full",
            force_full=True,
            incremental_controls=IncrementalConfig().to_dict(),
        )
        graph = client.project_graph("native-index", source_root=tree)
        queried = client.query_graph(
            graph,
            query="MATCH (n:Function) RETURN n.name",
            max_rows=10,
        )
        projects = client.list_projects(include_details=True)
        rows = load_rows(cache / "native-index.db", "native-index")
        deleted, _detail = client.delete_project("native-index")

    assert execution == {"route": "full", "reason": "explicit_force_full"}
    assert ["native_symbol"] in queried["rows"]
    assert any(project["name"] == "native-index" for project in projects["projects"])
    assert any(node["name"] == "native_symbol" for node in rows.nodes.values())
    assert deleted is True
    assert not (cache / "native-index.db").exists()


@pytest.mark.integration
def test_real_native_helper_restores_exact_rows_and_reuses_cache(tmp_path):
    configured = os.environ.get("GH_PULLER_TEST_CBM_NATIVE_HELPER")
    if configured is None:
        pytest.skip("real native helper not configured")
    helper = Path(configured)
    archive_path = tmp_path / "archive.kga"
    manifest, expected = write_archive(archive_path, "real-native", with_coverage=True)
    cache = tmp_path / "cache"

    with CBMClient(native_helper=helper, cache_root=cache, timeout=30) as first:
        loaded = first.load_archive(archive_path)
        queried = first.query_graph(
            loaded,
            query="MATCH (n:Function) RETURN n.name, n.file_path, n.docstring",
            max_rows=10,
        )
        searched = first.search_graph(loaded, label="Function", fields=["ratio"], limit=10)
        ranked = first.search_graph(loaded, query="native needle", limit=10)
        schema = first.get_graph_schema(loaded)
        traced = first.trace_path(
            loaded,
            function_name="newline",
            direction="outbound",
        )
        architecture = first.get_architecture(
            loaded,
            aspects=["structure", "dependencies"],
        )
        coverage = first.check_index_coverage(
            loaded,
            paths=["newline.py", "vendor/package.c"],
            scopes=["."],
            scope_limit=1,
        )
        status = first.index_status(loaded)
        restored = load_rows(loaded.database_path, "real-native")
        restored_coverage = load_coverage(loaded.database_path, "real-native")
    with CBMClient(native_helper=helper, cache_root=cache, timeout=30) as second:
        reused = second.load_archive(archive_path)

    assert loaded.materialized is True
    assert loaded.graph_digest == manifest["graph_digest"]
    assert loaded.materialization_digest == manifest["materialization_digest"]
    assert loaded.coverage_rows == manifest["coverage_rows"]
    assert loaded.coverage_fidelity == COVERAGE_FIDELITY_VERSION
    assert restored == expected
    assert restored_coverage is not None
    assert restored_coverage.rows == {
        ("newline.py", "parse_partial"): "3-4, 6-6",
        ("vendor", "not_indexed_dir"): "excluded subtree",
    }
    assert {
        key: value
        for key, value in restored_coverage.metadata.items()
        if key != "generation"
    } == {
        key: value
        for key, value in manifest["coverage_metadata"].items()
        if key != "generation"
    }
    assert {tuple(row[:2]) for row in queried["rows"]} == {
        ("bang", "bang.py"),
        ("newline", "newline.py"),
    }
    assert any(row[2] == "native needle" for row in queried["rows"])
    assert searched["total"] == 2
    assert {row[0] for group in searched["groups"] for row in group["rows"]} == {
        "a\n",
        "a!",
    }
    assert ranked["search_mode"] == "bm25"
    assert ranked["rows"][0][0] == "real-native.mod.unit.a\n"
    assert {item["label"] for item in schema["node_labels"]} >= {"Project", "Function"}
    assert traced["callees_total"] == 1
    assert traced["callees"]["groups"] == [
        {"qn_prefix": "real-native.mod.unit", "rows": [["a!", 1]]},
    ]
    assert architecture["total_nodes"] == 3
    assert architecture["total_edges"] == 3
    assert {tuple(row) for row in architecture["node_labels"]["rows"]} >= {
        ("Function", 2),
        ("Project", 1),
    }
    assert {tuple(row) for row in architecture["edge_types"]["rows"]} >= {
        ("CALLS", 1),
        ("CONTAINS", 2),
    }
    assert coverage["metadata"]["generation_matches"] is True
    assert coverage["metadata"]["index_mode"] == "delta"
    assert coverage["paths"][0]["status"] == "partial"
    assert coverage["paths"][0]["freshness"] == "unavailable"
    assert coverage["paths"][0]["coverage"][0]["ranges"] == [
        {"start": 3, "end": 4},
        {"start": 6, "end": 6},
    ]
    assert coverage["paths"][1]["status"] == "excluded"
    assert coverage["scopes"][0]["status"] == "known_gaps"
    assert coverage["scopes"][0]["total"] == 2
    assert coverage["scopes"][0]["has_more"] is True
    assert status["project"] == "real-native"
    assert status["nodes"] == 3
    assert status["edges"] == 3
    assert status["status"] == "ready"
    assert status["parse_partial"]["files"] == [
        {"path": "newline.py", "error_ranges": "3-4, 6-6"},
    ]
    assert status["not_indexed"]["dirs"] == ["vendor"]
    assert "git" not in status
    assert reused.materialized is False


@pytest.mark.integration
def test_real_native_helper_compares_archive_generations(tmp_path):
    configured = os.environ.get("GH_PULLER_TEST_CBM_NATIVE_HELPER")
    if configured is None:
        pytest.skip("real native helper not configured")
    project = "compare-native"
    base_archive = tmp_path / "base.kga"
    target_archive = tmp_path / "target.kga"
    write_archive(base_archive, project)
    write_archive(target_archive, project, extra_node="added")

    with CBMClient(
        native_helper=Path(configured),
        cache_root=tmp_path / "cache",
        timeout=30,
    ) as client:
        base = client.load_archive(base_archive)
        target = client.load_archive(target_archive)
        compared = client.compare_graphs(base, target, limit=10, scan_limit=100)

    assert base.project == target.project == project
    assert base.database_path != target.database_path
    assert compared["base"]["project"] == project
    assert compared["target"]["project"] == project
    assert compared["nodes"]["added"]["total"] == 1
    assert compared["nodes"]["added"]["items"] == [
        {
            "qualified_name": f"{project}.mod.unit.added",
            "label": "Function",
            "file_path": "added.py",
        },
    ]
    assert compared["nodes"]["removed"]["total"] == 0
    assert compared["edges"]["added"]["total"] == 1
    assert compared["edges"]["added"]["items"][0]["target"]["qualified_name"] == (
        f"{project}.mod.unit.added"
    )
    assert compared["edges"]["removed"]["total"] == 0


@pytest.mark.integration
def test_real_native_helper_rejects_missharded_identity(tmp_path):
    configured = os.environ.get("GH_PULLER_TEST_CBM_NATIVE_HELPER")
    if configured is None:
        pytest.skip("real native helper not configured")
    archive_path = tmp_path / "archive.kga"
    write_missharded_archive(archive_path)

    with (
        CBMClient(
            native_helper=Path(configured),
            cache_root=tmp_path / "cache",
            timeout=30,
        ) as client,
        pytest.raises(CBMTransportError, match="invalid node row in KGA leaf"),
    ):
        client.load_archive(archive_path)


@pytest.mark.integration
def test_real_native_helper_reads_captured_prefix_while_writer_appends(tmp_path):
    configured = os.environ.get("GH_PULLER_TEST_CBM_NATIVE_HELPER")
    if configured is None:
        pytest.skip("real native helper not configured")
    archive_path = tmp_path / "archive.kga"
    project = "live-native"
    rows = GraphRows(
        {
            project: {
                "label": "Project",
                "name": project,
                "file_path": "",
                "start_line": 0,
                "end_line": 0,
                "properties": {},
            },
        },
        {},
    )
    writer = ArchiveWriter(archive_path)
    node_root = RadixTree(writer, "nodes").build(rows.nodes.items())
    writer.commit(
        {
            "sha": "durable",
            "parents": [],
            "node_root": node_root.to_json(),
            "edge_root": None,
            "nodes": 1,
            "edges": 0,
            "graph_digest": graph_digest(node_root, None),
            "graph_fidelity_version": GRAPH_FIDELITY_VERSION,
            "cbm_project": project,
        },
    )
    captured = Archive(archive_path, allow_incomplete=True)
    captured_size = captured._reader.size
    writer.append(1, b'{"unreferenced":"new writer tail"}')
    writer._file.flush()
    assert archive_path.stat().st_size > captured_size

    try:
        with CBMClient(
            native_helper=Path(configured),
            cache_root=tmp_path / "cache",
            timeout=30,
        ) as client:
            loaded = client.load_archive(captured)
            queried = client.query_graph(loaded, query="MATCH (n:Project) RETURN n.name")
    finally:
        captured.close()
        writer.close_incomplete()

    assert loaded.nodes == 1
    assert loaded.edges == 0
    assert queried["rows"] == [[project]]
