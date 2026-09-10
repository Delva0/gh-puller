from hashlib import sha256

import pytest

import gh_puller.codebase.archive as archive_module
from gh_puller.codebase.archive import (
    Archive,
    ArchiveError,
    ArchiveWriter,
    KGACommit,
    KGARecorder,
    RadixTree,
    TreeRef,
    graph_digest,
)
from gh_puller.codebase.store import CoverageCapture, GraphCapture, GraphRows


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


def test_recorder_applies_exact_coverage_snapshots_and_deltas(tmp_path):
    path = tmp_path / "archive.kga"
    recorder = KGARecorder(path)
    project = {
        "label": "Project",
        "name": "p",
        "file_path": "",
        "start_line": 0,
        "end_line": 0,
        "properties": {},
    }
    first_metadata = {
        "project": "p",
        "generation": "generation-1",
        "index_mode": "full",
        "recorded_at": "2026-09-08T00:00:00Z",
        "recording_status": "complete",
        "ignored_files_stored": 1,
        "ignored_files_total": 1,
        "coverage_version": 3,
        "hash_records_complete": True,
    }
    first = recorder.append(
        KGACommit(0, "c1", (), None),
        GraphCapture(
            {"p": project},
            {},
            True,
            "full_snapshot",
            CoverageCapture(
                {
                    ("src/a.py", "parse_partial"): "1-2",
                    ("vendor", "not_indexed_dir"): "excluded subtree",
                },
                first_metadata,
                True,
            ),
        ),
        project="p",
        metadata={},
    )
    second_metadata = {**first_metadata, "generation": "generation-2", "index_mode": "delta"}
    second = recorder.append(
        KGACommit(1, "c2", ("c1",), 1),
        GraphCapture(
            {},
            {},
            False,
            "full_generation",
            CoverageCapture(
                {
                    ("src/a.py", "parse_partial"): "8-9",
                    ("vendor", "not_indexed_dir"): None,
                    ("docs", "not_indexed_dir"): "excluded subtree",
                },
                second_metadata,
                False,
            ),
        ),
        project="p",
        metadata={},
    )
    recorder.finalize()

    with Archive(path) as archive:
        assert archive.load_coverage("c1").rows == {
            ("src/a.py", "parse_partial"): "1-2",
            ("vendor", "not_indexed_dir"): "excluded subtree",
        }
        assert archive.load_coverage("c2").rows == {
            ("docs", "not_indexed_dir"): "excluded subtree",
            ("src/a.py", "parse_partial"): "8-9",
        }
        archive.verify()
    assert first["coverage_rows"] == 2
    assert second["coverage_rows"] == 2
    assert second["changed_coverage_rows"] == 3
    assert second["graph_digest"] == first["graph_digest"]
    assert second["materialization_digest"] != first["materialization_digest"]


def test_recorder_reuses_git_parent_root_across_physical_branch_switch(tmp_path):
    path = tmp_path / "archive.kga"
    recorder = KGARecorder(path)
    left_edge = ("pkg.node", "pkg.left", "CALLS", "")
    right_edge = ("pkg.node", "pkg.right", "CALLS", "")
    base = recorder.append(
        KGACommit(0, "c0", (), None),
        GraphCapture({"pkg.node": {"value": 0}}, {}, True, "full_snapshot"),
        project="p",
        metadata={},
    )
    left = recorder.append(
        KGACommit(1, "c1", ("c0",), 1),
        GraphCapture(
            {"pkg.node": {"value": 1}},
            {left_edge: {"properties": {"branch": "left"}}},
            False,
            "full_generation",
        ),
        project="p",
        metadata={},
    )
    right = recorder.append(
        KGACommit(2, "c2", ("c0",), 1),
        GraphCapture(
            {"pkg.node": {"value": 2}},
            {
                left_edge: None,
                right_edge: {"properties": {"branch": "right"}},
            },
            False,
            "full_generation",
        ),
        project="p",
        metadata={},
    )
    restored = recorder.append(
        KGACommit(3, "c3", ("c1",), 0),
        GraphCapture(
            {"pkg.node": {"value": 1}},
            {
                left_edge: {"properties": {"branch": "left"}},
                right_edge: None,
            },
            False,
            "full_generation",
        ),
        project="p",
        metadata={},
    )
    recorder.finalize()

    assert base["delta_base"] is None
    assert right["delta_base"] == "c0"
    assert restored["delta_base"] == "c1"
    assert restored["node_root"] == left["node_root"]
    assert restored["edge_root"] == left["edge_root"]
    assert restored["pages_written"] == 0
    with Archive(path) as archive:
        assert [archive.load_rows(commit).nodes["pkg.node"]["value"] for commit in archive.commit_ids()] == [
            0,
            1,
            2,
            1,
        ]
        assert [set(archive.load_rows(commit).edges) for commit in archive.commit_ids()] == [
            set(),
            {left_edge},
            {right_edge},
            {left_edge},
        ]


