"""Read exact graph snapshots and changes from published CBM SQLite stores.

Production rows retain full qualified names and treat properties as opaque JSON
objects. Full extraction and POSIX pinned-generation comparison expose the same
row-value contract to the archive recorder without interpreting CBM build routes.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .graph import EdgeKey, SnapshotGraph

# --- Exact row contract ---


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


# --- Published generation capture ---


@dataclass(frozen=True)
class ChangeSet:
    """Exact node and edge mutations between two published generations."""

    nodes: dict[str, dict | None]
    edges: dict[tuple, dict | None]

    @property
    def count(self) -> int:
        """Return the total number of changed graph rows."""
        return len(self.nodes) + len(self.edges)


class PinnedGeneration:
    """Keep one POSIX SQLite inode readable across atomic publication."""

    def __init__(self, db_path: str | Path, project: str):
        """Open a transaction against the generation currently at ``db_path``.

        Args:
            db_path: Published database pathname that CBM replaces atomically.
            project: Exact project identity required in the pinned generation.
        """
        if os.name != "posix":
            raise OSError("pinned-generation diff requires POSIX atomic rename semantics")
        self.db_path = Path(db_path)
        self.project = project
        self.connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=60)
        self.connection.execute("BEGIN")
        if self.connection.execute("SELECT 1 FROM projects WHERE name=?", (project,)).fetchone() is None:
            self.connection.close()
            raise sqlite3.OperationalError(f"project {project!r} is absent from previous generation")

    def close(self) -> None:
        """Release the pinned read transaction and old SQLite inode."""
        self.connection.close()

    def changes_after_publish(self) -> ChangeSet:
        """Return exact row mutations after CBM replaces the database pathname."""
        connection = self.connection
        connection.execute("ATTACH DATABASE ? AS current", (str(self.db_path),))
        node_keys = [
            row[0]
            for row in connection.execute(
                """
            SELECT old.qualified_name
              FROM main.nodes old
              LEFT JOIN current.nodes new
                ON new.project=old.project AND new.qualified_name=old.qualified_name
             WHERE old.project=? AND (
                   new.id IS NULL OR old.label IS NOT new.label OR old.name IS NOT new.name
                   OR old.file_path IS NOT new.file_path OR old.start_line IS NOT new.start_line
                   OR old.end_line IS NOT new.end_line OR old.properties IS NOT new.properties)
            UNION ALL
            SELECT new.qualified_name
              FROM current.nodes new
              LEFT JOIN main.nodes old
                ON old.project=new.project AND old.qualified_name=new.qualified_name
             WHERE new.project=? AND old.id IS NULL
            """,
                (self.project, self.project),
            )
        ]
        connection.execute("CREATE TEMP TABLE changed_nodes(qualified_name TEXT PRIMARY KEY)")
        connection.executemany("INSERT INTO changed_nodes VALUES (?)", ((key,) for key in node_keys))
        nodes = {}
        for old_key, node_id, label, name, file_path, start_line, end_line, properties in connection.execute(
            """
            SELECT k.qualified_name,n.id,n.label,n.name,n.file_path,n.start_line,n.end_line,
                   CAST(n.properties AS BLOB)
              FROM changed_nodes k
              LEFT JOIN current.nodes n ON n.project=? AND n.qualified_name=k.qualified_name
            """,
            (self.project,),
        ):
            key = old_key
            nodes[key] = (
                None
                if node_id is None
                else _node_row(
                    key,
                    label,
                    name,
                    file_path,
                    start_line,
                    end_line,
                    properties,
                )[1]
            )

        # Node IDs are generation-local. Materialize their exact FQN mapping once,
        # then both edge directions can use SQLite's integer-key unique indexes.
        connection.execute("CREATE TEMP TABLE node_map(old_id INTEGER PRIMARY KEY,new_id INTEGER UNIQUE)")
        connection.execute(
            """
            INSERT INTO node_map
            SELECT old.id,new.id FROM main.nodes old
              JOIN current.nodes new
                ON new.project=old.project AND new.qualified_name=old.qualified_name
             WHERE old.project=?
            """,
            (self.project,),
        )
        edge_rows = list(
            connection.execute(
                """
            SELECT source.qualified_name,target.qualified_name,old.type,old.local_name_gen
              FROM main.edges old
              JOIN main.nodes source ON source.id=old.source_id
              JOIN main.nodes target ON target.id=old.target_id
              LEFT JOIN node_map source_map ON source_map.old_id=old.source_id
              LEFT JOIN node_map target_map ON target_map.old_id=old.target_id
              LEFT JOIN current.edges new
                ON new.source_id=source_map.new_id AND new.target_id=target_map.new_id
               AND new.type=old.type
               AND new.local_name_gen=old.local_name_gen
             WHERE old.project=? AND (new.id IS NULL OR old.properties IS NOT new.properties)
            UNION ALL
            SELECT source.qualified_name,target.qualified_name,new.type,new.local_name_gen
              FROM current.edges new
              JOIN current.nodes source ON source.id=new.source_id
              JOIN current.nodes target ON target.id=new.target_id
              LEFT JOIN node_map source_map ON source_map.new_id=new.source_id
              LEFT JOIN node_map target_map ON target_map.new_id=new.target_id
              LEFT JOIN main.edges old
                ON old.source_id=source_map.old_id AND old.target_id=target_map.old_id
               AND old.type=new.type
               AND old.local_name_gen=new.local_name_gen
             WHERE new.project=? AND old.id IS NULL
            """,
                (self.project, self.project),
            ),
        )
        connection.execute(
            "CREATE TEMP TABLE changed_edges(source TEXT,target TEXT,type TEXT,local TEXT,"
            "PRIMARY KEY(source,target,type,local))",
        )
        connection.executemany("INSERT INTO changed_edges VALUES (?,?,?,?)", edge_rows)
        edges = {}
        for source, target, edge_type, local, edge_id, properties in connection.execute(
            """
            SELECT k.source,k.target,k.type,k.local,e.id,CAST(e.properties AS BLOB)
              FROM changed_edges k
              LEFT JOIN current.nodes s ON s.project=? AND s.qualified_name=k.source
              LEFT JOIN current.nodes t ON t.project=? AND t.qualified_name=k.target
              LEFT JOIN current.edges e ON e.project=? AND e.source_id=s.id AND e.target_id=t.id
                                       AND e.type=k.type AND e.local_name_gen=k.local
            """,
            (self.project, self.project, self.project),
        ):
            key = (source, target, edge_type, local)
            edges[key] = None if edge_id is None else _edge_row(*key, properties)[1]
        return ChangeSet(nodes, edges)


@dataclass(frozen=True, slots=True)
class GraphCapture:
    """Rows needed to reproduce one complete published CBM graph."""

    nodes: dict
    edges: dict
    snapshot: bool
    source: str

    @property
    def changed_rows(self) -> int:
        """Return the number of row mutations carried by this capture."""
        return len(self.nodes) + len(self.edges)


class GraphReader:
    """Read one project's atomically published SQLite graph generations."""

    def __init__(self, db_path: str | Path, project: str):
        """Bind a reader to one stable database pathname and project.

        Args:
            db_path: CBM database pathname replaced at publication boundaries.
            project: Exact project identity stored in every selected graph row.
        """
        self.db_path = Path(db_path)
        self.project = project

    def exists(self) -> bool:
        """Return whether the bound project has a readable published generation."""
        if not self.db_path.exists():
            return False
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as connection:
                return (
                    connection.execute(
                        "SELECT 1 FROM projects WHERE name=?",
                        (self.project,),
                    ).fetchone()
                    is not None
                )
        except sqlite3.Error:
            return False

    def pin(self) -> PinnedGeneration | None:
        """Pin the current generation for an exact diff after CBM publication."""
        return PinnedGeneration(self.db_path, self.project) if self.exists() else None

    def snapshot(self, *, source: str = "full_snapshot") -> GraphCapture:
        """Read and validate every graph row in the current generation.

        Args:
            source: Provenance label recorded with the capture.

        Raises:
            ExtractionError: CBM did not publish a complete restorable graph.
        """
        rows = GraphRows(
            dict(iter_nodes(self.db_path, self.project)),
            dict(iter_edges(self.db_path, self.project)),
        )
        validate_rows(rows, self.project)
        return GraphCapture(rows.nodes, rows.edges, True, source)

    def capture(
        self,
        previous: PinnedGeneration | None,
        *,
        force_snapshot: bool = False,
        unchanged: bool = False,
    ) -> GraphCapture:
        """Capture the current graph through the safest available path.

        Args:
            previous: Generation pinned before CBM published the current graph.
            force_snapshot: Ignore an available predecessor and read every row.
            unchanged: Trust CBM's explicit no-op result when a predecessor exists.

        Returns:
            A complete snapshot or an exact set of mutations from ``previous``.
        """
        if force_snapshot or previous is None:
            source = "full_snapshot_anchor" if force_snapshot else "full_snapshot"
            return self.snapshot(source=source)
        if unchanged:
            return GraphCapture({}, {}, False, "noop")
        changes = previous.changes_after_publish()
        return GraphCapture(changes.nodes, changes.edges, False, "full_generation")
