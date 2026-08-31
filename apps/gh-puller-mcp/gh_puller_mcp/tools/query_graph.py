"""query_graph: Query graph verbatim definition (from src/mcp/mcp.c) and its implementation.

The schema/title/description/annotations here are byte-identical to the C
server (see gh_puller_mcp/tests/test_manifest.py); edit this file only to change
query_graph's behavior, never to tweak the schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gh_puller_mcp.tools.base import ToolDef, passthrough, register

if TYPE_CHECKING:
    import mcp_types as types

    from gh_puller_mcp.config import ServerConfig

TOOL = ToolDef(
    name="query_graph",
    title="Query graph",
    description=(
        "Execute a Cypher query against the knowledge graph for complex multi-hop patterns, aggregations, a"
        "nd cross-service analysis. The response includes 'total' (returned row count). There is a hard 100"
        "k row ceiling — for broad queries add LIMIT in the Cypher itself or use search_graph + offset/limi"
        "t pagination instead. COMPLEXITY / BOTTLENECKS: every Function and Method node carries queryable c"
        "omplexity properties — cyclomatic (complexity), cognitive, loop_count, loop_depth (max nested-loop"
        " depth, a polynomial-degree proxy), plus interprocedural transitive_loop_depth (worst-case nested-"
        "loop degree propagated along CALLS edges) and a recursive flag. Additional hot-path signals: linea"
        "r_scan_in_loop (count of find/contains/indexOf-style scans inside a loop — the hidden O(n^2) that "
        "loop_depth misses), alloc_in_loop (allocations/appends inside a loop), recursion_in_loop (a self-c"
        "all inside a loop), unguarded_recursion (recursion with no conditionally-guarded base case), param"
        "_count and max_access_depth (structure smells). Find all hot-path candidates in one query, e.g. MA"
        "TCH (f:Function) WHERE f.transitive_loop_depth >= 3 OR f.linear_scan_in_loop >= 1 RETURN f.qualifi"
        "ed_name, f.transitive_loop_depth, f.linear_scan_in_loop ORDER BY f.transitive_loop_depth DESC. MIS"
        'SED GRAPH: pass graph="missed" to query the best-effort miss graph instead — the file structure '
        "of ONLY the files the indexer could NOT fully index (Project → Folder → File nodes with CONTAINS_F"
        'OLDER/CONTAINS_FILE edges; each File carries kind ("parse_partial" = indexed but constructs in t'
        "he flagged line ranges MAY be missing; or a skip phase) and detail (the line ranges / reason)). Ex"
        'ample: MATCH (f:File) WHERE f.kind = \\"parse_partial\\" RETURN f.file_path, f.detail. Absence f'
        "rom this graph is NOT a completeness guarantee."
    ),
    input_schema=(
        '{"type":"object","properties":{"query":{"type":"string","description":"Cypher query'
        '"},"project":{"type":"string"},"graph":{"type":"string","enum":["code","missed"'
        '],"default":"code","description":"Which graph to query: the code knowledge graph (default) '
        'or the missed graph (only files not fully indexed, laid out as their file structure)."},"max_row'
        's":{"type":"integer","description":"Optional row limit. Default: unlimited up to a 100k ro'
        'w ceiling. No offset support — use search_graph for paginated browsing."}},"required":["query'
        '","project"]}'
    ),
    annotations=(False, True, True, False),
)


@register
def query_graph(arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """Query graph：默认透传 cbm cli；具体定制在此扩展。"""
    return passthrough(TOOL, arguments, config)
