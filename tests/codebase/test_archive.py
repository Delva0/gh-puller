import math

import pytest

import gh_puller.codebase.archive as archive_module
from gh_puller.codebase.archive import (
    GRAPH_FIDELITY_VERSION,
    Archive,
    ArchiveError,
    ArchiveWriter,
    RadixTree,
    TreeRef,
    graph_digest,
)
from gh_puller.codebase.store import GraphRows


def commit_rows(writer, sha, parents, rows, node_root=None, edge_root=None):
    nodes = RadixTree(writer, "nodes")
    edges = RadixTree(writer, "edges")
    node_root = nodes.build(rows.nodes.items()) if node_root is None else nodes.apply(node_root, rows.nodes)
    edge_root = edges.build(rows.edges.items()) if edge_root is None else edges.apply(edge_root, rows.edges)
    writer.commit(
        {
            "sha": sha,
            "parents": parents,
            "node_root": node_root.to_json() if node_root else None,
            "edge_root": edge_root.to_json() if edge_root else None,
            "nodes": node_root.count if node_root else 0,
            "edges": edge_root.count if edge_root else 0,
            "graph_digest": graph_digest(node_root, edge_root),
        },
    )
    return node_root, edge_root, nodes.pages_written + edges.pages_written


def test_incremental_radix_archive_loads_every_commit(tmp_path):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    first = GraphRows({"a": {"label": "File"}, "b": {"label": "Function"}}, {})
    node_root, _edge_root, _ = commit_rows(writer, "c1", [], first)
    changes = GraphRows({"a": {"label": "Class"}, "c": {"label": "File"}}, {})
    node_tree = RadixTree(writer, "nodes")
    node_root = node_tree.apply(node_root, {"a": changes.nodes["a"], "b": None, "c": changes.nodes["c"]})
    writer.commit(
        {
            "sha": "c2",
            "parents": ["c1"],
            "node_root": node_root.to_json(),
            "edge_root": None,
            "nodes": 2,
            "edges": 0,
            "graph_digest": graph_digest(node_root, None),
        },
    )
    writer.finalize()

    archive = Archive(path)
    archive.verify()
    assert archive.load_rows("c1") == first
    assert archive.load_rows("c2") == changes
    assert archive.commit_ids() == ("c1", "c2")
    assert archive.manifest()["sha"] == "c2"
    manifest = archive.manifest("c2")
    manifest["parents"].clear()
    manifest["node_root"]["count"] = 0
    assert archive.manifest("c2")["parents"] == ["c1"]
    assert archive.manifest("c2")["node_root"]["count"] == 2


def test_networkx_view_preserves_parallel_edge_rows(tmp_path):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    rows = GraphRows(
        {
            "source": {"label": "Function"},
            "target": {"label": "Function"},
        },
        {
            ("source", "target", "CALLS", "site-b"): {"properties": {"line": 8}},
            ("source", "target", "CALLS", "site-a"): {"properties": {"line": 3}},
        },
    )
    commit_rows(writer, "c1", [], rows)
    writer.finalize()

    graph = Archive(path).load("c1")
    assert graph.nodes["source"] == {"label": "Function"}
    assert graph["source"]["target"] == {
        "edge_keys": [
            ("source", "target", "CALLS", "site-a"),
            ("source", "target", "CALLS", "site-b"),
        ],
        "data": [
            {"properties": {"line": 3}},
            {"properties": {"line": 8}},
        ],
    }


def test_one_change_writes_only_one_radix_path(tmp_path):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    rows = GraphRows({f"node-{index}": {"value": index} for index in range(1000)}, {})
    root, _, _ = commit_rows(writer, "c1", [], rows)
    tree = RadixTree(writer, "nodes")
    changed = tree.apply(root, {"node-500": {"value": "changed"}})
    assert tree.pages_written == 2
    writer.commit(
        {
            "sha": "c2",
            "parents": ["c1"],
            "node_root": changed.to_json(),
            "edge_root": None,
            "nodes": changed.count,
            "edges": 0,
            "graph_digest": graph_digest(changed, None),
        },
    )
    writer.finalize()
    assert Archive(path).load_rows("c2").nodes["node-500"]["value"] == "changed"


def test_no_changes_reuses_root_without_writing(tmp_path):
    writer = ArchiveWriter(tmp_path / "archive.kga")
    tree = RadixTree(writer, "nodes")
    root = tree.build({"a": {"v": 1}}.items())
    unchanged = RadixTree(writer, "nodes")
    assert unchanged.apply(root, {}) == root
    assert unchanged.pages_written == 0
    writer.close_incomplete()


def test_page_cache_is_bounded_by_raw_bytes(tmp_path):
    writer = ArchiveWriter(tmp_path / "archive.kga", cache_bytes=400)
    tree = RadixTree(writer, "nodes")
    tree.build({f"module{index}.node": {"value": "x" * 200} for index in range(20)}.items())
    assert writer._cache_size <= 400
    assert sum(size for _, size in writer._cache.values()) == writer._cache_size
    writer.close_incomplete()


