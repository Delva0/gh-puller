"""Extract explicit commit identities without guessing from free-form text.

Bundle and review-thread schemas are traversed only at fields whose GitHub contract
names a commit object. The returned JSON paths preserve every source relationship;
Git availability and publication belong to git_store and store respectively.
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


@dataclass(frozen=True, slots=True)
class CommitReferenceSource:
    kind: str  # bundle or independently published supplemental fact family.
    digest: str  # Content identity of the source payload.
    resource_number: int | None  # Related Issue/PR number.
    references: tuple[CommitReference, ...]  # Ordered structured fields in the source.


def bundle_commit_references(bundle: dict[str, Any]) -> tuple[CommitReference, ...]:
    """Extract all contract-defined commit fields from one historical bundle.

    Args:
        bundle: Decoded canonical Issue/PR bundle.

    Returns:
        Source-distinct references in their original collection order.

    Raises:
        ValueError: A non-null structured commit field is not an exact object ID.
    """
    result: list[CommitReference] = []
    pull = bundle.get("pull_request")
    if isinstance(pull, dict):
        for index, item in enumerate(_objects(pull.get("commits"))):
            _append(result, item.get("sha"), f"/pull_request/commits/{index}/sha", "pull_commit", item.get("sha"))
        for index, review in enumerate(_objects(pull.get("reviews"))):
            _append(
                result,
                review.get("commit_id"),
                f"/pull_request/reviews/{index}/commit_id",
                "review",
                review.get("id"),
            )
        for index, comment in enumerate(_objects(pull.get("review_comments"))):
            for field in ("commit_id", "original_commit_id"):
                _append(
                    result,
                    comment.get(field),
                    f"/pull_request/review_comments/{index}/{field}",
                    "review_comment",
                    comment.get("id"),
                )
    for collection in ("timeline", "events"):
        for index, event in enumerate(_objects(bundle.get(collection))):
            source_id = event.get("id", event.get("node_id"))
            _append(
                result,
                event.get("commit_id"),
                f"/{collection}/{index}/commit_id",
                "timeline_event",
                source_id,
            )
            if event.get("event") == "committed":
                _append(
                    result,
                    event.get("sha"),
                    f"/{collection}/{index}/sha",
                    "timeline_event",
                    source_id,
                )
            commit = event.get("commit")
            if isinstance(commit, dict):
                for field in ("sha", "oid"):
                    _append(
                        result,
                        commit.get(field),
                        f"/{collection}/{index}/commit/{field}",
                        "timeline_event",
                        source_id,
                    )
    return tuple(result)


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
