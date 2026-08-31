"""index_status: Index status verbatim definition (from src/mcp/mcp.c) and its implementation.

The schema/title/description/annotations here are byte-identical to the C
server (see gh_puller_mcp/tests/test_manifest.py); edit this file only to change
index_status's behavior, never to tweak the schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gh_puller_mcp.tools.base import ToolDef, passthrough, register

if TYPE_CHECKING:
    import mcp_types as types

    from gh_puller_mcp.config import ServerConfig

TOOL = ToolDef(
    name="index_status",
    title="Index status",
    description=(
        "Get the indexing status of a project: node/edge counts, root path, git context, and the indexing-C"
        "OVERAGE report — which files the indexer could NOT fully cover (best-effort signal): 'parse_partia"
        "l' files WERE indexed but contain line ranges tree-sitter could not parse — constructs there MAY b"
        "e missing from the graph (some are still recovered); 'skipped' files were not indexed at all (over"
        "sized/read/parse failure). Use this before trusting graph completeness on a file: if a file is lis"
        "ted, ALSO grep it (especially the flagged ranges). IMPORTANT: absence from these lists is NOT a co"
        "mpleteness guarantee — the signal only marks what the indexer can detect. For structural queries o"
        "ver the misses use query_graph(graph=\"missed\"). The report also carries 'not_indexed' — files/di"
        "rs excluded BY DESIGN (gitignore/.cbmignore/skip-lists): deliberate and deterministic, not failure"
        "s; change the ignore rules and re-index to include them."
    ),
    input_schema=(
        '{"type":"object","properties":{"project":{"type":"string"},"verbose":{"type":"boo'
        'lean","default":false,"description":"Include the git context block (worktree/shadow path var'
        "iants). Only needed when debugging where an index lives — omitted by default to keep the status le"
        'an."}},"required":["project"]}'
    ),
    annotations=(False, True, True, False),
)


@register
def index_status(arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """Index status：默认透传 cbm cli；具体定制在此扩展。"""
    return passthrough(TOOL, arguments, config)
