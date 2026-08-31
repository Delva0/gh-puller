"""get_architecture: Get architecture verbatim definition (from src/mcp/mcp.c) and its implementation.

The schema/title/description/annotations here are byte-identical to the C
server (see gh_puller_mcp/tests/test_manifest.py); edit this file only to change
get_architecture's behavior, never to tweak the schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gh_puller_mcp.tools.base import ToolDef, passthrough, register

if TYPE_CHECKING:
    import mcp_types as types

    from gh_puller_mcp.config import ServerConfig

TOOL = ToolDef(
    name="get_architecture",
    title="Get architecture",
    description=(
        "Get high-level architecture overview. DEFAULT (no aspects) is a compact summary — overview counts,"
        " languages, packages, entry_points; request more via aspects:[...] (structure, dependencies, route"
        "s, hotspots, boundaries, layers, clusters, file_tree) or [\"all\"]. 'clusters' runs Leiden communi"
        "ty detection over the call/import graph, surfacing the de-facto modules (label, member count, cohe"
        "sion score, representative top_nodes, binding packages/edge_types) — the real architectural seams,"
        " which often cut across the folder layout. Optional path scopes analysis to nodes under that direc"
        "tory prefix (file_path)."
    ),
    input_schema=(
        '{"type":"object","properties":{"project":{"type":"string"},"path":{"type":"string'
        '","description":"Optional directory prefix to scope architecture (e.g. apps/hoa)"},"aspects'
        '":{"type":"array","items":{"type":"string","enum":["all","overview","structure"'
        ',"dependencies","routes","languages","packages","entry_points","hotspots","boundaries'
        "\",\"layers\",\"file_tree\",\"clusters\",\"cycles\"]},\"description\":\"Aspects to include. 'all' "
        "= everything; 'overview' = compact summary (all except file_tree); omit = all. 'cycles' is opt-in "
        "ONLY (never via all/overview): it scans the whole call graph for circular CALLS dependencies (SCCs"
        ' of size > 1)."}},"required":["project"]}'
    ),
    annotations=(False, True, True, False),
)


@register
def get_architecture(arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """Get architecture：默认透传 cbm cli；具体定制在此扩展。"""
    return passthrough(TOOL, arguments, config)
