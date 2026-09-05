"""Extract explicit commit identities from fine-grained fact payloads.

Only fields whose GitHub contract names a commit object are traversed. Returned JSON
paths retain each source relationship; Git availability belongs to ``git_store``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_SHA = re.compile(r"[0-9a-f]{40,64}\Z")


@dataclass(frozen=True, slots=True)
class CommitReference:
    sha: str  # Exact Git object ID supplied by GitHub.
    field_path: str  # JSON Pointer-like path inside the source payload.
    source_kind: str  # Pull commit, review, review comment, or timeline event.
    source_id: str | int | None  # Stable GitHub source identity when present.


def review_thread_commit_references(
    payload: dict[str, Any],
) -> tuple[CommitReference, ...]:
    """Extract current and original commits from one complete R1 observation.

    Args:
        payload: Review-thread fact payload containing source-native GraphQL nodes.

    Returns:
        Source-distinct references in thread and comment order.
    """
    raw = payload.get("raw")
    threads = raw.get("nodes") if isinstance(raw, dict) else None
    result: list[CommitReference] = []
    for thread_index, thread in enumerate(_objects(threads)):
        comments = thread.get("comments")
        nodes = comments.get("nodes") if isinstance(comments, dict) else None
        for comment_index, comment in enumerate(_objects(nodes)):
            for field in ("commit", "originalCommit"):
                commit = comment.get(field)
                value = commit.get("oid") if isinstance(commit, dict) else None
                _append(
                    result,
                    value,
                    f"/raw/nodes/{thread_index}/comments/nodes/{comment_index}/{field}/oid",
                    "review_thread_comment",
                    comment.get("id"),
                )
    return tuple(result)


def observation_commit_references(
    family: str,
    payload: dict[str, Any],
) -> tuple[CommitReference, ...]:
    """Extract contract-defined commits from one fine-grained fact payload.

    Args:
        family: Semantic fact family that defines the payload shape.
        payload: Observation envelope containing stable ``value`` and source ``raw``.

    Returns:
        Source-distinct references in collection order. Unrelated families are empty.
    """
    if family == "pull-review-threads":
        return review_thread_commit_references(payload)
    value = payload.get("value")
    result: list[CommitReference] = []
    if family == "pull-commits":
        for index, commit in enumerate(_objects(value)):
            _append(
                result,
                commit.get("sha"),
                f"/value/{index}/sha",
                "pull_commit",
                commit.get("sha"),
            )
    elif family == "pull-reviews":
        for index, review in enumerate(_objects(value)):
            _append(
                result,
                review.get("commit_id"),
                f"/value/{index}/commit_id",
                "review",
                review.get("id"),
            )
    elif family == "pull-review-comments":
        for index, comment in enumerate(_objects(value)):
            for field in ("commit_id", "original_commit_id"):
                _append(
                    result,
                    comment.get(field),
                    f"/value/{index}/{field}",
                    "review_comment",
                    comment.get("id"),
                )
    elif family in {"issue-timeline", "issue-events"}:
        _append_event_references(result, family, _objects(value))
    return tuple(result)


def _append_event_references(
    result: list[CommitReference],
    family: str,
    events: list[dict[str, Any]],
) -> None:
    for index, event in enumerate(events):
        source_id = event.get("id", event.get("node_id"))
        _append(
            result,
            event.get("commit_id"),
            f"/value/{index}/commit_id",
            family,
            source_id,
        )
        if event.get("event") == "committed":
            _append(
                result,
                event.get("sha"),
                f"/value/{index}/sha",
                family,
                source_id,
            )
        commit = event.get("commit")
        if not isinstance(commit, dict):
            continue
        for field in ("sha", "oid"):
            _append(
                result,
                commit.get(field),
                f"/value/{index}/commit/{field}",
                family,
                source_id,
            )


def _append(
    result: list[CommitReference],
    value: Any,
    field_path: str,
    source_kind: str,
    source_id: Any,
) -> None:
    if value is None:
        return
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError(f"invalid structured commit ID at {field_path}")
    identity = source_id if isinstance(source_id, (int, str)) else None
    result.append(CommitReference(value, field_path, source_kind, identity))


def _objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
