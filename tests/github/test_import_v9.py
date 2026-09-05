"""Verify the isolated v9 archive importer and its restart contract."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from gh_puller.github.client import GitHubResource
from gh_puller.github.observations import iter_current_facts, iter_observations
from gh_puller.github.v10.import_v9 import V9ImportConfig, V9Importer

if TYPE_CHECKING:
    from pathlib import Path

_REPOSITORY = "acme/widgets"
_LEGACY_TIME = "2026-09-03T12:00:00Z"
_MIGRATION_TIME = datetime(2026, 9, 5, 8, tzinfo=UTC)
_SHA_A = "a" * 40
_SHA_B = "b" * 40


class _API:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def pull_review_threads(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> GitHubResource:
        assert f"{owner}/{repo}" == _REPOSITORY
        self.calls.append(("threads", number))
        raw = {
            "nodes": [
                {
                    "id": "thread-2",
                    "comments": {
                        "nodes": [
                            {
                                "id": "thread-comment-2",
                                "commit": {"oid": _SHA_B},
                                "originalCommit": {"oid": _SHA_A},
                            },
                        ],
                    },
                },
            ],
        }
        return GitHubResource({"review_comments": [], "threads": raw}, "graphql", raw)

    async def issue_relations(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> GitHubResource:
        assert f"{owner}/{repo}" == _REPOSITORY
        self.calls.append(("relations", number))
        raw = {"number": number, "subIssues": {"nodes": []}}
        return GitHubResource(raw, "graphql", raw)


class _Git:
    def __init__(self) -> None:
        self.retentions: list[list[str]] = []
        self.syncs = 0

    async def retain_commits(self, shas: list[str]) -> dict[str, dict[str, Any]]:
        self.retentions.append(shas)
        return {
            sha: {
                "sha": sha,
                "status": "available",
                "ref": f"refs/github-archive/commits/{sha}",
                "obtained": "existing",
                "verification": "commit-and-root-tree",
            }
            for sha in shas
        }

    async def sync_upstream(self) -> dict[str, Any]:
        self.syncs += 1
        return {
            "repository": _REPOSITORY,
            "symbolic_head": "refs/heads/main",
            "default_branch": "main",
            "refs": [{"name": "refs/heads/main", "sha": _SHA_B}],
        }


@pytest.mark.asyncio
async def test_imports_current_pending_resources_and_resumes_without_work(
    tmp_path: Path,
) -> None:
    source = tmp_path / "v9.sqlite3"
    destination = tmp_path / "facts.sqlite3"
    _source_archive(source)
    api = _API()
    git = _Git()
    importer = V9Importer(
        V9ImportConfig(source, destination, _REPOSITORY, concurrency=2),
        api=api,
        git=git,
        now=lambda: _MIGRATION_TIME,
    )

    result = await importer.migrate()
    observations = [fact async for fact in iter_observations(destination)]
    current = {
        (fact.family, fact.subject_key): fact
        async for fact in iter_current_facts(destination)
    }

    assert result.resources == 2
    assert result.checkpoint == datetime(2026, 9, 3, 12, tzinfo=UTC)
    assert api.calls == [("relations", 1), ("threads", 2)]
    assert git.syncs == 1
    assert set().union(*map(set, git.retentions)) == {_SHA_A, _SHA_B}
    assert current[("issue", "issue:1")].payload["value"]["title"] == "issue"
    assert current[("issue", "issue:1")].payload["raw"]["unknown"] == [1, 2]
    assert current[("issue-comments", "issue:1")].payload["cache"] == {
        "etag": '"comments"',
    }
    assert current[("pull", "pull:2")].payload["value"]["merged"] is False
    assert current[("pull-review-threads", "pull:2")].origin == "api"
    assert current[("issue-relations", "issue:1")].origin == "api"
    assert current[("commit-object", f"commit:{_SHA_A}")].coverage == "complete"
    assert current[("git-refs", "repository")].payload["value"]["default_branch"] == "main"
    assert all(fact.family != "legacy-bundle" for fact in observations)
    imported = [fact for fact in observations if fact.origin == "import"]
    assert imported
    assert all(fact.observed_until == _MIGRATION_TIME for fact in imported if fact.family != "pull-review-threads")
    assert all(
        fact.payload.get("legacy_import", {}).get("migration_policy")
        == "assumed-unchanged"
        for fact in imported
        if fact.family != "pull-review-threads"
    )

    counts = len(observations), len(api.calls), len(git.retentions), git.syncs
    repeated = await importer.migrate()
    assert repeated == result
    assert (
        len([fact async for fact in iter_observations(destination)]),
        len(api.calls),
        len(git.retentions),
        git.syncs,
    ) == counts


def _source_archive(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE archive_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE payload_blobs (
                digest TEXT PRIMARY KEY, codec TEXT NOT NULL,
                raw_size INTEGER NOT NULL, payload BLOB NOT NULL
            );
            CREATE TABLE bundle_http_cache (
                bundle_digest TEXT PRIMARY KEY, cache_digest TEXT NOT NULL,
                codec TEXT NOT NULL, raw_size INTEGER NOT NULL, payload BLOB NOT NULL
            );
            CREATE TABLE pull_runs (id INTEGER PRIMARY KEY, target_at TEXT NOT NULL);
            CREATE TABLE resource_versions (
                id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, observed_at TEXT NOT NULL,
                number INTEGER NOT NULL, github_id INTEGER NOT NULL, kind TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                summary_digest TEXT NOT NULL, bundle_digest TEXT,
                present INTEGER NOT NULL, missing_since TEXT
            );
            CREATE TABLE fact_versions (
                id INTEGER PRIMARY KEY, fact_kind TEXT NOT NULL,
                schema_version INTEGER NOT NULL, subject_key TEXT NOT NULL,
                resource_number INTEGER, source_digest TEXT,
                observed_from TEXT NOT NULL, observed_until TEXT NOT NULL,
                status TEXT NOT NULL, payload_digest TEXT NOT NULL
            );
            CREATE TABLE pending_fact_versions (
                id INTEGER PRIMARY KEY, fact_kind TEXT NOT NULL,
                schema_version INTEGER NOT NULL, subject_key TEXT NOT NULL,
                resource_number INTEGER, source_digest TEXT,
                observed_from TEXT NOT NULL, observed_until TEXT NOT NULL,
                status TEXT NOT NULL, payload_digest TEXT NOT NULL
            );
            """,
        )
        db.executemany(
            "INSERT INTO archive_meta(key, value) VALUES (?, ?)",
            (("schema_version", "9"), ("repository", _REPOSITORY)),
        )
        db.execute("INSERT INTO pull_runs VALUES (1, ?)", (_LEGACY_TIME,))
        for number, kind, bundle in (
            (1, "issue", _issue_bundle()),
            (2, "pull", _pull_bundle()),
        ):
            summary = bundle["issue"] | (
                {"pull_request": {"url": f"/repos/acme/widgets/pulls/{number}"}}
                if kind == "pull"
                else {}
            )
            summary_digest = _payload(db, summary)
            bundle_digest = _payload(db, bundle)
            cache_digest, raw = _json(
                {"issue_comments": {"etag": '"comments"'}},
            )
            db.execute(
                "INSERT INTO bundle_http_cache VALUES (?, ?, 'zlib-json-v1', ?, ?)",
                (bundle_digest, cache_digest, len(raw), zlib.compress(raw)),
            )
            db.execute(
                """
                INSERT INTO resource_versions VALUES (
                    ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL
                )
                """,
                (
                    number,
                    _LEGACY_TIME,
                    number,
                    number * 10,
                    kind,
                    _LEGACY_TIME,
                    _LEGACY_TIME,
                    summary_digest,
                    bundle_digest,
                ),
            )
        old_threads = _payload(
            db,
            {"operation": "PullReviewThreads", "raw": {"nodes": []}},
        )
        db.execute(
            """
            INSERT INTO fact_versions VALUES (
                1, 'review-threads', 1, 'pull:2', 2, NULL,
                ?, ?, 'complete', ?
            )
            """,
            (_LEGACY_TIME, _LEGACY_TIME, old_threads),
        )


