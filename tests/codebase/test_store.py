"""Verify the exact CBM SQLite extraction contract used by archives."""

import sqlite3

import pytest

from gh_puller.codebase.store import ExtractionError, GraphRows, load_rows, validate_rows

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
