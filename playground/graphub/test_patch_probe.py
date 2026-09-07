"""Test reference-blind source context and equal-budget patch-hunk ranking."""
# ruff: noqa: INP001, S101 - Standalone research tests use pytest assertions.

import copy
import json
import sqlite3

import pytest

from . import patch_probe, test_change_git, test_change_index
from .case_probe import frames
from .change_index import search, verify_hits
from .change_probe import serializable
from .episode_search import segment_input
from .patch_probe import LANES, byte_value, candidate_pool, evaluate, rank, source_input, tokens
from .test_change_git import commit
from .test_change_index import add, prepare, pull

repository = test_change_git.repository
canonical = test_change_index.canonical


def source_case(store):
    source = (b"class Thing:\n"
              b"    def run(self):\n"
              b"        if not hasattr(self, 'required_field'):\n"
              b"            raise AttributeError('Thing is missing support')\n"
              b"        return self.required_field\n")
    sha = commit(store, {b"demo.py": (b"100644", source)})
    log = ('Traceback (most recent call last):\n  File "demo.py", line 4, in run\n'
           "    raise AttributeError('Thing is missing support')\nAttributeError: Thing is missing support\n")
    parsed = frames(log, ["demo.py"])
    parsed[0].update(status="resolved", symbols=[{"qn": "graphub-probe.demo.Thing.run", "file": "demo.py",
                                                "start": 2, "end": 5}])
    response = {"cols": ["name", "label", "lines"], "has_more": False, "groups": [
        {"qn_prefix": "graphub-probe.demo.Thing", "file": "demo.py", "rows": [["run", "Method", "2-5"]]},
    ]}
    case = {"number": 1, "commit": sha, "log": {"text": log}, "frames": parsed, "references": [], "calls": [{
        "tool": "search_graph", "arguments": {"project": "graphub-probe", "file_pattern": "demo.py",
                                               "name_pattern": "^(run)$", "format": "json", "limit": 5000},
        "response": response,
    }]}
    episode = {"commit": sha, "segmentation": segment_input(log, parsed), "lexical": {"fusion": [20, 10, 77]}}
    return case, episode


def test_native_source_terms_are_bound_to_blob_and_captured_cbm(repository):
    case, episode = source_case(repository)
    result = source_input(repository, case, episode)
    assert result["status"] == "resolved_window" and (result["start"], result["end"]) == (2, 5)
    assert "required_field" in result["source_terms"] and "required_field" not in result["log_terms"]
    assert result["symbol"] == case["frames"][0]["symbols"][0]
    case["references"] = [{"number": 987654, "changed_files": ["invented.py"]}]
    assert source_input(repository, case, episode) == result
    case["frames"][0]["symbols"][0]["end"] = 4
    with pytest.raises(ValueError, match="native CBM"):
        source_input(repository, case, episode)


def test_source_window_rejects_coordinate_truncation_and_segmentation_tampering(repository):
    case, episode = source_case(repository)
    bad = copy.deepcopy(case)
    bad["frames"][0]["line"] = 3
    with pytest.raises(ValueError, match="coordinates"):
        source_input(repository, bad, episode)
    bad = copy.deepcopy(case)
    bad["calls"][0]["response"]["has_more"] = True
    with pytest.raises(ValueError, match="native CBM"):
        source_input(repository, bad, episode)
    episode["commit"] = "1" * 40
    with pytest.raises(ValueError, match="selection"):
        source_input(repository, case, episode)


def test_no_native_binding_keeps_log_only_terms(repository):
    case, episode = source_case(repository)
    case["frames"][0]["status"] = "ambiguous"
    result = source_input(repository, case, episode)
    assert result["status"] == "no_resolved_frame" and result["source_terms"] == []
    assert result["log_terms"] == ["missing", "support", "thing"]


def test_field_ablations_preserve_unavailable_slots_and_distinct_budgets():
    documents = [
        {"id": "a", "number": 10, "lines": [{"role": "context", "text": b"unique_target\n"},
                                             {"role": "added", "text": b"other_name\n"}]},
        {"id": "b", "number": 20, "lines": [{"role": "deleted", "text": b"unrelated\n"}]},
    ]
    pool = [20, 77, 10]
    delta = rank(pool, documents, ["unique_target"], context=False)
    context = rank(pool, documents, ["unique_target"], context=True)
    assert delta["ranking"] == pool and context["ranking"] == [10, 77, 20]
    assert delta["fixed_slots"] == context["fixed_slots"] == [1]
    assert delta["results"][1]["matched_terms"] == []
    assert context["results"][0]["matched_terms"] == ["unique_target"]
    assert rank(pool, documents, [], context=True)["ranking"] == pool
    assert rank(pool, [], ["unique_target"], context=True)["ranking"] == pool


def test_candidate_pool_and_tokenization_do_not_use_reference_metadata():
    episode = {"lexical": {"fusion": [1, 2, 3]}, "references": {"999": {"merged": True}}}
    changes = {"arms": {name: {"ranking": [3, 4, 5]} for name in LANES}, "references": {"888": {}}}
    assert candidate_pool(episode, changes, {1}) == [2, 3, 4, 5]
    assert tokens(b"ABC abc a ab foo_bar A12 123 \xff") == ["abc", "abc", "foo_bar", "a12"]
    raw = json.loads(json.dumps({"line": b"x\xff\r\n"}, default=byte_value))
    assert bytes.fromhex(raw["line"]["hex"]) == b"x\xff\r\n"


def test_native_end_to_end_patch_controls_are_reference_blind(canonical, repository, tmp_path, monkeypatch):
    db, _ = canonical
    case, episode = source_case(repository)
    a = case["commit"]
    b = commit(repository, {b"demo.py": (b"100644", b"required_field = 1\n")}, (a,))
    c = commit(repository, {b"demo.py": (b"100644", b"unrelated = 1\n")}, (a,))
    add(db, 1, 10, "pull", pull(a, b, merged=True, landing=b))
    add(db, 2, 20, "pull", pull(a, c, merged=True, landing=c))
    path = tmp_path / "changes.sqlite3"
    scope = prepare(db, repository, path)
    with sqlite3.connect(path) as index:
        changes = {"paths": {key: serializable([b"demo.py"]) for key in ("first_raw", "typed_entities")}, "arms": {}}
        for name in LANES:
            _, kind, method = name.rsplit("_", 2)
            hits = search(index, [b"demo.py"], kind=kind, method=method, excluded={1}, limit=100)
            changes["arms"][name] = {"results": serializable(hits), "ranking": [hit["number"] for hit in hits]}
        original, hits = evaluate(repository, index, case, episode, changes, {1})
        assert verify_hits(db, repository, index, hits, scope)["changes"] == 4
        case["references"] = [{"number": 999999, "merged": True}]
        changes["references"] = {"888888": {"merged": True}}
        replay, _ = evaluate(repository, index, case, episode, changes, {1})
        original["cost"].pop("seconds")
        replay["cost"].pop("seconds")
        assert original == replay and set(original["pool"]) == {10, 20, 77}
        for arm in original["arms"].values():
            assert len(arm["ranking"]) == len(set(arm["ranking"])) == 3
            assert arm["ranking"][original["pool"].index(77)] == 77
        assert original["cost"]["verified_line_sides"] > 0
        monkeypatch.setattr(patch_probe, "read_comparison", lambda *_: {"status": "file_limit", "files": 201,
                                                                        "limit": 200})
        limited, _ = evaluate(repository, index, case, episode, changes, {1})
        assert all(item["patch"]["files"] == 201 for item in limited["patches"])
        assert all(arm["ranking"] == limited["pool"] for arm in limited["arms"].values())
