"""Append exact CBM graph captures to durable Merkle KGA generations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .archive import GRAPH_FIDELITY_VERSION, Archive, ArchiveWriter, RadixTree, TreeRef, graph_digest
from .store import ExtractionError, GraphRows, validate_rows

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .graph_reader import GraphCapture


@dataclass(frozen=True, slots=True)
class KGACommit:
    """Git identity and archive position for one graph generation."""

    ordinal: int
    sha: str
    parents: tuple[str, ...]
    changed_files: int | None


class KGARecorder:
    """Own one append transaction and the complete roots of its latest graph."""

    def __init__(self, path: str | Path, *, compression_level: int = 1):
        """Open a new, incomplete, or finalized KGA for append.

        Args:
            path: Archive path that receives durable commit checkpoints.
            compression_level: Per-frame zlib level preserved by the KGA format.
        """
        self.path = Path(path)
        complete = False
        if self.path.exists():
            with Archive(self.path, allow_incomplete=True) as archive:
                complete = archive.complete
        self.writer = ArchiveWriter(
            self.path,
            compression_level=compression_level,
            extend_complete=complete,
        )
        last = self.writer.commits[-1] if self.writer.commits else None
        self.node_root = self._root(last, "node_root")
        self.edge_root = self._root(last, "edge_root")

    @staticmethod
    def _root(item: dict | None, name: str) -> TreeRef | None:
        return TreeRef.from_json(item.get(name)) if item else None

    @property
    def commits(self) -> Sequence[dict]:
        """Return the durable manifest prefix visible to this writer."""
        return self.writer.commits

    @property
    def latest_commit(self) -> str | None:
        """Return the latest archived commit identity, if any."""
        return self.writer.commits[-1]["sha"] if self.writer.commits else None

    def append(
        self,
        commit: KGACommit,
        capture: GraphCapture,
        *,
        project: str,
        metadata: Mapping[str, object],
    ) -> dict:
        """Atomically publish one exact logical graph generation.

        Args:
            commit: Git identity, archive ordinal, and source-change count.
            capture: Full rows or exact mutations produced by :class:`GraphReader`.
            project: CBM project identity required by the graph fidelity contract.
            metadata: CBM execution and binary provenance for this generation.

        Returns:
            The manifest committed at the new durable archive boundary.

        Raises:
            ExtractionError: A full capture is incomplete or contains deletions.
        """
        if capture.snapshot:
            if any(value is None for value in capture.nodes.values()) or any(
                value is None for value in capture.edges.values()
            ):
                raise ExtractionError("complete graph snapshot contains deletions")
            validate_rows(GraphRows(capture.nodes, capture.edges), project)
            self.node_root, self.edge_root = None, None
        node_tree = RadixTree(self.writer, "nodes")
        edge_tree = RadixTree(self.writer, "edges")
        self.node_root = node_tree.apply(self.node_root, capture.nodes)
        self.edge_root = edge_tree.apply(self.edge_root, capture.edges)
        manifest = {
            **metadata,
            "ordinal": commit.ordinal,
            "sha": commit.sha,
            "parents": list(commit.parents),
            "node_root": self.node_root.to_json() if self.node_root else None,
            "edge_root": self.edge_root.to_json() if self.edge_root else None,
            "nodes": self.node_root.count if self.node_root else 0,
            "edges": self.edge_root.count if self.edge_root else 0,
            "graph_digest": graph_digest(self.node_root, self.edge_root),
            "changed_files": commit.changed_files,
            "changed_rows": capture.changed_rows,
            "pages_read": node_tree.pages_read + edge_tree.pages_read,
            "pages_written": node_tree.pages_written + edge_tree.pages_written,
            "generation_diff_source": capture.source,
            "graph_fidelity_version": GRAPH_FIDELITY_VERSION,
            "cbm_project": project,
        }
        self.writer.commit(manifest)
        return manifest

    def finalize(self, metadata: Mapping[str, object] | None = None) -> None:
        """Write and verify the final archive index.

        Args:
            metadata: Optional build-wide summary embedded in the final index.
        """
        self.writer.finalize(dict(metadata or {}))
        self.writer.verify_appended()

    def close_incomplete(self) -> None:
        """Release the writer while preserving its latest durable checkpoint."""
        self.writer.close_incomplete()
