"""Verify the exact CBM SQLite extraction contract used by archives."""

import sqlite3

import pytest

from gh_puller.codebase.store import (
    CoverageSnapshot,
    ExtractionError,
    GraphRows,
    load_coverage,
    load_rows,
    validate_coverage,
    validate_rows,
)

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


def write_store(path, properties):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO projects VALUES ('p')")
    connection.execute("INSERT INTO nodes VALUES (1,'p','Project','p','p','',0,0,'{}')")
    connection.execute(
        "INSERT INTO nodes VALUES (2,'p','Function','a','p.a','a.py',1,2,?)",
        (properties,),
    )
    connection.execute(
        "INSERT INTO edges(id,project,source_id,target_id,type,properties) "
        "VALUES (1,'p',1,2,'CONTAINS','{\"qualified_name\":\"p.a\"}')",
    )
    connection.commit()
    connection.close()


def test_load_rows_preserves_full_names_and_opaque_strings(tmp_path):
    path = tmp_path / "graph.db"
    write_store(
        path,
        '{"plain":"a","prefixed":"p.a","project":"p","sentinel":"__project__"}',
    )

    rows = load_rows(path, "p")

    assert set(rows.nodes) == {"p", "p.a"}
    assert rows.nodes["p.a"]["properties"] == {
        "plain": "a",
        "prefixed": "p.a",
        "project": "p",
        "sentinel": "__project__",
    }
    assert rows.edges == {
        ("p", "p.a", "CONTAINS", ""): {"properties": {"qualified_name": "p.a"}},
    }
    validate_rows(rows, "p")


def test_load_rows_rejects_ambiguous_property_json(tmp_path):
    path = tmp_path / "graph.db"
    write_store(path, '{"duplicate":1,"duplicate":2}')

    with pytest.raises(ExtractionError, match="not exact JSON"):
        load_rows(path, "p")


@pytest.mark.parametrize(
    ("column", "message"),
    [
        ("file_path", "invalid file path"),
        ("start_line", "invalid source lines"),
        ("end_line", "invalid source lines"),
        ("properties", "unsupported type NoneType"),
    ],
)
def test_load_rows_rejects_null_fields_instead_of_filling(tmp_path, column, message):
    path = tmp_path / "graph.db"
    write_store(path, "{}")
    with sqlite3.connect(path) as connection, connection:
        connection.execute(
            f"UPDATE nodes SET {column}=NULL WHERE qualified_name='p.a'",  # noqa: S608 - fixed columns.
        )

    with pytest.raises(ExtractionError, match=message):
        load_rows(path, "p")


def test_load_rows_rejects_invalid_stored_edge_endpoints(tmp_path):
    path = tmp_path / "graph.db"
    write_store(path, "{}")
    with sqlite3.connect(path) as connection, connection:
        connection.execute("DELETE FROM nodes WHERE qualified_name='p.a'")

    with pytest.raises(ExtractionError, match="1 invalid edge endpoints"):
        load_rows(path, "p")


def test_validate_rows_rejects_edge_identity_drift():
    node = {
        "label": "Project",
        "name": "p",
        "file_path": "",
        "start_line": 0,
        "end_line": 0,
        "properties": {},
    }
    rows = GraphRows(
        {"p": node},
        {("p", "p", "IMPORTS", "recorded"): {"properties": {"local_name": "actual"}}},
    )

    with pytest.raises(ExtractionError, match="inconsistent identity"):
        validate_rows(rows, "p")


def test_load_coverage_preserves_rows_and_generation_metadata(tmp_path):
    path = tmp_path / "graph.db"
    write_store(path, "{}")
    with sqlite3.connect(path) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE index_coverage(
              project TEXT,rel_path TEXT,kind TEXT,detail TEXT,
              PRIMARY KEY(project,rel_path,kind));
            CREATE TABLE index_coverage_meta(
              project TEXT PRIMARY KEY,generation TEXT,index_mode TEXT,recorded_at TEXT,
              recording_status TEXT,ignored_files_stored INTEGER,ignored_files_total INTEGER,
              coverage_version INTEGER,hash_records_complete INTEGER);
            """,
        )
        connection.execute(
            "INSERT INTO index_coverage VALUES ('p','src/a.py','parse_partial','4-8,10')",
        )
        connection.execute(
            "INSERT INTO index_coverage_meta VALUES "
            "('p','generation-1','delta','2026-09-08T00:00:00Z','complete',2,3,3,1)",
        )

    coverage = load_coverage(path, "p")

    assert coverage == CoverageSnapshot(
        {("src/a.py", "parse_partial"): "4-8,10"},
        {
            "project": "p",
            "generation": "generation-1",
            "index_mode": "delta",
            "recorded_at": "2026-09-08T00:00:00Z",
            "recording_status": "complete",
            "ignored_files_stored": 2,
            "ignored_files_total": 3,
            "coverage_version": 3,
            "hash_records_complete": True,
        },
    )
    validate_coverage(coverage, "p")
