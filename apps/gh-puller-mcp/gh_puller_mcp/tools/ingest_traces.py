"""ingest_traces: Ingest traces verbatim definition (from src/mcp/mcp.c) and its implementation.

The schema/title/description/annotations here are byte-identical to the C
server (see gh_puller_mcp/tests/test_manifest.py); edit this file only to change
ingest_traces's behavior, never to tweak the schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gh_puller_mcp.tools.base import ToolDef, passthrough, register

if TYPE_CHECKING:
    import mcp_types as types

    from gh_puller_mcp.config import ServerConfig

TOOL = ToolDef(
    name="ingest_traces",
    title="Ingest traces",
    description=(
        "Ingest runtime traces to enhance the knowledge graph"
    ),
    input_schema=(
        '{"type":"object","properties":{"traces":{"type":"array","items":{"type":"object"'
        ',"properties":{"caller":{"type":"string"},"callee":{"type":"string"},"count":{"ty'
        'pe":"integer"}},"additionalProperties":false}},"project":{"type":"string"}},"required'
        '":["traces","project"]}'
    ),
    annotations=(False, False, False, False),
)


@register
def ingest_traces(arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """Ingest traces：默认透传 cbm cli；具体定制在此扩展。"""
    return passthrough(TOOL, arguments, config)
