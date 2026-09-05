"""Test exact structured commit extraction without free-text guesses."""

from __future__ import annotations

import pytest

from gh_puller.github.commit_references import (
    bundle_commit_references,
    review_thread_commit_references,
)


def test_bundle_commit_references_preserve_every_source_path() -> None:
    shas = [f"{number:040x}" for number in range(1, 8)]
    bundle = {
        "number": 7,
        "body": f"free text {8:040x}",
        "pull_request": {
            "commits": [{"sha": shas[0], "tree": {"sha": f"{9:040x}"}}],
            "reviews": [{"id": 71, "commit_id": shas[1]}],
            "review_comments": [
                {
                    "id": 72,
                    "body": f"also free text {10:040x}",
                    "commit_id": shas[2],
                    "original_commit_id": shas[1],
                },
            ],
        },
        "timeline": [
            {"id": 73, "event": "referenced", "commit_id": shas[3]},
            {"id": 74, "event": "committed", "sha": shas[4]},
            {"id": 75, "event": "cross-referenced", "sha": f"{11:040x}"},
        ],
        "events": [
            {"id": 76, "event": "closed", "commit_id": shas[5]},
            {"id": 77, "event": "other", "commit": {"oid": shas[6]}},
        ],
    }

    references = bundle_commit_references(bundle)

    assert [reference.sha for reference in references] == [
        shas[0],
        shas[1],
        shas[2],
        shas[1],
        shas[3],
        shas[4],
        shas[5],
        shas[6],
    ]
    assert references[0].field_path == "/pull_request/commits/0/sha"
    assert references[3].field_path.endswith("/original_commit_id")
    assert references[-1].source_id == 77


def test_review_thread_commit_references_keep_membership_paths() -> None:
    current = "a" * 40
    original = "b" * 40
    payload = {
        "raw": {
            "totalCount": 1,
            "nodes": [
                {
                    "id": "thread-1",
                    "comments": {
                        "totalCount": 1,
                        "nodes": [
                            {
                                "id": "comment-1",
                                "commit": {"oid": current},
                                "originalCommit": {"oid": original},
                            },
                        ],
                    },
                },
            ],
        },
    }

    references = review_thread_commit_references(payload)

    assert [(reference.sha, reference.source_id) for reference in references] == [
        (current, "comment-1"),
        (original, "comment-1"),
    ]
    assert references[1].field_path == "/raw/nodes/0/comments/nodes/0/originalCommit/oid"


def test_invalid_structured_commit_is_not_silently_ignored() -> None:
    bundle = {
        "pull_request": {
            "commits": [],
            "reviews": [{"id": 1, "commit_id": "deadbeef"}],
            "review_comments": [],
        },
    }

    with pytest.raises(ValueError, match="invalid structured commit ID"):
        bundle_commit_references(bundle)
