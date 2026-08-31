"""detect_changes: Detect changes verbatim definition (from src/mcp/mcp.c) and its implementation.

The schema/title/description/annotations here are byte-identical to the C
server (see gh_puller_mcp/tests/test_manifest.py); edit this file only to change
detect_changes's behavior, never to tweak the schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gh_puller_mcp.tools.base import ToolDef, passthrough, register

if TYPE_CHECKING:
    import mcp_types as types

    from gh_puller_mcp.config import ServerConfig

TOOL = ToolDef(
    name="detect_changes",
    title="Detect changes",
    description=(
        "Map a git diff to its BLAST RADIUS. Resolves changed files to the symbols they define, then runs O"
        "NE multi-source graph traversal to the transitive impact set. RESPONSE: base + merge_base SHA, cha"
        "nged_files list, then impacted = prefix-grouped tree rows (name label hop; full qn = group prefix "
        "+ dot + name) + an impacted_modules rollup; impacted_total + truncated are exact. Seeds (the chang"
        "ed symbols) are excluded from impacted; a changed file reached from another changed file is not co"
        'unted as extra impact. format="json" returns the same model as structured JSON.'
    ),
    input_schema=(
        '{"type":"object","properties":{"project":{"type":"string"},"scope":{"type":"strin'
        'g","enum":["files","impact"],"description":"files: changed files only (no traversal). im'
        'pact (default): files + the transitive impact set."},"direction":{"type":"string","enum":'
        '["inbound","outbound","both"],"default":"inbound","description":"inbound (default) = '
        "the blast radius: transitive CALLERS of the changed symbols. outbound = what the changed code depe"
        'nds on. both = union."},"depth":{"type":"integer","default":2,"description":"Max trave'
        'rsal hops from the changed symbols."},"limit":{"type":"integer","default":200,"maximum"'
        ':5000,"description":"Per-symbol impacted rows shown (nearest hops first). impacted_total is alw'
        'ays exact and the impacted_modules rollup always complete regardless."},"base_branch":{"type"'
        ':"string","default":"main"},"since":{"type":"string","description":"Git ref or tag '
        'to compare from (e.g. HEAD~5, v0.5.0). Diffs <ref>...HEAD."},"format":{"type":"string","en'
        'um":["tree","json"],"default":"tree"}},"required":["project"]}'
    ),
    annotations=(False, True, True, False),
)


@register
def detect_changes(arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """Detect changes：默认透传 cbm cli；具体定制在此扩展。"""
    return passthrough(TOOL, arguments, config)
