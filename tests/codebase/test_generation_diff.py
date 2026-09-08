import os
import sqlite3
from contextlib import closing

import pytest

from gh_puller.codebase.store import ExtractionError, PinnedGeneration

SCHEMA = """
CREATE TABLE projects(name TEXT PRIMARY KEY);
CREATE TABLE nodes (
 id INTEGER PRIMARY KEY, project TEXT, label TEXT, name TEXT, qualified_name TEXT,
 file_path TEXT, start_line INTEGER, end_line INTEGER, properties TEXT);
CREATE TABLE edges (
 id INTEGER PRIMARY KEY, project TEXT, source_id INTEGER, target_id INTEGER, type TEXT, properties TEXT,
 local_name_gen TEXT GENERATED ALWAYS AS (
   CASE WHEN type='IMPORTS' THEN coalesce(json_extract(properties,'$.local_name'),'') ELSE '' END));
CREATE TABLE index_coverage(
 project TEXT, rel_path TEXT, kind TEXT, detail TEXT,
 PRIMARY KEY(project,rel_path,kind));
CREATE TABLE index_coverage_meta(
 project TEXT PRIMARY KEY, generation TEXT, index_mode TEXT, recorded_at TEXT,
 recording_status TEXT, ignored_files_stored INTEGER, ignored_files_total INTEGER,
 coverage_version INTEGER, hash_records_complete INTEGER);
"""


def write_generation(path, node_value, *, include_edge, id_offset=0, coverage_detail="1-2"):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO projects VALUES ('p')")
    connection.execute("INSERT INTO nodes VALUES (?,'p','File','a','p.a','a.py',1,2,?)", (id_offset + 1, node_value))
    connection.execute("INSERT INTO nodes VALUES (?,'p','File','b','p.b','b.py',1,2,'{}')", (id_offset + 2,))
    if include_edge:
        connection.execute(
            "INSERT INTO edges(id,project,source_id,target_id,type,properties) VALUES (1,'p',?,?, 'CALLS','{}')",
            (id_offset + 1, id_offset + 2),
        )
    connection.execute(
        "INSERT INTO index_coverage VALUES ('p','a.py','parse_partial',?)",
        (coverage_detail,),
    )
    connection.execute(
        "INSERT INTO index_coverage_meta VALUES "
        "('p',?,'delta','2026-09-08T00:00:00Z','complete',0,0,3,1)",
        (f"generation-{id_offset}",),
    )
    connection.commit()
    connection.close()


def test_diff_keeps_old_inode_across_atomic_publish(tmp_path):
    path = tmp_path / "graph.db"
    replacement = tmp_path / "replacement.db"
    write_generation(path, '{"v":1}', include_edge=True)
    old = PinnedGeneration(path, "p")
    write_generation(replacement, '{"v":2}', include_edge=False, id_offset=100)
    os.replace(replacement, path)
    try:
        changes = old.changes_after_publish()
    finally:
        old.close()
    assert changes.nodes["p.a"]["properties"] == {"v": 2}
    assert changes.edges[("p.a", "p.b", "CALLS", "")] is None
    assert "p.b" not in changes.nodes


def test_diff_rejects_non_utf8_property_bytes(tmp_path):
    path = tmp_path / "graph.db"
    replacement = tmp_path / "replacement.db"
    write_generation(path, "{}", include_edge=True)
    old = PinnedGeneration(path, "p")
    write_generation(replacement, "{}", include_edge=True, id_offset=100)
    bad_node = b'{"node":"\xff"}'
    bad_edge = b'{"edge":"\xfe"}'
    with closing(sqlite3.connect(replacement)) as connection, connection:
        connection.execute(
            "UPDATE nodes SET properties=CAST(? AS TEXT) WHERE qualified_name='p.a'",
            (bad_node,),
        )
        connection.execute("UPDATE edges SET properties=CAST(? AS TEXT) WHERE id=1", (bad_edge,))
    os.replace(replacement, path)
    try:
        with pytest.raises(ExtractionError, match="not UTF-8"):
            old.changes_after_publish()
    finally:
        old.close()


def test_diff_preserves_project_like_property_strings(tmp_path):
    path = tmp_path / "graph.db"
    replacement = tmp_path / "replacement.db"
    write_generation(path, "{}", include_edge=False)
    old = PinnedGeneration(path, "p")
    properties = '{"plain":"a","prefixed":"p.a","project":"p","sentinel":"__project__"}'
    write_generation(replacement, properties, include_edge=False, id_offset=100)
    os.replace(replacement, path)
    try:
        changes = old.changes_after_publish()
    finally:
        old.close()
    assert changes.nodes["p.a"]["properties"] == {
        "plain": "a",
        "prefixed": "p.a",
        "project": "p",
        "sentinel": "__project__",
    }


def test_diff_captures_coverage_rows_and_current_metadata(tmp_path):
    path = tmp_path / "graph.db"
    replacement = tmp_path / "replacement.db"
    write_generation(path, "{}", include_edge=False, coverage_detail="1-2")
    old = PinnedGeneration(path, "p")
    write_generation(
        replacement,
        "{}",
        include_edge=False,
        id_offset=100,
        coverage_detail="8-9",
    )
    os.replace(replacement, path)
    try:
        changes = old.changes_after_publish()
    finally:
        old.close()

    assert changes.coverage is not None
    assert changes.coverage.snapshot is False
    assert changes.coverage.rows == {("a.py", "parse_partial"): "8-9"}
    assert changes.coverage.metadata["generation"] == "generation-100"
