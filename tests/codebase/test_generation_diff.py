import os
import sqlite3
from contextlib import closing

from gh_puller.codebase.generation_diff import PinnedGeneration

SCHEMA = """
CREATE TABLE projects(name TEXT PRIMARY KEY);
CREATE TABLE nodes (
 id INTEGER PRIMARY KEY, project TEXT, label TEXT, name TEXT, qualified_name TEXT,
 file_path TEXT, start_line INTEGER, end_line INTEGER, properties TEXT);
CREATE TABLE edges (
 id INTEGER PRIMARY KEY, project TEXT, source_id INTEGER, target_id INTEGER, type TEXT, properties TEXT,
 local_name_gen TEXT GENERATED ALWAYS AS (
   CASE WHEN type='IMPORTS' THEN coalesce(json_extract(properties,'$.local_name'),'') ELSE '' END));
"""


def write_generation(path, node_value, *, include_edge, id_offset=0):
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
    assert changes.nodes["a"]["properties"] == {"v": 2}
    assert changes.edges[("a", "b", "CALLS", "")] is None
    assert "b" not in changes.nodes


def test_diff_preserves_non_utf8_property_bytes(tmp_path):
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
        changes = old.changes_after_publish()
    finally:
        old.close()
    assert changes.nodes["a"]["properties"] == {"_raw_bytes": bad_node.hex()}
    assert changes.edges[("a", "b", "CALLS", "")]["properties"] == {"_raw_bytes": bad_edge.hex()}
