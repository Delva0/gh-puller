import gh_puller.codebase.archive as archive_module
from gh_puller.codebase.archive import Archive, ArchiveWriter, RadixTree, TreeRef, graph_digest
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
    monkeypatch.setattr(archive_module, "_scan", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError))
    assert len(Archive(path)) == 1


def test_complete_archive_extension_uses_footer_and_verifies_only_append(tmp_path, monkeypatch):
    path = tmp_path / "archive.kga"
    writer = ArchiveWriter(path)
    old_root, _, _ = commit_rows(writer, "c1", [], GraphRows({"a": {"v": 1}}, {}))
    writer.finalize()

    monkeypatch.setattr(archive_module, "_scan", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError))
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

    monkeypatch.setattr(archive_module, "_scan", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError))
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

    monkeypatch.setattr(archive_module, "_scan", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError))
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
