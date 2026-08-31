"""Manifest fidelity tests: the verbatim codebase-memory-mcp surface data.

Self-contained assertions cover every field. A gate test (@C_SOURCE_AVAILABLE)
re-extracts the TOOLS table and instructions from the C source byte-for-byte and
compares against the baked-in manifest, so any transcription drift is caught.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from gh_puller_mcp import manifest

C_SOURCE = Path("/home/delva/projects/codebase-memory-mcp/src/mcp/mcp.c")

EXPECTED_ORDER = [
    "index_repository",
    "search_graph",
    "query_graph",
    "trace_path",
    "get_code_snippet",
    "get_graph_schema",
    "get_architecture",
    "search_code",
    "list_projects",
    "delete_project",
    "index_status",
    "check_index_coverage",
    "detect_changes",
    "manage_adr",
    "ingest_traces",
]

EXPECTED_ANNOTATIONS = {
    "index_repository": (False, False, True, False),
    "search_graph": (False, True, True, False),
    "query_graph": (False, True, True, False),
    "trace_path": (False, True, True, False),
    "get_code_snippet": (False, True, True, False),
    "get_graph_schema": (False, True, True, False),
    "get_architecture": (False, True, True, False),
    "search_code": (False, True, True, False),
    "list_projects": (True, False, True, False),
    "delete_project": (False, True, True, False),
    "index_status": (False, True, True, False),
    "check_index_coverage": (False, True, True, False),
    "detect_changes": (False, True, True, False),
    "manage_adr": (False, True, False, False),
    "ingest_traces": (False, False, False, False),
}

EXPECTED_ANALYSIS = [
    "search_graph", "query_graph", "trace_path", "get_code_snippet", "get_graph_schema",
    "get_architecture", "search_code", "list_projects", "index_status",
    "check_index_coverage", "detect_changes",
]

EXPECTED_SCOUT = [
    "search_graph", "trace_path", "get_code_snippet", "get_architecture",
    "list_projects", "index_status", "check_index_coverage",
]


def test_tool_count_and_order() -> None:
    assert [t.name for t in manifest.TOOLS] == EXPECTED_ORDER


def test_every_field_populated() -> None:
    for tool in manifest.TOOLS:
        assert tool.title
        assert tool.description
        assert tool.input_schema
        assert len(tool.annotations) == 4


def test_schemas_roundtrip_compact() -> None:
    """json.loads -> compact re-serialization must reproduce the schema verbatim."""
    for tool in manifest.TOOLS:
        parsed = json.loads(tool.input_schema)
        assert json.dumps(parsed, ensure_ascii=False, separators=(",", ":")) == tool.input_schema, tool.name


def test_annotations_match_table() -> None:
    assert manifest.TOOL_ANNOTATIONS == EXPECTED_ANNOTATIONS
    for tool in manifest.TOOLS:
        assert tool.annotations == EXPECTED_ANNOTATIONS[tool.name]
    assert manifest.DEFAULT_ANNOTATIONS == (False, True, False, True)


def test_profiles() -> None:
    assert sorted(manifest.ANALYSIS_TOOLS) == sorted(EXPECTED_ANALYSIS)
    assert sorted(manifest.SCOUT_TOOLS) == sorted(EXPECTED_SCOUT)
    assert manifest.SCOUT_TOOLS <= manifest.ANALYSIS_TOOLS
    assert {t.name for t in manifest.TOOLS} >= manifest.ANALYSIS_TOOLS


def test_instructions() -> None:
    assert manifest.INSTRUCTIONS_ALL.startswith("Use graph tools first for structural code discovery:")
    assert manifest.INSTRUCTIONS_ALL.endswith("paginate when present.")
    assert manifest.INSTRUCTIONS_ANALYSIS.startswith("This is the analysis tool profile")
    assert manifest.INSTRUCTIONS_ANALYSIS.endswith("index or refresh it.")
    assert manifest.INSTRUCTIONS_SCOUT.startswith("This is the scout tool profile")
    assert manifest.INSTRUCTIONS_SCOUT.endswith("index or refresh it.")


def test_prompts_static_surface() -> None:
    assert [p.name for p in manifest.PROMPTS] == ["explore_codebase", "review_change_impact"]
    explore, review = manifest.PROMPTS
    assert explore.title == "Explore codebase"
    assert explore.description == "Explore a codebase with graph-first structural discovery."
    assert explore.get_description == "Graph-first codebase exploration"
    assert [(a.name, a.required) for a in explore.arguments] == [("project", True), ("question", True)]
    assert review.title == "Review change impact"
    assert review.description == "Review affected callers, tests, boundaries, and risks."
    assert review.get_description == "Graph-first change-impact review"
    assert [(a.name, a.required) for a in review.arguments] == [
        ("project", True), ("change", True), ("base_branch", False),
    ]
    assert explore.template.count("%s") == 2
    assert review.template.count("%s") == 3
    assert explore.template.startswith('Explore project "%s" to answer: %s\n\n')
    assert review.template.startswith('Review change impact in project "%s" for: %s\n\n')
    assert review.template.endswith("do not modify files.")


# ── byte-level re-extraction gate against the C source (this machine only) ──

_LIT = re.compile(r'"((?:\\.|[^"\\])*)"')


def _unescape(s: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(nxt, "\\" + nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _extract_tools(source_text: str) -> list[dict[str, str]]:
    lines = source_text.splitlines()
    body = "\n".join(lines[375:701])
    m = re.search(r"TOOLS\[\]\s*=\s*\{(.*)\};", body, re.DOTALL)
    assert m, "TOOLS table not found"
    tools = []
    for entry in re.split(r"}\s*,\s*[\n\r]+\s*\{", m.group(1)):
        text = entry.strip()
        if not text:
            continue
        name_m = re.match(r'\{?\s*"([a-z_][a-z_0-9]*)"\s*,\s*', text)
        assert name_m, text[:80]
        name = name_m.group(1)
        lits = [_unescape(x) for x in _LIT.findall(text[name_m.end():])]
        schema_i = next((i for i, s in enumerate(lits) if s.startswith('{"type"')), None)
        assert schema_i is not None, name
        tools.append({
            "name": name,
            "title": lits[0],
            "description": "".join(lits[1:schema_i]),
            "input_schema": "".join(lits[schema_i:]),
        })
    return tools


def _extract_instructions(source_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, name in [("all", "MCP_SERVER_INSTRUCTIONS"), ("analysis", "MCP_ANALYSIS_SERVER_INSTRUCTIONS"),
                      ("scout", "MCP_SCOUT_SERVER_INSTRUCTIONS")]:
        pattern = rf'static const char {name}\[\]\s*=\s*((?:"(?:\\.|[^"\\])*"\s*)+);'
        m = re.search(pattern, source_text, re.DOTALL)
        assert m, name
        out[key] = "".join(_unescape(x) for x in _LIT.findall(m.group(1)))
    return out


@pytest.mark.skipif(not C_SOURCE.exists(), reason="C source checkout not present")
def test_manifest_matches_c_source() -> None:
    source_text = C_SOURCE.read_text(encoding="utf-8")
    tools = _extract_tools(source_text)
    assert [t["name"] for t in tools] == [t.name for t in manifest.TOOLS]
    for fresh, baked in zip(tools, manifest.TOOLS, strict=True):
        assert fresh["title"] == baked.title, fresh["name"]
        assert fresh["description"] == baked.description, fresh["name"]
        assert fresh["input_schema"] == baked.input_schema, fresh["name"]
    instructions = _extract_instructions(source_text)
    assert instructions["all"] == manifest.INSTRUCTIONS_ALL
    assert instructions["analysis"] == manifest.INSTRUCTIONS_ANALYSIS
    assert instructions["scout"] == manifest.INSTRUCTIONS_SCOUT