def test_complete_archive_can_be_extended_without_changing_old_roots(tmp_path):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    root, _, _ = commit_rows(writer, "c1", [], GraphRows({"a": {"v": 1}}, {}))
    writer.finalize()
    old_root = Archive(path)._entries["c1"]["node_root"]

    writer = ArchiveWriter(path, extend_complete=True)
    tree = RadixTree(writer, "nodes")
    root = tree.apply(TreeRef.from_json(old_root), {"a": {"v": 2}})
    writer.commit(
        {
            "sha": "c2",
            "parents": ["c1"],
            "node_root": root.to_json(),
            "edge_root": None,
            "nodes": 1,
            "edges": 0,
            "graph_digest": graph_digest(root, None),
        },
    )
    writer.finalize()

    archive = Archive(path)
    assert archive._entries["c1"]["node_root"] == old_root
    assert archive.load_rows("c1").nodes["a"]["v"] == 1
    assert archive.load_rows("c2").nodes["a"]["v"] == 2


def test_complete_archive_open_uses_footer_without_scanning(tmp_path, monkeypatch):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    commit_rows(writer, "c1", [], GraphRows({"a": {"v": 1}}, {}))
    writer.finalize()
    monkeypatch.setattr(archive_module, "_scan_reader", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError))
    assert len(Archive(path)) == 1


def test_complete_archive_extension_uses_footer_and_verifies_only_append(tmp_path, monkeypatch):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    old_root, _, _ = commit_rows(writer, "c1", [], GraphRows({"a": {"v": 1}}, {}))
    writer.finalize()

    monkeypatch.setattr(archive_module, "_scan_reader", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError))
    writer = ArchiveWriter(path, extend_complete=True)
    root = RadixTree(writer, "nodes").apply(old_root, {"a": {"v": 2}})
    writer.commit(
        {
            "sha": "c2",
            "parents": ["c1"],
            "node_root": root.to_json(),
            "edge_root": None,
            "nodes": 1,
            "edges": 0,
            "graph_digest": graph_digest(root, None),
        },
    )
    writer.finalize()
    writer.verify_appended()

    archive = Archive(path)
    archive.verify_index()
    assert [item["sha"] for item in archive._commits] == ["c1", "c2"]
    assert archive.load_rows("c1").nodes["a"]["v"] == 1
    assert archive.load_rows("c2").nodes["a"]["v"] == 2


def test_incomplete_checkpoint_discards_partial_tail_without_scanning(tmp_path, monkeypatch):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    root, _, _ = commit_rows(writer, "c1", [], GraphRows({"a": {"v": 1}}, {}))
    writer.close_incomplete()
    durable_size = path.stat().st_size
    with path.open("ab") as file:
        file.write(b"partial next frame")

    monkeypatch.setattr(archive_module, "_scan_reader", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError))
    assert len(Archive(path, allow_incomplete=True)) == 1
    writer = ArchiveWriter(path)
    assert path.stat().st_size == durable_size
    root = RadixTree(writer, "nodes").apply(root, {"b": {"v": 2}})
    writer.commit(
        {
            "sha": "c2",
            "parents": ["c1"],
            "node_root": root.to_json(),
            "edge_root": None,
            "nodes": 2,
            "edges": 0,
            "graph_digest": graph_digest(root, None),
        },
    )
    writer.finalize()
    writer.verify_appended()
    assert set(Archive(path).load_rows("c2").nodes) == {"a", "b"}


def test_interrupted_first_extension_recovers_embedded_footer_without_scanning(tmp_path, monkeypatch):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    commit_rows(writer, "c1", [], GraphRows({"a": {"v": 1}}, {}))
    # Model an archive finalized by the pre-checkpoint implementation.
    legacy_end = writer.frames[-1].end
    writer._file.truncate(legacy_end)
    writer._file.seek(legacy_end)
    writer.finalize()
    durable_size = path.stat().st_size
    with path.open("ab") as file:
        file.write(b"partial first extension page")

    monkeypatch.setattr(archive_module, "_scan_reader", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError))
    assert len(Archive(path, allow_incomplete=True)) == 1
    writer = ArchiveWriter(path)
    assert path.stat().st_size == durable_size
    assert [item["sha"] for item in writer.commits] == ["c1"]
    writer.close_incomplete()


def test_full_verify_accepts_embedded_checkpoints_and_prior_footer(tmp_path):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    root, _, _ = commit_rows(writer, "c1", [], GraphRows({"a": {"v": 1}}, {}))
    writer.finalize()
    writer = ArchiveWriter(path, extend_complete=True)
    root = RadixTree(writer, "nodes").apply(root, {"a": {"v": 2}})
    writer.commit(
        {
            "sha": "c2",
            "parents": ["c1"],
            "node_root": root.to_json(),
            "edge_root": None,
            "nodes": 1,
            "edges": 0,
            "graph_digest": graph_digest(root, None),
        },
    )
    writer.finalize()
    Archive(path).verify()


