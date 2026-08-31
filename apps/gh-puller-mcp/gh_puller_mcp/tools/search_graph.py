"""search_graph: Search graph verbatim definition (from src/mcp/mcp.c) and its implementation.

The schema/title/description/annotations here are byte-identical to the C
server (see gh_puller_mcp/tests/test_manifest.py); edit this file only to change
search_graph's behavior, never to tweak the schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gh_puller_mcp.tools.base import ToolDef, passthrough, register

if TYPE_CHECKING:
    import mcp_types as types

    from gh_puller_mcp.config import ServerConfig

TOOL = ToolDef(
    name="search_graph",
    title="Search graph",
    description=(
        "Search the code knowledge graph for functions, classes, routes, and variables. Use INSTEAD OF grep"
        "/glob when finding code definitions, implementations, or relationships. Three search modes: (1) qu"
        "ery='update settings' for BM25 ranked full-text search with camelCase splitting and structural lab"
        "el boosting — recommended for natural-language discovery; (2) name_pattern='.*regex.*' for exact p"
        "attern matching; (3) semantic_query=[...] for vector cosine search that bridges vocabulary (finds "
        "'publish' when you search 'send'). The three modes are independent and can be combined in a single"
        " call. RESPONSE: prefix-grouped tree rows by default — a shared (qn-prefix, file) group header pri"
        "nted once, then `name label lines in out` per row (full qn = group prefix + dot + name). in/out = "
        "selected degree across CALLS, USAGE, CALL_REFERENCE, INHERITS, and IMPLEMENTS; other edge types ar"
        "e excluded. These are NOT caller/callee counts — use trace_path for callers. Add per-node property"
        ' columns via fields (e.g. ["complexity","signature","docstring"]); format="json" returns t'
        "he SAME tree model as structured JSON. PAGINATION: results are capped at limit (default 50). The r"
        "esponse always includes 'total' (full match count before limit) and 'has_more' (true when total > "
        "offset+returned). Detect truncation with has_more, then page by re-calling with offset=offset+limi"
        "t until has_more is false. Narrow first via label/file_pattern/min_degree before paginating large "
        "result sets."
    ),
    input_schema=(
        '{"type":"object","properties":{"project":{"type":"string"},"query":{"type":"strin'
        'g","description":"Natural-language or keyword full-text search using BM25 ranking. Tokens are '
        "split on whitespace; camelCase identifiers are indexed as individual words (updateCloudClient → up"
        "date, cloud, client). Results are ranked with structural boosting: Functions/Methods +10, Routes +"
        "8, Classes/Interfaces +5. Noise labels (File/Folder/Module/Variable) are filtered out. When provid"
        'ed, name_pattern is ignored."},"label":{"type":"string"},"name_pattern":{"type":"strin'
        'g"},"qn_pattern":{"type":"string"},"file_pattern":{"type":"string"},"relationship":'
        '{"type":"string"},"min_degree":{"type":"integer"},"max_degree":{"type":"integer"},'
        '"exclude_entry_points":{"type":"boolean"},"include_connected":{"type":"boolean"},"sem'
        'antic_query":{"type":"array","items":{"type":"string"},"description":"MUST be an ARR'
        'AY of keyword strings (e.g. [\\"send\\",\\"pubsub\\",\\"publish\\"]) — NOT a single string. '
        "Each keyword is scored independently via per-keyword min-cosine; results reflect functions that sc"
        "ore well on ALL keywords. Requires moderate/full index mode. Results appear in the 'semantic_resul"
        "ts' field (separate from 'results').\"},\"limit\":{\"type\":\"integer\",\"description\":\"Max resu"
        "lts per call. Default 50. Response carries 'total' (full match count) and 'has_more' (true if trun"
        'cated) so callers can detect the limit and paginate."},"offset":{"type":"integer","default'
        "\":0,\"description\":\"Skip the first N matching nodes. Combine with 'limit' to page: increment of"
        'fset by limit and re-call while has_more is true."},"format":{"type":"string","enum":["t'
        'ree","json"],"default":"tree","description":"Response encoding. tree (default): prefix-g'
        "rouped text rows. json: the SAME tree model as structured JSON (groups + column-ordered row arrays"
        ')."},"fields":{"type":"array","items":{"type":"string"},"description":"Extra per-n'
        "ode property columns, e.g. complexity, cognitive, signature, docstring, return_type, is_test, line"
        "s(int). Core row columns (qn/label/file/lines/in/out) are always present — do not request them her"
        'e. Missing values emit as empty cells."},"detail":{"type":"string","enum":["ids","defa'
        'ult"],"default":"default","description":"ids: bare qualified-name enumeration (one column)'
        ' — cheapest form for wide sweeps where per-row metadata is noise. default: full rows."}},"requir'
        'ed":["project"]}'
    ),
    annotations=(False, True, True, False),
)


@register
def search_graph(arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """Search graph：默认透传 cbm cli；具体定制在此扩展。"""
    return passthrough(TOOL, arguments, config)
