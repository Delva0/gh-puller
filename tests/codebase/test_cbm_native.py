import json
import os
from pathlib import Path

import pytest

from gh_puller.codebase import Archive, ArchiveWriter, CBMClient, CBMTransportError
from gh_puller.codebase.archive import GRAPH_FIDELITY_VERSION, RadixTree, graph_digest
from gh_puller.codebase.cbm_native import NativeArchiveTransport
from gh_puller.codebase.store import GraphRows, load_rows


def write_fake_helper(path: Path, *, hang_on_query: bool = False) -> None:
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import struct
import sys
import time

if sys.argv[1:] == ["--version"]:
    print("gh-puller-cbm-helper 1")
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
        result = {{"protocol": 1, "kga_format": 5, "graph_fidelity": 2, "store_format": 1,
                  "capabilities": ["archive-load", "query-graph"]}}
    elif method == "load":
        Path(params["database_path"]).touch()
        result = {{"project": params["project"], "graph_digest": params["graph_digest"],
                  "nodes": params["node_count"], "edges": params["edge_count"],
                  "materialized": not params["reuse"]}}
    elif method == "query":
        if {hang_on_query!r}:
            time.sleep(60)
        result = {{"columns": ["backend", "project"],
                  "rows": [["native", params["project"]]], "total": 1,
                  "pid": os.getpid()}}
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


def write_archive(path: Path, project: str = "native-test") -> tuple[dict, GraphRows]:
    newline = f"{project}.mod.unit.a\n"
    bang = f"{project}.mod.unit.a!"
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
        },
        {
            (project, newline, "CONTAINS", ""): {"properties": {}},
            (project, bang, "CONTAINS", ""): {"properties": {}},
        },
    )
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
    writer.commit(manifest)
    writer.finalize()
    return manifest, rows


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
    assert Path(loaded["database_path"]).is_file()
    assert load_request["params"]["node_root"] == manifest["node_root"]
    assert load_request["params"]["edge_root"] == manifest["edge_root"]
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
        assert client._transport is None
        project = client.load_archive(archive_path)["project"]
        result = client.query_graph(project=project, query="MATCH (n) RETURN n")
        assert result["rows"] == [["native", project]]
        assert result["pid"] == client.native_pid
        assert client._transport is None


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


@pytest.mark.integration
def test_real_native_helper_restores_exact_rows_and_reuses_cache(tmp_path):
    configured = os.environ.get("GH_PULLER_TEST_CBM_NATIVE_HELPER")
    if configured is None:
        pytest.skip("real native helper not configured")
    helper = Path(configured)
    archive_path = tmp_path / "archive.kga"
    manifest, expected = write_archive(archive_path, "real-native")
    cache = tmp_path / "cache"

    with CBMClient(native_helper=helper, cache_root=cache, timeout=30) as first:
        loaded = first.load_archive(archive_path)
        queried = first.query_graph(
            project="real-native",
            query="MATCH (n:Function) RETURN n.name, n.file_path, n.docstring",
            max_rows=10,
        )
        restored = load_rows(loaded["database_path"], "real-native")
    with CBMClient(native_helper=helper, cache_root=cache, timeout=30) as second:
        reused = second.load_archive(archive_path)

    assert loaded["materialized"] is True
    assert loaded["graph_digest"] == manifest["graph_digest"]
    assert restored == expected
    assert {tuple(row[:2]) for row in queried["rows"]} == {
        ("bang", "bang.py"),
        ("newline", "newline.py"),
    }
    assert any(row[2] == "native needle" for row in queried["rows"])
    assert reused["materialized"] is False


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
            queried = client.query_graph(project=project, query="MATCH (n:Project) RETURN n.name")
    finally:
        captured.close()
        writer.close_incomplete()

    assert loaded["nodes"] == 1
    assert loaded["edges"] == 0
    assert queried["rows"] == [[project]]
