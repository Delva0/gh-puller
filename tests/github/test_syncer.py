"""Test the fine-grained production discovery and observation pipeline."""

from __future__ import annotations

import asyncio
import random
import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import pytest

from gh_puller.github.errors import GitHubAPIError
from gh_puller.github.observations import (
    Coverage,
    DiscoveryItemDraft,
    ObservationArchive,
    TaskDraft,
    iter_current_facts,
    iter_observations,
)
from gh_puller.github.syncer import GitHubSyncConfig, GitHubSyncer
from tests.github._puller_support import _BASE, _T0, Clock, FakeAPI, FakeGitStore, _iso

if TYPE_CHECKING:
    from pathlib import Path


def _syncer(
    path: Path,
    api: FakeAPI,
    git: FakeGitStore,
    clock: Clock,
    **config: Any,
) -> GitHubSyncer:
    return GitHubSyncer(
        GitHubSyncConfig("acme/widgets", path, **config),
        api=api,
        git=git,
        now=clock,
        sleep=clock.sleep,
    )


def _pull_detail(number: int, *, commits: int = 0) -> dict[str, Any]:
    return {
        "id": number * 10,
        "number": number,
        "base": {"sha": "a" * 40},
        "head": {"sha": f"{number:040x}"},
        "changed_files": 0,
        "commits": commits,
        "review_comments": 0,
        "requested_reviewers": [],
        "requested_teams": [],
    }


