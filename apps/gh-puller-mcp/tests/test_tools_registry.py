"""Per-tool module registry tests: aggregation, binding, and register validation."""

from __future__ import annotations

import sys
from types import ModuleType

import mcp_types as types
import pytest

from gh_puller_mcp.server import ServerConfig, dispatch_tool_call
from gh_puller_mcp.tools import TOOL_ANNOTATIONS, TOOL_HANDLERS, TOOLS, tool_for
from gh_puller_mcp.tools import base as tools_base
from gh_puller_mcp.tools.base import ToolDef, register

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


def test_ordering_and_aggregation() -> None:
    assert [t.name for t in TOOLS] == EXPECTED_ORDER  # C TOOLS[] order (wire-visible)
    assert TOOL_ANNOTATIONS == EXPECTED_ANNOTATIONS
    assert set(TOOL_HANDLERS) == set(EXPECTED_ORDER)  # lookup keyed by name; order irrelevant
    assert {tool_for(t.name).name for t in TOOLS} == set(EXPECTED_ORDER)
    assert tool_for("no_such_tool") is None


def test_every_tool_module_binds_fn_named_after_tool() -> None:
    for tool in TOOLS:
        handler = TOOL_HANDLERS[tool.name]
        assert handler.__name__ == tool.name
        assert handler.__module__ == f"gh_puller_mcp.tools.{tool.name}"


def test_handler_default_passthrough_behavior() -> None:
    seen: list[tuple[str, dict]] = []
    config = ServerConfig(
        version="0.10.8",
        call_tool=lambda n, a: (seen.append((n, a)) or tools_base.mcp_text_result('{"ok": true}', False)),
    )
    result = TOOL_HANDLERS["list_projects"]({"limit": 1}, config)
    assert result.content[0].text == '{"ok": true}'
    assert result.structured_content == {"ok": True}
    assert result.is_error is False
    assert seen == [("list_projects", {"limit": 1})]


def test_register_rejects_module_without_tool() -> None:
    def orphan(arguments: dict, config: ServerConfig) -> types.CallToolResult:
        ...

    with pytest.raises(TypeError, match="has no TOOL definition"):
        register(orphan)


def test_register_rejects_fn_name_mismatch(monkeypatch) -> None:
    fake_module = ModuleType("fake_tool_module")
    fake_module.TOOL = ToolDef("search_graph", "t", "d", '{"type":"object"}', (False, True, True, False))
    monkeypatch.setitem(sys.modules, "fake_tool_module", fake_module)

    def wrong_name(arguments: dict, config: ServerConfig) -> types.CallToolResult:
        ...

    wrong_name.__name__ = "not_the_tool_name"
    wrong_name.__module__ = "fake_tool_module"
    with pytest.raises(ValueError, match="!= tool name"):
        register(wrong_name)


def test_dispatch_uses_registered_handler(monkeypatch) -> None:
    """dispatch routes through the registry: a custom handler is actually called."""
    seen: list[str] = []

    def custom(arguments: dict, config: ServerConfig) -> types.CallToolResult:
        seen.append(arguments["marker"])
        return types.CallToolResult(content=[types.TextContent(type="text", text="custom")], is_error=False)

    monkeypatch.setitem(TOOL_HANDLERS, "query_graph", custom)
    config = ServerConfig(version="0.10.8")
    result = dispatch_tool_call("query_graph", {"marker": "hit"}, config)
    assert seen == ["hit"]
    assert result.content[0].text == "custom"