def test_merge_selects_parent_with_smallest_compressed_delta(tmp_path):
    path = tmp_path / "archive.kga"
    recorder = KGARecorder(path)
    recorder.append(
        KGACommit(0, "c0", (), None),
        GraphCapture({"pkg.node": {"value": "base"}}, {}, True, "full_snapshot"),
        project="p",
        metadata={},
    )
    left = recorder.append(
        KGACommit(1, "c1", ("c0",), 1),
        GraphCapture({"pkg.node": {"value": "left"}}, {}, False, "full_generation"),
        project="p",
        metadata={},
    )
    recorder.append(
        KGACommit(2, "c2", ("c0",), 1),
        GraphCapture({"pkg.node": {"value": "right"}}, {}, False, "full_generation"),
        project="p",
        metadata={},
    )
    merge = recorder.append(
        KGACommit(3, "merge", ("c2", "c1"), None),
        GraphCapture({"pkg.node": {"value": "left"}}, {}, False, "full_generation"),
        project="p",
        metadata={"git_parent_changed_files": {"c2": 3, "c1": 1}},
    )
    recorder.finalize()

    assert merge["delta_base"] == "c1"
    assert merge["changed_files"] == 1
    assert merge["delta_base_bytes"] == 0
    assert merge["node_root"] == left["node_root"]


def test_merge_compares_nonzero_precompressed_parent_plans(tmp_path):
    path = tmp_path / "archive.kga"
    recorder = KGARecorder(path)
    first = "alpha.left.node"
    second = "beta.right.node"
    compact = "a" * 16384
    noisy = "".join(sha256(str(index).encode()).hexdigest() for index in range(256))
    recorder.append(
        KGACommit(0, "c0", (), None),
        GraphCapture({first: {"value": "base"}, second: {"value": "base"}}, {}, True, "full_snapshot"),
        project="p",
        metadata={},
    )
    recorder.append(
        KGACommit(1, "c1", ("c0",), 1),
        GraphCapture({first: {"value": compact}}, {}, False, "full_generation"),
        project="p",
        metadata={},
    )
    recorder.append(
        KGACommit(2, "c2", ("c0",), 1),
        GraphCapture(
            {first: {"value": "base"}, second: {"value": noisy}},
            {},
            False,
            "full_generation",
        ),
        project="p",
        metadata={},
    )
    recorder.append(
        KGACommit(3, "side", ("c0",), 2),
        GraphCapture(
            {first: {"value": "side-a"}, second: {"value": "side-b"}},
            {},
            False,
            "full_generation",
        ),
        project="p",
        metadata={},
    )
    merge = recorder.append(
        KGACommit(4, "merge", ("c1", "c2"), None),
        GraphCapture(
            {first: {"value": compact}, second: {"value": noisy}},
            {},
            False,
            "full_generation",
        ),
        project="p",
        metadata={},
    )
    recorder.finalize()

    assert merge["delta_base"] == "c2"
    assert merge["delta_base_bytes"] > 0
    with Archive(path) as archive:
        assert archive.load_rows("merge").nodes == {
            first: {"value": compact},
            second: {"value": noisy},
        }


def test_radix_tree_rejects_duplicate_build_identities(tmp_path):
    writer = ArchiveWriter(tmp_path / "archive.kga")
    with pytest.raises(ArchiveError, match="duplicate nodes identity"):
        RadixTree(writer, "nodes").build([("a", {"v": 1}), ("a", {"v": 2})])
    writer.close_incomplete()


def test_radix_tree_rejects_deleting_absent_identity(tmp_path):
    writer = ArchiveWriter(tmp_path / "archive.kga")
    root = RadixTree(writer, "nodes").build({"a": {"v": 1}}.items())
    with pytest.raises(ArchiveError, match="cannot delete absent nodes identity"):
        RadixTree(writer, "nodes").apply(root, {"b": None})
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


def test_unsupported_magic_is_rejected_without_mutating_the_file(tmp_path):
    path = tmp_path / "archive.kga"
    original = b"not-a-kga-archive"
    path.write_bytes(original)

    with pytest.raises(ArchiveError, match="not a supported archive"):
        ArchiveWriter(path)
    assert path.read_bytes() == original


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


def test_writer_rejects_nonfinite_json(tmp_path):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    with pytest.raises(ValueError, match="Out of range float values"):
        commit_rows(writer, "c1", [], GraphRows({"a": {"value": float("nan")}}, {}))
    writer.close_incomplete()


def test_archive_treats_dangling_edges_as_opaque_graph_rows(tmp_path):
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
    manifest = {
        "sha": "dangling",
        "parents": [],
        "node_root": nodes.to_json(),
        "edge_root": edges.to_json(),
        "nodes": 1,
        "edges": 1,
        "graph_digest": graph_digest(nodes, edges),
        "project": "p",
    }
    writer.commit(manifest)
    writer.finalize()

    with Archive(path) as archive:
        archive.verify()
        assert archive.load_rows("dangling").edges == {
            ("p", "p.missing", "CONTAINS", ""): {"properties": {}},
        }
