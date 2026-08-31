"""search_code: Search code verbatim definition (from src/mcp/mcp.c) and its implementation.

The schema/title/description/annotations here are byte-identical to the C
server (see gh_puller_mcp/tests/test_manifest.py); edit this file only to change
search_code's behavior, never to tweak the schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gh_puller_mcp.tools.base import ToolDef, passthrough, register

if TYPE_CHECKING:
    import mcp_types as types

    from gh_puller_mcp.config import ServerConfig

TOOL = ToolDef(
    name="search_code",
    title="Search code",
    description=(
        "Graph-augmented code search. Finds text patterns via grep, then enriches results with the knowledg"
        "e graph: deduplicates matches into containing functions, ranks by structural importance (definitio"
        "ns first, popular functions next, tests last). Modes: compact (default, signatures only — token ef"
        "ficient), full (source capped at a 60-line window around the first match per hit; source_truncated"
        " marks the cut — use get_code_snippet for the complete symbol), files (just file paths). Use path_"
        "filter regex to scope results. TRUNCATION: enriched results are capped at limit (default 10). Resp"
        "onse carries 'total_grep_matches' (raw grep hit count) and 'total_results' (deduplicated function "
        "count) — compare to limit to detect truncation. There is no offset parameter; to see more, raise l"
        "imit or narrow the query with file_pattern / path_filter."
    ),
    input_schema=(
        '{"type":"object","properties":{"pattern":{"type":"string"},"project":{"type":"str'
        'ing"},"file_pattern":{"type":"string","description":"Glob for grep --include (e.g. *.go)'
        '"},"path_filter":{"type":"string","description":"Regex filter on result file paths (e.g.'
        ' ^src/ or \\\\.(go|ts)$)"},"mode":{"type":"string","enum":["compact","full","files"'
        '],"default":"compact","description":"compact: signatures+metadata (default). full: with sou'
        'rce. files: just file list."},"context":{"type":"integer","description":"Lines of contex'
        't around each match (like grep -C). Only used in compact mode."},"regex":{"type":"boolean",'
        '"default":false},"debug":{"type":"boolean","default":false,"description":"Include sco'
        'pe_ms, scan_ms, and enrich_ms phase timing diagnostics."},"limit":{"type":"integer","descr'
        "iption\":\"Max enriched results per call. Default 10. Response includes 'total_grep_matches' and '"
        "total_results' so callers can detect truncation. No offset parameter — raise limit or narrow with "
        'file_pattern / path_filter to see more.","default":10,"minimum":1}},"required":["pattern"'
        ',"project"]}'
    ),
    annotations=(False, True, True, False),
)


@register
def search_code(arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """Search code：默认透传 cbm cli；具体定制在此扩展。"""
    return passthrough(TOOL, arguments, config)
