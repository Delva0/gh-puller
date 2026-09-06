"""Exact and streaming extraction from a CBM SQLite graph store."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .graph import EdgeKey, SnapshotGraph

if TYPE_CHECKING:
    from pathlib import Path


class ExtractionError(Exception):
    """The requested project graph cannot be extracted from a store."""


@dataclass(frozen=True, slots=True)
class GraphRows:
    """Attribute-exact normalized node and parallel-edge rows."""

    nodes: dict[str, dict]
    edges: dict[EdgeKey, dict]

    def __len__(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)


def _parse_properties(value):
    """Decode CBM's JSON property column while preserving malformed values."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return {"_raw_bytes": value.hex()}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"_raw": value}
        return parsed if isinstance(parsed, dict) else {"_raw": parsed}
    return {"_raw": value}


def _normalize(value, project: str):
    """Remove generation-specific project prefixes recursively."""
    prefix = f"{project}."
    if isinstance(value, str):
        if value == project:
            return "__project__"
        return value.removeprefix(prefix)
    if isinstance(value, list):
        return [_normalize(item, project) for item in value]
    if isinstance(value, dict):
        return {key: _normalize(item, project) for key, item in value.items()}
    return value


def _project(connection: sqlite3.Connection, project: str) -> str:
    row = connection.execute("SELECT name FROM projects WHERE name = ?", (project,)).fetchone()
    if row is None:
        raise ExtractionError(f"project {project!r} not found")
    return row[0]


def iter_nodes(db_path: str | Path, project: str):
    """Yield normalized node rows in deterministic key order."""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    try:
        proj = _project(connection, project)
        prefix = f"{proj}."
        query = (
            "SELECT qualified_name,label,name,file_path,start_line,end_line,CAST(properties AS BLOB) "
            "FROM nodes WHERE project=? "
            "ORDER BY CASE WHEN qualified_name=? THEN '__project__' "
            "WHEN substr(qualified_name,1,?)=? "
            "THEN substr(qualified_name,?) ELSE qualified_name END"
        )
        params = (proj, proj, len(prefix), prefix, len(prefix) + 1)
        for fqn, label, name, file_path, start_line, end_line, properties in connection.execute(query, params):
            yield (
                _normalize(fqn, proj),
                _normalize(
                    {
                        "label": label,
                        "name": name,
                        "file_path": file_path or "",
                        "start_line": start_line or 0,
                        "end_line": end_line or 0,
                        "properties": _parse_properties(properties),
                    },
                    proj,
                ),
            )
    finally:
        connection.close()


def iter_edges(db_path: str | Path, project: str):
    """Yield normalized parallel-edge rows in deterministic key order."""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    try:
        proj = _project(connection, project)
        prefix = f"{proj}."
        columns = {row[1] for row in connection.execute("PRAGMA table_xinfo(edges)")}
        local = "e.local_name_gen" if "local_name_gen" in columns else "''"
        query = (
            f"SELECT s.qualified_name,t.qualified_name,e.type,{local},CAST(e.properties AS BLOB) "  # noqa: S608
            "FROM edges e JOIN nodes s ON s.id=e.source_id JOIN nodes t ON t.id=e.target_id "
            "WHERE e.project=? "
            "ORDER BY CASE WHEN s.qualified_name=? THEN '__project__' "
            "WHEN substr(s.qualified_name,1,?)=? THEN substr(s.qualified_name,?) "
            "ELSE s.qualified_name END, "
            "CASE WHEN t.qualified_name=? THEN '__project__' "
            "WHEN substr(t.qualified_name,1,?)=? THEN substr(t.qualified_name,?) "
            "ELSE t.qualified_name END,e.type,4"
        )
        params = (
            proj,
            proj,
            len(prefix),
            prefix,
            len(prefix) + 1,
            proj,
            len(prefix),
            prefix,
            len(prefix) + 1,
        )
        for source, target, edge_type, local_name, properties in connection.execute(query, params):
            key: EdgeKey = (
                _normalize(source, proj),
                _normalize(target, proj),
                edge_type,
                local_name or "",
            )
            yield key, _normalize({"properties": _parse_properties(properties)}, proj)
    finally:
        connection.close()


def load_rows(db_path: str | Path, project: str) -> GraphRows:
    """Load the normalized representation used by production archives."""
    return GraphRows(nodes=dict(iter_nodes(db_path, project)), edges=dict(iter_edges(db_path, project)))


def load_store_rows(db_path: str | Path, project: str) -> GraphRows:
    """Load the historical fidelity-oracle representation of a project graph.

    This intentionally preserves the oracle normalization used by the recorded
    experiments. Production archive extraction uses :func:`load_rows`.
    """
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
    except sqlite3.Error as exc:
        raise ExtractionError(f"cannot open store {db_path} read-only: {exc}") from exc
    try:
        proj = _project(connection, project)
        node_rows = connection.execute(
            "SELECT qualified_name,label,name,file_path,start_line,end_line,CAST(properties AS BLOB) "
            "FROM nodes WHERE project=? ORDER BY qualified_name",
            (proj,),
        ).fetchall()
        columns = {row[1] for row in connection.execute("PRAGMA table_xinfo(edges)")}
        local = "e.local_name_gen" if "local_name_gen" in columns else "''"
        edge_rows = connection.execute(
            f"SELECT s.qualified_name,t.qualified_name,e.type,{local},CAST(e.properties AS BLOB) "  # noqa: S608
            "FROM edges e JOIN nodes s ON s.id=e.source_id JOIN nodes t ON t.id=e.target_id "
            "WHERE e.project=? ORDER BY s.qualified_name,t.qualified_name,e.type,4",
            (proj,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ExtractionError(f"store query failed: {exc}") from exc
    finally:
        connection.close()

    prefix = f"{proj}."

    def strip_fqn(value: str) -> str:
        return value.removeprefix(prefix)

    nodes = {
        strip_fqn(fqn): {
            "label": label,
            "name": name,
            "file_path": file_path or "",
            "start_line": start_line or 0,
            "end_line": end_line or 0,
            "properties": _parse_properties(properties),
        }
        for fqn, label, name, file_path, start_line, end_line, properties in node_rows
    }
    edges = {
        (strip_fqn(source), strip_fqn(target), edge_type, local_name or ""): {
            "properties": _parse_properties(properties),
        }
        for source, target, edge_type, local_name, properties in edge_rows
    }
    return GraphRows(nodes, edges)


def rows_to_snapshot(rows: GraphRows) -> SnapshotGraph:
    """Convert extracted rows to the archive's exact graph value."""
    return SnapshotGraph(
        nodes=frozenset(rows.nodes),
        edges=frozenset(rows.edges),
        node_attrs={key: dict(value) for key, value in rows.nodes.items()},
        edge_attrs={key: dict(value) for key, value in rows.edges.items()},
    )
