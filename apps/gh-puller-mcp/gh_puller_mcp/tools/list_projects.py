"""list_projects: List projects verbatim definition (from src/mcp/mcp.c) and its implementation.

The schema/title/description/annotations here are byte-identical to the C
server (see gh_puller_mcp/tests/test_manifest.py); edit this file only to change
list_projects's behavior, never to tweak the schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gh_puller_mcp.tools.base import ToolDef, passthrough, register

if TYPE_CHECKING:
    import mcp_types as types

    from gh_puller_mcp.config import ServerConfig

TOOL = ToolDef(
    name="list_projects",
    title="List projects",
    description=(
        "List indexed projects with deterministic pagination"
    ),
    input_schema=(
        '{"type":"object","properties":{"offset":{"type":"integer","minimum":0,"default":0}'
        ',"limit":{"type":"integer","minimum":1,"maximum":100,"default":50},"include_details"'
        ':{"type":"boolean","default":false,"description":"Include branch, node/edge counts and da'
        'tabase size. Slower."},"metadata_only":{"type":"boolean","description":"Deprecated compa'
        'tibility alias for include_details=false."}}}'
    ),
    annotations=(True, False, True, False),
)


@register
def list_projects(arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """List projects：默认透传 cbm cli；具体定制在此扩展。"""
    return passthrough(TOOL, arguments, config)
