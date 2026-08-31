"""--tool-profile analysis|scout: instruction selection, surface, dispatch blocking."""

from __future__ import annotations

from gh_puller_mcp.server import (
    ServerConfig,
    dispatch_tool_call,
    instructions_for,
    tools_list_page,
)


def test_instructions_per_profile() -> None:
    assert instructions_for("all").startswith("Use graph tools first for structural code discovery:")
    assert instructions_for("analysis").startswith("This is the analysis tool profile")
    assert instructions_for("scout").startswith("This is the scout tool profile")


def test_analysis_surface_is_eleven() -> None:
    page = tools_list_page("analysis", None)
    assert [tool.name for tool in page.tools] == [
        "search_graph", "query_graph", "trace_path", "get_code_snippet", "get_graph_schema",
        "get_architecture", "search_code", "list_projects", "index_status",
        "check_index_coverage", "detect_changes",
    ]


def test_scout_surface_is_seven() -> None:
    assert [tool.name for tool in tools_list_page("scout", None).tools] == [
        "search_graph", "trace_path", "get_code_snippet", "get_architecture",
        "list_projects", "index_status", "check_index_coverage",
    ]


def test_profile_cursor_pagination_against_shortened_lists() -> None:
    p0 = tools_list_page("analysis", {"cursor": "0"})
    assert len(p0.tools) == 8
    assert p0.next_cursor == "8"
    p8 = tools_list_page("analysis", {"cursor": "8"})
    assert len(p8.tools) == 3
    assert p8.next_cursor is None, "8+3 covers all 11"
    # scout: cursor 0 -> 7 tools, everything, no nextCursor
    assert tools_list_page("scout", {"cursor": "0"}).next_cursor is None
    # invalid cursor in analysis -> offset 15 clamps to 11 -> empty page
    assert tools_list_page("analysis", {"cursor": "abc"}).tools == []


def test_blocked_call_message_and_no_backend_call() -> None:
    calls: list[tuple[str, dict]] = []
    config = ServerConfig(profile="analysis", version="0.10.8", call_tool=lambda n, a: (calls.append((n, a)) or {}))
    result = dispatch_tool_call("delete_project", {"project": "x"}, config)
    message = "tool 'delete_project' is not available in the analysis tool profile"
    assert result.content[0].text == message
    assert result.structured_content == {"error": message}
    assert result.is_error is True
    assert calls == []


def test_scout_profile_name_in_message() -> None:
    config = ServerConfig(profile="scout", version="0.10.8", call_tool=lambda n, a: _noop())
    result = dispatch_tool_call("query_graph", {}, config)
    assert result.content[0].text == "tool 'query_graph' is not available in the scout tool profile"


def test_alias_is_profile_checked_before_aliasing() -> None:
    # C dispatch checks the name against the profile BEFORE the trace_call_path alias swap
    config = ServerConfig(profile="analysis", version="0.10.8", call_tool=lambda n, a: _noop())
    result = dispatch_tool_call("trace_call_path", {}, config)
    assert result.content[0].text == (
        "tool 'trace_call_path' is not available in the analysis tool profile"
    )


def _noop() -> dict:
    raise AssertionError("backend must not run")
