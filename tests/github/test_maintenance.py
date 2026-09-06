"""Test durable targeted observations independent of discovery signals."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from gh_puller.github.commit_references import (
    commit_reference_payload,
    observation_commit_references,
)
from gh_puller.github.maintenance import GitHubMaintainer
from gh_puller.github.observations import (
    Coverage,
    FactDraft,
    FactObservation,
    ObservationArchive,
    Origin,
    iter_observations,
)
from gh_puller.github.syncer import GitHubSyncConfig
from tests.github._puller_support import _T0, Clock, FakeAPI, FakeGitStore

if TYPE_CHECKING:
    from pathlib import Path


async def _roots(database: Path, api: FakeAPI) -> None:
    async with ObservationArchive(database, "acme/widgets") as archive:
        await archive.publish(
            "seed:roots",
            "import",
            _T0,
            tuple(
                FactDraft(
                    "issue",
                    f"issue:{item['number']}",
                    _T0,
                    _T0,
                    Coverage.COMPLETE,
                    Origin.IMPORT,
                    {"value": item},
                    resource_number=int(item["number"]),
                )
                for item in api.catalog
            ),
        )


async def _source(
    archive: ObservationArchive,
    key: str,
    family: str,
    payload: dict[str, Any],
    *,
    resource_number: int = 7,
) -> FactObservation:
    return (
        await archive.publish(
            f"seed:source:{key}",
            "import",
            _T0,
            (
                FactDraft(
                    family,
                    f"{family}:{key}",
                    _T0,
                    _T0,
                    Coverage.COMPLETE,
                    Origin.IMPORT,
                    payload,
                    resource_number=resource_number,
                ),
            ),
        )
    )[0]


async def _scan(archive: ObservationArchive, key: str, source: FactObservation) -> None:
    references = observation_commit_references(source.family, source.payload)
    await archive.publish(
        f"seed:scan:{key}",
        "import",
        _T0,
        (
            FactDraft(
                "commit-references",
                f"payload:{source.payload_digest}",
                source.observed_from,
                source.observed_until,
                Coverage.COMPLETE,
                Origin.DERIVED,
                {
                    "operation": "StructuredCommitReferenceScan",
                    "repository": "acme/widgets",
                    "source_family": source.family,
                    "source_observation_id": source.id,
                    "source_payload_digest": source.payload_digest,
                    "references": [
                        commit_reference_payload(reference) for reference in references
                    ],
                },
                resource_number=source.resource_number,
                source_digest=source.payload_digest,
            ),
        ),
    )


def _maintainer(
    database: Path,
    api: FakeAPI,
    git: FakeGitStore,
    clock: Clock,
) -> GitHubMaintainer:
    return GitHubMaintainer(
        GitHubSyncConfig("acme/widgets", database),
        api=api,
        git=git,
        now=clock,
    )


@pytest.mark.asyncio
async def test_refresh_observes_silent_thread_and_relation_replacements(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    api = FakeAPI()
    api.add_issue(7, pull=True)
    api.add_issue(8)
    sha = "a" * 40
    api.review_threads[7] = [_thread("old", sha, resolved=False)]
    api.issue_relation_sets[8] = _relations(8, 9)
    await _roots(database, api)
    git = FakeGitStore()
    clock = Clock(_T0 + timedelta(hours=1))
    maintainer = _maintainer(database, api, git, clock)

    first = await maintainer.refresh(pulls=[7], issues=[8])
    first_calls = len(api.calls)
    repeated = await maintainer.refresh(
        pulls=[7],
        issues=[8],
        idempotency_key="research-sample-1",
    )
    repeated_calls = len(api.calls)
    assert repeated.job_id != first.job_id
    assert repeated_calls == first_calls + 2
    assert (
        await maintainer.refresh(
            pulls=[7],
            issues=[8],
            idempotency_key="research-sample-1",
        )
        == repeated
    )
    assert len(api.calls) == repeated_calls

    api.review_threads[7] = [_thread("new", sha, resolved=True)]
    api.issue_relation_sets[8] = _relations(8, 10)
    clock.current += timedelta(hours=1)
    second = await maintainer.refresh(pulls=[7], issues=[8])

    assert second.job_id != repeated.job_id
    observations = [fact async for fact in iter_observations(database)]
    threads = [fact for fact in observations if fact.family == "pull-review-threads"]
    relations = [fact for fact in observations if fact.family == "issue-relations"]
    assert [fact.payload["raw"]["nodes"][0]["isResolved"] for fact in threads] == [
        False,
        False,
        True,
    ]
    assert [fact.payload["raw"]["subIssues"]["nodes"][0]["number"] for fact in relations] == [
        9,
        9,
        10,
    ]
    assert any(fact.family == "commit-references" for fact in observations)
    assert any(fact.family == "commit-object" and fact.subject_key == f"commit:{sha}" for fact in observations)


@pytest.mark.asyncio
async def test_interrupted_refresh_resumes_only_unpublished_work(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"

    class InterruptedAPI(FakeAPI):
        def __init__(self) -> None:
            super().__init__()
            self.interrupted = False

        async def pull_review_threads(self, owner: str, repo: str, number: int) -> Any:
            if not self.interrupted:
                self.interrupted = True
                self._called("review_threads", "/graphql", {"number": number})
                raise RuntimeError("injected disconnect")
            return await super().pull_review_threads(owner, repo, number)

    api = InterruptedAPI()
    api.add_issue(7, pull=True)
    await _roots(database, api)
    clock = Clock(_T0 + timedelta(hours=1))
    maintainer = _maintainer(database, api, FakeGitStore(), clock)

    with pytest.raises(RuntimeError, match="injected disconnect"):
        await maintainer.refresh(pulls=[7])
    async with ObservationArchive(database, "acme/widgets") as archive:
        active = await archive.active_maintenance_job()
        assert active is not None
        job_id = active.id

    result = await maintainer.refresh(pulls=[7])

    assert result.job_id == job_id
    async with ObservationArchive(database, "acme/widgets") as archive:
        assert await archive.active_maintenance_job() is None
    threads = [fact async for fact in iter_observations(database, family="pull-review-threads")]
    assert len(threads) == 1
    assert api.request_count == 2


@pytest.mark.asyncio
async def test_failed_refresh_preserves_the_last_successful_observation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "facts.sqlite3"

    class FailingAPI(FakeAPI):
        fail = False

        async def issue_relations(self, owner: str, repo: str, number: int) -> Any:
            if self.fail:
                self._called("relations", "/graphql", {"number": number})
                raise RuntimeError("injected relation failure")
            return await super().issue_relations(owner, repo, number)

    api = FailingAPI()
    api.add_issue(8)
    api.issue_relation_sets[8] = _relations(8, 9)
    await _roots(database, api)
    clock = Clock(_T0 + timedelta(hours=1))
    maintainer = _maintainer(database, api, FakeGitStore(), clock)
    await maintainer.refresh(issues=[8])
    api.fail = True
    clock.current += timedelta(hours=1)

    with pytest.raises(RuntimeError, match="injected relation failure"):
        await maintainer.refresh(issues=[8])

    relations = [fact async for fact in iter_observations(database, family="issue-relations")]
    assert len(relations) == 1
    assert relations[0].observed_until == _T0 + timedelta(hours=1)
    async with ObservationArchive(database, "acme/widgets") as archive:
        active = await archive.active_maintenance_job()
        assert active is not None and active.completed_tasks == 0
    with sqlite3.connect(database) as connection:
        attempt = connection.execute(
            "SELECT attempts, last_attempt_from, last_attempt_until, last_error "
            "FROM maintenance_tasks WHERE job_id = ?",
            (active.id,),
        ).fetchone()
    assert attempt is not None
    assert attempt[0] == 1
    assert attempt[1] is not None and attempt[2] is not None
    assert attempt[3] == "RuntimeError: injected relation failure"


@pytest.mark.asyncio
async def test_two_unkeyed_refresh_invocations_at_same_time_are_distinct(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    api = FakeAPI()
    api.add_issue(8)
    api.issue_relation_sets[8] = _relations(8, 9)
    await _roots(database, api)
    maintainer = _maintainer(
        database,
        api,
        FakeGitStore(),
        Clock(_T0 + timedelta(hours=1)),
    )

    first = await maintainer.refresh(issues=[8])
    second = await maintainer.refresh(issues=[8])

    assert first.job_id != second.job_id
    observations = [fact async for fact in iter_observations(database, family="issue-relations")]
    assert len(observations) == 2


@pytest.mark.asyncio
async def test_backfill_freezes_all_reference_sources_and_replays_by_cursor(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    first_sha = "a" * 40
    second_sha = "b" * 40
    async with ObservationArchive(database, "acme/widgets") as archive:
        source = await _source(
            archive,
            "first",
            "pull-review-comments",
            {
                "value": [
                    {
                        "id": 101,
                        "commit_id": first_sha,
                        "original_commit_id": second_sha,
                    },
                ],
            },
        )
        cutoff = source.id
    clock = Clock(_T0 + timedelta(hours=1))
    git = FakeGitStore()
    maintainer = _maintainer(database, FakeAPI(), git, clock)

    result = await maintainer.backfill(idempotency_key="baseline-1")
    prefix = [fact async for fact in iter_observations(database)]
    split = prefix[len(prefix) // 2].id
    resumed = [fact for fact in prefix if fact.id <= split]
    resumed.extend([fact async for fact in iter_observations(database, after=split)])
    repeated = await maintainer.backfill(idempotency_key="baseline-1")

    assert repeated == result
    assert resumed == prefix
    assert git.retentions == [(first_sha, second_sha)]
    assert result.requests == 0
    objects = [fact for fact in prefix if fact.family == "commit-object"]
    scans = [fact for fact in prefix if fact.family == "commit-references"]
    assert len(scans) == 1 and scans[0].id > cutoff
    assert {fact.subject_key for fact in objects} == {
        f"commit:{first_sha}",
        f"commit:{second_sha}",
    }
    assert all(fact.schema_version == 2 for fact in objects)
    assert all(fact.maintenance_job_id == result.job_id for fact in objects)
    assert all(fact.payload["reference_scope"]["reference_count"] == 1 for fact in objects)
    assert all(fact.payload["reference_scope"]["observation_ids"] for fact in objects)


@pytest.mark.asyncio
async def test_backfill_closes_every_raw_source_family_without_api_calls(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    sha = "c" * 40
    payloads = {
        "pull-commits": {"value": [{"sha": sha}]},
        "pull-reviews": {"value": [{"id": 1, "commit_id": sha}]},
        "pull-review-comments": {"value": [{"id": 2, "commit_id": sha}]},
        "pull-review-threads": {"raw": {"nodes": [_thread("scan", sha, resolved=False)]}},
        "issue-timeline": {"value": [{"id": 3, "commit_id": sha}]},
        "issue-events": {"value": [{"id": 4, "commit_id": sha}]},
    }
    source_ids = []
    async with ObservationArchive(database, "acme/widgets") as archive:
        await archive.start_cycle(_T0)
        for index, (family, payload) in enumerate(payloads.items()):
            source = await _source(archive, str(index), family, payload)
            source_ids.append(source.id)
        duplicate = await _source(
            archive,
            "duplicate",
            "pull-reviews",
            payloads["pull-reviews"],
            resource_number=8,
        )
        source_ids.append(duplicate.id)
        empty = await _source(
            archive,
            "empty",
            "issue-events",
            {"value": []},
            resource_number=9,
        )
        source_ids.append(empty.id)
    api = FakeAPI()
    git = FakeGitStore()
    maintainer = _maintainer(database, api, git, Clock(_T0 + timedelta(hours=1)))

    result = await maintainer.backfill()

    assert result.requests == 0
    assert api.request_count == 0
    assert git.retentions == [(sha,)]
    facts = [fact async for fact in iter_observations(database)]
    scans = [fact for fact in facts if fact.family == "commit-references"]
    assert {fact.payload["source_observation_id"] for fact in scans} == set(source_ids)
    assert sum(not fact.payload["references"] for fact in scans) == 1
    commit = next(fact for fact in facts if fact.family == "commit-object")
    assert commit.payload["reference_scope"]["reference_count"] == len(payloads) + 1
    assert len(commit.payload["reference_scope"]["observation_ids"]) == len(payloads) + 1
    async with ObservationArchive(database, "acme/widgets") as archive:
        job = await archive.maintenance_job(result.job_id)
        assert job.scope["source_population"] == {
            "digest": job.scope["source_population"]["digest"],
            "digest_algorithm": "sha256-json-lines-v1",
            "empty_observations": 1,
            "observations": len(source_ids),
            "pending_scans": len(source_ids),
            "preexisting_scans": 0,
            "reference_edges": len(payloads) + 1,
        }
        assert await archive.discovery_checkpoint() is None
        assert (await archive.active_cycle()) is not None


@pytest.mark.asyncio
async def test_empty_source_is_scanned_but_empty_archive_has_no_work(tmp_path: Path) -> None:
    empty_database = tmp_path / "empty.sqlite3"
    empty_result = await _maintainer(
        empty_database,
        FakeAPI(),
        FakeGitStore(),
        Clock(_T0),
    ).backfill()
    assert empty_result.total_tasks == 0
    async with ObservationArchive(empty_database, "acme/widgets") as archive:
        empty_job = await archive.maintenance_job(empty_result.job_id)
        assert empty_job.scope["source_population"]["observations"] == 0

    source_database = tmp_path / "source.sqlite3"
    async with ObservationArchive(source_database, "acme/widgets") as archive:
        source = await _source(archive, "empty", "issue-timeline", {"value": []})
    source_result = await _maintainer(
        source_database,
        FakeAPI(),
        FakeGitStore(),
        Clock(_T0),
    ).backfill()

    assert source_result.total_tasks == 1
    scans = [
        fact
        async for fact in iter_observations(source_database, family="commit-references")
    ]
    assert len(scans) == 1
    assert scans[0].payload["source_observation_id"] == source.id
    assert scans[0].payload["references"] == []


@pytest.mark.asyncio
async def test_interrupted_reference_rebuild_resumes_before_git(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    shas = ("d" * 40, "e" * 40)
    async with ObservationArchive(database, "acme/widgets") as archive:
        for index, sha in enumerate(shas):
            await _source(
                archive,
                str(index),
                "pull-reviews",
                {"value": [{"id": index, "commit_id": sha}]},
            )
        cutoff = await archive.observation_cutoff()
    api = FakeAPI()
    git = FakeGitStore()
    maintainer = _maintainer(database, api, git, Clock(_T0 + timedelta(hours=1)))
    original = maintainer._syncer._structured_commits
    interrupted = False

    async def interrupt_after_first_source(archive, task, sources):
        nonlocal interrupted
        if task.kind == "commit-reference-scan-batch" and not interrupted:
            interrupted = True
            await original(archive, task, sources[:1])
            raise RuntimeError("injected reference rebuild failure")
        return await original(archive, task, sources)

    maintainer._syncer._structured_commits = interrupt_after_first_source
    with pytest.raises(RuntimeError, match="injected reference rebuild failure"):
        await maintainer.backfill()
    assert git.retentions == []
    maintainer._syncer._structured_commits = original

    result = await maintainer.backfill()

    assert api.request_count == 0
    assert git.retentions == [shas]
    scans = [fact async for fact in iter_observations(database, family="commit-references")]
    assert len(scans) == len(shas)
    assert all(fact.id > cutoff for fact in scans)
    with sqlite3.connect(database) as connection:
        attempts = connection.execute(
            "SELECT attempts FROM maintenance_tasks "
            "WHERE job_id = ? AND kind = 'commit-reference-scan-batch'",
            (result.job_id,),
        ).fetchone()
    assert attempts == (2,)


@pytest.mark.asyncio
async def test_later_raw_source_belongs_to_the_next_backfill(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    first_sha = "f" * 40
    second_sha = "1" * 40
    async with ObservationArchive(database, "acme/widgets") as archive:
        await _source(
            archive,
            "first",
            "issue-events",
            {"value": [{"id": 1, "commit_id": first_sha}]},
        )
    git = FakeGitStore()
    maintainer = _maintainer(database, FakeAPI(), git, Clock(_T0 + timedelta(hours=1)))

    first = await maintainer.backfill()
    async with ObservationArchive(database, "acme/widgets") as archive:
        later = await _source(
            archive,
            "second",
            "issue-events",
            {"value": [{"id": 2, "commit_id": second_sha}]},
        )
        first_job = await archive.maintenance_job(first.job_id)
        assert later.id > first_job.scope["source_observation_cutoff"]
    second = await maintainer.backfill()

    assert second.job_id != first.job_id
    assert git.retentions == [(first_sha,), (second_sha,)]


@pytest.mark.asyncio
async def test_explicit_commit_refresh_records_unavailable_then_retries_successfully(
    tmp_path: Path,
) -> None:
    database = tmp_path / "facts.sqlite3"
    sha = "c" * 40

    class AvailabilityGit(FakeGitStore):
        def __init__(self) -> None:
            super().__init__()
            self.available = False

        async def retain_commits(
            self,
            shas: list[str],
            **_: Any,
        ) -> dict[str, dict[str, Any]]:
            self.retentions.append(shas)
            return {
                value: {
                    "sha": value,
                    "status": "available" if self.available else "unavailable",
                    "attempts": [{"outcome": "available" if self.available else "unavailable"}],
                    "verification": {},
                }
                for value in shas
            }

    git = AvailabilityGit()
    clock = Clock(_T0 + timedelta(hours=1))
    maintainer = _maintainer(database, FakeAPI(), git, clock)

    first = await maintainer.refresh(commits=[sha])
    git.available = True
    clock.current += timedelta(hours=1)
    second = await maintainer.refresh(commits=[sha])

    assert first.job_id != second.job_id
    objects = [fact async for fact in iter_observations(database, family="commit-object")]
    assert [fact.coverage for fact in objects] == [Coverage.UNAVAILABLE, Coverage.COMPLETE]
    assert [fact.observed_until for fact in objects] == [
        _T0 + timedelta(hours=1),
        _T0 + timedelta(hours=2),
    ]


@pytest.mark.asyncio
async def test_interrupted_commit_backfill_reuses_its_frozen_job(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    sha = "d" * 40
    async with ObservationArchive(database, "acme/widgets") as archive:
        await _source(
            archive,
            "commit",
            "pull-review-comments",
            {"value": [{"id": 101, "commit_id": sha}]},
        )

    class InterruptedGit(FakeGitStore):
        interrupted = False

        async def retain_commits(self, shas: list[str], **kwargs: Any) -> dict[str, dict[str, Any]]:
            if not self.interrupted:
                self.interrupted = True
                raise RuntimeError("injected Git failure")
            return await super().retain_commits(shas, **kwargs)

    git = InterruptedGit()
    maintainer = _maintainer(
        database,
        FakeAPI(),
        git,
        Clock(_T0 + timedelta(hours=1)),
    )
    with pytest.raises(RuntimeError, match="injected Git failure"):
        await maintainer.backfill()
    async with ObservationArchive(database, "acme/widgets") as archive:
        active = await archive.active_maintenance_job()
        assert active is not None

    result = await maintainer.backfill()

    assert result.job_id == active.id
    objects = [fact async for fact in iter_observations(database, family="commit-object")]
    assert len(objects) == 1
    with sqlite3.connect(database) as connection:
        attempt = connection.execute(
            "SELECT attempts, completed_at, last_error FROM maintenance_tasks "
            "WHERE job_id = ? AND kind = 'commit-object-batch'",
            (result.job_id,),
        ).fetchone()
    assert attempt is not None
    assert attempt[0] == 2
    assert attempt[1] is not None
    assert attempt[2] is None


@pytest.mark.asyncio
async def test_backfill_retries_publication_after_git_check(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    sha = "2" * 40
    async with ObservationArchive(database, "acme/widgets") as archive:
        await _source(
            archive,
            "publish",
            "pull-commits",
            {"value": [{"sha": sha}]},
        )
    git = FakeGitStore()
    maintainer = _maintainer(
        database,
        FakeAPI(),
        git,
        Clock(_T0 + timedelta(hours=1)),
    )
    original = maintainer._syncer._publish
    failed = False

    async def fail_first_commit_publication(archive, task, operation, facts, **kwargs):
        nonlocal failed
        if operation == "commit-objects" and not failed:
            failed = True
            raise RuntimeError("injected commit publication failure")
        return await original(archive, task, operation, facts, **kwargs)

    maintainer._syncer._publish = fail_first_commit_publication
    with pytest.raises(RuntimeError, match="injected commit publication failure"):
        await maintainer.backfill()
    maintainer._syncer._publish = original

    result = await maintainer.backfill()

    assert result.completed_tasks == result.total_tasks
    assert git.retentions == [(sha,), (sha,)]
    objects = [fact async for fact in iter_observations(database, family="commit-object")]
    assert len(objects) == 1


@pytest.mark.asyncio
async def test_backfill_batches_commit_verification_and_publication(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    shas = [f"{number:040x}" for number in range(1, 258)]
    async with ObservationArchive(database, "acme/widgets") as archive:
        source = await _source(
            archive,
            "many",
            "pull-commits",
            {"value": [{"sha": sha} for sha in shas]},
        )
        await _scan(archive, "many", source)
    git = FakeGitStore()

    result = await _maintainer(
        database,
        FakeAPI(),
        git,
        Clock(_T0 + timedelta(hours=1)),
    ).backfill()

    assert result.total_tasks == 2
    assert list(map(len, git.retentions)) == [256, 1]
    scans = [fact async for fact in iter_observations(database, family="commit-references")]
    assert len(scans) == 1
    objects = [fact async for fact in iter_observations(database, family="commit-object")]
    assert len(objects) == len(shas)
    assert len({fact.batch_id for fact in objects}) == 2
    async with ObservationArchive(database, "acme/widgets") as archive:
        job = await archive.maintenance_job(result.job_id)
        assert job.scope["source_population"]["preexisting_scans"] == 1
        assert job.scope["source_population"]["pending_scans"] == 0


def _thread(identity: str, sha: str, *, resolved: bool) -> dict[str, Any]:
    return {
        "id": f"thread-{identity}",
        "isResolved": resolved,
        "path": "src/a.py",
        "comments": {
            "totalCount": 1,
            "nodes": [
                {
                    "id": f"comment-{identity}",
                    "commit": {"oid": sha},
                    "originalCommit": None,
                },
            ],
        },
    }


def _relations(number: int, member: int) -> dict[str, Any]:
    return {
        "issue": {"id": f"issue-{number}", "number": number},
        "parent": None,
        "subIssues": {
            "totalCount": 1,
            "nodes": [{"id": f"issue-{member}", "number": member}],
        },
        "blockedBy": {"totalCount": 0, "nodes": []},
        "blocking": {"totalCount": 0, "nodes": []},
    }
