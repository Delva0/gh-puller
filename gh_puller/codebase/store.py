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


def _validate_node(qualified_name: object, value: object) -> None:
    if not isinstance(qualified_name, str) or not qualified_name or "\0" in qualified_name:
        raise ExtractionError("CBM node has an invalid qualified name")
    if not isinstance(value, dict):
        raise ExtractionError(f"node {qualified_name!r} has invalid attributes")
    label, name, file_path = value.get("label"), value.get("name"), value.get("file_path")
    if not isinstance(label, str) or not label or not isinstance(name, str):
        raise ExtractionError(f"node {qualified_name!r} has invalid identity fields")
    if not isinstance(file_path, str):
        raise ExtractionError(f"node {qualified_name!r} has an invalid file path")
    if any("\0" in item for item in (label, name, file_path)):
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


def _node_row(qualified_name, label, name, file_path, start_line, end_line, properties):
    value = {
        "label": label,
        "name": name,
        "file_path": file_path,
        "start_line": start_line,
        "end_line": end_line,
        "properties": _parse_properties(properties),
    }
    _validate_node(qualified_name, value)
    return qualified_name, value


def _validate_edge(key: object, value: object) -> None:
    if not isinstance(key, tuple) or len(key) != 4:
        raise ExtractionError("invalid edge key")
    source, target, edge_type, local_name = key
    if not all(isinstance(item, str) and "\0" not in item for item in key):
        raise ExtractionError("invalid edge identity")
    if not source or not target or not edge_type:
        raise ExtractionError("invalid edge identity")
    if not isinstance(value, dict) or not isinstance(value.get("properties"), dict):
        raise ExtractionError(f"edge {key!r} has invalid properties")
    expected_local = value["properties"].get("local_name", "") if edge_type == "IMPORTS" else ""
    if not isinstance(expected_local, str) or local_name != expected_local:
        raise ExtractionError(f"edge {key!r} has inconsistent identity")
    try:
        json.dumps(value["properties"], allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ExtractionError(f"edge {key!r} has non-JSON properties") from exc


def _edge_row(source, target, edge_type, local_name, properties):
    key: EdgeKey = (source, target, edge_type, local_name)
    value = {"properties": _parse_properties(properties)}
    _validate_edge(key, value)
    return key, value


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
    if not isinstance(project, str) or not project or "\0" in project:
        raise ExtractionError("invalid project identity")
    root = rows.nodes.get(project)
    if not isinstance(root, dict) or root.get("label") != "Project":
        raise ExtractionError(f"project root {project!r} is absent")
    for qualified_name, value in rows.nodes.items():
        _validate_node(qualified_name, value)
    missing = set()
    for key, value in rows.edges.items():
        _validate_edge(key, value)
        source, target, *_ = key
        if source not in rows.nodes:
            missing.add(source)
        if target not in rows.nodes:
            missing.add(target)
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
        previous = None
        for fqn, label, name, file_path, start_line, end_line, properties in connection.execute(query, (proj,)):
            row = _node_row(
                fqn,
                label,
                name,
                file_path,
                start_line,
                end_line,
                properties,
            )
            if fqn == previous:
                raise ExtractionError(f"CBM node identity is duplicated: {fqn!r}")
            previous = fqn
            yield row
    finally:
        connection.close()


def iter_edges(db_path: str | Path, project: str):
    """Yield exact parallel-edge rows in deterministic identity order."""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    try:
        proj = _project(connection, project)
        columns = {row[1] for row in connection.execute("PRAGMA table_xinfo(edges)")}
        if "local_name_gen" not in columns:
            raise ExtractionError("CBM edges lack exact local identities")
        invalid_endpoints = connection.execute(
            "SELECT count(*) FROM edges e "
            "LEFT JOIN nodes s ON s.id=e.source_id LEFT JOIN nodes t ON t.id=e.target_id "
            "WHERE e.project=? AND (s.id IS NULL OR t.id IS NULL OR s.project IS NOT e.project "
            "OR t.project IS NOT e.project)",
            (proj,),
        ).fetchone()[0]
        if invalid_endpoints:
            raise ExtractionError(f"CBM store has {invalid_endpoints} invalid edge endpoints")
        query = (
            "SELECT s.qualified_name,t.qualified_name,e.type,e.local_name_gen,CAST(e.properties AS BLOB) "
            "FROM edges e JOIN nodes s ON s.id=e.source_id JOIN nodes t ON t.id=e.target_id "
            "WHERE e.project=? ORDER BY s.qualified_name,t.qualified_name,e.type,4"
        )
        previous = None
        for source, target, edge_type, local_name, properties in connection.execute(query, (proj,)):
            row = _edge_row(source, target, edge_type, local_name, properties)
            if row[0] == previous:
                raise ExtractionError(f"CBM edge identity is duplicated: {row[0]!r}")
            previous = row[0]
            yield row
    finally:
        connection.close()


def load_rows(db_path: str | Path, project: str) -> GraphRows:
    """Load the exact representation used by production archives."""
    return GraphRows(nodes=dict(iter_nodes(db_path, project)), edges=dict(iter_edges(db_path, project)))


def rows_to_snapshot(rows: GraphRows) -> SnapshotGraph:
    """Convert extracted rows to the archive's exact graph value."""
    return SnapshotGraph(
        nodes=frozenset(rows.nodes),
        edges=frozenset(rows.edges),
        node_attrs={key: dict(value) for key, value in rows.nodes.items()},
        edge_attrs={key: dict(value) for key, value in rows.edges.items()},
    )
