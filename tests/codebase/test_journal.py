"""Exercise exact expansion of CBM native change-journal candidates."""

import os
import shutil
import sqlite3

import pytest

from gh_puller.codebase.generation_diff import PinnedGeneration
from gh_puller.codebase.journal import JournalUnavailableError, available, candidate_counts, changes_after_publish

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE projects(name TEXT PRIMARY KEY);
CREATE TABLE nodes (
 id INTEGER PRIMARY KEY, project TEXT, label TEXT, name TEXT, qualified_name TEXT,
 file_path TEXT, start_line INTEGER, end_line INTEGER, properties TEXT);
CREATE UNIQUE INDEX nodes_project_qn ON nodes(project, qualified_name);
CREATE TABLE edges (
 id INTEGER PRIMARY KEY, project TEXT, source_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
 target_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE, type TEXT, properties TEXT,
 local_name_gen TEXT GENERATED ALWAYS AS (
   CASE WHEN type='IMPORTS' THEN coalesce(json_extract(properties,'$.local_name'),'') ELSE '' END));
CREATE UNIQUE INDEX edges_identity ON edges(source_id,target_id,type,local_name_gen);
"""

JOURNAL_SCHEMA = """
CREATE TABLE cbm_delta_change_journal (
 project TEXT PRIMARY KEY, format_version INTEGER NOT NULL,
 node_count INTEGER NOT NULL, edge_count INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE cbm_delta_changed_nodes (
 project TEXT NOT NULL, node_id INTEGER NOT NULL,
 PRIMARY KEY(project,node_id)) WITHOUT ROWID;
CREATE TABLE cbm_delta_changed_edges (
 project TEXT NOT NULL, edge_id INTEGER NOT NULL,
 PRIMARY KEY(project,edge_id)) WITHOUT ROWID;
"""


def write_generation(path):
    """Create the minimal graph generation used by each journal test."""
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO projects VALUES ('p')")
    connection.execute("INSERT INTO nodes VALUES (1,'p','File','a','p.a','a.py',1,2,'{}')")
    connection.execute("INSERT INTO nodes VALUES (2,'p','File','b','p.b','b.py',1,2,'{}')")
    connection.execute(
        "INSERT INTO edges(id,project,source_id,target_id,type,properties) VALUES (1,'p',1,2,'CALLS','{}')",
    )
    connection.commit()
    connection.close()


def write_journal(connection, *, node_ids, edge_ids):
    """Persist the native journal shape emitted by CBM after a delta."""
    connection.executescript(JOURNAL_SCHEMA)
    connection.executemany("INSERT INTO cbm_delta_changed_nodes VALUES ('p',?)", ((value,) for value in node_ids))
    connection.executemany("INSERT INTO cbm_delta_changed_edges VALUES ('p',?)", ((value,) for value in edge_ids))
    connection.execute(
        "INSERT INTO cbm_delta_change_journal VALUES ('p',1,?,?)",
        (len(node_ids), len(edge_ids)),
    )


def test_native_journal_matches_full_generation_diff(tmp_path):
    """Expand changed row ids, including edges affected only by a node rename."""
    path = tmp_path / "graph.db"
    replacement = tmp_path / "replacement.db"
    write_generation(path)
    journal_old = PinnedGeneration(path, "p")
    full_old = PinnedGeneration(path, "p")
    shutil.copy2(path, replacement)
    with sqlite3.connect(replacement) as connection:
        connection.execute("UPDATE nodes SET label='Class' WHERE id=1")
        connection.execute("UPDATE nodes SET qualified_name='p.c' WHERE id=2")
        connection.execute("INSERT INTO nodes VALUES (3,'p','File','d','p.d','d.py',1,2,'{}')")
        connection.execute(
            "INSERT INTO edges(id,project,source_id,target_id,type,properties) VALUES (2,'p',3,1,'CALLS','{}')",
        )
        write_journal(connection, node_ids=(1, 2, 3), edge_ids=(2,))
    os.replace(replacement, path)
    try:
        journal = changes_after_publish(journal_old)
        full = full_old.changes_after_publish()
    finally:
        journal_old.close()
        full_old.close()
    assert journal == full
    assert set(journal.nodes) == {"a", "b", "c", "d"}
    assert set(journal.edges) == {
        ("a", "b", "CALLS", ""),
        ("a", "c", "CALLS", ""),
        ("d", "a", "CALLS", ""),
    }
    assert available(path, "p")
    assert candidate_counts(path, "p") == {"nodes": 3, "edges": 1}


def test_journal_exact_filter_removes_noop_candidates_and_captures_deletes(tmp_path):
    """Keep row-id capture cheap by filtering no-op values while reading."""
    path = tmp_path / "graph.db"
    replacement = tmp_path / "replacement.db"
    write_generation(path)
    old = PinnedGeneration(path, "p")
    shutil.copy2(path, replacement)
    with sqlite3.connect(replacement) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("UPDATE nodes SET label=label WHERE id=2")
        connection.execute("DELETE FROM nodes WHERE id=1")
        write_journal(connection, node_ids=(1, 2), edge_ids=(1,))
    os.replace(replacement, path)
    try:
        changes = changes_after_publish(old)
    finally:
        old.close()
    assert set(changes.nodes) == {"a"}
    assert changes.nodes["a"] is None
    assert changes.edges == {("a", "b", "CALLS", ""): None}


def test_journal_preserves_non_utf8_property_bytes(tmp_path):
    path = tmp_path / "graph.db"
    replacement = tmp_path / "replacement.db"
    write_generation(path)
    old = PinnedGeneration(path, "p")
    shutil.copy2(path, replacement)
    bad_node = b'{"node":"\xff"}'
    bad_edge = b'{"edge":"\xfe"}'
    with sqlite3.connect(replacement) as connection:
        connection.execute("UPDATE nodes SET properties=CAST(? AS TEXT) WHERE id=1", (bad_node,))
        connection.execute("UPDATE edges SET properties=CAST(? AS TEXT) WHERE id=1", (bad_edge,))
        write_journal(connection, node_ids=(1,), edge_ids=(1,))
    os.replace(replacement, path)
    try:
        changes = changes_after_publish(old)
    finally:
        old.close()
    assert changes.nodes["a"]["properties"] == {"_raw_bytes": bad_node.hex()}
    assert changes.edges[("a", "b", "CALLS", "")]["properties"] == {"_raw_bytes": bad_edge.hex()}


def test_fresh_full_generation_reports_journal_unavailable(tmp_path):
    """Leave full generations on the established exact-diff fallback."""
    path = tmp_path / "graph.db"
    replacement = tmp_path / "replacement.db"
    write_generation(path)
    old = PinnedGeneration(path, "p")
    write_generation(replacement)
    os.replace(replacement, path)
    try:
        assert not available(path, "p")
        with pytest.raises(JournalUnavailableError):
            changes_after_publish(old)
    finally:
        old.close()


def test_incomplete_journal_is_unavailable(tmp_path):
    """Reject a partially persisted candidate set instead of missing changes."""
    path = tmp_path / "graph.db"
    write_generation(path)
    with sqlite3.connect(path) as connection:
        write_journal(connection, node_ids=(1,), edge_ids=())
        connection.execute("UPDATE cbm_delta_change_journal SET node_count=2")
    assert not available(path, "p")
