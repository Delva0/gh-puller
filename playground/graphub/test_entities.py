"""Keep diagnostic entity matching, graph evidence, and native coordinate ambiguity explicit."""
# ruff: noqa: INP001, S101 - Standalone research tests use pytest assertions.

from hashlib import sha256
from types import SimpleNamespace

import pytest

from . import entity_search
from .entity_search import candidate_scopes, native_locations, probe, relationships, verify_inputs
from .stack_search import symbols


@pytest.mark.parametrize(("lines", "expected"), [("", (0, 0)), ("7", (7, 7)), ("7-9", (7, 9))])
def test_native_compact_line_coordinates(lines, expected):
    response = {"groups": [{"qn_prefix": "project.pkg", "file": "pkg", "rows": [["pkg", "Folder", lines]]}]}
    node = symbols(response)[0]
    assert (node["start"], node["end"]) == expected


def test_folder_matches_remain_visible_without_becoming_file_histories():
    response = {"groups": [{"qn_prefix": "project", "file": "PackageName",
                            "rows": [["PackageName", "Folder", "", 0, 0]]}], "has_more": False}

    def call(name, _arguments):
        assert name == "search_graph"
        return {"structuredContent": response}

    result = probe(SimpleNamespace(call_tool=call), "TypeError: PackageName unavailable", [], {"PackageName/a.py"})
    assert result["mentions"][0]["symbols"][0]["start"] == 0
    assert result["seeds"] == [] and candidate_scopes(result, {"PackageName/a.py"}) == []


def test_native_coordinates_keep_qualified_paths_without_basename_guessing():
    log = ("C:\\checkout\\pkg\\a.cc:3:4: error: missing symbol\n"
           "/external/a.cc:5:6: error: external failure\n"
           "/checkout/pkg/a.cc:9:1: note: declaration is here\n")
    locations = native_locations(log, {"pkg/a.cc", "a.cc"})
    assert len(locations) == 2
    assert locations[0]["file"] == "pkg/a.cc"
    assert locations[0]["line"] == 3 and locations[0]["column"] == 4
    assert locations[1]["file"] is None and locations[1]["path_candidates"] == ["a.cc"]
    assert log[locations[0]["offset"]:locations[0]["end"]].endswith("error:")


def test_native_query_uses_a_sentinel_row_for_truncation():
    captured = []

    def call(name, arguments):
        captured.append((name, arguments))
        return {"structuredContent": {"columns": ["confidence"], "rows": [[""], ["0.85"], ["1"]], "total": 3}}

    result = relationships(SimpleNamespace(call_tool=call), "a.name = 'Widget'", limit=2)
    assert result["truncated"] is True
    assert len(result["rows"]) == 2
    assert result["rows"][0]["confidence"] == ""
    assert captured[0][0] == "query_graph" and captured[0][1]["format"] == "json"
    assert captured[0][1]["max_rows"] == 3 and captured[0][1]["query"].endswith("LIMIT 3")


def test_exact_name_matches_remain_ambiguous_without_runtime_identity():
    response = {"groups": [{"qn_prefix": "project.pkg.a", "file": "pkg/a.py",
                            "rows": [["WidgetType", "Class", "1-5", 1, 1]]},
                           {"qn_prefix": "project.pkg.b", "file": "pkg/b.py",
                            "rows": [["WidgetType", "Class", "7-9", 1, 1]]}], "has_more": False}

    def call(name, _arguments):
        return {"structuredContent": response if name == "search_graph" else
                {"columns": [], "rows": [], "total": 0}}

    result = probe(SimpleNamespace(call_tool=call), "TypeError: WidgetType is unsupported", [],
                   {"pkg/a.py", "pkg/b.py"})
    assert result["mentions"][0]["status"] == "ambiguous"
    assert len(result["mentions"][0]["symbols"]) == 2
    assert len(candidate_scopes(result, {"pkg/a.py", "pkg/b.py"})) == 2


def test_empty_diagnostics_do_not_become_an_unbounded_graph_search():
    captured = []

    def call(name, arguments):
        captured.append((name, arguments))
        return {"structuredContent": {"groups": [], "has_more": False}}

    result = probe(SimpleNamespace(call_tool=call), "RuntimeError: failed", [], set())
    assert result["relationships"] == {} and result["mentions"] == []
    assert len(captured) == 1 and captured[0][1]["name_pattern"] == "a^"


def test_file_scope_deduplication_keeps_the_other_code_witnesses():
    nodes = [{"qn": "project.pkg.WidgetType", "file": "pkg/core.py", "start": 1, "end": 4},
             {"qn": "project.pkg.OtherType", "file": "pkg/core.py", "start": 5, "end": 8},
             {"qn": "project.external.WidgetType", "file": "", "start": 0, "end": 0}]
    result = {"mentions": [{"name": "WidgetType", "status": "ambiguous", "symbols": nodes}],
              "native_locations": [], "seeds": [], "relationships": {}}
    scopes = candidate_scopes(result, {"pkg/core.py"})
    assert len(scopes) == 1
    assert scopes[0]["lower_anchor"]["qn"] == "project.pkg.WidgetType"
    assert len(scopes[0]["additional_anchors"]) == 1


def test_latest_unavailable_case_input_does_not_fall_back(monkeypatch):
    monkeypatch.setattr(entity_search, "observation", lambda *_: {"coverage": "unavailable"})
    with pytest.raises(ValueError, match="frozen observation"):
        verify_inputs(None, {"scope": {"cutoff": 10}, "cases": [{"number": 1}]})


def test_matching_log_digest_is_not_enough_without_the_source_region(monkeypatch):
    body = "```\nTraceback (most recent call last):\nRuntimeError: original\n```"
    source = {"coverage": "complete", "observation": 1, "digest": "source", "payload": {"value": {"body": body}}}
    monkeypatch.setattr(entity_search, "observation", lambda *_: source)
    log = entity_search.error_log(body)
    case = {"number": 1, "source": {"observation": 1, "digest": "source", "pointer": "/value/body"},
            "log": {**log, "digest": sha256(log["text"].encode()).hexdigest()}}
    data = {"scope": {"cutoff": 1}, "cases": [case]}
    verify_inputs(None, data)
    case["log"]["text"] = "RuntimeError: tampered"
    case["log"]["digest"] = sha256(case["log"]["text"].encode()).hexdigest()
    with pytest.raises(ValueError, match="verbatim source region"):
        verify_inputs(None, data)
