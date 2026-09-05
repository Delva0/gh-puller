"""Test resumable supplemental backfill and explicit refresh operations."""

from __future__ import annotations

import asyncio
import random
import sqlite3
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from gh_puller.github import iter_facts
from gh_puller.github.commit_references import bundle_commit_references
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
async def test_maintenance_preserves_an_independent_pending_normal_run(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite3"
    await _publish(database, _iso(_T0), [_bundle(1, "issue", "a" * 40)])
    async with SQLiteArchive(database, "acme/widgets") as archive:
        pending = await archive.start_run(
            _iso(_T0 + timedelta(hours=1)),
            _iso(_T0 + timedelta(hours=1)),
        )
    api = FakeAPI()
    api.add_issue(1)
    maintainer = GitHubFactMaintainer(
        _config(database),
        api=api,
        git=FakeGitStore(),
        now=lambda: _T0 + timedelta(hours=2),
    )

    result = await maintainer.backfill(fact_groups=["relations"])

    assert result.completed_tasks == 1
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT id, status FROM pull_runs ORDER BY id",
        ).fetchall()
    assert rows == [(1, "committed"), (pending.id, "pending")]


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


@pytest.mark.asyncio
async def test_parent_backfill_obeys_configured_concurrency(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite3"
    await _publish(
        database,
        _iso(_T0),
        [_bundle(number, "pull", f"{number:040x}") for number in range(1, 6)],
    )

    class ConcurrentAPI(FakeAPI):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.maximum = 0
            self.saturated = asyncio.Event()
            self.release = asyncio.Event()

        async def pull_review_threads(self, owner: str, repo: str, number: int) -> Any:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.active == 2:
                self.saturated.set()
            try:
                await self.release.wait()
                return await super().pull_review_threads(owner, repo, number)
            finally:
                self.active -= 1

    api = ConcurrentAPI()
    for number in range(1, 6):
        api.add_issue(number, pull=True)
    maintainer = GitHubFactMaintainer(
        _config(database, concurrency=2),
        api=api,
        git=FakeGitStore(),
        now=lambda: _T0 + timedelta(hours=1),
    )

    running = asyncio.create_task(
        maintainer.backfill(fact_groups=["reviews"], batch_size=5),
    )
    await asyncio.wait_for(api.saturated.wait(), 1)
    await asyncio.sleep(0)
    assert (api.active, api.maximum) == (2, 2)
    api.release.set()
    result = await running

    assert result.completed_tasks == 5
    assert api.maximum == 2


@pytest.mark.asyncio
async def test_large_random_backfill_and_refresh_match_a_naive_fact_model(
    tmp_path: Path,
) -> None:
    rng = random.Random(20260905)  # noqa: S311 - The simulation must be reproducible.
    database = tmp_path / "archive.sqlite3"
    history: dict[str, dict[str, Any]] = {}
    for epoch in range(16):
        target = _T0 + timedelta(hours=epoch)
        bundles = []
        for number in range(1, 97):
            kind = "pull" if number % 3 == 0 else "issue"
            sha = f"{epoch * 1000 + number:040x}"
            bundle = _bundle(number, kind, sha)
            history[json_digest(bundle)] = bundle
            bundles.append(bundle)
        await _publish(database, _iso(target), bundles)

    api = FakeAPI()
    thread_shas: set[str] = set()
    for number in range(1, 97):
        pull = number % 3 == 0
        api.add_issue(number, pull=pull)
        if pull:
            sha = f"{100_000 + number:040x}"
            thread_shas.add(sha)
            api.review_threads[number] = [_thread(number, (sha,))]
        else:
            api.issue_relation_sets[number] = _relations(number, rng.sample(range(1, 97), 3))
    git = FakeGitStore()
    clock = Clock(_T0 + timedelta(hours=20))
    maintainer = GitHubFactMaintainer(
        _config(database, concurrency=32),
        api=api,
        git=git,
        now=clock,
        sleep=clock.sleep,
    )

    await maintainer.backfill(batch_size=100)

    facts = [fact async for fact in iter_facts(database)]
    scans = {fact.subject_key: fact for fact in facts if fact.fact_kind == "commit-references"}
    expected_bundle_subjects = {f"bundle:{digest}" for digest in history}
    assert expected_bundle_subjects <= scans.keys()
    assert len([subject for subject in scans if subject.startswith("bundle:")]) == len(history)
    expected_shas = {
        reference.sha for bundle in history.values() for reference in bundle_commit_references(bundle)
    } | thread_shas
    observed_shas = {
        fact.subject_key.removeprefix("commit:")
        for fact in facts
        if fact.fact_kind == "commit-object" and fact.status == "complete"
    }
    assert observed_shas == expected_shas
    assert {fact.resource_number for fact in facts if fact.fact_kind == "review-threads"} == set(range(3, 97, 3))
    assert {fact.resource_number for fact in facts if fact.fact_kind == "issue-relations"} == {
        number for number in range(1, 97) if number % 3 != 0
    }

    for epoch in range(12):
        selected_pulls = rng.sample(range(3, 97, 3), 6)
        selected_issues = rng.sample([number for number in range(1, 97) if number % 3 != 0], 6)
        for number in selected_pulls:
            count = rng.randrange(4)
            shas = tuple(f"{200_000 + epoch * 1000 + number * 4 + index:040x}" for index in range(count))
            api.review_threads[number] = [] if not shas else [_thread(number, shas)]
        for number in selected_issues:
            members = rng.sample(range(1, 97), rng.randrange(6))
            api.issue_relation_sets[number] = _relations(number, members)
        clock.current += timedelta(hours=1)
        await maintainer.refresh(
            clock.current,
            pulls=selected_pulls,
            issues=selected_issues,
            fact_groups=["reviews", "relations"],
            batch_size=12,
        )

    current: dict[tuple[str, str], Any] = {}
    async for fact in iter_facts(database):
        if fact.status in {"complete", "null"}:
            current[(fact.fact_kind, fact.subject_key)] = fact
    for number in range(3, 97, 3):
        raw = current[("review-threads", f"pull:{number}")].payload["raw"]
        assert raw == {
            "totalCount": len(api.review_threads[number]),
            "nodes": api.review_threads[number],
        }
    for number in range(1, 97):
        if number % 3 != 0:
            raw = current[("issue-relations", f"issue:{number}")].payload["raw"]
            assert raw == api.issue_relation_sets[number]


def _thread(number: int, shas: tuple[str, ...]) -> dict[str, Any]:
    return {
        "id": f"thread-{number}",
        "path": f"src/{number}.py",
        "comments": {
            "totalCount": len(shas),
            "nodes": [
                {
                    "id": f"comment-{number}-{index}",
                    "commit": {"oid": sha},
                    "originalCommit": None,
                }
                for index, sha in enumerate(shas)
            ],
        },
    }


def _relations(number: int, members: list[int]) -> dict[str, Any]:
    nodes = [{"id": f"issue-{member}", "number": member} for member in members]
    return {
        "issue": {"id": f"issue-{number}", "number": number},
        "parent": None,
        "subIssues": {"totalCount": len(nodes), "nodes": nodes},
        "blockedBy": {"totalCount": 0, "nodes": []},
        "blocking": {"totalCount": 0, "nodes": []},
    }
