"""Verbatim MCP surface data for the codebase-memory-mcp re-implementation.

Source of truth: /home/delva/projects/codebase-memory-mcp/src/mcp/mcp.c at v0.10.8 (== HEAD for the
MCP surface). Extracted once, mechanically, from:
  * TOOLS[] table                     lines 376-701  (split per-tool into gh_puller_mcp/tools/)
  * TOOL_ANNOTATIONS[]                lines 710-731  (read_only/destructive/idempotent/open_world)
  * analysis_tools[]/scout_tools[]    lines 782-790
  * SUPPORTED_PROTOCOL_VERSIONS[]     lines 1257-1262
  * MCP_*_SERVER_INSTRUCTIONS[]       lines 1266-1293
  * cbm_mcp_prompts_list + templates  lines 1090-1232  (hand-transcribed)

Every string here is byte-identical to the C string (schema roundtrip asserted in tests).
"""

from __future__ import annotations

from dataclasses import dataclass

from gh_puller_mcp.tools import TOOL_ANNOTATIONS, TOOLS
from gh_puller_mcp.tools.base import ToolDef

__all__ = [
    "ANALYSIS_TOOLS",
    "DEFAULT_ANNOTATIONS",
    "INSTRUCTIONS_ALL",
    "INSTRUCTIONS_ANALYSIS",
    "INSTRUCTIONS_SCOUT",
    "MAX_HEADER_SIZE",
    "MAX_MESSAGE_SIZE",
    "PAGE_SIZE",
    "PROMPTS",
    "PROTOCOL_VERSIONS",
    "SCOUT_TOOLS",
    "TOOLS",
    "TOOL_ANNOTATIONS",
    "TRACE_CALL_PATH_ALIAS",
    "PromptArg",
    "PromptDef",
    "ToolDef",
]

#: tools/list page size (MCP_TOOLS_PAGE_SIZE in mcp.c).
PAGE_SIZE = 8
#: max MCP message body / line, bytes (MCP_MAX_MESSAGE_SIZE).
MAX_MESSAGE_SIZE = 10 * 1024 * 1024
#: max total header bytes for Content-Length framing (MCP_MAX_HEADER_SIZE).
MAX_HEADER_SIZE = 8 * 1024

PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
#: legacy dispatch alias accepted for trace_path (mcp.c dispatch_tool).
TRACE_CALL_PATH_ALIAS = "trace_call_path"


@dataclass(frozen=True)
class PromptArg:
    """One prompt argument definition (mcp_add_prompt_argument)."""

    name: str
    title: str
    description: str
    required: bool


@dataclass(frozen=True)
class PromptDef:
    """One prompt definition (prompts/list + prompts/get)."""

    name: str
    title: str
    description: str
    arguments: tuple[PromptArg, ...]
    get_description: str
    template: str  # %s substitution, positional


#: annotation fallback when a tool is missing from TOOL_ANNOTATIONS (mcp_add_tool_def).
DEFAULT_ANNOTATIONS: tuple[bool, bool, bool, bool] = (False, True, False, True)


ANALYSIS_TOOLS: frozenset[str] = frozenset((
    "search_graph",
    "query_graph",
    "trace_path",
    "get_code_snippet",
    "get_graph_schema",
    "get_architecture",
    "search_code",
    "list_projects",
    "index_status",
    "check_index_coverage",
    "detect_changes",
))


SCOUT_TOOLS: frozenset[str] = frozenset((
    "search_graph",
    "trace_path",
    "get_code_snippet",
    "get_architecture",
    "list_projects",
    "index_status",
    "check_index_coverage",
))


INSTRUCTIONS_ALL = (
        "Use graph tools first for structural code discovery: search_graph to find symbols, trace_path for callers "
        "and callees, get_code_snippet for exact source, query_graph for complex multi-hop patterns, and get_archit"
        "ecture for orientation. Use search_code or filesystem grep for literal or non-code text, or when graph cov"
        "erage is insufficient. Call list_projects before initial use and index_repository only when a repository i"
        "s not indexed or to force immediate freshness after a large external update. Once indexed, watched project"
        "s auto-refresh in the background; use index_status for project health and check_index_coverage for every c"
        "ited path and for scopes behind negative or exhaustive claims. Coverage is best-effort, never proof of com"
        "pleteness. Check has_more or nextCursor and paginate when present."
    )


INSTRUCTIONS_ANALYSIS = (
        "This is the analysis tool profile; graph and index mutation tools are unavailable. Use list_projects and i"
        "ndex_status to select a current graph project, then use search_graph, trace_path, get_code_snippet, query_"
        "graph, get_architecture, and search_code for read-only analysis. Call check_index_coverage for every cited"
        " path and for scopes behind negative or exhaustive claims; read flagged ranges or skipped files directly. "
        "Coverage is best-effort, never proof of completeness. Check has_more or nextCursor and paginate when prese"
        "nt. If the project is missing or stale, ask the parent agent to index or refresh it."
    )


INSTRUCTIONS_SCOUT = (
        "This is the scout tool profile; only the fast positive-discovery graph tools are available. Use list_proje"
        "cts and index_status to select a current graph project, then use search_graph, trace_path, get_code_snippe"
        "t, and get_architecture with narrow limits. Call check_index_coverage once for every cited path and read f"
        "lagged ranges directly. Findings are provisional: do not make absence, exhaustive-impact, or dead-code cla"
        "ims. If the project is missing or stale, ask the parent agent to index or refresh it."
    )


#: prompts/list + prompts/get definitions, verbatim (cbm_mcp_prompts_list, templates).
PROMPTS: tuple[PromptDef, ...] = (
    PromptDef(
        name="explore_codebase",
        title="Explore codebase",
        description="Explore a codebase with graph-first structural discovery.",
        arguments=(
            PromptArg("project", "Project", "Indexed project name from list_projects.", True),
            PromptArg("question", "Question", "Architecture or implementation question to investigate.", True),
        ),
        get_description="Graph-first codebase exploration",
        template=(
        'Explore project "%s" to answer: %s\n\nUse graph tools first: search_graph to find relevant symbols, get_'
        'code_snippet for exact source, and trace_path(direction="both") for callers and callees. Use get_archite'
        "cture for broad orientation and query_graph only for multi-hop patterns. Check has_more and paginate. Fall"
        " back to search_code or grep only for literal or non-code text, or where graph coverage is incomplete."
    ),
    ),
    PromptDef(
        name="review_change_impact",
        title="Review change impact",
        description="Review affected callers, tests, boundaries, and risks.",
        arguments=(
            PromptArg("project", "Project", "Indexed project name from list_projects.", True),
            PromptArg("change", "Change", "Change, symbol, or area whose impact should be reviewed.", True),
            PromptArg("base_branch", "Base branch", "Git branch or ref for detect_changes; defaults to main.", False),
        ),
        get_description="Graph-first change-impact review",
        template=(
        'Review change impact in project "%s" for: %s\n\nUse detect_changes with base_branch "%s", then trace_p'
        'ath(direction="both", include_tests=true) for affected callers, callees, and tests. Read exact definitio'
        "ns with get_code_snippet and use query_graph for cross-boundary patterns. Report affected callers, tests, "
        "boundaries, and risks; do not modify files."
    ),
    ),
)