@pytest.mark.asyncio
async def test_cold_sync_publishes_independent_lossless_issue_facts(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    api = FakeAPI()
    git = FakeGitStore()
    issue = api.add_issue(7)
    issue["comments"] = 1
    issue["reactions"]["total_count"] = 1
    api.pages[f"{_BASE}/issues/7/comments"] = [
        {
            "id": 701,
            "node_id": "comment-701",
            "body": "evidence",
            "reactions": {"total_count": 0},
            "unknown": {"preserved": True},
        },
    ]
    api.pages[f"{_BASE}/issues/7/timeline"] = [
        {"id": 702, "event": "renamed", "rename": {"from": "a", "to": "b"}},
    ]
    api.pages[f"{_BASE}/issues/7/events"] = [
        {"id": 703, "event": "labeled", "label": {"name": "bug"}},
    ]
    api.pages[f"{_BASE}/issues/7/reactions"] = [
        {"id": 704, "content": "heart", "user": {"login": "alice"}},
    ]
    clock = Clock(_T0)

    result = await _syncer(database, api, git, clock).sync()

    assert result.cycle_id == 1
    assert result.started_at == result.completed_at == _T0
    assert result.discovered_items == 1
    catalog = next(call for call in api.calls if call[0] == "page" and call[1] == f"{_BASE}/issues")
    assert catalog[2] is not None
    assert catalog[2]["sort"] == "created"
    assert catalog[2]["direction"] == "asc"
    assert "since" not in catalog[2]
    current = {fact.family: fact async for fact in iter_current_facts(database) if fact.resource_number == 7}
    assert {
        "issue",
        "issue-comments",
        "issue-comment-reactions",
        "issue-events",
        "issue-reactions",
        "issue-relations",
        "issue-timeline",
    } <= current.keys()
    assert current["issue"].payload["raw"]["unknown_detail_field"] == [
        1,
        {"raw": "yes"},
    ]
    assert current["issue-comments"].payload["raw"][0]["unknown"] == {
        "preserved": True,
    }
    assert current["issue-comment-reactions"].origin.value == "derived"
    assert current["issue-comment-reactions"].payload["value"] == []
    assert current["issue-relations"].payload["raw"]["blocking"]["nodes"] == []
    assert git.syncs == 1
    async with ObservationArchive(database, "acme/widgets") as archive:
        assert await archive.discovery_checkpoint() == _T0
        assert await archive.active_cycle() is None


@pytest.mark.asyncio
async def test_crash_resumes_page_and_closed_collections_without_api_replay(
    tmp_path: Path,
) -> None:
    database = tmp_path / "facts.sqlite3"
    api = FakeAPI()
    git = FakeGitStore()
    api.add_issue(7)
    events_path = f"{_BASE}/issues/7/events"
    api.fail_once.add(events_path)
    clock = Clock(_T0)
    syncer = _syncer(database, api, git, clock)

    with pytest.raises(RuntimeError, match="injected failure"):
        await syncer.sync()

    partial = [fact async for fact in iter_observations(database)]
    assert {fact.family for fact in partial} >= {
        "git-refs",
        "issue",
        "issue-comments",
        "issue-timeline",
    }
    assert "issue-events" not in {fact.family for fact in partial}
    async with ObservationArchive(database, "acme/widgets") as archive:
        cycle = await archive.active_cycle()
        assert cycle is not None
        assert cycle.discovery_complete
        assert await archive.discovery_checkpoint() is None

    result = await syncer.sync()

    assert result.cycle_id == 1
    assert sum(call[1].endswith("/timeline") for call in api.calls) == 1
    assert sum(call[1].endswith("/events") for call in api.calls) == 2
    assert sum(call[1] == f"{_BASE}/issues" for call in api.calls) == 1
    observations = [fact async for fact in iter_observations(database)]
    assert sum(fact.family == "issue-timeline" for fact in observations) == 1
    assert sum(fact.family == "issue-events" for fact in observations) == 1


@pytest.mark.asyncio
async def test_warm_sync_combines_root_and_comment_signals_without_full_scan(
    tmp_path: Path,
) -> None:
    database = tmp_path / "facts.sqlite3"
    api = FakeAPI()
    git = FakeGitStore()
    first = api.add_issue(1)
    second = api.add_issue(2)
    clock = Clock(_T0)
    syncer = _syncer(database, api, git, clock)
    await syncer.sync()
    initial_count = len([fact async for fact in iter_observations(database)])
    api.calls.clear()

    clock.current += timedelta(hours=1)
    first["updated_at"] = _iso(clock.current)
    comment = {
        "id": 201,
        "body": "child-only change",
        "created_at": _iso(clock.current),
        "updated_at": _iso(clock.current),
        "reactions": {"total_count": 0},
    }
    api.pages[f"{_BASE}/issues/2/comments"] = [comment]
    api.pages[f"{_BASE}/issues/comments"] = [
        comment | {"issue_url": f"{_BASE}/issues/2"},
    ]
    second["comments"] = 1

    result = await syncer.sync()

    assert result.checkpoint_from == _T0
    catalog = next(call for call in api.calls if call[0] == "page" and call[1] == f"{_BASE}/issues")
    assert catalog[2] is not None
    assert catalog[2]["sort"] == "created"
    assert catalog[2]["direction"] == "asc"
    assert "since" in catalog[2]
    assert sum(call[1] == f"{_BASE}/issues/2" for call in api.calls) == 1
    assert sum(call[1] == f"{_BASE}/issues/2/comments" for call in api.calls) == 1
    current_comments = [
        fact
        async for fact in iter_current_facts(
            database,
            family="issue-comments",
            subject_key="issue:2",
        )
    ]
    assert current_comments[0].payload["value"][0]["body"] == "child-only change"
    assert len([fact async for fact in iter_observations(database)]) > initial_count
    async with ObservationArchive(database, "acme/widgets") as archive:
        assert await archive.discovery_checkpoint() == clock.current


@pytest.mark.asyncio
async def test_created_order_keeps_warm_candidates_stable_across_pages(
    tmp_path: Path,
) -> None:
    database = tmp_path / "facts.sqlite3"
    api = FakeAPI()
    git = FakeGitStore()
    clock = Clock(_T0)
    async with ObservationArchive(database, "acme/widgets") as archive:
        cycle = await archive.start_cycle(clock.current)
        cursor = await archive.begin_discovery(cycle.id, "empty")
        assert cursor is not None
        await archive.save_discovery_page(cycle.id, cursor, None, ())
        await archive.complete_cycle(cycle.id, clock.current)

    changed_at = _T0 + timedelta(minutes=10)
    for number in range(2, 103):
        api.add_issue(
            number,
            created_at=_T0 - timedelta(days=2) + timedelta(seconds=number),
            updated_at=changed_at,
        )
    moved = api.json[f"{_BASE}/issues/2"]
    clock.current += timedelta(hours=1)

    def move_first_item_after_page_one() -> None:
        kind, path, _ = api.calls[-1]
        if kind != "page" or path != f"{_BASE}/issues":
            return
        moved["updated_at"] = _iso(clock.current + timedelta(seconds=1))
        api.on_request = None

    api.on_request = move_first_item_after_page_one
    await _syncer(database, api, git, clock).sync()

    current = {fact.resource_number: fact async for fact in iter_current_facts(database, family="issue")}
    assert set(current) == set(range(2, 103))
    assert current[2].payload["value"]["updated_at"] == _iso(changed_at)

    clock.current += timedelta(hours=1)
    await _syncer(database, api, git, clock).sync()

    refreshed = [
        fact
        async for fact in iter_current_facts(
            database,
            family="issue",
            subject_key="issue:2",
        )
    ]
    assert refreshed[0].payload["value"]["updated_at"] == moved["updated_at"]


@pytest.mark.asyncio
async def test_pull_git_and_closing_relations_keep_batched_throughput(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    api = FakeAPI()
    git = FakeGitStore()
    for number in range(1, 10):
        api.add_issue(number, pull=True)
        api.json[f"{_BASE}/pulls/{number}"] = _pull_detail(number)
    clock = Clock(_T0)

    await _syncer(
        database,
        api,
        git,
        clock,
        concurrency=32,
        git_batch_size=8,
    ).sync()

    assert [len(batch) for batch in git.prefetches] == [8, 1]
    assert {number for batch in git.prefetches for number in batch} == set(range(1, 10))
    assert set(git.captures) == set(range(1, 10))
    closing = [call for call in api.calls if call[0] == "closing"]
    assert len(closing) == 1
    assert closing[0][2] is not None
    assert closing[0][2]["owner"] == "acme"
    assert closing[0][2]["repo"] == "widgets"
    assert set(closing[0][2]["numbers"]) == set(range(1, 10))
    current = [fact async for fact in iter_current_facts(database)]
    assert sum(fact.family == "pull-git" for fact in current) == 9
    assert sum(fact.family == "pull-closing-issues" for fact in current) == 9
    assert all(
        fact.coverage is Coverage.COMPLETE for fact in current if fact.family in {"pull-git", "pull-closing-issues"}
    )


@pytest.mark.asyncio
async def test_commit_checks_are_batched_independently_from_pull_fetches(
    tmp_path: Path,
) -> None:
    database = tmp_path / "facts.sqlite3"
    api = FakeAPI()
    git = FakeGitStore()
    api.add_issue(1, pull=True)
    api.json[f"{_BASE}/pulls/1"] = _pull_detail(1, commits=257)
    commits = [
        {"sha": f"{index:040x}", "commit": {"message": f"commit {index}"}}
        for index in range(1, 258)
    ]
    api.comparisons["a" * 40, f"{1:040x}"] = commits

    await _syncer(
        database,
        api,
        git,
        Clock(_T0),
        concurrency=1,
        git_batch_size=8,
    ).sync()

    assert [len(batch) for batch in git.prefetches] == [1]
    assert [len(batch) for batch in git.retentions] == [257]
    assert len({sha for batch in git.retentions for sha in batch}) == 257


@pytest.mark.asyncio
async def test_git_lane_consumes_pr_children_while_a_later_parent_waits(
    tmp_path: Path,
) -> None:
    database = tmp_path / "facts.sqlite3"
    sha = "c" * 40

    class BlockingAPI(FakeAPI):
        def __init__(self) -> None:
            super().__init__()
            self.blocked = asyncio.Event()
            self.release = asyncio.Event()

        async def paginate_cached(
            self,
            path: str,
            *,
            previous: list[dict[str, Any]] | None,
            cache: dict[str, Any] | None,
            params: dict[str, Any] | None = None,
            page_observer: Any = None,
        ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
            if path == f"{_BASE}/issues/2/timeline":
                self.blocked.set()
                await self.release.wait()
            return await super().paginate_cached(
                path,
                previous=previous,
                cache=cache,
                params=params,
                page_observer=page_observer,
            )

    class TrackingGit(FakeGitStore):
        def __init__(self) -> None:
            super().__init__()
            self.pull_captured = asyncio.Event()
            self.commit_retained = asyncio.Event()

        async def capture(self, number: int, pull: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            result = await super().capture(number, pull, **kwargs)
            self.pull_captured.set()
            return result

        async def retain_commits(
            self,
            shas: list[str],
            **kwargs: Any,
        ) -> dict[str, dict[str, Any]]:
            result = await super().retain_commits(shas, **kwargs)
            self.commit_retained.set()
            return result

    api = BlockingAPI()
    pull = api.add_issue(1, pull=True)
    issue = api.add_issue(2)
    api.json[f"{_BASE}/pulls/1"] = _pull_detail(1, commits=1)
    api.pages[f"{_BASE}/pulls/1/commits"] = [
        {"sha": sha, "commit": {"message": "retained while API waits"}},
    ]
    clock = Clock(_T0)
    git = TrackingGit()
    async with ObservationArchive(database, "acme/widgets") as archive:
        cycle = await archive.start_cycle(clock.current)
        cursor = await archive.begin_discovery(cycle.id, "catalog")
        assert cursor == "catalog"
        await archive.save_discovery_page(
            cycle.id,
            cursor,
            None,
            (
                DiscoveryItemDraft(1, "pull", _T0, _T0, pull),
                DiscoveryItemDraft(2, "issue", _T0, _T0, issue),
            ),
            (
                TaskDraft("parent:1", "parent", "issue:1", {"number": 1}, 1),
                TaskDraft("parent:2", "parent", "issue:2", {"number": 2}, 2),
            ),
        )

    running = asyncio.create_task(
        _syncer(
            database,
            api,
            git,
            clock,
            concurrency=1,
            git_batch_size=8,
        ).sync(),
    )
    try:
        await asyncio.wait_for(api.blocked.wait(), 1)
        await asyncio.wait_for(git.pull_captured.wait(), 1)
        await asyncio.wait_for(git.commit_retained.wait(), 1)
    finally:
        api.release.set()
        await running

    assert git.captures == [1]
    assert {value for batch in git.retentions for value in batch} == {sha}


@pytest.mark.asyncio
async def test_structured_commit_sources_are_retained_and_traceable(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    api = FakeAPI()
    git = FakeGitStore()
    api.add_issue(2, pull=True)
    api.json[f"{_BASE}/pulls/2"] = _pull_detail(2, commits=1) | {
        "head": {
            "sha": f"{2:040x}",
            "ref": "feature",
            "repo": {
                "full_name": "alice/widgets",
                "html_url": "https://github.example/alice/widgets",
            },
        },
        "review_comments": 1,
    }
    api.pages[f"{_BASE}/pulls/2/commits"] = [
        {"sha": "c" * 40, "commit": {"message": "fix"}},
    ]
    api.pages[f"{_BASE}/pulls/2/reviews"] = [
        {"id": 21, "commit_id": "d" * 40, "body": "approved"},
    ]
    api.pages[f"{_BASE}/pulls/2/comments"] = [
        {
            "id": 22,
            "commit_id": "e" * 40,
            "original_commit_id": "f" * 40,
            "reactions": {"total_count": 0},
        },
    ]
    api.review_threads[2] = [
        {
            "id": "thread-2",
            "comments": {
                "totalCount": 1,
                "nodes": [
                    {
                        "id": "comment-22",
                        "commit": {"oid": "e" * 40},
                        "originalCommit": {"oid": "f" * 40},
                    },
                ],
            },
        },
    ]
    api.pages[f"{_BASE}/issues/2/timeline"] = [
        {"id": 23, "event": "committed", "sha": "1" * 40},
    ]
    clock = Clock(_T0)

    await _syncer(database, api, git, clock, concurrency=16).sync()

    retained = {sha for batch in git.retentions for sha in batch}
    assert retained == {"1" * 40, "c" * 40, "d" * 40, "e" * 40, "f" * 40}
    references = [fact async for fact in iter_current_facts(database, family="commit-references")]
    assert {reference["sha"] for fact in references for reference in fact.payload["references"]} == retained
    objects = [fact async for fact in iter_current_facts(database, family="commit-object")]
    assert {fact.subject_key for fact in objects} == {f"commit:{sha}" for sha in retained}
    assert all(fact.coverage is Coverage.COMPLETE for fact in objects)
    reference_ids = {fact.id for fact in references}
    assert all(set(fact.payload["reference_scope"]["observation_ids"]) <= reference_ids for fact in objects)
    sources = {source for batch in git.retention_sources for selected in batch.values() for source in selected}
    assert {(source.kind, source.remote_ref) for source in sources} == {
        ("pull-ref", "refs/pull/2/head"),
        ("repository-ref", "refs/heads/feature"),
    }


@pytest.mark.asyncio
async def test_shared_commit_sources_from_multiple_parents_do_not_conflict(
    tmp_path: Path,
) -> None:
    database = tmp_path / "facts.sqlite3"
    api = FakeAPI()
    git = FakeGitStore()
    sha = "c" * 40
    for number in (2, 3):
        api.add_issue(number, pull=True)
        api.json[f"{_BASE}/pulls/{number}"] = _pull_detail(number, commits=1)
        api.pages[f"{_BASE}/pulls/{number}/commits"] = [
            {"sha": sha, "commit": {"message": "shared"}},
        ]

    await _syncer(
        database,
        api,
        git,
        Clock(_T0),
        concurrency=16,
    ).sync()

    assert {value for batch in git.retentions for value in batch} == {sha}
    references = [
        fact async for fact in iter_observations(database, family="commit-references") if fact.payload["references"]
    ]
    objects = [fact async for fact in iter_observations(database, family="commit-object")]
    assert {fact.resource_number for fact in references} == {2, 3}
    assert len(objects) == 2
    assert all(
        set(fact.payload["reference_scope"]["observation_ids"]) == {reference.id for reference in references}
        for fact in objects
    )


@pytest.mark.asyncio
async def test_permission_outcome_is_fact_not_failed_execution(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    api = FakeAPI()
    api.add_issue(7)
    api.issue_relation_errors[7] = GitHubAPIError("forbidden", status_code=403)
    clock = Clock(_T0)

    await _syncer(database, api, FakeGitStore(), clock).sync()

    relations = [
        fact
        async for fact in iter_current_facts(
            database,
            family="issue-relations",
            subject_key="issue:7",
        )
    ]
    assert relations[0].coverage is Coverage.FORBIDDEN
    assert relations[0].payload["error"]["status_code"] == 403


@pytest.mark.asyncio
async def test_catalog_cursor_survives_failure_before_next_page(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    api = FakeAPI()
    git = FakeGitStore()
    for number in range(1, 102):
        api.add_issue(number)
    api.fail_once.add(f"{_BASE}/issues/1/events")
    clock = Clock(_T0)
    syncer = _syncer(database, api, git, clock, concurrency=1)

    with pytest.raises(RuntimeError, match="injected failure"):
        await syncer.sync()
    async with ObservationArchive(database, "acme/widgets") as archive:
        cycle = await archive.active_cycle()
        assert cycle is not None
        assert not cycle.discovery_complete
        assert cycle.discovery_pages == 1
        assert parse_qs(urlsplit(cycle.discovery_cursor or "").query)["__offset"] == [
            "100",
        ]

    await syncer.sync()

    catalog_calls = [call for call in api.calls if call[0] == "page" and call[1] == f"{_BASE}/issues"]
    assert len(catalog_calls) == 2
    assert sum(call[1] == f"{_BASE}/issues/1/timeline" for call in api.calls) == 1
    assert sum(call[1] == f"{_BASE}/issues/1/events" for call in api.calls) == 2
    async with ObservationArchive(database, "acme/widgets") as archive:
        assert await archive.discovery_checkpoint() == _T0


@pytest.mark.asyncio
async def test_random_churn_and_failures_converge_for_every_discovered_parent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "facts.sqlite3"
    api = FakeAPI()
    git = FakeGitStore()
    clock = Clock(_T0)
    randomizer = random.Random(7)  # noqa: S311 - deterministic non-security simulation
    active = set(range(1, 81))
    deleted: set[int] = set()
    next_number = 81
    next_comment = 10_000
    for number in sorted(active):
        _add_random_parent(api, number, pull=number % 4 == 0)
    syncer = _syncer(database, api, git, clock, concurrency=16)
    await syncer.sync()

    for _ in range(10):
        previous_checkpoint = clock.current
        clock.current += timedelta(hours=1)
        added = set(range(next_number, next_number + 3))
        next_number += 3
        active.update(added)
        for number in sorted(added):
            _add_random_parent(api, number, pull=number % 4 == 0, at=clock.current)

        touched = set(randomizer.sample(sorted(active), 5))
        for number in touched:
            summary = api.json[f"{_BASE}/issues/{number}"]
            summary["body"] = f"root update at {clock.current.isoformat()}"
            summary["updated_at"] = _iso(clock.current)

        comment_parents = set(randomizer.sample(sorted(active), 6))
        touched.update(comment_parents)
        for number in comment_parents:
            path = f"{_BASE}/issues/{number}/comments"
            comments = api.pages.setdefault(path, [])
            if comments and randomizer.random() < 0.4:
                comments.pop(0)
                api.json[f"{_BASE}/issues/{number}"]["updated_at"] = _iso(clock.current)
            else:
                comments.append(
                    {
                        "id": next_comment,
                        "body": f"comment {next_comment}",
                        "created_at": _iso(clock.current),
                        "updated_at": _iso(clock.current),
                        "reactions": {"total_count": 0},
                    },
                )
                next_comment += 1
            api.json[f"{_BASE}/issues/{number}"]["comments"] = len(comments)

        pulls = [number for number in active if "pull_request" in api.json[f"{_BASE}/issues/{number}"]]
        review_parents = set(randomizer.sample(pulls, min(3, len(pulls))))
        touched.update(review_parents)
        for number in review_parents:
            path = f"{_BASE}/pulls/{number}/comments"
            comments = api.pages.setdefault(path, [])
            comments.append(
                {
                    "id": next_comment,
                    "body": f"review {next_comment}",
                    "created_at": _iso(clock.current),
                    "updated_at": _iso(clock.current),
                    "commit_id": f"{number:040x}",
                    "original_commit_id": f"{number:040x}",
                    "reactions": {"total_count": 0},
                },
            )
            next_comment += 1
            api.json[f"{_BASE}/pulls/{number}"]["review_comments"] = len(comments)

        removed = randomizer.choice(sorted(active - added - touched))
        active.remove(removed)
        deleted.add(removed)
        api.catalog = [item for item in api.catalog if item["number"] != removed]
        api.json.pop(f"{_BASE}/issues/{removed}")
        api.json.pop(f"{_BASE}/pulls/{removed}", None)

        failed = min(added)
        api.fail_once.add(f"{_BASE}/issues/{failed}/events")
        with pytest.raises(RuntimeError, match="injected failure"):
            await syncer.sync()
        async with ObservationArchive(database, "acme/widgets") as archive:
            assert await archive.discovery_checkpoint() == previous_checkpoint
        await syncer.sync()
        async with ObservationArchive(database, "acme/widgets") as archive:
            assert await archive.discovery_checkpoint() == clock.current

        current = {(fact.family, fact.subject_key): fact async for fact in iter_current_facts(database)}
        for number in active:
            issue = api.json[f"{_BASE}/issues/{number}"]
            assert current[("issue", f"issue:{number}")].payload["value"] == issue
            expected_comments = api.pages.get(f"{_BASE}/issues/{number}/comments", [])
            actual_comments = current[("issue-comments", f"issue:{number}")].payload["value"]
            assert len(actual_comments) == len(expected_comments)
            assert all(
                {key: actual[key] for key in expected} == expected
                for actual, expected in zip(
                    actual_comments,
                    expected_comments,
                    strict=True,
                )
            )
        assert all(("issue", f"issue:{number}") in current for number in deleted)

    with sqlite3.connect(database) as connection:
        duplicate = connection.execute(
            """
            SELECT b.cycle_id, o.family, o.subject_key, COUNT(*)
            FROM fact_observations AS o
            JOIN fact_batches AS b ON b.id = o.batch_id
            WHERE b.cycle_id IS NOT NULL
            GROUP BY b.cycle_id, o.family, o.subject_key
            HAVING COUNT(*) > 1
            LIMIT 1
            """,
        ).fetchone()
        assert duplicate is None
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sync_cycles WHERE status != 'complete'",
            ).fetchone()[0]
            == 0
        )


def _add_random_parent(
    api: FakeAPI,
    number: int,
    *,
    pull: bool,
    at: datetime = _T0 - timedelta(days=1),
) -> None:
    api.add_issue(number, created_at=at, updated_at=at, pull=pull)
    if pull:
        api.json[f"{_BASE}/pulls/{number}"] = _pull_detail(number)
