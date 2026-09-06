"""Exact graph values stored by the persistent archive.

The archive keeps parallel edges as four-part keys. ``networkx.DiGraph`` is
only a convenience view: parallel rows for one ``(source, target)`` pair are
grouped in the edge's ``edge_keys`` and ``data`` lists.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass, field

import networkx as nx

Node = Hashable
EdgeKey = tuple[Node, Node, str | None, str | None]


@dataclass(frozen=True, slots=True)
class SnapshotGraph:
    """An attribute-exact graph snapshot; ``edges`` retains parallel rows."""

    nodes: frozenset[Node]
    edges: frozenset[EdgeKey]
    node_attrs: dict[Node, dict] = field(default_factory=dict)
    edge_attrs: dict[EdgeKey, dict] = field(default_factory=dict)


def snapshot_to_networkx(snapshot: SnapshotGraph) -> nx.DiGraph:
    """Return an aggregated ``DiGraph`` view without losing parallel-edge data."""
    graph = nx.DiGraph()
    for node in snapshot.nodes:
        graph.add_node(node, **snapshot.node_attrs.get(node, {}))

    grouped: dict[tuple[Node, Node], list[EdgeKey]] = {}
    for key in snapshot.edges:
        grouped.setdefault((key[0], key[1]), []).append(key)
    for (source, target), keys in grouped.items():
        keys.sort(key=lambda key: (key[2] or "", key[3] or ""))
        graph.add_edge(
            source,
            target,
            edge_keys=list(keys),
            data=[dict(snapshot.edge_attrs.get(key, {})) for key in keys],
        )
    return graph
