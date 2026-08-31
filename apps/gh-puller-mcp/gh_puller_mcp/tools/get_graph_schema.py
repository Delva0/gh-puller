"""get_graph_schema: Get graph schema verbatim definition (from src/mcp/mcp.c) and its implementation.

The schema/title/description/annotations here are byte-identical to the C
server (see gh_puller_mcp/tests/test_manifest.py); edit this file only to change
get_graph_schema's behavior, never to tweak the schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gh_puller_mcp.tools.base import ToolDef, passthrough, register

if TYPE_CHECKING:
    import mcp_types as types

    from gh_puller_mcp.config import ServerConfig

TOOL = ToolDef(
    name="get_graph_schema",
    title="Get graph schema",
    description=(
        "Get the schema of the knowledge graph (node labels, edge types)"
    ),
    input_schema=(
        '{"type":"object","properties":{"project":{"type":"string"}},"required":["project"]'
        "}"
    ),
    annotations=(False, True, True, False),
)


@register
def get_graph_schema(arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """Get graph schema：默认透传 cbm cli；具体定制在此扩展。"""
    return passthrough(TOOL, arguments, config)
