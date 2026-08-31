"""Semantic dispatch tests: envelope rules and tools/call semantics.

dispatch_tool_call returns an SDK CallToolResult; mcp_text_result pins the
envelope rules it builds on.
"""

from __future__ import annotations

from gh_puller_mcp.backend import BackendError
from gh_puller_mcp.server import ServerConfig, dispatch_tool_call, mcp_text_result


def cfg_(**kwargs) -> ServerConfig:
    return ServerConfig(version="0.10.8", **kwargs)


# ── envelope rules ──


def test_envelope_json_object_text_gets_structured_content() -> None:
    assert mcp_text_result('{"a": 1}', False) == {
        "content": [{"type": "text", "text": '{"a": 1}'}],
        "structuredContent": {"a": 1},
        "isError": False,
    }


def test_envelope_error_text_uses_error_structured_content() -> None:
    env = mcp_text_result("boom", True)
    assert env["structuredContent"] == {"error": "boom"}
    assert env["isError"] is True


def test_envelope_success_non_object_omits_structured_content() -> None:
    env = mcp_text_result("plain text", False)
    assert "structuredContent" not in env
    assert env["isError"] is False


# ── tools/call semantics (CallToolResult) ──


def test_call_unknown_tool() -> None:
    config = cfg_(call_tool=lambda n, a: (_ for _ in ()).throw(AssertionError("no backend call")))
    result = dispatch_tool_call("bogus_tool", {}, config)
    assert result.content[0].text == "unknown tool: bogus_tool"
    assert result.structured_content == {"error": "unknown tool: bogus_tool"}
    assert result.is_error is True


def test_call_valid_passes_through_envelope() -> None:
    seen: list[tuple[str, dict]] = []
    config = cfg_(
        call_tool=lambda n, a: (
            seen.append((n, a))
            or {
                "content": [{"type": "text", "text": '{"rows": [[1]]}'}],
                "structuredContent": {"rows": [[1]]},
                "isError": False,
            }
        ),
    )
    result = dispatch_tool_call("query_graph", {"query": "MATCH (n) RETURN n"}, config)
    assert result.content[0].text == '{"rows": [[1]]}'
    assert result.structured_content == {"rows": [[1]]}
    assert result.is_error is False
    assert seen == [("query_graph", {"query": "MATCH (n) RETURN n"})]


def test_call_arguments_default_and_passthrough() -> None:
    seen: list[tuple[str, dict]] = []
    config = cfg_(call_tool=lambda n, a: (seen.append((n, a)) or mcp_text_result("ok", False)))
    dispatch_tool_call("list_projects", None, config)
    assert seen == [("list_projects", {})]

    args = {"limit": 1, "include_details": True}
    dispatch_tool_call("list_projects", args, config)
    assert seen[-1] == ("list_projects", args)

    # non-object arguments are forwarded as-is (yyjson semantics)
    dispatch_tool_call("list_projects", [1, 2], config)
    assert seen[-1] == ("list_projects", [1, 2])


def test_call_trace_call_path_alias() -> None:
    seen: list[tuple[str, dict]] = []
    config = cfg_(call_tool=lambda n, a: (seen.append((n, a)) or mcp_text_result("ok", False)))
    dispatch_tool_call("trace_call_path", {"function_name": "f"}, config)
    assert seen == [("trace_path", {"function_name": "f"})]


def test_call_backend_error_becomes_error_envelope() -> None:
    config = cfg_(call_tool=lambda n, a: (_ for _ in ()).throw(BackendError("backend timed out")))
    result = dispatch_tool_call("list_projects", {}, config)
    assert result.content[0].text == "backend error: backend timed out"
    assert result.structured_content == {"error": "backend error: backend timed out"}
    assert result.is_error is True
