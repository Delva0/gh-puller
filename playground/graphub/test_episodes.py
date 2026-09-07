"""Check source-preserving traceback segments and counterpart-matched history controls."""
# ruff: noqa: INP001, S101 - Standalone research tests use pytest assertions.

from hashlib import sha256

import pytest

from . import episode_search
from .case_probe import frames
from .episode_search import code_scopes, combine, segment_input
from .future_search import rank


def test_first_segment_keeps_preamble_but_excludes_later_wrapper():
    preamble = "native.cc:1:2: error: missing native_symbol\n"
    first = 'Traceback (most recent call last):\n  File "/repo/pkg/a.py", line 3, in run\nTypeError: original'
    second = '\nTraceback (most recent call last):\n  File "/repo/pkg/b.py", line 9, in wrap\nRuntimeError: wrapper'
    log = preamble + first + second
    parsed = frames(log, {"pkg/a.py", "pkg/b.py"})
    result = segment_input(log, parsed)
    assert result["selected"] == 0 and result["fallback"] is False
    assert result["region"]["frame_indices"] == [0]
    text = log[result["region"]["start"]:result["region"]["end"]]
    assert text == preamble + first + "\n"
    assert result["digest"] == sha256(text.encode()).hexdigest()
    assert result["segments"][1]["frame_indices"] == [1]


def test_incomplete_leading_traceback_is_retained_but_not_selected():
    log = ('Traceback (most recent call last):\npartial header\n'
           'Traceback (most recent call last):\n  File "/vendor/pkg/a.py", line 3, in run\nTypeError: actual')
    parsed = frames(log, set())
    result = segment_input(log, parsed)
    assert result["selected"] == 1 and result["segments"][0]["complete"] is False
    assert result["region"]["frame_indices"] == [0]
    assert parsed[0]["file"] is None


@pytest.mark.parametrize("log", ["RuntimeError: no traceback", "Traceback (most recent call last):\npartial"])
def test_incomplete_input_falls_back_without_dropping_text(log):
    result = segment_input(log, [])
    assert result["fallback"] is True and result["selected"] is None
    assert result["region"] == {"start": 0, "end": len(log), "frame_indices": []}


def test_segment_offsets_remain_in_the_original_unicode_input():
    log = ('错误\nTraceback (most recent call last):\n  File "/repo/pkg/a.py", line 3, in run\nTypeError: bad\n'
           'Traceback (most recent call last):\n  File "/repo/pkg/a.py", line 7, in wrap\nRuntimeError: later')
    parsed = frames(log, {"pkg/a.py"})
    selected = segment_input(log, parsed)
    for segment in selected["segments"]:
        assert all(segment["start"] <= parsed[i]["offset"] < segment["end"] for i in segment["frame_indices"])


def test_counterpart_file_control_preserves_exact_symbol_pairs_and_actual_upper_path(monkeypatch):
    lower = {"qn": "probe.pkg.run", "file": "old.py", "start": 2, "end": 4}
    upper = {"qn": "probe.pkg.run", "file": "new.py", "start": 5, "end": 9}
    case = {"commit": "lower", "frames": [
        {"file": "old.py", "line": 3, "name": "run", "status": "resolved", "symbols": [lower]},
        {"file": "unknown.py", "line": 1, "name": "unknown", "status": "unresolved"},
    ]}
    trace = {"generated": {"histories": [
        {"kind": "trace_file", "lower_anchor": {"file": "old.py", "line": 3, "name": "run"},
         "file": "old.py", "commits": [{"sha": "a"}]},
        {"kind": "trace_file", "lower_anchor": {"file": "unknown.py", "line": 1, "name": "unknown"},
         "file": "unknown.py", "commits": [{"sha": "b"}]},
        {"kind": "upper_symbol", "lower_anchor": lower, "upper_anchor": upper, "correspondence": "same_qualified_name",
         "file": "old.py", "commits": [{"sha": "c"}]},
    ]}}
    called = []

    def full_history(*args):
        called.append(args)
        return [{"sha": "c"}, {"sha": "d"}]

    monkeypatch.setattr(episode_search, "history", full_history)
    scopes = code_scopes("store", case, trace, "upper", {"region": {"frame_indices": [0]}})
    groups = {item["kind"]: item for item in scopes if item["kind"] != "all_raw"}
    assert called == [("store", "lower", "upper", "new.py")]
    assert groups["first_raw"]["file"] == "old.py"
    for prefix in ("first", "all"):
        narrow, broad = (groups[f"{prefix}_{kind}"] for kind in ("symbol", "upper_file"))
        assert narrow["lower_anchor"] == broad["lower_anchor"] == lower
        assert narrow["upper_anchor"] == broad["upper_anchor"] == upper
        assert narrow["file"] == broad["file"] == "new.py"
        assert narrow["commits"] == [{"sha": "c"}] and broad["commits"] == [{"sha": "c"}, {"sha": "d"}]
    assert len([item for item in scopes if item["kind"] == "all_raw"]) == 2


def test_rank_retains_upper_correspondence_for_whole_file_controls():
    upper = {"qn": "probe.run", "file": "new.py", "start": 1, "end": 4}
    scopes = [{"kind": "all_upper_file", "roles": ["evidence"], "lower_anchor": {"file": "old.py"},
               "upper_anchor": upper, "correspondence": "same_qualified_name", "commits": [{"sha": "a"}]}]
    ranked = rank(scopes, {"a": [{"number": 1, "relation": "pull_landing"}]}, {"a": 0}, set(),
                  kinds=("all_upper_file",), roles=("evidence",))
    evidence = ranked["evidence_all_upper_file"]["results"][0]["evidence"][0]
    assert evidence["upper_anchor"] == upper and evidence["correspondence"] == "same_qualified_name"


def test_every_combination_uses_the_same_unique_candidate_budget():
    result = combine([1, 2, 3], [4, 2, 5], [2, 6], [7, 8], {"first_raw": [9, 2, 10]}, budgets=(3,))
    assert result["3"] == {"text_all": [1, 2, 3], "text_first": [4, 2, 5],
                            "baseline": [1, 2, 7], "first_raw": [4, 9, 7]}


def test_empty_code_and_entity_lanes_preserve_selected_text_order():
    result = combine([1, 2], [2, 1], [], [], {"first_symbol": []}, budgets=(20,))
    assert result["20"]["baseline"] == [1, 2] and result["20"]["first_symbol"] == [2, 1]
