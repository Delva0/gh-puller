"""Keep query construction reference-blind and candidate budgets comparable."""
# ruff: noqa: INP001, S101 - Standalone research tests use pytest assertions.

from copy import deepcopy

from . import change_probe
from .case_probe import frames
from .episode_search import segment_input


def inputs():
    log = ('Traceback (most recent call last):\n  File "/repo/pkg/a.py", line 3, in run\nTypeError: original\n'
           'Traceback (most recent call last):\n  File "/repo/pkg/b.py", line 8, in wrap\nRuntimeError: wrapper')
    parsed = frames(log, {"pkg/a.py", "pkg/b.py"})
    case = {"number": 1000, "commit": "version", "frames": parsed, "log": {"text": log},
            "references": [{"number": 123, "changed_files": ["unavailable/oracle.py"]}]}
    episode = {"segmentation": segment_input(log, parsed), "lexical": {"fusion": [99, 2, 1, 3]},
               "rankings": {str(b): {"first_raw": [8, 9]} for b in (10, 20, 30)}}
    binding = {"commit": "version", "selected_files": {"source_and_kind": ["pkg/entity.py"]}}
    return case, episode, binding


def test_source_paths_keep_first_segment_and_typed_entity_channels_separate():
    assert change_probe.query_paths(*inputs()) == {
        "first_raw": [b"pkg/a.py"], "typed_entities": [b"pkg/entity.py"],
        "first_with_entities": [b"pkg/a.py", b"pkg/entity.py"],
    }


def test_references_do_not_construct_or_order_queries(monkeypatch):
    called = []

    def search(index, paths, **kwargs):
        called.append((index, paths, kwargs))
        return [{"number": 2, "change": 1, "score": 1.0, "matched": paths}]

    monkeypatch.setattr(change_probe, "search", search)
    monkeypatch.setattr(change_probe.time, "monotonic", lambda: 0)
    case, episode, binding = inputs()
    first = change_probe.evaluate(None, case, episode, binding, {1000, 99})
    previous_calls = deepcopy(called)
    case["references"] = [{"number": 999999, "changed_files": ["different/oracle.py"]}]
    second = change_probe.evaluate(None, case, episode, binding, {1000, 99})
    assert first == second and called[12:] == previous_calls
    for budget, methods in first["rankings"].items():
        for values in methods.values():
            assert len(values) <= int(budget) and len(values) == len(set(values)) and 99 not in values


def test_serialization_retains_original_non_utf8_git_path_bytes():
    value = change_probe.serializable({"paths": [b"opaque\xff"]})["paths"][0]
    assert bytes.fromhex(value["git_path_hex"]) == b"opaque\xff"
