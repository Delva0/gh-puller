"""Read exact graph changes from CBM's native delta candidate journal.

The journal contains generation-local row ids, not graph values. This module
expands those candidates against the pinned previous generation and the newly
published generation, then applies the same normalized value comparison used
by the full generation diff.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

from .generation_diff import ChangeSet, PinnedGeneration
from .store import ExtractionError, _edge_attributes, _node_attributes

if TYPE_CHECKING:
    from pathlib import Path

FORMAT_VERSION = 1


class JournalUnavailableError(Exception):
    """Indicate that the current generation has no supported native journal."""


def available(db_path: str | Path, project: str) -> bool:
    """Return whether the published generation has a complete supported journal."""
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)) as connection, connection:
            row = connection.execute(
                "SELECT format_version,node_count,edge_count FROM cbm_delta_change_journal WHERE project=?",
                (project,),
            ).fetchone()
            if row is None or row[0] != FORMAT_VERSION:
                return False
            node_count = connection.execute(
                "SELECT count(*) FROM cbm_delta_changed_nodes WHERE project=?",
                (project,),
            ).fetchone()[0]
            edge_count = connection.execute(
                "SELECT count(*) FROM cbm_delta_changed_edges WHERE project=?",
                (project,),
            ).fetchone()[0]
        return row[1:] == (node_count, edge_count)
    except sqlite3.Error:
        return False


def candidate_counts(db_path: str | Path, project: str) -> dict[str, int]:
    """Read the native journal candidate counts after validating its metadata."""
    if not available(db_path, project):
        raise JournalUnavailableError("published generation has no complete native journal")
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)) as connection, connection:
        node_count, edge_count = connection.execute(
            "SELECT node_count,edge_count FROM cbm_delta_change_journal WHERE project=?",
            (project,),
        ).fetchone()
    return {"nodes": node_count, "edges": edge_count}


def _node_value(row: tuple) -> dict | None:
    node_id, label, name, file_path, start_line, end_line, properties = row
    if node_id is None:
        return None
    return _node_attributes(label, name, file_path, start_line, end_line, properties)


def _edge_value(edge_id, properties) -> dict | None:
    if edge_id is None:
        return None
    return _edge_attributes(properties)


def _prepare_candidates(connection: sqlite3.Connection, project: str) -> None:
    connection.executescript(
        """
        CREATE TEMP TABLE journal_node_keys(
            qualified_name TEXT PRIMARY KEY
        ) WITHOUT ROWID;
        CREATE TEMP TABLE journal_identity_node_ids(
            node_id INTEGER PRIMARY KEY
        ) WITHOUT ROWID;
        CREATE TEMP TABLE journal_edge_keys(
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            type TEXT NOT NULL,
            local TEXT NOT NULL,
            PRIMARY KEY(source,target,type,local)
        ) WITHOUT ROWID;
        """,
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO journal_node_keys
        SELECT old.qualified_name
          FROM current.cbm_delta_changed_nodes candidate
          JOIN main.nodes old ON old.project=candidate.project AND old.id=candidate.node_id
         WHERE candidate.project=?
        """,
        (project,),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO journal_node_keys
        SELECT new.qualified_name
          FROM current.cbm_delta_changed_nodes candidate
          JOIN current.nodes new ON new.project=candidate.project AND new.id=candidate.node_id
         WHERE candidate.project=?
        """,
        (project,),
    )
    connection.execute(
        """
        INSERT INTO journal_identity_node_ids
        SELECT candidate.node_id
          FROM current.cbm_delta_changed_nodes candidate
          LEFT JOIN main.nodes old
            ON old.project=candidate.project AND old.id=candidate.node_id
          LEFT JOIN current.nodes new
            ON new.project=candidate.project AND new.id=candidate.node_id
         WHERE candidate.project=? AND old.qualified_name IS NOT new.qualified_name
        """,
        (project,),
    )
    for schema in ("main", "current"):
        query = f"""
            INSERT OR IGNORE INTO journal_edge_keys
            SELECT source.qualified_name,target.qualified_name,edge.type,
                   COALESCE(edge.local_name_gen,'')
              FROM current.cbm_delta_changed_edges candidate
              JOIN {schema}.edges edge
                ON edge.project=candidate.project AND edge.id=candidate.edge_id
              JOIN {schema}.nodes source ON source.id=edge.source_id
              JOIN {schema}.nodes target ON target.id=edge.target_id
             WHERE candidate.project=?
            """  # noqa: S608 - schema is selected from the fixed tuple above.
        connection.execute(
            query,
            (project,),
        )
        for endpoint in ("source_id", "target_id"):
            query = f"""
                INSERT OR IGNORE INTO journal_edge_keys
                SELECT source.qualified_name,target.qualified_name,edge.type,
                       COALESCE(edge.local_name_gen,'')
                  FROM journal_identity_node_ids changed
                  JOIN {schema}.edges edge ON edge.{endpoint}=changed.node_id
                  JOIN {schema}.nodes source ON source.id=edge.source_id
                  JOIN {schema}.nodes target ON target.id=edge.target_id
                 WHERE edge.project=?
                """  # noqa: S608 - schema and endpoint come from fixed tuples.
            connection.execute(
                query,
                (project,),
            )


def changes_after_publish(previous: PinnedGeneration) -> ChangeSet:
    """Expand and exact-filter native candidates against two SQLite generations."""
    connection = previous.connection
    try:
        connection.execute("ATTACH DATABASE ? AS current", (str(previous.db_path),))
        metadata = connection.execute(
            "SELECT format_version,node_count,edge_count FROM current.cbm_delta_change_journal WHERE project=?",
            (previous.project,),
        ).fetchone()
        if metadata is None or metadata[0] != FORMAT_VERSION:
            raise JournalUnavailableError("published generation has no supported native journal")
        counts = (
            connection.execute(
                "SELECT count(*) FROM current.cbm_delta_changed_nodes WHERE project=?",
                (previous.project,),
            ).fetchone()[0],
            connection.execute(
                "SELECT count(*) FROM current.cbm_delta_changed_edges WHERE project=?",
                (previous.project,),
            ).fetchone()[0],
        )
        if metadata[1:] != counts:
            raise JournalUnavailableError("native journal metadata is incomplete")
        _prepare_candidates(connection, previous.project)

        nodes = {}
        rows = connection.execute(
            """
            SELECT key.qualified_name,
                   old.id,old.label,old.name,old.file_path,old.start_line,old.end_line,
                   CAST(old.properties AS BLOB),
                   new.id,new.label,new.name,new.file_path,new.start_line,new.end_line,
                   CAST(new.properties AS BLOB)
              FROM journal_node_keys key
              LEFT JOIN main.nodes old
                ON old.project=? AND old.qualified_name=key.qualified_name
              LEFT JOIN current.nodes new
                ON new.project=? AND new.qualified_name=key.qualified_name
            """,
            (previous.project, previous.project),
        )
        for row in rows:
            key = row[0]
            old_value = _node_value(row[1:8])
            new_value = _node_value(row[8:15])
            if old_value != new_value:
                nodes[key] = new_value

        edges = {}
        rows = connection.execute(
            """
            SELECT key.source,key.target,key.type,key.local,
                   old.id,CAST(old.properties AS BLOB),
                   new.id,CAST(new.properties AS BLOB)
              FROM journal_edge_keys key
              LEFT JOIN main.nodes old_source
                ON old_source.project=? AND old_source.qualified_name=key.source
              LEFT JOIN main.nodes old_target
                ON old_target.project=? AND old_target.qualified_name=key.target
              LEFT JOIN main.edges old
                ON old.project=? AND old.source_id=old_source.id AND old.target_id=old_target.id
               AND old.type=key.type AND COALESCE(old.local_name_gen,'')=key.local
              LEFT JOIN current.nodes new_source
                ON new_source.project=? AND new_source.qualified_name=key.source
              LEFT JOIN current.nodes new_target
                ON new_target.project=? AND new_target.qualified_name=key.target
              LEFT JOIN current.edges new
                ON new.project=? AND new.source_id=new_source.id AND new.target_id=new_target.id
               AND new.type=key.type AND COALESCE(new.local_name_gen,'')=key.local
            """,
            (previous.project,) * 6,
        )
        for source, target, edge_type, local, old_id, old_props, new_id, new_props in rows:
            key = (
                source,
                target,
                edge_type,
                local,
            )
            old_value = _edge_value(old_id, old_props)
            new_value = _edge_value(new_id, new_props)
            if old_value != new_value:
                edges[key] = new_value
        return ChangeSet(nodes, edges)
    except (sqlite3.Error, ExtractionError) as exc:
        raise JournalUnavailableError(str(exc)) from exc
