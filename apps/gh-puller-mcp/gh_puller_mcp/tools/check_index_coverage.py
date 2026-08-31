"""check_index_coverage: Check index coverage verbatim definition (from src/mcp/mcp.c) and its implementation.

The schema/title/description/annotations here are byte-identical to the C
server (see gh_puller_mcp/tests/test_manifest.py); edit this file only to change
check_index_coverage's behavior, never to tweak the schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gh_puller_mcp.tools.base import ToolDef, passthrough, register

if TYPE_CHECKING:
    import mcp_types as types

    from gh_puller_mcp.config import ServerConfig

TOOL = ToolDef(
    name="check_index_coverage",
    title="Check index coverage",
    description=(
        "Check authoritative indexing-coverage metadata for exact repository-relative paths and bounded pat"
        "h scopes. Use this after graph discovery for every cited or operated-on file; use scopes before ne"
        "gative/exhaustive claims because fully skipped files cannot appear in normal graph results. Return"
        "s coverage status separately from filesystem metadata freshness, plus structured parse-error range"
        "s and direct-source fallback actions. The signal is best-effort: indexed_no_recorded_gap is not a "
        "completeness guarantee. At least one of 'paths' or 'scopes' is required; the call is rejected at r"
        "untime if both are omitted."
    ),
    input_schema=(
        '{"type":"object","properties":{"project":{"type":"string"},"paths":{"type":"array'
        '","items":{"type":"string"},"maxItems":128,"description":"Repository-relative files to'
        " check exactly. Required if 'scopes' is omitted.\"},\"scopes\":{\"type\":\"array\",\"items\":{\"ty"
        'pe":"string"},"maxItems":32,"description":"Repository-relative path prefixes; use . for th'
        "e project root. Required if 'paths' is omitted.\"},\"scope_limit\":{\"type\":\"integer\",\"default"
        '":200,"minimum":1,"maximum":1000},"scope_offset":{"type":"integer","default":0,"mini'
        'mum":0}},"required":["project"]}'
    ),
    annotations=(False, True, True, False),
)


@register
def check_index_coverage(arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """Check index coverage：默认透传 cbm cli；具体定制在此扩展。"""
    return passthrough(TOOL, arguments, config)
