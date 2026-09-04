"""Verify the baked manifest against an explicitly supplied upstream C source."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from gh_puller_mcp import manifest

pytestmark = pytest.mark.e2e

_SOURCE_ENV = "GH_PULLER_MCP_C_SOURCE"
_LIT = re.compile(r'"((?:\\.|[^"\\])*)"')


@pytest.fixture(scope="module")
def c_source() -> Path:
    value = os.environ.get(_SOURCE_ENV)
    if not value:
        pytest.fail(f"{_SOURCE_ENV} must point to the pinned upstream mcp.c")
    path = Path(value)
    if not path.is_file():
        pytest.fail(f"{_SOURCE_ENV} is not a file: {path}")
    return path


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
    match = re.search(r"\bTOOLS\[\]\s*=\s*\{(.*?)^\};", source_text, re.DOTALL | re.MULTILINE)
    assert match, "TOOLS table not found"
    tools = []
    for entry in re.split(r"}\s*,\s*[\n\r]+\s*\{", match.group(1)):
        text = entry.strip()
        if not text:
            continue
        name_match = re.match(r'\{?\s*"([a-z_][a-z_0-9]*)"\s*,\s*', text)
        assert name_match, text[:80]
        name = name_match.group(1)
        literals = [_unescape(value) for value in _LIT.findall(text[name_match.end():])]
        schema_index = next((index for index, value in enumerate(literals) if value.startswith('{"type"')), None)
        assert schema_index is not None, name
        tools.append(
            {
                "name": name,
                "title": literals[0],
                "description": "".join(literals[1:schema_index]),
                "input_schema": "".join(literals[schema_index:]),
            },
        )
    return tools


def _extract_instructions(source_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, name in [
        ("all", "MCP_SERVER_INSTRUCTIONS"),
        ("analysis", "MCP_ANALYSIS_SERVER_INSTRUCTIONS"),
        ("scout", "MCP_SCOUT_SERVER_INSTRUCTIONS"),
    ]:
        pattern = rf'static const char {name}\[\]\s*=\s*((?:"(?:\\.|[^"\\])*"\s*)+);'
        match = re.search(pattern, source_text, re.DOTALL)
        assert match, name
        out[key] = "".join(_unescape(value) for value in _LIT.findall(match.group(1)))
    return out


def test_manifest_matches_c_source(c_source: Path) -> None:
    source_text = c_source.read_text(encoding="utf-8")
    tools = _extract_tools(source_text)
    assert [tool["name"] for tool in tools] == [tool.name for tool in manifest.TOOLS]
    for fresh, baked in zip(tools, manifest.TOOLS, strict=True):
        assert fresh["title"] == baked.title, fresh["name"]
        assert fresh["description"] == baked.description, fresh["name"]
        assert fresh["input_schema"] == baked.input_schema, fresh["name"]
    instructions = _extract_instructions(source_text)
    assert instructions["all"] == manifest.INSTRUCTIONS_ALL
    assert instructions["analysis"] == manifest.INSTRUCTIONS_ANALYSIS
    assert instructions["scout"] == manifest.INSTRUCTIONS_SCOUT
