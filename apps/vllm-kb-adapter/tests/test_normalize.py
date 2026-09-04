"""Compact upstream table normalization tests."""

from vllm_kb_adapter.normalize import normalize_result


def _result(data: dict) -> dict:
    return {
        "content": [{"type": "text", "text": "native"}],
        "structuredContent": data,
        "isError": False,
    }


def test_normalize_search_flat_and_grouped_tables() -> None:
    flat = normalize_result(
        "search_graph",
        _result({"cols": ["qn", "label"], "rows": [["pkg.f", "Function"]]}),
    )
    grouped = normalize_result(
        "search_graph",
        _result(
            {
                "cols": ["name", "label"],
                "groups": [
                    {"qn_prefix": "pkg.C", "file": "pkg/c.py", "rows": [["f", "Method"]]},
                ],
            },
        ),
    )

    assert flat["structuredContent"]["rows"] == [{"qn": "pkg.f", "label": "Function"}]
    assert grouped["structuredContent"]["rows"] == [
        {"name": "f", "label": "Method", "qn": "pkg.C.f", "file": "pkg/c.py"},
    ]


def test_normalize_trace_and_query_tables() -> None:
    trace = normalize_result(
        "trace_path",
        _result(
            {
                "callees": {
                    "cols": ["name", "hop"],
                    "groups": [{"qn_prefix": "pkg", "rows": [["f", 1]]}],
                },
                "next_cursor": "token",
            },
        ),
    )
    query = normalize_result(
        "query_graph",
        _result({"columns": ["qn", "count"], "rows": [["pkg.f", 2]]}),
    )

    assert trace["structuredContent"]["callees"] == [{"name": "f", "hop": 1, "qn": "pkg.f"}]
    assert trace["structuredContent"]["next"] == "token"
    assert query["structuredContent"]["rows"] == [{"qn": "pkg.f", "count": 2}]


def test_normalize_new_tool_tables() -> None:
    search = normalize_result(
        "search_code",
        _result(
            {
                "cols": ["qn", "file"],
                "rows": [["pkg.f", "pkg/f.py"]],
                "raw_matches": {
                    "cols": ["file", "line"],
                    "rows": [["pkg/f.py", 3]],
                },
            },
        ),
    )
    architecture = normalize_result(
        "get_architecture",
        _result(
            {
                "project": "project",
                "clusters": {
                    "cols": ["label", "member_count"],
                    "rows": [["core", 4]],
                },
            },
        ),
    )

    assert search["structuredContent"]["rows"] == [{"qn": "pkg.f", "file": "pkg/f.py"}]
    assert search["structuredContent"]["raw_matches"]["rows"] == [
        {"file": "pkg/f.py", "line": 3},
    ]
    assert architecture["structuredContent"]["clusters"] == [
        {"label": "core", "member_count": 4},
    ]


def test_preserve_tool_error() -> None:
    result = {"content": [], "isError": True}

    assert normalize_result("search_graph", result) is result
