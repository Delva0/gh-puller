"""Read exact graph and coverage snapshots from published CBM SQLite stores.

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

CoverageKey = tuple[str, str]


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


@dataclass(frozen=True, slots=True)
class CoverageSnapshot:
    """Exact coverage rows and run metadata from one published generation."""

    rows: dict[CoverageKey, str]
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class CoverageCapture:
    """Complete coverage rows or exact mutations plus current run metadata."""

    rows: dict[CoverageKey, str | None]
    metadata: dict[str, object]
    snapshot: bool


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


def _text(value: object, field: str) -> str:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExtractionError(f"CBM {field} is not UTF-8") from exc
    if not isinstance(value, str) or "\0" in value:
        raise ExtractionError(f"CBM {field} is not exact text")
    return value


def _coverage_row(rel_path: object, kind: object, detail: object) -> tuple[CoverageKey, str]:
    return (_text(rel_path, "coverage path"), _text(kind, "coverage kind")), _text(
        detail,
        "coverage detail",
    )


def validate_coverage(snapshot: CoverageSnapshot, project: str) -> None:
    """Validate the exact persisted coverage contract accepted by CBM import.

    Args:
        snapshot: Complete coverage rows and metadata to validate.
        project: Project identity that must match the metadata row.

    Raises:
        ExtractionError: Coverage contains a value the CBM SDK cannot restore.
    """
    metadata = snapshot.metadata
    strings = ("project", "generation", "index_mode", "recorded_at", "recording_status")
    if not isinstance(metadata, dict) or any(
        not isinstance(metadata.get(name), str) or "\0" in metadata[name] for name in strings
    ):
        raise ExtractionError("CBM coverage metadata has invalid text fields")
    if metadata["project"] != project:
        raise ExtractionError("CBM coverage metadata has the wrong project")
    integers = ("ignored_files_stored", "ignored_files_total", "coverage_version")
    if any(type(metadata.get(name)) is not int or metadata[name] < 0 for name in integers):
        raise ExtractionError("CBM coverage metadata has invalid integer fields")
    if metadata["coverage_version"] < 1 or type(metadata.get("hash_records_complete")) is not bool:
        raise ExtractionError("CBM coverage metadata has invalid version fields")
    for key, detail in snapshot.rows.items():
        if (
            not isinstance(key, tuple)
            or len(key) != 2
            or any(not isinstance(value, str) or "\0" in value for value in key)
            or not isinstance(detail, str)
            or "\0" in detail
        ):
            raise ExtractionError(f"CBM coverage row has invalid values: {key!r}")


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


def _coverage_prefix(connection: sqlite3.Connection, schema: str) -> str | None:
    if schema not in {"main", "current"}:
        raise ValueError(f"unsupported SQLite schema {schema!r}")
    prefix = "" if schema == "main" else "current."
    tables = {
        row[0]
        for row in connection.execute(
            f"SELECT name FROM {prefix}sqlite_master "  # noqa: S608 - fixed internal schema.
            "WHERE type='table' AND name IN ('index_coverage','index_coverage_meta')",
        )
    }
    if not tables:
        return None
    if tables != {"index_coverage", "index_coverage_meta"}:
        raise ExtractionError("CBM coverage tables are incomplete")
    return prefix


def _coverage_metadata_from(
    connection: sqlite3.Connection,
    project: str,
    prefix: str,
) -> dict[str, object] | None:

    metadata_row = connection.execute(
        f"SELECT CAST(project AS BLOB),CAST(generation AS BLOB),"  # noqa: S608 - fixed internal schema.
        "CAST(index_mode AS BLOB),CAST(recorded_at AS BLOB),CAST(recording_status AS BLOB),"
        "ignored_files_stored,ignored_files_total,coverage_version,hash_records_complete "
        f"FROM {prefix}index_coverage_meta WHERE project=?",
        (project,),
    ).fetchone()
    if metadata_row is None:
        count = connection.execute(
            f"SELECT count(*) FROM {prefix}index_coverage WHERE project=?",  # noqa: S608 - fixed internal schema.
            (project,),
        ).fetchone()[0]
        if count:
            raise ExtractionError("CBM coverage rows have no generation metadata")
        return None
    if type(metadata_row[8]) is not int or metadata_row[8] not in {0, 1}:
        raise ExtractionError("CBM coverage metadata has an invalid hash-record flag")
    metadata = {
        "project": _text(metadata_row[0], "coverage project"),
        "generation": _text(metadata_row[1], "coverage generation"),
        "index_mode": _text(metadata_row[2], "coverage index mode"),
        "recorded_at": _text(metadata_row[3], "coverage recorded time"),
        "recording_status": _text(metadata_row[4], "coverage recording status"),
        "ignored_files_stored": metadata_row[5],
        "ignored_files_total": metadata_row[6],
        "coverage_version": metadata_row[7],
        "hash_records_complete": metadata_row[8] == 1,
    }
    validate_coverage(CoverageSnapshot({}, metadata), project)
    return metadata


def _coverage_rows_from(
    connection: sqlite3.Connection,
    project: str,
    prefix: str,
) -> dict[CoverageKey, str]:
    rows = {}
    query = (
        f"SELECT CAST(rel_path AS BLOB),CAST(kind AS BLOB),CAST(detail AS BLOB) "  # noqa: S608 - fixed internal schema.
        f"FROM {prefix}index_coverage WHERE project=? ORDER BY rel_path,kind"
    )
    for raw in connection.execute(query, (project,)):
        key, detail = _coverage_row(*raw)
        if key in rows:
            raise ExtractionError(f"CBM coverage identity is duplicated: {key!r}")
        rows[key] = detail
    return rows


def _coverage_snapshot_from(
    connection: sqlite3.Connection,
    project: str,
    *,
    schema: str = "main",
) -> CoverageSnapshot | None:
    prefix = _coverage_prefix(connection, schema)
    if prefix is None:
        return None
    metadata = _coverage_metadata_from(connection, project, prefix)
    if metadata is None:
        return None
    snapshot = CoverageSnapshot(_coverage_rows_from(connection, project, prefix), metadata)
    validate_coverage(snapshot, project)
    return snapshot


def _coverage_changes_after_publish(
    connection: sqlite3.Connection,
    project: str,
    *,
    force_snapshot: bool,
) -> CoverageCapture | None:
    current_prefix = _coverage_prefix(connection, "current")
    if current_prefix is None:
        return None
    metadata = _coverage_metadata_from(connection, project, current_prefix)
    if metadata is None:
        return None
    previous_prefix = _coverage_prefix(connection, "main")
    previous_metadata = (
        _coverage_metadata_from(connection, project, previous_prefix)
        if previous_prefix is not None
        else None
    )
    if force_snapshot or previous_metadata is None:
        return CoverageCapture(
            _coverage_rows_from(connection, project, current_prefix),
            metadata,
            True,
        )

    keys = list(
        connection.execute(
            """
            SELECT old.rel_path,old.kind
              FROM main.index_coverage old
              LEFT JOIN current.index_coverage new
                ON new.project=old.project AND new.rel_path=old.rel_path AND new.kind=old.kind
             WHERE old.project=? AND (new.project IS NULL OR old.detail IS NOT new.detail)
            UNION ALL
            SELECT new.rel_path,new.kind
              FROM current.index_coverage new
              LEFT JOIN main.index_coverage old
                ON old.project=new.project AND old.rel_path=new.rel_path AND old.kind=new.kind
             WHERE new.project=? AND old.project IS NULL
            """,
            (project, project),
        ),
    )
    connection.execute(
        "CREATE TEMP TABLE changed_coverage("
        "rel_path TEXT,kind TEXT,PRIMARY KEY(rel_path,kind))",
    )
    connection.executemany("INSERT INTO changed_coverage VALUES (?,?)", keys)
    rows = {}
    for rel_path, kind, present, detail in connection.execute(
        """
        SELECT keys.rel_path,keys.kind,current.project,CAST(current.detail AS BLOB)
          FROM changed_coverage keys
          LEFT JOIN current.index_coverage current
            ON current.project=? AND current.rel_path=keys.rel_path AND current.kind=keys.kind
        """,
        (project,),
    ):
        key = (_text(rel_path, "coverage path"), _text(kind, "coverage kind"))
        rows[key] = None if present is None else _text(detail, "coverage detail")
    return CoverageCapture(rows, metadata, False)


def load_coverage(db_path: str | Path, project: str) -> CoverageSnapshot | None:
    """Load coverage metadata when the published CBM generation records it."""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    try:
        _project(connection, project)
        return _coverage_snapshot_from(connection, project)
    finally:
        connection.close()


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
    """Exact graph and coverage mutations between two published generations."""

    nodes: dict[str, dict | None]
    edges: dict[tuple, dict | None]
    coverage: CoverageCapture | None = None

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

    def changes_after_publish(self, *, force_coverage_snapshot: bool = False) -> ChangeSet:
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
        coverage = _coverage_changes_after_publish(
            connection,
            self.project,
            force_snapshot=force_coverage_snapshot,
        )
        return ChangeSet(nodes, edges, coverage)


@dataclass(frozen=True, slots=True)
class GraphCapture:
    """Rows needed to reproduce one complete published CBM generation."""

    nodes: dict
    edges: dict
    snapshot: bool
    source: str
    coverage: CoverageCapture | None = None

    @property
    def changed_rows(self) -> int:
        """Return the number of row mutations carried by this capture."""
        return len(self.nodes) + len(self.edges)

    @property
    def changed_coverage_rows(self) -> int:
        """Return the number of coverage mutations carried by this capture."""
        return len(self.coverage.rows) if self.coverage is not None else 0


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
        coverage = load_coverage(self.db_path, self.project)
        coverage_capture = (
            CoverageCapture(coverage.rows, coverage.metadata, True) if coverage is not None else None
        )
        return GraphCapture(rows.nodes, rows.edges, True, source, coverage_capture)

    def capture(
        self,
        previous: PinnedGeneration | None,
        *,
        force_snapshot: bool = False,
        force_coverage_snapshot: bool = False,
        unchanged: bool = False,
    ) -> GraphCapture:
        """Capture the current graph through the safest available path.

        Args:
            previous: Generation pinned before CBM published the current graph.
            force_snapshot: Ignore an available predecessor and read every row.
            force_coverage_snapshot: Read every coverage row even when graph diffing.
            unchanged: Trust CBM's explicit no-op result when a predecessor exists.

        Returns:
            A complete snapshot or an exact set of mutations from ``previous``.
        """
        if force_snapshot or previous is None:
            source = "full_snapshot_anchor" if force_snapshot else "full_snapshot"
            return self.snapshot(source=source)
        if unchanged:
            current = load_coverage(self.db_path, self.project)
            coverage = (
                CoverageCapture(
                    current.rows if force_coverage_snapshot else {},
                    current.metadata,
                    force_coverage_snapshot,
                )
                if current is not None
                else None
            )
            return GraphCapture({}, {}, False, "noop", coverage)
        changes = previous.changes_after_publish(
            force_coverage_snapshot=force_coverage_snapshot,
        )
        return GraphCapture(changes.nodes, changes.edges, False, "full_generation", changes.coverage)
