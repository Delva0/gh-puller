"""Test literal frame alignment without repository vocabulary or reference labels."""
# ruff: noqa: INP001, S101 - Standalone research tests use pytest assertions.

import copy

import pytest

from . import frame_alignment, test_change_git
from .case_probe import frames
from .change_git import run
from .frame_alignment import align, evaluate, printed_statement, read_source
from .test_patch_probe import source_case

repository = test_change_git.repository


def frame(log):
    return frames(log, ["demo.py"])[0]


@pytest.mark.parametrize("prefix", ["", "[worker pid=17] ", "    | ", r"[C:\new\tmp] "])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_physical_statements_preserve_source_spans_prefixes_and_unicode(prefix, newline):
    text = 'return route("你好", r"C:\\new\\tmp")'
    log = f'{prefix}  File "demo.py", line 0003, in route{newline}{prefix}    {text}{newline}ValueError: failed'
    result = printed_statement(log, frame(log))
    assert result["status"] == "statement" and result["encoding"] == "physical"
    assert result["text"] == text == log[result["start"]:result["end"]]
    assert log[result["prefix_start"]:result["prefix_end"]] == prefix + "  "


@pytest.mark.parametrize("depth", [1, 2, 4])
@pytest.mark.parametrize("crlf", [False, True])
def test_escaped_separator_depth_does_not_decode_literal_code_escapes(depth, crlf):
    slash = "\\" * depth
    separator = slash + "r" + slash + "n" if crlf else slash + "n"
    text = 'route(r"C:' + "\\" * (2 * depth) + 'new", "' + "\\" * (2 * depth) + 'n")'
    log = ("Traceback (most recent call last):" + separator
           + '  File "demo.py", line 3, in route' + separator + "    " + text + separator + "ValueError: failed")
    result = printed_statement(log, frame(log))
    assert result["status"] == "statement" and result["encoding"] == "escaped"
    assert result["backslash_depth"] == depth and result["text"] == text
    assert log[result["start"]:result["end"]] == text


def test_standalone_escaped_frame_needs_no_invented_log_prefix():
    log = '  File "demo.py", line 3, in route\\n    route(value)\\nValueError: failed'
    assert printed_statement(log, frame(log))["text"] == "route(value)"


@pytest.mark.parametrize("following", ["", " \t\r", '  File "demo.py", line 4, in next_frame',
                                        "  ValueError: failed", "  Traceback (most recent call last):",
                                        "  [Previous line repeated 2 more times]"])
def test_missing_statements_are_not_filled_from_traceback_boundaries(following):
    log = '  File "demo.py", line 3, in route\n' + following
    assert printed_statement(log, frame(log))["status"] == "no_printed_statement"


def test_prefix_changes_and_mixed_escape_depth_are_explicit():
    log = '[old]  File "demo.py", line 3, in route\n[new]    route(value)\n'
    assert printed_statement(log, frame(log))["status"] == "prefix_mismatch"
    log = '  File "demo.py", line 3, in route\\r\\\\n    route(value)'
    assert printed_statement(log, frame(log))["status"] == "mixed_escape_depth"
    log = '  File "demo.py", line 3, in route trailing prose'
    assert printed_statement(log, frame(log))["status"] == "no_line_boundary"
    bad = frame(log) | {"line": 8}
    with pytest.raises(ValueError, match="original header"):
        printed_statement(log, bad)


def nodes(*ranges):
    return [{"qn": f"example.Class{i}.route", "file": "demo.py", "start": start, "end": end}
            for i, (start, end) in enumerate(ranges)]


def test_coordinate_and_unique_statement_candidates_remain_different_relations():
    source = b"first()\nreturn old\n\nsecond()\nreturn new\n"
    candidates = nodes((1, 2), (4, 5))
    exact = align(source, 2, "return old", candidates)
    shifted = align(source, 2, "return new", candidates)
    assert exact["status"] == "exact_symbol_coordinate" and exact["coordinate_equal"] is True
    assert shifted["status"] == "unique_symbol_statement_candidate" and shifted["reported_line"] == 2
    assert shifted["coordinate_equal"] is False and shifted["coordinate_candidates"] == [0]
    assert shifted["symbol_matches"]["associations"] == [{"candidate": 1, "line": 5}]


