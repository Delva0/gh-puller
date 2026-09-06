"""Resolve refresh targets into immutable fact-family dependency plans.

The planner owns family applicability and dependency closure. Maintenance persists
its plans as JSON-compatible mappings and reconstructs the same values when it
validates durable tasks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .commit_references import COMMIT_REFERENCE_SOURCE_FAMILIES

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .observations import MaintenanceTask

_COMMON_PARENT_FAMILIES = frozenset(
    {
        "commit-object",
        "commit-references",
        "issue",
        "issue-comment-reactions",
        "issue-comments",
        "issue-events",
        "issue-reactions",
        "issue-timeline",
    },
)
ISSUE_FAMILIES = _COMMON_PARENT_FAMILIES | {"issue-relations"}
PULL_FAMILIES = _COMMON_PARENT_FAMILIES | {
    "pull",
    "pull-closing-issues",
    "pull-commits",
    "pull-git",
    "pull-requested-reviewers",
    "pull-review-comment-reactions",
    "pull-review-comments",
    "pull-review-threads",
    "pull-reviews",
}
REFRESH_FAMILIES = tuple(sorted(ISSUE_FAMILIES | PULL_FAMILIES | {"git-refs"}))
_FAMILY_DEPENDENCIES = {
    "commit-object": frozenset({"commit-references"}),
    "issue-comment-reactions": frozenset({"issue-comments"}),
    "pull-commits": frozenset({"pull"}),
    "pull-git": frozenset({"pull"}),
    "pull-requested-reviewers": frozenset({"pull"}),
    "pull-review-comment-reactions": frozenset({"pull-review-comments"}),
    "pull-review-comments": frozenset({"pull", "pull-review-threads"}),
}


@dataclass(frozen=True, slots=True)
class FamilyPlan:
    """Hold one target kind's requested and dependency-closed fact families."""

    requested: tuple[str, ...]
    effective: tuple[str, ...]
    reference_sources: tuple[str, ...]

    def payload(self) -> dict[str, list[str]]:
        """Return the stable representation stored in job and task scopes."""
        return {
            "requested_families": list(self.requested),
            "effective_families": list(self.effective),
            "reference_source_families": list(self.reference_sources),
        }


def refresh_request(
    pulls: Iterable[int],
    issues: Iterable[int],
    commits: Iterable[str],
    families: Iterable[str] | None,
) -> dict[str, Any]:
    """Normalize one caller selection into its durable request representation."""
    pull_numbers = _numbers(pulls)
    issue_numbers = _numbers(issues)
    shas = tuple(sorted({validate_commit_sha(value) for value in commits}))
    if set(pull_numbers) & set(issue_numbers):
        raise ValueError("one parent cannot be selected as both Issue and PR")
    if families is None:
        selected = set()
        if pull_numbers:
            selected.update(PULL_FAMILIES)
        if issue_numbers:
            selected.update(ISSUE_FAMILIES)
        if shas:
            selected.add("commit-object")
    else:
        selected = set(families)
        unknown = selected - set(REFRESH_FAMILIES)
        if unknown:
            raise ValueError(f"unknown refresh families: {', '.join(sorted(unknown))}")
    if not pull_numbers and not issue_numbers and not shas and "git-refs" not in selected:
        raise ValueError("refresh requires an Issue, PR, commit, or git-refs target")
    return {
        "commits": list(shas),
        "families": sorted(selected),
        "issues": list(issue_numbers),
        "pulls": list(pull_numbers),
    }


def refresh_plan(request: dict[str, Any]) -> dict[str, FamilyPlan]:
    """Build dependency-closed plans for every selected target kind."""
    selected = set(request["families"])
    matched = set()
    plan = {}
    for group, kind, applicable in (
        ("issues", "issue", ISSUE_FAMILIES),
        ("pulls", "pull", PULL_FAMILIES),
    ):
        if not request[group]:
            continue
        requested = selected & applicable
        if not requested:
            raise ValueError(f"selected families do not apply to {kind} targets")
        plan[group] = parent_family_plan(kind, requested)
        matched.update(requested)
    if request["commits"]:
        if "commit-object" not in selected:
            raise ValueError("commit targets require the commit-object family")
        plan["commits"] = _singular_plan("commit-object")
        matched.add("commit-object")
    if "git-refs" in selected:
        plan["repository"] = _singular_plan("git-refs")
        matched.add("git-refs")
    unmatched = selected - matched
    if unmatched:
        raise ValueError(
            f"selected families have no compatible target: {', '.join(sorted(unmatched))}",
        )
    if not plan:
        raise ValueError("refresh has no applicable target")
    return plan


def parent_family_plan(kind: str, requested: set[str]) -> FamilyPlan:
    """Close one Issue or PR family selection over its source dependencies."""
    applicable = ISSUE_FAMILIES if kind == "issue" else PULL_FAMILIES
    source_families = COMMIT_REFERENCE_SOURCE_FAMILIES & applicable
    effective = set(requested) | {"issue"}
    while True:
        dependencies = {
            dependency
            for family in effective
            for dependency in (
                source_families
                if family == "commit-references"
                else _FAMILY_DEPENDENCIES.get(family, ())
            )
        }
        expanded = effective | dependencies
        if expanded == effective:
            break
        effective = expanded
    reference_sources = effective & source_families
    if reference_sources:
        effective.update({"commit-object", "commit-references"})
    return FamilyPlan(
        tuple(sorted(requested)),
        tuple(sorted(effective)),
        tuple(sorted(reference_sources)),
    )


def parent_refresh_scope(
    task: MaintenanceTask,
) -> tuple[str, set[str], tuple[str, ...]]:
    """Validate and decode a persisted parent refresh plan."""
    kind = task.payload.get("kind")
    number = task.payload.get("number")
    requested = task.payload.get("requested_families")
    if (
        kind not in {"issue", "pull"}
        or type(number) is not int
        or number < 1
        or number != task.resource_number
        or not isinstance(requested, list)
        or not requested
        or any(not isinstance(family, str) for family in requested)
    ):
        raise RuntimeError(f"maintenance task {task.task_key} has invalid parent scope")
    expected = parent_family_plan(kind, set(requested))
    for field, value in expected.payload().items():
        if task.payload.get(field) != value:
            raise RuntimeError(f"maintenance task {task.task_key} has invalid family plan")
    return kind, set(expected.effective), expected.reference_sources


def _singular_plan(family: str) -> FamilyPlan:
    return FamilyPlan((family,), (family,), ())


def _numbers(values: Iterable[int]) -> tuple[int, ...]:
    selected = tuple(sorted(set(values)))
    if any(type(number) is not int or number < 1 for number in selected):
        raise ValueError("Issue and PR numbers must be positive integers")
    return selected


def validate_commit_sha(value: object) -> str:
    """Return one normalized full commit ID or reject the refresh input."""
    if not isinstance(value, str) or len(value) not in {40, 64}:
        raise ValueError("commit IDs must contain 40 or 64 lowercase hexadecimal digits")
    if any(character not in "0123456789abcdef" for character in value):
        raise ValueError("commit IDs must contain 40 or 64 lowercase hexadecimal digits")
    return value
