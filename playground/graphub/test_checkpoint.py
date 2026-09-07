"""Check fixed-budget evaluation without oracle-driven ranking decisions."""
# ruff: noqa: INP001, S101 - Standalone research tests use pytest assertions.

from copy import deepcopy

import pytest

from .checkpoint import evaluate, interleave


def test_round_robin_budget_counts_unique_threads():
    assert interleave([[1, 2, 3], [1, 4], []], 3) == [1, 2, 4]
    assert interleave([[1], [], [2]], 20) == [1, 2]
    assert interleave([], 10) == []


def test_empty_extra_lanes_preserve_the_text_ranking():
    assert interleave([[1, 2, 3], [], []], 2) == [1, 2]


def test_nonpositive_budget_is_rejected():
    with pytest.raises(ValueError, match="positive"):
        interleave([[1]], 0)


def test_reference_labels_do_not_affect_checkpoint_ranking():
    text = {"scope": {}, "primary_messages": True, "cases": [{"number": 1, "fusion": [10, 11, 12],
            "references": {"10": {"merged": True, "fusion": 1, "diagnostic_identifiers": 1}}}]}
    temporal = {"scope": {}, "upper": "fixed", "cases": [{"number": 1, "lower": "version", "arms": {
        "terminal_trace_file": {"ranking": [20, 10, 21]},
    }}]}
    entities = {"scope": {}, "upper": "fixed", "cases": [{"number": 1, "commit": "version", "arms": {
        "evidence_entity_file": {"ranking": [30, 20]},
    }}]}
    control = evaluate(text, temporal, entities, [2, 3, 4])
    changed = deepcopy(text)
    changed["cases"][0]["references"] = {"999": {"merged": False, "fusion": None, "diagnostic_identifiers": None}}
    result = evaluate(changed, temporal, entities, [2, 3, 4])
    assert control["cases"][0]["rankings"] == result["cases"][0]["rankings"]
    assert result["cases"][0]["rankings"]["3"]["hybrid"] == [10, 20, 30]
    mismatched = deepcopy(entities)
    mismatched["upper"] = "different"
    with pytest.raises(ValueError, match="registered controls"):
        evaluate(text, temporal, mismatched, [3])
