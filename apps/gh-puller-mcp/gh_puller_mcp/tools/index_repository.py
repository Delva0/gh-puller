"""index_repository: Index repository verbatim definition (from src/mcp/mcp.c) and its implementation.

The schema/title/description/annotations here are byte-identical to the C
server (see gh_puller_mcp/tests/test_manifest.py); edit this file only to change
index_repository's behavior, never to tweak the schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gh_puller_mcp.tools.base import ToolDef, passthrough, register

if TYPE_CHECKING:
    import mcp_types as types

    from gh_puller_mcp.config import ServerConfig

TOOL = ToolDef(
    name="index_repository",
    title="Index repository",
    description=(
        "Index a repository into the knowledge graph. Special mode 'cross-repo-intelligence': skip extracti"
        "on, only match Routes/Channels across projects to create CROSS_HTTP_CALLS/CROSS_ASYNC_CALLS/CROSS_"
        "CHANNEL edges. Requires target_projects param. Ensure target projects have fresh indexes first. CO"
        "VERAGE: the response reports files that were NOT fully indexed — 'skipped' (not indexed at all: ov"
        "ersized/read/parse failures) and 'parse_partial' (indexed, but constructs inside the listed line r"
        "anges could not be parsed and MAY be missing from the graph). The embedded lists carry counts plus"
        " a FEW EXAMPLES only; the complete lists are in the per-run 'logfile' (path in the response) and q"
        'ueryable any time via index_status or structurally via query_graph(graph="missed"). Both signals'
        " are best-effort: absence of a flag is NOT a completeness guarantee; prefer grep inside flagged ra"
        "nges. Separately, 'excluded' + 'not_indexed_files' list what was deliberately NOT indexed (gitigno"
        "re/.cbmignore/skip-lists) — by design, not failures."
    ),
    input_schema=(
        '{"type":"object","properties":{"repo_path":{"type":"string","description":"Path to '
        'the repository"},"mode":{"type":"string","enum":["full","moderate","fast","cross-r'
        'epo-intelligence"],"default":"full","description":"All modes run type-aware LSP call/usage'
        " resolution (per-file + cross-file). full: all files + similarity/semantic edges. moderate: filter"
        "ed files + similarity/semantic. fast: filtered files, no similarity/semantic. cross-repo-intellige"
        'nce: match Routes/Channels across projects."},"target_projects":{"type":"array","items":{'
        '"type":"string"},"description":"Projects to search for cross-repo links (cross-repo-intelli'
        'gence mode). Use [\\"*\\"] for all indexed projects. Run list_projects to see available projects'
        '."},"name":{"type":"string","description":"Override the derived project name. Non-ASCII '
        'bytes are encoded and unsafe path characters are normalized."},"persistence":{"type":"boolea'
        'n","default":false,"description":"Write compressed artifact to .codebase-memory/graph.db.zst'
        ' for team sharing. Teammates can bootstrap from the artifact instead of full re-indexing."}},"re'
        'quired":["repo_path"]}'
    ),
    annotations=(False, False, True, False),
)


@register
def index_repository(arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """Index repository：默认透传 cbm cli；具体定制在此扩展。"""
    return passthrough(TOOL, arguments, config)
