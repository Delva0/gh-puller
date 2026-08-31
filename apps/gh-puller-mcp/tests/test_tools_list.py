"""tools/list pagination: exact C algorithm (mcp.c mcp_tools_cursor_offset / tools_list_page).

tools_list_page returns the SDK ListToolsResult model directly.
"""

from __future__ import annotations

import json

import pytest

from gh_puller_mcp.manifest import TOOLS
from gh_puller_mcp.server import tools_list_page


def names(page) -> list[str]:
    return [tool.name for tool in page.tools]


def test_no_cursor_returns_all_without_pagination() -> None:
    for params in (None, {}, "not-an-object", [], 5):
        page = tools_list_page("all", params)
        assert len(page.tools) == 15, params
        assert page.next_cursor is None
        assert names(page) == [t.name for t in TOOLS]


def test_per_entry_shape_and_key_order() -> None:
    page = tools_list_page("all", None)
    tool = page.tools[0]
    assert tool.name == TOOLS[0].name
    assert tool.title == TOOLS[0].title
    assert tool.description == TOOLS[0].description
    assert tool.input_schema == json.loads(TOOLS[0].input_schema)
    assert tool.output_schema is None  # deliberately no outputSchema on any tool


def test_cursor_walks_pages_of_eight() -> None:
    p0 = tools_list_page("all", {"cursor": "0"})
    assert len(p0.tools) == 8
    assert p0.next_cursor == "8"
    p8 = tools_list_page("all", {"cursor": "8"})
    assert len(p8.tools) == 7
    assert p8.next_cursor is None
    assert names(p0) + names(p8) == [t.name for t in TOOLS]


def test_cursor_page_boundaries() -> None:
    assert len(tools_list_page("all", {"cursor": "4"}).tools) == 8
    assert tools_list_page("all", {"cursor": "4"}).next_cursor == "12"
    assert len(tools_list_page("all", {"cursor": "12"}).tools) == 3
    assert tools_list_page("all", {"cursor": "12"}).next_cursor is None


def test_cursor_at_and_beyond_end_is_empty() -> None:
    assert tools_list_page("all", {"cursor": "15"}).tools == []
    assert tools_list_page("all", {"cursor": "16"}).tools == []


@pytest.mark.parametrize(
    "cursor",
    ["", "abc", "-1", "--1", "12x", "1 2", "∑"],
)
def test_invalid_cursor_strings_fall_back_to_end(cursor: str) -> None:
    page = tools_list_page("all", {"cursor": cursor})
    assert page.tools == []
    assert page.next_cursor is None


def test_signed_and_spaced_cursors_parse_like_strtol() -> None:
    assert names(tools_list_page("all", {"cursor": "+3"}))[0] == TOOLS[3].name
    assert names(tools_list_page("all", {"cursor": "  4"}))[0] == TOOLS[4].name
    assert names(tools_list_page("all", {"cursor": "\t4"}))[0] == TOOLS[4].name
    assert names(tools_list_page("all", {"cursor": "003"}))[0] == TOOLS[3].name
    # trailing junk (including a trailing space) fails strtol's endptr-at-NUL rule
    assert tools_list_page("all", {"cursor": " 4 "}).tools == []


@pytest.mark.parametrize("cursor", [True, 5, 5.5, [], {}, None])
def test_non_string_cursor_is_invalid(cursor) -> None:
    # the cursor KEY must exist; a non-string value is invalid -> empty page
    assert tools_list_page("all", {"cursor": cursor}).tools == []


def test_annotations_spot_checks() -> None:
    by_name = {tool.name: tool.annotations for tool in tools_list_page("all", None).tools}
    assert by_name["index_repository"] == types_annotations(False, False, True, False)
    assert by_name["list_projects"] == types_annotations(True, False, True, False)
    assert by_name["ingest_traces"] == types_annotations(False, False, False, False)


def types_annotations(read_only: bool, destructive: bool, idempotent: bool, open_world: bool):
    """Shortcut: construct the expected ToolAnnotations model."""
    import mcp_types as types

    return types.ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=open_world,
    )
