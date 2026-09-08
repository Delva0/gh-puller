"""Read complete or generation-diff graph captures from published CBM stores.

This module owns the choice between the complete reference path and the pinned
SQLite generation-diff optimization. Both expose the same exact row contract
defined by :mod:`.store`; consumers never infer graph value from CBM routing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .generation_diff import PinnedGeneration
from .store import GraphRows, iter_edges, iter_nodes, validate_rows


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
