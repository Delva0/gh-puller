"""trace_path: Trace path verbatim definition (from src/mcp/mcp.c) and its implementation.

The schema/title/description/annotations here are byte-identical to the C
server (see gh_puller_mcp/tests/test_manifest.py); edit this file only to change
trace_path's behavior, never to tweak the schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gh_puller_mcp.tools.base import ToolDef, passthrough, register

if TYPE_CHECKING:
    import mcp_types as types

    from gh_puller_mcp.config import ServerConfig

TOOL = ToolDef(
    name="trace_path",
    title="Trace path",
    description=(
        "Trace paths through the code graph. Modes: calls (callers/callees), data_flow (value propagation w"
        "ith args at each hop), cross_service (through HTTP/async Route nodes). Use INSTEAD OF grep for cal"
        "lers, dependencies, impact analysis, or data flow tracing. RESPONSE: prefix-grouped tree rows — ca"
        "llees/callers grouped under their shared qn-prefix, `name hop` per row (full qn = group prefix + d"
        "ot + name); exact callees_total/callers_total on every page = ALL nodes reachable within depth (tr"
        "ansitive, not just direct; test files excluded unless include_tests). risk/args flags use a flat t"
        'able. `truncated: true` + `next` = more rows — pass next back as cursor. format="json" returns t'
        "he SAME tree model as structured JSON."
    ),
    input_schema=(
        '{"type":"object","properties":{"function_name":{"type":"string"},"project":{"type"'
        ':"string"},"direction":{"type":"string","enum":["inbound","outbound","both"],"def'
        'ault":"both"},"depth":{"type":"integer","default":3},"limit":{"type":"integer","'
        'default":100,"minimum":1,"maximum":5000,"description":"Rows per page. callees_total/caller'
        "s_total always carry the exact full counts; when a page is truncated the response carries next — s"
        'ee cursor."},"cursor":{"type":"string","description":"Resume token from a previous respo'
        "nse's 'next' field. Pass it back with ALL other arguments identical to get the following page with"
        " no duplicates. Cursors outlive nothing: after a reindex you get a stale_cursor error — just re-ru"
        'n the original query."},"mode":{"type":"string","enum":["calls","data_flow","cross_s'
        'ervice"],"default":"calls","description":"calls: follow CALLS edges. data_flow: follow CAL'
        "LS+DATA_FLOWS with arg expressions. cross_service: follow HTTP_CALLS+ASYNC_CALLS+DATA_FLOWS throug"
        "h Routes, plus CROSS_* cross-repo edges (CROSS_HTTP_CALLS/ASYNC_CALLS/CHANNEL/GRPC_CALLS/GRAPHQL_C"
        'ALLS/TRPC_CALLS) to hop into other services."},"parameter_name":{"type":"string","descript'
        'ion":"For data_flow mode: scope trace to a specific parameter name"},"edge_types":{"type":'
        '"array","items":{"type":"string"}},"risk_labels":{"type":"boolean","default":false'
        ',"description":"Add risk classification (CRITICAL/HIGH/MEDIUM/LOW) based on hop distance"},"i'
        'nclude_tests":{"type":"boolean","default":false,"description":"Include test files in res'
        "ults. When false (default), test files are filtered out. When true, test nodes are included with a"
        ' test column/marker."},"format":{"type":"string","enum":["tree","json"],"default":'
        '"tree","description":"Response encoding. tree (default): prefix-grouped text rows. json: the '
        'SAME tree model as structured JSON (groups + column-ordered row arrays)."},"include_evidence":{'
        '"type":"boolean","default":false,"description":"Add how each hop was resolved: a strategy'
        " class (lsp | language_rule | heuristic | unresolved) and the resolver's confidence. Off by defaul"
        "t — it adds two columns per row. Use it to judge whether an edge is trustworthy, not to find edges"
        '."}},"required":["function_name","project"]}'
    ),
    annotations=(False, True, True, False),
)


@register
def trace_path(arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """Trace path：默认透传 cbm cli；具体定制在此扩展。"""
    return passthrough(TOOL, arguments, config)
