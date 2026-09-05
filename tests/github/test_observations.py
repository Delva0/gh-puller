"""Test fine-grained fact observations and recoverable sync-cycle state."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from gh_puller.github.observations import (
    Coverage,
    FactDraft,
    ObservationArchive,
    Origin,
    TaskDraft,
    iter_current_facts,
    iter_facts_as_of,
    iter_observations,
)

if TYPE_CHECKING:
    from pathlib import Path

_REPOSITORY = "acme/widgets"
_T0 = datetime(2026, 9, 5, 12, tzinfo=UTC)


def _fact(
    family: str,
    subject: str,
    observed_at: datetime,
    payload: dict[str, object],
    *,
    coverage: Coverage = Coverage.COMPLETE,
    origin: Origin = Origin.API,
    resource_number: int | None = 7,
) -> FactDraft:
    return FactDraft(
        family=family,
        subject_key=subject,
        resource_number=resource_number,
        observed_from=observed_at - timedelta(seconds=2),
        observed_until=observed_at,
        coverage=coverage,
        origin=origin,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_fact_publication_is_atomic_idempotent_and_content_addressed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "facts.sqlite3"
    facts = (
        _fact("issue", "issue:7", _T0, {"number": 7, "title": "first"}),
        _fact(
            "issue-relations",
            "issue:7",
            _T0,
            {"members": []},
            coverage=Coverage.NULL,
        ),
    )

    async with ObservationArchive(database, _REPOSITORY) as archive:
        first = await archive.publish(
            "import:7",
            "import",
            _T0 + timedelta(seconds=1),
            facts,
        )
        repeated = await archive.publish(
            "import:7",
            "import",
            _T0 + timedelta(minutes=1),
            facts,
        )
        assert repeated == first

        changed = _fact("issue", "issue:7", _T0, {"number": 7, "title": "other"})
        with pytest.raises(RuntimeError, match="different facts"):
            await archive.publish(
                "import:7",
                "import",
                _T0 + timedelta(minutes=1),
                (changed, facts[1]),
            )

        with pytest.raises(ValueError, match="unknown source digest"):
            await archive.publish(
                "import:bad-source",
                "import",
                _T0 + timedelta(minutes=1),
                (
                    replace(
                        facts[0],
                        source_digest="a" * 64,
                        payload={"rolled": "back"},
                    ),
                ),
            )

    observations = [fact async for fact in iter_observations(database)]
    assert observations == list(first)
    assert [fact.id for fact in observations] == [1, 2]
    assert [fact.ordinal for fact in observations] == [0, 1]
    assert observations[0].batch_id == observations[1].batch_id
    assert observations[0].payload == {"number": 7, "title": "first"}
    assert observations[1].coverage is Coverage.NULL

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM fact_batches").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM payload_blobs").fetchone() == (2,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.asyncio
async def test_heads_and_as_of_reads_follow_observation_time_not_publication_order(
    tmp_path: Path,
) -> None:
    database = tmp_path / "facts.sqlite3"
    old_at = _T0 + timedelta(hours=1)
    new_at = _T0 + timedelta(hours=2)

    async with ObservationArchive(database, _REPOSITORY) as archive:
        newer = await archive.publish(
            "refresh:new",
            "refresh",
            new_at + timedelta(minutes=1),
            (_fact("issue", "issue:7", new_at, {"title": "new"}),),
        )
        older = await archive.publish(
            "refresh:old-arrived-late",
            "refresh",
            new_at + timedelta(minutes=2),
            (_fact("issue", "issue:7", old_at, {"title": "old"}),),
        )
        await archive.publish(
            "refresh:relations",
            "refresh",
            new_at + timedelta(minutes=3),
            (
                _fact(
                    "issue-relations",
                    "issue:7",
                    new_at,
                    {"blocking": []},
                ),
            ),
        )

    replay = [fact async for fact in iter_observations(database, after=newer[0].id)]
    assert [fact.id for fact in replay] == [older[0].id, older[0].id + 1]

    current = [fact async for fact in iter_current_facts(database)]
    assert [(fact.family, fact.payload) for fact in current] == [
        ("issue", {"title": "new"}),
        ("issue-relations", {"blocking": []}),
    ]
    old = [
        fact
        async for fact in iter_facts_as_of(
            database,
            old_at,
            family="issue",
        )
    ]
    assert [fact.payload for fact in old] == [{"title": "old"}]
    new = [fact async for fact in iter_facts_as_of(database, new_at)]
    assert [(fact.family, fact.payload) for fact in new] == [
        ("issue", {"title": "new"}),
        ("issue-relations", {"blocking": []}),
    ]


@pytest.mark.asyncio
async def test_cycle_resumes_pages_and_tasks_without_hiding_published_facts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "facts.sqlite3"
    task_7 = TaskDraft("issue:7", "issue", "issue:7", {"number": 7}, 7)
    task_8 = TaskDraft("issue:8", "issue", "issue:8", {"number": 8}, 8)

    async with ObservationArchive(database, _REPOSITORY) as archive:
        cycle = await archive.start_cycle(_T0)
        assert cycle.checkpoint_from is None
        assert await archive.begin_discovery(cycle.id, "catalog?page=1") == "catalog?page=1"
        cycle = await archive.save_discovery_page(
            cycle.id,
            "catalog?page=1",
            "catalog?page=2",
            100,
            (task_7,),
        )
        assert (cycle.discovery_pages, cycle.discovered_items) == (1, 100)
        claimed = await archive.take_tasks(cycle.id, 1)
        assert (claimed[0].task_key, claimed[0].attempts) == ("issue:7", 1)
        await archive.record_task_error(claimed[0].id, "GitHubAPIError: retry")

    async with ObservationArchive(database, _REPOSITORY) as archive:
        resumed = await archive.start_cycle(_T0 + timedelta(hours=4))
        assert resumed.id == cycle.id
        assert resumed.started_at == _T0
        assert await archive.begin_discovery(resumed.id, "ignored") == "catalog?page=2"
        resumed = await archive.save_discovery_page(
            resumed.id,
            "catalog?page=2",
            None,
            23,
            (task_8,),
        )
        assert resumed.discovery_complete
        assert (resumed.discovery_pages, resumed.discovered_items) == (2, 123)

        claimed = await archive.take_tasks(resumed.id, 2)
        assert [(task.task_key, task.attempts) for task in claimed] == [
            ("issue:7", 2),
            ("issue:8", 1),
        ]
        published = await archive.publish(
            "sync:1:issue:7",
            "sync",
            _T0 + timedelta(minutes=2),
            (_fact("issue", "issue:7", _T0 + timedelta(minutes=1), {"number": 7}),),
            cycle_id=resumed.id,
            task_id=claimed[0].id,
        )
        assert published[0].task_id == claimed[0].id
        assert await archive.discovery_checkpoint() is None
        assert [fact.payload async for fact in iter_current_facts(database)] == [{"number": 7}]

        with pytest.raises(RuntimeError, match="all sync tasks"):
            await archive.complete_cycle(resumed.id, _T0 + timedelta(minutes=3))
        await archive.complete_task(claimed[1].id, _T0 + timedelta(minutes=3))
        completed = await archive.complete_cycle(
            resumed.id,
            _T0 + timedelta(minutes=4),
        )
        assert completed.status == "complete"
        assert await archive.discovery_checkpoint() == _T0

        next_cycle = await archive.start_cycle(_T0 + timedelta(hours=1))
        assert next_cycle.id != completed.id
        assert next_cycle.checkpoint_from == _T0


@pytest.mark.asyncio
async def test_discovery_page_and_task_definition_are_transactional(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    first = TaskDraft("issue:7", "issue", "issue:7", {"number": 7}, 7)
    conflicting = TaskDraft("issue:7", "pull", "pull:7", {"number": 7}, 7)

    async with ObservationArchive(database, _REPOSITORY) as archive:
        cycle = await archive.start_cycle(_T0)
        await archive.begin_discovery(cycle.id, "page:1")
        await archive.enqueue_tasks(cycle.id, (first,))
        with pytest.raises(ValueError, match="another definition"):
            await archive.save_discovery_page(
                cycle.id,
                "page:1",
                "page:2",
                100,
                (conflicting,),
            )
        current = await archive.active_cycle()
        assert current is not None
        assert (current.discovery_cursor, current.discovery_pages, current.discovered_items) == (
            "page:1",
            0,
            0,
        )


@pytest.mark.asyncio
async def test_old_schema_and_naive_times_are_rejected(tmp_path: Path) -> None:
    database = tmp_path / "old.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE archive_meta(key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO archive_meta VALUES ('schema_version', '9')")

    with pytest.raises(ValueError, match="unsupported GitHub observation archive schema"):
        async with ObservationArchive(database, _REPOSITORY):
            pass

    with pytest.raises(ValueError, match="timezone"):
        async with ObservationArchive(tmp_path / "new.sqlite3", _REPOSITORY) as archive:
            await archive.start_cycle(_T0.replace(tzinfo=None))
