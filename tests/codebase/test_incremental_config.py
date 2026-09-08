import argparse
from dataclasses import replace

import pytest

from gh_puller.codebase.build_plan import (
    BuildPlan,
    IncrementalConfig,
    IncrementalConfigError,
    add_incremental_arguments,
)


def test_defaults_explicitly_override_every_cbm_axis():
    assert IncrementalConfig().environment() == {
        "CBM_INCREMENTAL_CLOSURE_OVERFLOW": "full",
        "CBM_INCREMENTAL_CLOSURE_COST_PERCENT": "0",
        "CBM_INCREMENTAL_DEPENDENT_SCOPE": "file",
        "CBM_INCREMENTAL_NEW_SURFACE": "full",
        "CBM_INCREMENTAL_REFERENCE_FANOUT_CAP": "0",
        "CBM_INCREMENTAL_PAIR_OUTPUTS": "eager",
        "CBM_INCREMENTAL_PAIR_REFRESH_BUDGET": "0",
        "CBM_INCREMENTAL_PAIR_INPUT_MISSING": "full",
    }


def test_arguments_expose_independent_controls():
    parser = argparse.ArgumentParser()
    add_incremental_arguments(parser)
    args = parser.parse_args(
        [
            "--delta-closure-overflow",
            "repair",
            "--delta-closure-cost-percent",
            "20",
            "--delta-dependent-scope",
            "symbol",
            "--delta-new-surface",
            "bounded",
            "--delta-reference-fanout-cap",
            "64",
            "--delta-pair-outputs",
            "lazy",
            "--delta-pair-refresh-budget",
            "10000",
            "--delta-pair-input-missing",
            "skip",
        ],
    )

    config = IncrementalConfig.from_namespace(args)

    assert config.to_dict() == {
        "closure_overflow": "repair",
        "closure_cost_percent": 20,
        "dependent_scope": "symbol",
        "new_surface": "bounded",
        "reference_fanout_cap": 64,
        "pair_outputs": "lazy",
        "pair_refresh_budget": 10000,
        "pair_input_missing": "skip",
    }
    assert len(config.digest()) == 64


def test_symbol_scope_is_independent_of_closure_overflow():
    config = replace(IncrementalConfig(), dependent_scope="symbol")

    config.validate()


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (replace(IncrementalConfig(), closure_cost_percent=101), "closure_cost_percent"),
        (replace(IncrementalConfig(), new_surface="bounded"), "reference_fanout_cap"),
        (replace(IncrementalConfig(), reference_fanout_cap=64), "requires new_surface"),
        (replace(IncrementalConfig(), pair_refresh_budget=1), "pair_outputs=lazy"),
    ],
)
def test_invalid_combinations_fail_before_cbm_starts(config, message):
    with pytest.raises(IncrementalConfigError, match=message):
        config.validate()


def test_incremental_configuration_is_recorded_per_commit_plan():
    first = replace(
        IncrementalConfig(),
        closure_overflow="repair",
        dependent_scope="symbol",
    )
    second = IncrementalConfig()

    first_metadata = BuildPlan(incremental=first).metadata()["cbm_incremental"]
    second_metadata = BuildPlan(incremental=second).metadata()["cbm_incremental"]
    assert first_metadata["options"] == first.to_dict()
    assert first_metadata["digest"] != second_metadata["digest"]
