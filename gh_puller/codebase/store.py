"""Extract exact, portable graph rows from a CBM SQLite store.

Production rows retain full qualified names and treat properties as opaque JSON
objects. The archive and generation diff depend on this module for their shared
row-value contract.
"""

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
    """Attribute-exact node and parallel-edge rows keyed by full qualified names."""

    nodes: dict[str, dict]
    edges: dict[EdgeKey, dict]

    def __len__(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)


def _reject_constant(value: str):
    raise ValueError(f"non-finite JSON number {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON property {key!r}")
        result[key] = value
    return result


def _parse_properties(value) -> dict:
    """Decode one standards-compliant JSON object without changing its strings."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExtractionError("CBM properties are not UTF-8") from exc
    if not isinstance(value, str):
        raise ExtractionError(f"CBM properties have unsupported type {type(value).__name__}")
    try:
        parsed = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExtractionError("CBM properties are not exact JSON") from exc
    if not isinstance(parsed, dict):
        raise ExtractionError("CBM properties must be a JSON object")
    return parsed


def _node_attributes(label, name, file_path, start_line, end_line, properties) -> dict:
    return {
        "label": label,
        "name": name,
        "file_path": file_path or "",
        "start_line": start_line or 0,
        "end_line": end_line or 0,
        "properties": _parse_properties(properties),
    }


def _edge_attributes(properties) -> dict:
    return {"properties": _parse_properties(properties)}


def _project(connection: sqlite3.Connection, project: str) -> str:
    row = connection.execute("SELECT name FROM projects WHERE name = ?", (project,)).fetchone()
    if row is None:
        raise ExtractionError(f"project {project!r} not found")
    return row[0]


def validate_rows(rows: GraphRows, project: str) -> None:
    """Validate the graph invariants required by exact CBM restoration.

    Args:
        rows: Complete graph snapshot to validate.
        project: Original CBM project name, which must identify the project root.

    Raises:
        ExtractionError: A row has an unsupported shape, invalid value, or missing
            edge endpoint.
    """
    if not project or "\0" in project:
        raise ExtractionError("invalid project identity")
    root = rows.nodes.get(project)
    if not isinstance(root, dict) or root.get("label") != "Project":
        raise ExtractionError(f"project root {project!r} is absent")
    for qualified_name, value in rows.nodes.items():
        if not isinstance(qualified_name, str) or "\0" in qualified_name or not isinstance(value, dict):
            raise ExtractionError("invalid node row")
        if not isinstance(value.get("label"), str) or not isinstance(value.get("name"), str):
            raise ExtractionError(f"node {qualified_name!r} has invalid identity fields")
        if not isinstance(value.get("file_path"), str):
            raise ExtractionError(f"node {qualified_name!r} has an invalid file path")
        strings = (value["label"], value["name"], value["file_path"])
        if any("\0" in item for item in strings):
            raise ExtractionError(f"node {qualified_name!r} contains NUL")
        lines = (value.get("start_line"), value.get("end_line"))
        if any(type(item) is not int or not -(1 << 31) <= item < (1 << 31) for item in lines):
            raise ExtractionError(f"node {qualified_name!r} has invalid source lines")
        if not isinstance(value.get("properties"), dict):
            raise ExtractionError(f"node {qualified_name!r} has invalid properties")
        try:
            json.dumps(value["properties"], allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ExtractionError(f"node {qualified_name!r} has non-JSON properties") from exc
    missing = set()
    for key, value in rows.edges.items():
        if not isinstance(key, tuple) or len(key) != 4:
            raise ExtractionError("invalid edge key")
        source, target, edge_type, local_name = key
        if not all(isinstance(item, str) and "\0" not in item for item in key):
            raise ExtractionError("invalid edge identity")
        if source not in rows.nodes:
            missing.add(source)
        if target not in rows.nodes:
            missing.add(target)
        if not isinstance(value, dict) or not isinstance(value.get("properties"), dict):
            identity = (source, target, edge_type, local_name)
            raise ExtractionError(f"edge {identity!r} has invalid properties")
        expected_local = value["properties"].get("local_name", "") if edge_type == "IMPORTS" else ""
        if not isinstance(expected_local, str) or local_name != expected_local:
            raise ExtractionError(f"edge {(source, target, edge_type, local_name)!r} has inconsistent identity")
        try:
            json.dumps(value["properties"], allow_nan=False)
        except (TypeError, ValueError) as exc:
            identity = (source, target, edge_type, local_name)
            raise ExtractionError(f"edge {identity!r} has non-JSON properties") from exc
    if missing:
        raise ExtractionError(f"graph has {len(missing)} missing edge endpoints: {sorted(missing)[:3]}")


def iter_nodes(db_path: str | Path, project: str):
    """Yield exact node rows in deterministic full-qualified-name order."""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    try:
        proj = _project(connection, project)
        query = (
            "SELECT qualified_name,label,name,file_path,start_line,end_line,CAST(properties AS BLOB) "
            "FROM nodes WHERE project=? ORDER BY qualified_name"
        )
        for fqn, label, name, file_path, start_line, end_line, properties in connection.execute(query, (proj,)):
            yield (
                fqn,
                _node_attributes(label, name, file_path, start_line, end_line, properties),
            )
    finally:
        connection.close()


def iter_edges(db_path: str | Path, project: str):
    """Yield exact parallel-edge rows in deterministic identity order."""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    try:
        proj = _project(connection, project)
        columns = {row[1] for row in connection.execute("PRAGMA table_xinfo(edges)")}
        local = "e.local_name_gen" if "local_name_gen" in columns else "''"
        query = (
            f"SELECT s.qualified_name,t.qualified_name,e.type,{local},CAST(e.properties AS BLOB) "  # noqa: S608
            "FROM edges e JOIN nodes s ON s.id=e.source_id JOIN nodes t ON t.id=e.target_id "
            "WHERE e.project=? ORDER BY s.qualified_name,t.qualified_name,e.type,4"
        )
        for source, target, edge_type, local_name, properties in connection.execute(query, (proj,)):
            key: EdgeKey = (source, target, edge_type, local_name or "")
            yield key, _edge_attributes(properties)
    finally:
        connection.close()


def load_rows(db_path: str | Path, project: str) -> GraphRows:
    """Load the exact representation used by production archives."""
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
