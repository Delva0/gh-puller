"""manage_adr: Manage ADR verbatim definition (from src/mcp/mcp.c) and its implementation.

The schema/title/description/annotations here are byte-identical to the C
server (see gh_puller_mcp/tests/test_manifest.py); edit this file only to change
manage_adr's behavior, never to tweak the schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gh_puller_mcp.tools.base import ToolDef, passthrough, register

if TYPE_CHECKING:
    import mcp_types as types

    from gh_puller_mcp.config import ServerConfig

TOOL = ToolDef(
    name="manage_adr",
    title="Manage ADR",
    description=(
        "Create or update Architecture Decision Records"
    ),
    input_schema=(
        '{"type":"object","properties":{"project":{"type":"string"},"mode":{"type":"string'
        '","enum":["get","update","sections"],"description":"update replaces the entire ADR doc'
        'ument; sections only lists existing headings"},"content":{"type":"string","description":'
        '"Complete replacement document required by update"}},"additionalProperties":false,"required"'
        ':["project"]}'
    ),
    annotations=(False, True, False, False),
)


@register
def manage_adr(arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """Manage ADR：默认透传 cbm cli；具体定制在此扩展。"""
    return passthrough(TOOL, arguments, config)
