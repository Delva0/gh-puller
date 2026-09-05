"""Test resumable supplemental backfill and explicit refresh operations."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from gh_puller.github import iter_facts
from gh_puller.github.maintenance import GitHubFactMaintainer
from gh_puller.github.store import (
    CatalogItem,
    SQLiteArchive,
    StagedResource,
    StoredHead,
    json_digest,
)
from tests.github._puller_support import (
    _T0,
    Clock,
    FakeAPI,
    FakeGitStore,
    _config,
    _iso,
)

if TYPE_CHECKING:
    from pathlib import Path


def _bundle(number: int, kind: str, sha: str) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "schema_version": 7,
        "repository": "acme/widgets",
        "number": number,
        "kind": kind,
        "issue": {"id": number * 10, "number": number},
        "timeline": [{"id": number * 100, "event": "committed", "sha": sha}],
        "events": [],
    }
    if kind == "pull":
        bundle["pull_request"] = {
            "detail": {"merged": False},
            "commits": [{"sha": sha}],
            "git": {
                "base_sha": "e" * 40,
                "head_sha": sha,
                "comparison_kind": "empty_tree",
            },
            "reviews": [],
            "review_comments": [],
        }
    return bundle


async def _publish(
    database: Path,
    target: str,
    bundles: list[dict[str, Any]],
) -> None:
    async with SQLiteArchive(database, "acme/widgets") as archive:
        run = await archive.start_run(target, target)
        await archive.start_pass(run.id, "closing", target, "full")
        await archive.prepare_pass(run.id, (), len(bundles))
        catalog = [
            CatalogItem(
                number=int(bundle["number"]),
                github_id=int(bundle["number"]) * 10,
                kind=str(bundle["kind"]),
                created_at=target,
                updated_at=target,
                summary={
                    "id": int(bundle["number"]) * 10,
                    "number": int(bundle["number"]),
                    "created_at": target,
                    "updated_at": target,
                },
            )
            for bundle in bundles
        ]
        await archive.stage_catalog_page(run.id, catalog, None)
        tasks = {task.number: task for task in await archive.pending_catalog_tasks(run.id)}
        for item, bundle in zip(catalog, bundles, strict=True):
            task = tasks[item.number]
            head = StoredHead(
                number=item.number,
                github_id=item.github_id,
                kind=item.kind,
                created_at=item.created_at,
                updated_at=item.updated_at,
                summary_digest=task.summary_digest or "",
                bundle_digest=json_digest(bundle),
                present=True,
                missing_since=None,
            )
            resource = StagedResource(head, target, item.summary, bundle)
            assert await archive.stage_task(run.id, item.number, task.summary_digest, resource)
        await archive.finish_pass(run.id, target)
        await archive.finalize(run.id, target)


@pytest.mark.asyncio
async def test_backfill_scans_all_historical_bundles_and_reuses_complete_scope(
    tmp_path: Path,
) -> None:
    database = tmp_path / "archive.sqlite3"
    old = "a" * 40
    pull = "b" * 40
    current = "c" * 40
    thread = "d" * 40
    await _publish(database, _iso(_T0), [_bundle(1, "issue", old), _bundle(2, "pull", pull)])
    await _publish(
        database,
        _iso(_T0 + timedelta(hours=1)),
        [_bundle(1, "issue", current)],
    )
    api = FakeAPI()
    api.add_issue(1)
    api.add_issue(2, pull=True)
    api.review_threads[2] = [
        {
            "id": "thread-2",
            "path": "src/a.py",
            "comments": {
                "totalCount": 1,
                "nodes": [
                    {
                        "id": "comment-2",
                        "commit": {"oid": thread},
                        "originalCommit": None,
                    },
                ],
            },
        },
    ]
    api.issue_relation_sets[1] = {
        "issue": {"id": "issue-1", "number": 1},
        "parent": None,
        "subIssues": {"totalCount": 0, "nodes": []},
        "blockedBy": {"totalCount": 0, "nodes": []},
        "blocking": {"totalCount": 0, "nodes": []},
    }
    git = FakeGitStore()
    maintainer = GitHubFactMaintainer(
        _config(database),
        api=api,
        git=git,
        now=lambda: _T0 + timedelta(hours=2),
    )

    result = await maintainer.backfill(batch_size=2)

    assert (result.completed_tasks, result.total_tasks) == (6, 6)
    facts = [fact async for fact in iter_facts(database)]
    bundle_scans = {
        fact.source_digest
        for fact in facts
        if fact.fact_kind == "commit-references" and fact.payload["source_kind"] == "bundle"
    }
    assert bundle_scans == {
        json_digest(_bundle(1, "issue", old)),
        json_digest(_bundle(1, "issue", current)),
        json_digest(_bundle(2, "pull", pull)),
    }
    assert {fact.subject_key for fact in facts if fact.fact_kind == "commit-object" and fact.status == "complete"} == {
        f"commit:{sha}" for sha in (old, pull, current, thread)
    }
    assert any(fact.fact_kind == "review-threads" for fact in facts)
    assert any(fact.fact_kind == "issue-relations" for fact in facts)
    assert any(fact.fact_kind == "git-refs" for fact in facts)
    calls = list(api.calls)
    retentions = list(git.retentions)

    repeated = await maintainer.backfill(batch_size=1)

    assert repeated == result
    assert api.calls == calls
    assert git.retentions == retentions
    with sqlite3.connect(database) as connection:
        scope = connection.execute(
            "SELECT total_tasks, completed_tasks, status FROM fact_jobs",
        ).fetchone()
    assert scope == (6, 6, "complete")


@pytest.mark.asyncio
async def test_refresh_is_idempotent_at_one_target_and_observes_again_at_a_new_target(
    tmp_path: Path,
) -> None:
    database = tmp_path / "archive.sqlite3"
    sha = "a" * 40
    await _publish(database, _iso(_T0), [_bundle(1, "issue", sha), _bundle(2, "pull", sha)])
    api = FakeAPI()
    api.add_issue(1)
    api.add_issue(2, pull=True)
    git = FakeGitStore()
    clock = Clock(_T0 + timedelta(hours=1))
    maintainer = GitHubFactMaintainer(
        _config(database),
        api=api,
        git=git,
        now=clock,
        sleep=clock.sleep,
    )

    first = await maintainer.refresh(
        clock.current,
        pulls=[2],
        issues=[1],
        fact_groups=["reviews", "relations"],
        batch_size=2,
    )
    calls = len(api.calls)
    assert (
        await maintainer.refresh(
            clock.current,
            pulls=[2],
            issues=[1],
            fact_groups=["reviews", "relations"],
        )
        == first
    )
    assert len(api.calls) == calls

    clock.current += timedelta(hours=1)
    second = await maintainer.refresh(
        clock.current,
        pulls=[2],
        issues=[1],
        fact_groups=["reviews", "relations"],
    )

    assert second.job_id != first.job_id
    assert len(api.calls) == calls + 2
    facts = [fact async for fact in iter_facts(database)]
    assert sum(fact.fact_kind == "review-threads" for fact in facts) == 2
    assert sum(fact.fact_kind == "issue-relations" for fact in facts) == 2


@pytest.mark.asyncio
async def test_maintenance_rejects_an_unpublished_normal_run(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite3"
    async with SQLiteArchive(database, "acme/widgets") as archive:
        await archive.start_run(_iso(_T0), _iso(_T0))
    maintainer = GitHubFactMaintainer(
        _config(database),
        api=FakeAPI(),
        git=FakeGitStore(),
        now=lambda: _T0,
    )

    with pytest.raises(RuntimeError, match="pending pull"):
        await maintainer.backfill()


@pytest.mark.asyncio
async def test_cancelled_backfill_resumes_only_its_unpublished_task(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite3"
    await _publish(database, _iso(_T0), [_bundle(2, "pull", "a" * 40)])

    class CancelledAPI(FakeAPI):
        async def pull_review_threads(self, owner: str, repo: str, number: int) -> Any:
            del owner, repo, number
            raise asyncio.CancelledError

    cancelled = GitHubFactMaintainer(
        _config(database),
        api=CancelledAPI(),
        git=FakeGitStore(),
        now=lambda: _T0 + timedelta(hours=1),
    )
    with pytest.raises(asyncio.CancelledError):
        await cancelled.backfill(fact_groups=["reviews"])

    api = FakeAPI()
    api.add_issue(2, pull=True)
    resumed = GitHubFactMaintainer(
        _config(database),
        api=api,
        git=FakeGitStore(),
        now=lambda: _T0 + timedelta(hours=2),
    )
    result = await resumed.backfill(fact_groups=["reviews"])

    assert (result.job_id, result.completed_tasks, result.total_tasks) == (1, 1, 1)
    with sqlite3.connect(database) as connection:
        task = connection.execute(
            "SELECT attempts, completed, outcome FROM fact_tasks",
        ).fetchone()
        batches = connection.execute("SELECT count(*) FROM fact_batches").fetchone()[0]
    assert task == (2, 1, "complete")
    assert batches == 1


@pytest.mark.asyncio
async def test_explicit_refresh_retries_an_unavailable_commit(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite3"
    sha = "a" * 40
    await _publish(database, _iso(_T0), [_bundle(1, "issue", sha)])

    class AvailabilityGit(FakeGitStore):
        def __init__(self) -> None:
            super().__init__()
            self.available = False

        async def retain_commits(
            self,
            shas: list[str],
            *,
            heartbeat: Any = None,
            retry: Any = None,
        ) -> dict[str, dict[str, Any]]:
            del heartbeat, retry
            self.retentions.append(shas)
            return {
                item: (
                    {"sha": item, "status": "available", "obtained": "fetched"}
                    if self.available
                    else {"sha": item, "status": "unavailable", "reason": "not our ref"}
                )
                for item in shas
            }

    git = AvailabilityGit()
    clock = Clock(_T0 + timedelta(hours=1))
    maintainer = GitHubFactMaintainer(
        _config(database),
        api=FakeAPI(),
        git=git,
        now=clock,
        sleep=clock.sleep,
    )
    first = await maintainer.refresh(
        clock.current,
        commits=[sha],
        fact_groups=["commits"],
    )
    git.available = True
    clock.current += timedelta(hours=1)
    second = await maintainer.refresh(
        clock.current,
        commits=[sha],
        fact_groups=["commits"],
    )

    assert second.job_id != first.job_id
    objects = [fact async for fact in iter_facts(database) if fact.fact_kind == "commit-object"]
    assert [fact.status for fact in objects] == ["unavailable", "complete"]
    with sqlite3.connect(database) as connection:
        current = connection.execute(
            "SELECT status FROM current_facts WHERE fact_kind = 'commit-object'",
        ).fetchone()
    assert current == ("complete",)
