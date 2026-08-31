"""delete_project: Delete project verbatim definition (from src/mcp/mcp.c) and its implementation.

The schema/title/description/annotations here are byte-identical to the C
server (see gh_puller_mcp/tests/test_manifest.py); edit this file only to change
delete_project's behavior, never to tweak the schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gh_puller_mcp.tools.base import ToolDef, passthrough, register

if TYPE_CHECKING:
    import mcp_types as types

    from gh_puller_mcp.config import ServerConfig

TOOL = ToolDef(
    name="delete_project",
    title="Delete project",
    description=(
        "Delete a project from the index"
    ),
    input_schema=(
        '{"type":"object","properties":{"project":{"type":"string"}},"required":["project"]'
        "}"
    ),
    annotations=(False, True, True, False),
)


@register
def delete_project(arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """Delete project：默认透传 cbm cli；具体定制在此扩展。"""
    return passthrough(TOOL, arguments, config)
