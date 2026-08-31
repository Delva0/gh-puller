"""get_code_snippet: Get code snippet verbatim definition (from src/mcp/mcp.c) and its implementation.

The schema/title/description/annotations here are byte-identical to the C
server (see gh_puller_mcp/tests/test_manifest.py); edit this file only to change
get_code_snippet's behavior, never to tweak the schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gh_puller_mcp.tools.base import ToolDef, passthrough, register

if TYPE_CHECKING:
    import mcp_types as types

    from gh_puller_mcp.config import ServerConfig

TOOL = ToolDef(
    name="get_code_snippet",
    title="Get code snippet",
    description=(
        "Read source code for a function/class/symbol. IMPORTANT: First call search_graph to find the exact"
        " qualified_name, then pass it here. This is a read tool, not a search tool. Accepts full qualified"
        "_name (exact match) or short function name (returns suggestions if ambiguous). If the response car"
        "ries a 'coverage_note', the file was only partially indexed — constructs in the noted line ranges "
        "may be missing from the graph (best-effort signal); prefer grep there and treat the returned sourc"
        "e as ground truth."
    ),
    input_schema=(
        '{"type":"object","properties":{"qualified_name":{"type":"string","description":"Ful'
        'l qualified_name from search_graph, or short function name"},"project":{"type":"string"},"'
        'include_neighbors":{"type":"boolean","default":false}},"required":["qualified_name","p'
        'roject"]}'
    ),
    annotations=(False, True, True, False),
)


@register
def get_code_snippet(arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """Get code snippet：默认透传 cbm cli；具体定制在此扩展。"""
    return passthrough(TOOL, arguments, config)
