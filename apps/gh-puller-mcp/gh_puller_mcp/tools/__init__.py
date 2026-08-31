"""One module per tool: verbatim surface data + implementation bound together.

Importing this package registers every tool in manifest order (the C TOOLS[]
order, which tests/test_manifest.py pins against the C source). Extend a tool
by editing its own module — never tweak the schema, only behavior.
"""

from __future__ import annotations

from gh_puller_mcp.tools import (
    check_index_coverage,
    delete_project,
    detect_changes,
    get_architecture,
    get_code_snippet,
    get_graph_schema,
    index_repository,
    index_status,
    ingest_traces,
    list_projects,
    manage_adr,
    query_graph,
    search_code,
    search_graph,
    trace_path,
)
from gh_puller_mcp.tools.base import HANDLERS, ToolDef

__all__ = ["TOOLS", "TOOL_ANNOTATIONS", "TOOL_HANDLERS", "ToolDef", "tool_for"]

_TOOL_MODULES = (
    index_repository,
    search_graph,
    query_graph,
    trace_path,
    get_code_snippet,
    get_graph_schema,
    get_architecture,
    search_code,
    list_projects,
    delete_project,
    index_status,
    check_index_coverage,
    detect_changes,
    manage_adr,
    ingest_traces,
)

#: all tool definitions in C TOOLS[] order
TOOLS: tuple[ToolDef, ...] = tuple(module.TOOL for module in _TOOL_MODULES)
#: registry filled by @register; name -> implementation fn
TOOL_HANDLERS = HANDLERS
#: per-tool annotation table, keys in TOOLS order
TOOL_ANNOTATIONS: dict[str, tuple[bool, bool, bool, bool]] = {
    tool.name: tool.annotations for tool in TOOLS
}


def tool_for(name: str) -> ToolDef | None:
    return next((tool for tool in TOOLS if tool.name == name), None)
