"""Test atomic supplemental fact publication and replay."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from gh_puller.github import iter_facts
from gh_puller.github.store import (
    CatalogItem,
    FactTaskSpec,
    SQLiteArchive,
    StagedFact,
    StagedResource,
    StagedTaskFact,
    StoredHead,
    json_digest,
)

if TYPE_CHECKING:
    from pathlib import Path

_REPOSITORY = "acme/widgets"
_T0 = "2026-09-05T12:00:00Z"
_T1 = "2026-09-05T13:00:00Z"


def _resource(observed_at: str, facts: tuple[StagedFact, ...]) -> StagedResource:
    summary = {
        "created_at": _T0,
        "id": 70,
        "number": 7,
        "title": "fact publication",
        "updated_at": _T0,
    }
    bundle = {
        "issue": summary,
        "kind": "issue",
        "number": 7,
        "schema_version": 7,
    }
    return StagedResource(
        head=StoredHead(
            number=7,
            github_id=70,
            kind="issue",
            created_at=_T0,
            updated_at=_T0,
            summary_digest=json_digest(summary),
            bundle_digest=json_digest(bundle),
            present=True,
            missing_since=None,
        ),
        observed_at=observed_at,
        summary=summary,
        bundle=bundle,
        facts=facts,
    )


def _fact(
    kind: str,
    subject: str,
    observed_at: str,
    status: str,
    payload: dict[str, object],
) -> StagedFact:
    return StagedFact(
        fact_kind=kind,
        schema_version=1,
        subject_key=subject,
        resource_number=7,
        source_digest=None,
        observed_from=observed_at,
        observed_until=observed_at,
        status=status,
        payload=payload,
    )


async def _stage(
    archive: SQLiteArchive,
    target: str,
    resource: StagedResource,
) -> tuple[int, int]:
    run = await archive.start_run(target, target)
    await archive.start_pass(run.id, "closing", target, "full")
    await archive.prepare_pass(run.id, (), 1)
    await archive.stage_catalog_page(
        run.id,
        (
            CatalogItem(
                number=7,
                github_id=70,
                kind="issue",
                created_at=_T0,
                updated_at=_T0,
                summary=resource.summary or {},
            ),
        ),
        None,
    )
    task = (await archive.pending_catalog_tasks(run.id))[0]
    assert await archive.stage_task(run.id, 7, task.summary_digest, resource)
    await archive.finish_pass(run.id, target)
    changed, _, _ = await archive.finalize(run.id, target)
    return run.id, changed


@pytest.mark.asyncio
async def test_fact_publication_is_atomic_replayable_and_keeps_success(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite3"
    complete = _fact(
        "issue-relations",
        "issue:7",
        _T0,
        "complete",
        {"blocking": [], "source": "graphql"},
    )
    refs = _fact(
        "commit-references",
        "issue:7",
        _T0,
        "null",
        {"references": None},
    )

    async with SQLiteArchive(database, _REPOSITORY) as archive:
        run = await archive.start_run(_T0, _T0)
        await archive.start_pass(run.id, "closing", _T0, "full")
        await archive.prepare_pass(run.id, (), 1)
        await archive.stage_catalog_page(
            run.id,
            (
                CatalogItem(7, 70, "issue", _T0, _T0, _resource(_T0, ()).summary or {}),
            ),
            None,
        )
        task = (await archive.pending_catalog_tasks(run.id))[0]
        assert await archive.stage_task(
            run.id,
            7,
            task.summary_digest,
            _resource(_T0, (complete, refs)),
        )
        assert [fact async for fact in iter_facts(database)] == []
        await archive.finish_pass(run.id, _T0)
        await archive.finalize(run.id, _T0)

        failed = _fact(
            "issue-relations",
            "issue:7",
            _T1,
            "failed",
            {"error": "temporary"},
        )
        _, changed = await _stage(archive, _T1, _resource(_T1, (failed,)))
        assert changed == 0

    facts = [fact async for fact in iter_facts(database)]
    assert [fact.id for fact in facts] == [1, 2, 3]
    assert [fact.ordinal for fact in facts] == [0, 1, 0]
    assert facts[0].batch_id == facts[1].batch_id != facts[2].batch_id
    assert [fact.status for fact in facts] == ["complete", "null", "failed"]
    assert facts[0].payload == {"blocking": [], "source": "graphql"}
    assert [fact async for fact in iter_facts(database, after=facts[1].id)] == facts[2:]

    with sqlite3.connect(database) as connection:
        current = connection.execute(
            "SELECT version_id, status FROM current_facts WHERE fact_kind = 'issue-relations'",
        ).fetchone()
        latest = connection.execute(
            "SELECT version_id, status FROM latest_fact_attempts "
            "WHERE fact_kind = 'issue-relations'",
        ).fetchone()
    assert current == (1, "complete")
    assert latest == (3, "failed")


@pytest.mark.asyncio
async def test_fact_job_is_idempotent_resumable_and_publishes_batches(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite3"
    scope = {
        "fact_sets": {"issue-relations": 1},
        "resource_version_cutoff": 42,
        "targets": [7, 8],
    }
    specs = (
        FactTaskSpec("issue-relations", "issue:7", 7, None),
        FactTaskSpec("issue-relations", "issue:8", 8, None),
    )

    async with SQLiteArchive(database, _REPOSITORY) as archive:
        job = await archive.start_fact_job("baseline:42", "backfill", _T0, _T0, scope, specs)
        assert job.status == "pending"
        assert (job.completed_tasks, job.total_tasks) == (0, 2)
        assert await archive.start_fact_job(
            "baseline:42",
            "backfill",
            _T0,
            _T1,
            scope,
            reversed(specs),
        ) == job
        with pytest.raises(ValueError, match="different scope"):
            await archive.start_fact_job(
                "baseline:42",
                "backfill",
                _T1,
                _T1,
                scope,
                specs,
            )
        with pytest.raises(RuntimeError, match="must finish"):
            await archive.start_fact_job("refresh:1", "refresh", _T1, _T1, scope, specs)

        task = (await archive.take_fact_tasks(job.id, 1))[0]
        assert task.attempts == 1
        await archive.record_fact_task_error(job.id, task.id, "retry")
        task = (await archive.take_fact_tasks(job.id, 1))[0]
        assert task.attempts == 2
        first = StagedTaskFact(
            task.id,
            _fact("issue-relations", "issue:7", _T0, "complete", {"blocking": []}),
        )
        job = await archive.publish_fact_batch(job.id, _T0, (first,))
        assert (job.status, job.completed_tasks) == ("pending", 1)
        assert await archive.publish_fact_batch(job.id, _T0, (first,)) == job

        task = (await archive.take_fact_tasks(job.id, 1))[0]
        second = StagedTaskFact(
            task.id,
            StagedFact(
                fact_kind="issue-relations",
                schema_version=1,
                subject_key="issue:8",
                resource_number=8,
                source_digest=None,
                observed_from=_T1,
                observed_until=_T1,
                status="unavailable",
                payload={"reason": "not-found"},
            ),
        )
        job = await archive.publish_fact_batch(job.id, _T1, (second,))
        assert (job.status, job.completed_tasks, job.completed_at) == ("complete", 2, _T1)
        assert (await archive.publish_fact_batch(job.id, _T0, (second,))).completed_at == _T1
        assert await archive.take_fact_tasks(job.id) == []

    facts = [fact async for fact in iter_facts(database)]
    assert [(fact.id, fact.batch_kind, fact.job_id) for fact in facts] == [
        (1, "backfill", job.id),
        (2, "backfill", job.id),
    ]
    assert all(fact.task_id is not None for fact in facts)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM fact_batches").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM current_facts").fetchone()[0] == 1
