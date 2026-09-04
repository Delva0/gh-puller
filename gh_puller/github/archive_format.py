"""Project retained PR bundles into the public relational Git index.

This module defines the deterministic boundary between lossless compressed payloads
and the small SQL index used by network-free consumers. It does not inspect Git or
infer relationships absent from a stored bundle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
_COMPARISON_KINDS = {"empty_tree", "merge_base", "unavailable"}
GIT_REF_PREFIX = "refs/github-archive"
_PULL_ROLES = {"bases", "comparisons", "heads", "landings"}
_UPSTREAM_KINDS = {"heads", "tags"}


def pull_ref(number: int, role: str, sha: str) -> str:
    """Return one permanent PR evidence ref.

    Args:
        number: Repository-local PR number.
        role: One of bases, comparisons, heads, or landings.
        sha: Git object ID retained by the ref.
    """
    if number < 1 or role not in _PULL_ROLES or _SHA.fullmatch(sha) is None:
        raise ValueError("invalid pull evidence ref")
    return f"{GIT_REF_PREFIX}/pulls/{number}/{role}/{sha}"


def pull_staging_ref(number: int) -> str:
    """Return the mutable fetch ref for one PR.

    Args:
        number: Repository-local PR number.
    """
    if number < 1:
        raise ValueError("invalid pull staging ref")
    return f"{GIT_REF_PREFIX}/staging/pulls/{number}/head"


def upstream_ref(kind: str, sha: str) -> str:
    """Return a permanent pin for an observed upstream object.

    Args:
        kind: Heads or tags namespace of the source ref.
        sha: Git object ID retained by the ref.
    """
    if kind not in _UPSTREAM_KINDS or _SHA.fullmatch(sha) is None:
        raise ValueError("invalid upstream evidence ref")
    return f"{GIT_REF_PREFIX}/upstream/{kind}/{sha}"


@dataclass(frozen=True, slots=True)
class PullGitSnapshot:
    bundle_digest: str  # Canonical bundle identity.
    number: int  # Repository-local PR number.
    merged: bool  # GitHub merge state in this bundle.
    base_sha: str  # API-observed base commit.
    head_sha: str  # API-observed original PR head.
    comparison_kind: str  # merge_base, empty_tree, or unavailable.
    comparison_sha: str | None  # Persisted diff origin when available.
    base_ref: str | None  # Permanent archive ref when available.
    head_ref: str | None  # Permanent original-head archive ref when available.
    comparison_ref: str | None  # Permanent diff-origin ref when available.
    landing_sha: str | None  # GitHub-reported result for a merged PR.
    landing_ref: str | None  # Permanent result ref when the object is available.
    history_preserved: bool | None  # Exact head-to-landing ancestry proof.
    commits: tuple[str, ...]  # API-observed original PR commits in order.


def pull_git_snapshot(bundle_digest: str, bundle: dict[str, Any]) -> PullGitSnapshot | None:
    """Extract a PR Git index row from one canonical bundle.

    Args:
        bundle_digest: Digest used by ``payload_blobs`` and resource rows.
        bundle: Decoded canonical Issue/PR bundle.

    Returns:
        A relational projection for a PR, or None for an Issue.

    Raises:
        TypeError: A PR bundle has an invalid structural field.
        ValueError: A PR bundle lacks a required exact Git identity.
    """
    if bundle.get("kind") != "pull":
        return None
    pull = _mapping(bundle.get("pull_request"), "pull_request")
    detail = _mapping(pull.get("detail"), "pull_request.detail")
    git = _mapping(pull.get("git"), "pull_request.git")
    commits = pull.get("commits")
    if not isinstance(commits, list):
        raise TypeError("pull_request.commits is not a list")
    number = bundle.get("number")
    if not isinstance(number, int):
        raise TypeError("pull bundle has no integer number")
    comparison_kind = git.get("comparison_kind")
    if comparison_kind not in _COMPARISON_KINDS:
        raise ValueError(f"pull #{number} has invalid comparison kind")
    merged = detail.get("merged") is True
    landing_sha = _optional_sha(git.get("landing_sha")) if merged else None
    landing_ref = _optional_ref(git.get("landing_ref")) if landing_sha is not None else None
    history_preserved = git.get("history_preserved")
    if not isinstance(history_preserved, bool):
        history_preserved = None
    return PullGitSnapshot(
        bundle_digest=bundle_digest,
        number=number,
        merged=merged,
        base_sha=_required_sha(git.get("base_sha"), f"pull #{number} base"),
        head_sha=_required_sha(git.get("head_sha"), f"pull #{number} head"),
        comparison_kind=str(comparison_kind),
        comparison_sha=_optional_sha(git.get("comparison_sha")),
        base_ref=_optional_ref(git.get("base_ref")),
        head_ref=_optional_ref(git.get("head_ref")),
        comparison_ref=_optional_ref(git.get("comparison_ref")),
        landing_sha=landing_sha,
        landing_ref=landing_ref,
        history_preserved=history_preserved,
        commits=tuple(
            _required_sha(_mapping(commit, "pull commit").get("sha"), f"pull #{number} commit") for commit in commits
        ),
    )


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{field} is not an object")
    return value


def _required_sha(value: Any, field: str) -> str:
    sha = _optional_sha(value)
    if sha is None:
        raise ValueError(f"{field} has no valid SHA")
    return sha


def _optional_sha(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


def _optional_ref(value: Any) -> str | None:
    prefix = f"{GIT_REF_PREFIX}/"
    return value if isinstance(value, str) and value.startswith(prefix) else None
