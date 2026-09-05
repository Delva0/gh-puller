"""Define stable refs in the repository-bound bare Git object store."""

from __future__ import annotations

import re

_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
_PREFIX = "refs/github-archive"
_PULL_ROLES = {"bases", "comparisons", "heads", "landings"}
_UPSTREAM_KINDS = {"heads", "tags"}


def pull_ref(number: int, role: str, sha: str) -> str:
    """Return one permanent PR evidence ref.

    Args:
        number: Repository-local PR number.
        role: Bases, comparisons, heads, or landings.
        sha: Retained Git object ID.
    """
    if number < 1 or role not in _PULL_ROLES or _SHA.fullmatch(sha) is None:
        raise ValueError("invalid pull evidence ref")
    return f"{_PREFIX}/pulls/{number}/{role}/{sha}"


def pull_staging_ref(number: int) -> str:
    """Return the mutable fetch ref for one PR.

    Args:
        number: Repository-local PR number.
    """
    if number < 1:
        raise ValueError("invalid pull staging ref")
    return f"{_PREFIX}/staging/pulls/{number}/head"


def upstream_ref(kind: str, sha: str) -> str:
    """Return a permanent observed-upstream pin.

    Args:
        kind: Heads or tags namespace of the source ref.
        sha: Retained Git object ID.
    """
    if kind not in _UPSTREAM_KINDS or _SHA.fullmatch(sha) is None:
        raise ValueError("invalid upstream evidence ref")
    return f"{_PREFIX}/upstream/{kind}/{sha}"


def commit_ref(sha: str) -> str:
    """Return a permanent structured-commit pin.

    Args:
        sha: Retained Git commit ID named by an archived API field.
    """
    if _SHA.fullmatch(sha) is None:
        raise ValueError("invalid commit evidence ref")
    return f"{_PREFIX}/commits/{sha}"
