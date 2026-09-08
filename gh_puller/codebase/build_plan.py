"""Define the CBM indexing policy applied to one repository commit."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .incremental_config import IncrementalConfig

AnalysisMode = Literal["full", "moderate", "fast", "cross-repo-intelligence"]
BuildRoute = Literal["delta", "full"]


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

