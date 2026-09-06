"""Diff two atomically published CBM SQLite generations.

On POSIX, a read transaction opened before CBM publishes its replacement keeps
the old inode alive. After publication we attach the new pathname and let SQLite
compute exact row differences in C, without copying a database or decoding the
whole graph in Python.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .store import _normalize, _parse_properties


@dataclass(frozen=True)
class ChangeSet:
    nodes: dict[str, dict | None]
    edges: dict[tuple, dict | None]

    @property
    def count(self) -> int:
        return len(self.nodes) + len(self.edges)


class PinnedGeneration:
    def __init__(self, db_path: str | Path, project: str):
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
        self.connection.close()

    def changes_after_publish(self) -> ChangeSet:
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
            key = _normalize(old_key, self.project)
            nodes[key] = (
                None
                if node_id is None
                else _normalize(
                    {
                        "label": label,
                        "name": name,
                        "file_path": file_path or "",
                        "start_line": start_line or 0,
                        "end_line": end_line or 0,
                        "properties": _parse_properties(properties),
                    },
                    self.project,
                )
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
            SELECT source.qualified_name,target.qualified_name,old.type,COALESCE(old.local_name_gen,'')
              FROM main.edges old
              JOIN main.nodes source ON source.id=old.source_id
              JOIN main.nodes target ON target.id=old.target_id
              LEFT JOIN node_map source_map ON source_map.old_id=old.source_id
              LEFT JOIN node_map target_map ON target_map.old_id=old.target_id
              LEFT JOIN current.edges new
                ON new.source_id=source_map.new_id AND new.target_id=target_map.new_id
               AND new.type=old.type
               AND COALESCE(new.local_name_gen,'')=COALESCE(old.local_name_gen,'')
             WHERE old.project=? AND (new.id IS NULL OR old.properties IS NOT new.properties)
            UNION ALL
            SELECT source.qualified_name,target.qualified_name,new.type,COALESCE(new.local_name_gen,'')
              FROM current.edges new
              JOIN current.nodes source ON source.id=new.source_id
              JOIN current.nodes target ON target.id=new.target_id
              LEFT JOIN node_map source_map ON source_map.new_id=new.source_id
              LEFT JOIN node_map target_map ON target_map.new_id=new.target_id
              LEFT JOIN main.edges old
                ON old.source_id=source_map.old_id AND old.target_id=target_map.old_id
               AND old.type=new.type
               AND COALESCE(old.local_name_gen,'')=COALESCE(new.local_name_gen,'')
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
                                       AND e.type=k.type AND COALESCE(e.local_name_gen,'')=k.local
            """,
            (self.project, self.project, self.project),
        ):
            key = (_normalize(source, self.project), _normalize(target, self.project), edge_type, local)
            edges[key] = (
                None if edge_id is None else _normalize({"properties": _parse_properties(properties)}, self.project)
            )
        return ChangeSet(nodes, edges)