def test_repeated_text_nested_candidates_and_output_caps_cannot_manufacture_uniqueness():
    source = b"same()\nother()\nsame()\n"
    result = align(source, 2, "same()", nodes((1, 3)), limit=1)
    assert result["status"] == "ambiguous_symbol_statement"
    assert result["symbol_matches"] == {"count": 2, "associations": [{"candidate": 0, "line": 1}],
                                        "truncated": True, "native_complete": True}
    assert result["file_matches"] == {"count": 2, "positions": [1], "truncated": True}
    nested = align(source, 1, "same()", nodes((1, 3), (1, 1)))
    assert nested["status"] == "ambiguous_symbol_at_coordinate"
    incomplete = align(source, 1, "same()", nodes((1, 3)), truncated=True)
    assert incomplete["status"] == "native_candidates_truncated"
    assert incomplete["coordinate_equal"] is True and incomplete["symbol_matches"]["native_complete"] is False


def test_file_only_evidence_invalid_ranges_and_unavailable_coordinates_stay_explicit():
    source = b"first()\nsecond()\n"
    assert align(source, 1, "first()", [])["status"] == "exact_file_coordinate"
    result = align(source, 8, "second()", nodes((0, 0), (1, 9)))
    assert result["status"] == "unique_file_statement_candidate" and result["coordinate_equal"] is None
    assert result["unusable_native_ranges"] == [0, 1] and result["symbol_matches"]["count"] == 0
    assert align(source, 1, "missing()", [])["status"] == "statement_not_found"
    assert align(b"same()\nsame()\n", 8, "same()", [])["status"] == "ambiguous_file_statement"
    with pytest.raises(ValueError, match="budget"):
        align(source, 1, "first()", [], limit=0)


def test_literal_comparison_does_not_rewrite_quotes_comments_or_source_encoding():
    source = b"  route('x')  # comment\r\n\xff\n"
    assert align(source, 1, "route('x')  # comment", [])["coordinate_equal"] is True
    assert align(source, 1, 'route("x")', [])["coordinate_equal"] is False
    assert align(source, 1, "route('x')", [])["coordinate_equal"] is False


def test_native_source_reader_bounds_bytes_and_keeps_absence_unknown(repository, monkeypatch):
    missing = [b"100644", b"blob", b"1" * 40]
    assert read_source(repository, missing)["status"] == "source_unavailable"
    assert read_source(repository, [b"160000", b"commit", b"1" * 40])["status"] == "non_blob"
    large = run(repository, "hash-object", "-w", "--stdin", data=b"x" * 4194305)
    large.check_returncode()

    def unexpected(*_):
        raise AssertionError("The content read must follow the size gate")

    monkeypatch.setattr(frame_alignment, "blob", unexpected)
    result = read_source(repository, [b"100644", b"blob", large.stdout.strip()])
    assert result["status"] == "source_limit" and result["bytes"] == 4194305


def test_native_snapshot_alignment_replays_queries_and_ignores_reference_labels(repository):
    case, _ = source_case(repository)
    case["graph_digest"] = "0" * 64
    original = evaluate(repository, case)
    assert original["frames"][0]["status"] == "exact_symbol_coordinate"
    case["references"] = [{"number": 987654, "merged": True}]
    assert evaluate(repository, case) == original
    tampered = copy.deepcopy(case)
    tampered["calls"][0]["arguments"]["name_pattern"] = "anything"
    with pytest.raises(ValueError, match="native query"):
        evaluate(repository, tampered)
    tampered = copy.deepcopy(case)
    tampered["frames"][0]["symbols"][0]["end"] += 1
    with pytest.raises(ValueError, match="Captured frames"):
        evaluate(repository, tampered)