def test_live_readers_capture_independent_durable_snapshots(tmp_path):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    root, _, _ = commit_rows(writer, "c1", [], GraphRows({"a": {"v": 1}}, {}))
    first = Archive(path, allow_incomplete=True)

    root = RadixTree(writer, "nodes").apply(root, {"b": {"v": 2}})
    writer.commit(
        {
            "sha": "c2",
            "parents": ["c1"],
            "node_root": root.to_json(),
            "edge_root": None,
            "nodes": 2,
            "edges": 0,
            "graph_digest": graph_digest(root, None),
        },
    )
    later = [Archive(path, allow_incomplete=True) for _ in range(4)]

    assert first.commit_ids() == ("c1",)
    assert first.load_rows().nodes == {"a": {"v": 1}}
    assert all(reader.commit_ids() == ("c1", "c2") for reader in later)
    assert all(reader.load_rows().nodes == {"a": {"v": 1}, "b": {"v": 2}} for reader in later)

    first.close()
    for reader in later:
        reader.close()
    writer.close_incomplete()


def test_reader_uses_footer_offset_captured_before_extension_append(tmp_path, monkeypatch):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    commit_rows(writer, "c1", [], GraphRows({"a": {"v": 1}}, {}))
    writer.finalize()
    extension = ArchiveWriter(path, extend_complete=True)
    original = archive_module._footer_offset
    appended = False

    def append_after_capture(reader):
        nonlocal appended
        offset = original(reader)
        if offset is not None and not appended:
            appended = True
            extension.append(archive_module.PAGE, b'{"kind":"leaf"}')
            extension._file.flush()
        return offset

    monkeypatch.setattr(archive_module, "_footer_offset", append_after_capture)
    try:
        with Archive(path, allow_incomplete=True) as archive:
            assert archive.commit_ids() == ("c1",)
    finally:
        extension.close_incomplete()


def test_archive_writer_is_exclusive(tmp_path):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    with pytest.raises(archive_module.ArchiveError, match="already has a writer"):
        ArchiveWriter(path)
    writer.close_incomplete()

    resumed = ArchiveWriter(path)
    resumed.close_incomplete()


def test_reader_cache_budget_and_context_manager(tmp_path):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    commit_rows(writer, "c1", [], GraphRows({"a": {"v": 1}}, {}))
    writer.finalize()

    with Archive(path, cache_bytes=0) as archive:
        assert archive.load_rows().nodes == {"a": {"v": 1}}
        assert not archive._cache
        reader = archive._reader
    assert reader.fd == -1


def test_native_page_decoder_preserves_rows_without_standard_json(tmp_path, monkeypatch):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    rows = GraphRows(
        {"source": {"label": "Function", "properties": {"nested": [1, {"ok": True}]}}},
        {("source", "target", None, None): {"properties": {"line": 3}}},
    )
    commit_rows(writer, "c1", [], rows)
    writer.finalize()
    archive = Archive(path)

    monkeypatch.setattr(
        archive_module.json,
        "loads",
        lambda _raw: (_ for _ in ()).throw(AssertionError("standard json decoder used")),
    )
    assert archive.load_rows() == rows
    archive.close()


def test_native_page_decoder_falls_back_for_nonfinite_json(tmp_path):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    commit_rows(writer, "c1", [], GraphRows({"a": {"value": math.nan}}, {}))
    writer.finalize()

    with Archive(path) as archive:
        assert math.isnan(archive.load_rows().nodes["a"]["value"])


def test_snapshot_fidelity_rejects_legacy_and_dangling_graphs(tmp_path):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    node = {
        "label": "Project",
        "name": "p",
        "file_path": "",
        "start_line": 0,
        "end_line": 0,
        "properties": {},
    }
    nodes = RadixTree(writer, "nodes").build({"p": node}.items())
    edges = RadixTree(writer, "edges").build(
        {("p", "p.missing", "CONTAINS", ""): {"properties": {}}}.items(),
    )
    common = {
        "node_root": nodes.to_json(),
        "edge_root": edges.to_json(),
        "nodes": 1,
        "edges": 1,
        "graph_digest": graph_digest(nodes, edges),
        "cbm_project": "p",
    }
    writer.commit({"sha": "legacy", "parents": [], **common})
    writer.commit(
        {
            "sha": "current",
            "parents": ["legacy"],
            "graph_fidelity_version": GRAPH_FIDELITY_VERSION,
            **common,
        },
    )
    missing = {**node, "label": "Function", "name": "missing"}
    nodes = RadixTree(writer, "nodes").apply(nodes, {"p.missing": missing})
    writer.commit(
        {
            "sha": "closed",
            "parents": ["current"],
            "node_root": nodes.to_json(),
            "edge_root": edges.to_json(),
            "nodes": 2,
            "edges": 1,
            "graph_digest": graph_digest(nodes, edges),
            "graph_fidelity_version": GRAPH_FIDELITY_VERSION,
            "cbm_project": "p",
        },
    )
    writer.finalize()

    with Archive(path) as archive:
        with pytest.raises(ArchiveError, match="no verified CBM fidelity"):
            archive.verify_snapshot("legacy")
        with pytest.raises(ArchiveError, match="1 missing edge endpoints"):
            archive.verify_snapshot("current")
        archive.verify_snapshot("closed")
