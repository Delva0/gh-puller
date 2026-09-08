"""Define and validate the CBM indexing policy applied to one commit.

The immutable policy is shared by the build orchestrator and reusable CBM
runner. Command-line parsing lives here only to construct that same policy;
execution and persistence remain outside this module.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import argparse

AnalysisMode = Literal["full", "moderate", "fast", "cross-repo-intelligence"]
BuildRoute = Literal["delta", "full"]


class IncrementalConfigError(ValueError):
    """Indicate an internally inconsistent set of CBM delta controls."""


@dataclass(frozen=True, slots=True)
class IncrementalConfig:
    """One explicit value for every accuracy/performance axis in CBM delta."""

    closure_overflow: str = "full"
    closure_cost_percent: int = 0
    dependent_scope: str = "file"
    new_surface: str = "full"
    reference_fanout_cap: int = 0
    pair_outputs: str = "eager"
    pair_refresh_budget: int = 0
    pair_input_missing: str = "full"

    def validate(self) -> None:
        choices = {
            "closure_overflow": (self.closure_overflow, {"full", "repair"}),
            "dependent_scope": (self.dependent_scope, {"file", "symbol"}),
            "new_surface": (self.new_surface, {"full", "bounded"}),
            "pair_outputs": (self.pair_outputs, {"eager", "lazy"}),
            "pair_input_missing": (self.pair_input_missing, {"full", "skip"}),
        }
        for name, (value, allowed) in choices.items():
            if value not in allowed:
                raise IncrementalConfigError(f"{name} must be one of {', '.join(sorted(allowed))}, got {value!r}")
        if not 0 <= self.closure_cost_percent <= 100:
            raise IncrementalConfigError("closure_cost_percent must be between 0 and 100")
        if not 0 <= self.reference_fanout_cap <= 1_000_000_000:
            raise IncrementalConfigError("reference_fanout_cap must be between 0 and 1000000000")
        if not 0 <= self.pair_refresh_budget <= 1_000_000_000:
            raise IncrementalConfigError("pair_refresh_budget must be between 0 and 1000000000")
        if self.new_surface == "bounded" and self.reference_fanout_cap == 0:
            raise IncrementalConfigError("new_surface=bounded requires a positive reference_fanout_cap")
        if self.reference_fanout_cap > 0 and self.new_surface != "bounded" and self.closure_overflow != "repair":
            raise IncrementalConfigError("reference_fanout_cap requires new_surface=bounded or closure_overflow=repair")
        if self.pair_refresh_budget > 0 and self.pair_outputs != "lazy":
            raise IncrementalConfigError("pair_refresh_budget requires pair_outputs=lazy")

    @classmethod
    def from_namespace(cls, args: argparse.Namespace) -> IncrementalConfig:
        config = cls(
            closure_overflow=args.delta_closure_overflow,
            closure_cost_percent=args.delta_closure_cost_percent,
            dependent_scope=args.delta_dependent_scope,
            new_surface=args.delta_new_surface,
            reference_fanout_cap=args.delta_reference_fanout_cap,
            pair_outputs=args.delta_pair_outputs,
            pair_refresh_budget=args.delta_pair_refresh_budget,
            pair_input_missing=args.delta_pair_input_missing,
        )
        config.validate()
        return config

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)

    def digest(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()

    def environment(self) -> dict[str, str]:
        """Return a complete override set so inherited legacy flags cannot leak in."""
        self.validate()
        return {
            "CBM_INCREMENTAL_CLOSURE_OVERFLOW": self.closure_overflow,
            "CBM_INCREMENTAL_CLOSURE_COST_PERCENT": str(self.closure_cost_percent),
            "CBM_INCREMENTAL_DEPENDENT_SCOPE": self.dependent_scope,
            "CBM_INCREMENTAL_NEW_SURFACE": self.new_surface,
            "CBM_INCREMENTAL_REFERENCE_FANOUT_CAP": str(self.reference_fanout_cap),
            "CBM_INCREMENTAL_PAIR_OUTPUTS": self.pair_outputs,
            "CBM_INCREMENTAL_PAIR_REFRESH_BUDGET": str(self.pair_refresh_budget),
            "CBM_INCREMENTAL_PAIR_INPUT_MISSING": self.pair_input_missing,
        }


@dataclass(frozen=True, slots=True)
class BuildPlan:
    """Select CBM analysis coverage and routing for one commit.

    ``route`` controls whether CBM must rebuild from scratch. A delta request
    leaves CBM's existing conservative full fallbacks intact; the resulting
    published graph remains the archive source of truth.
    """

    analysis_mode: AnalysisMode = "full"
    route: BuildRoute = "delta"
    incremental: IncrementalConfig = field(default_factory=IncrementalConfig)

    def __post_init__(self) -> None:
        if self.analysis_mode not in {"full", "moderate", "fast", "cross-repo-intelligence"}:
            raise ValueError(f"invalid CBM analysis mode: {self.analysis_mode!r}")
        if self.route not in {"delta", "full"}:
            raise ValueError(f"invalid CBM build route: {self.route!r}")
        self.incremental.validate()

    @property
    def force_full(self) -> bool:
        """Return whether CBM must bypass incremental routing."""
        return self.route == "full"

    def required_capabilities(self) -> frozenset[str]:
        """Return CBM features required to execute this plan."""
        required = {"granular-delta-controls"}
        if self.force_full:
            required.add("force-full-route")
        return frozenset(required)

    def metadata(self) -> dict:
        """Return stable per-commit provenance for the KGA manifest."""
        return {
            "cbm_analysis_mode": self.analysis_mode,
            "cbm_requested_route": self.route,
            "cbm_incremental": {
                "version": 1,
                "digest": self.incremental.digest(),
                "options": self.incremental.to_dict(),
            },
        }


def add_incremental_arguments(parser: argparse.ArgumentParser) -> None:
    """Add explicit CBM delta controls to a command-line parser.

    Args:
        parser: Parser whose namespace will later construct an
            :class:`IncrementalConfig`.
    """
    group = parser.add_argument_group("CBM delta controls")
    group.add_argument(
        "--delta-closure-overflow",
        choices=("full", "repair"),
        default="full",
        help="fall back to full or keep repairing when the exact closure exceeds 30%%",
    )
    group.add_argument(
        "--delta-closure-cost-percent",
        type=int,
        default=0,
        metavar="PERCENT",
        help="fall back to full above this selected-closure percentage; 0 disables the extra gate",
    )
    group.add_argument(
        "--delta-dependent-scope",
        choices=("file", "symbol"),
        default="file",
        help="use all file dependents or only changed-symbol dependents for closure repair",
    )
    group.add_argument(
        "--delta-new-surface",
        choices=("full", "bounded"),
        default="full",
        help="fall back on uncertain new surfaces or use bounded lexical-reference repair",
    )
    group.add_argument(
        "--delta-reference-fanout-cap",
        type=int,
        default=0,
        metavar="FILES",
        help="maximum Bloom hits admitted for one new reference name; 0 disables capping",
    )
    group.add_argument(
        "--delta-pair-outputs",
        choices=("eager", "lazy"),
        default="eager",
        help="rebuild global pair/vector outputs immediately or retain them lazily",
    )
    group.add_argument(
        "--delta-pair-refresh-budget",
        type=int,
        default=0,
        metavar="SYMBOLS",
        help="with lazy pair outputs, refresh after this many dirty symbols; 0 never auto-refreshes",
    )
    group.add_argument(
        "--delta-pair-input-missing",
        choices=("full", "skip"),
        default="full",
        help="fall back to full or skip pair inputs whose endpoints cannot be loaded",
    )