def _issue_bundle() -> dict[str, Any]:
    issue = {
        "id": 10,
        "number": 1,
        "title": "issue",
        "created_at": _LEGACY_TIME,
        "updated_at": _LEGACY_TIME,
        "comments": 1,
        "reactions": {"total_count": 0},
        "unknown": [1, 2],
    }
    return {
        "schema_version": 8,
        "repository": _REPOSITORY,
        "number": 1,
        "kind": "issue",
        "issue": issue,
        "issue_comments": [{"id": 101, "body": "hello"}],
        "timeline": [{"id": 11, "event": "committed", "sha": _SHA_A}],
        "events": [],
        "reactions": [],
        "issue_comment_reactions": {"101": []},
        "api_sources": {},
    }


def _pull_bundle() -> dict[str, Any]:
    bundle = _issue_bundle() | {"number": 2, "kind": "pull"}
    bundle["issue"] = _issue_bundle()["issue"] | {
        "id": 20,
        "number": 2,
        "title": "pull",
    }
    bundle["issue_comments"] = []
    bundle["issue_comment_reactions"] = {}
    bundle["pull_request"] = {
        "detail": {
            "number": 2,
            "merged": False,
            "base": {"sha": _SHA_A},
            "head": {"sha": _SHA_B},
        },
        "reviews": [{"id": 21, "commit_id": _SHA_A}],
        "review_comments": [
            {"id": 22, "commit_id": _SHA_B, "original_commit_id": _SHA_A},
        ],
        "review_comment_reactions": {"22": []},
        "commits": [{"sha": _SHA_B}],
        "git": {
            "base_sha": _SHA_A,
            "head_sha": _SHA_B,
            "comparison_kind": "merge_base",
        },
        "requested_reviewers": {"users": [], "teams": []},
        "closing_issues_references": [{"number": 1}],
        "api_sources": {},
    }
    return bundle


def _payload(db: sqlite3.Connection, value: dict[str, Any]) -> str:
    digest, raw = _json(value)
    db.execute(
        "INSERT OR IGNORE INTO payload_blobs VALUES (?, 'zlib-json-v1', ?, ?)",
        (digest, len(raw), zlib.compress(raw)),
    )
    return digest


def _json(value: dict[str, Any]) -> tuple[str, bytes]:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(raw).hexdigest(), raw
