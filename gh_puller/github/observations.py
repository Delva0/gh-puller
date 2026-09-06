"""持久化细粒度 GitHub 事实观测与可恢复的同步执行状态。

一条观测对应一次语义闭合的 API、Git 或派生读取，而不是单字段、HTTP 页或仓库级
快照。``observed_from`` 与 ``observed_until`` 给出真实读取窗口；同一事实后续观测
只追加，不覆盖历史。当前状态按读取窗口结束时刻选择，写入较晚的旧观测不会倒退
事实头。

同步 cycle 只承载发现游标、任务恢复和内部 checkpoint；maintenance job 独立承载
定向刷新与补采。事实一经闭合便独立发布，无需等待所属操作完成。请求失败属于任务
执行状态，不进入事实观测流。
"""

from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import aiosqlite

from .commit_references import (
    COMMIT_REFERENCE_SOURCE_FAMILIES,
    commit_reference_index_rows,
    commit_reference_provenance,
)
from .schema import ARCHIVE_SCHEMA_VERSION, FACT_SCHEMAS, GIT_LAYOUT_VERSION, SCHEMA

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection, Iterable

_CODEC = "zlib-json-v1"


class Coverage(StrEnum):
    """Describe the completeness conclusion of one source read."""

    COMPLETE = "complete"
    NULL = "null"
    PARTIAL = "partial"
    FORBIDDEN = "forbidden"
    UNAVAILABLE = "unavailable"


class Origin(StrEnum):
    """Identify how an observation obtained its payload."""

    API = "api"
    GIT = "git"
    DERIVED = "derived"
    IMPORT = "import"


@dataclass(frozen=True, slots=True)
class FactDraft:
    """A semantically complete observation ready for atomic publication."""

    family: str
    subject_key: str
    observed_from: datetime
    observed_until: datetime
    coverage: Coverage
    origin: Origin
    payload: dict[str, Any]
    resource_number: int | None = None
    source_digest: str | None = None
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class TaskDraft:
    """A durable unit whose successful execution publishes closed fact batches."""

    task_key: str
    kind: str
    subject_key: str
    payload: dict[str, Any]
    resource_number: int | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryItemDraft:
    """One raw Issue/PR summary observed in a repository catalog page."""

    number: int
    kind: str
    observed_from: datetime
    observed_until: datetime
    summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DiscoveryItem:
    """Durable transit evidence used to hydrate one selected Issue or PR."""

    cycle_id: int
    number: int
    kind: str
    observed_from: datetime
    observed_until: datetime
    summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SyncCycle:
    """Operational state for one resumable discovery-and-fetch cycle."""

    id: int
    started_at: datetime
    checkpoint_from: datetime | None
    completed_at: datetime | None
    status: str
    discovery_started: bool
    discovery_cursor: str | None
    discovery_complete: bool
    discovery_pages: int
    discovered_items: int
    request_count: int


@dataclass(frozen=True, slots=True)
class SyncTask:
    """One persisted source operation selected by discovery or another task."""

    id: int
    cycle_id: int
    task_key: str
    kind: str
    subject_key: str
    resource_number: int | None
    payload: dict[str, Any]
    completed_at: datetime | None
    attempts: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class MaintenanceJob:
    """One recoverable refresh or backfill scope independent of discovery."""

    id: int
    job_key: str
    kind: str
    requested_at: datetime
    completed_at: datetime | None
    status: str
    scope_digest: str
    scope: dict[str, Any]
    total_tasks: int
    completed_tasks: int
    request_count: int


@dataclass(frozen=True, slots=True)
class MaintenanceTask:
    """One durable work unit containing idempotently published source operations."""

    id: int
    job_id: int
    task_key: str
    kind: str
    subject_key: str
    resource_number: int | None
    payload: dict[str, Any]
    completed_at: datetime | None
    outcome: Coverage | None
    attempts: int
    last_attempt_from: datetime | None
    last_attempt_until: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class FactObservation:
    """One immutable fact observation in the global replay stream."""

    id: int
    batch_id: int
    publication_key: str
    batch_kind: str
    cycle_id: int | None
    task_id: int | None
    maintenance_job_id: int | None
    maintenance_task_id: int | None
    published_at: datetime
    ordinal: int
    family: str
    schema_version: int
    subject_key: str
    resource_number: int | None
    source_digest: str | None
    origin: Origin
    observed_from: datetime
    observed_until: datetime
    coverage: Coverage
    payload_digest: str
    payload: dict[str, Any]


class ObservationArchive:
    """单写者 SQLite 观测事实库。

    Args:
        path: 新格式 SQLite 文件路径；父目录按需创建。
        repository: 此数据库固定绑定的 GitHub ``owner/repo``。
        git_store: 配套 bare Git 对象库；None 使用 ``PATH.git``。
    """

    def __init__(
        self,
        path: Path,
        repository: str,
        git_store: Path | None = None,
    ) -> None:
        owner, separator, repo = repository.partition("/")
        if not separator or not owner or not repo or "/" in repo:
            raise ValueError("repository must be 'owner/repo'")
        self.path = Path(path)
        self.repository = repository
        self.git_store = (
            Path(f"{self.path}.git") if git_store is None else Path(git_store)
        ).resolve()
        self._db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        try:
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA foreign_keys = ON")
            await self._db.execute("PRAGMA journal_mode = WAL")
            await self._db.execute("PRAGMA synchronous = FULL")
            await self._db.execute("PRAGMA busy_timeout = 5000")
            await self._initialize()
        except BaseException:
            await self._db.close()
            self._db = None
            raise
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._connection.close()
        self._db = None

    async def start_cycle(self, started_at: datetime) -> SyncCycle:
        """Create a cycle or resume the sole active cycle.

        Args:
            started_at: Actual start of a new cycle. It is ignored when an active
                cycle is resumed and must not precede the committed checkpoint.

        Returns:
            The active cycle, including its durable discovery cursor.
        """
        db = self._connection
        row = await _fetchone(db, "SELECT * FROM sync_cycles WHERE status = 'active'")
        if row is not None:
            return _cycle(row)
        started = _iso(started_at)
        await db.execute("BEGIN IMMEDIATE")
        try:
            row = await _fetchone(db, "SELECT * FROM sync_cycles WHERE status = 'active'")
            if row is not None:
                await db.commit()
                return _cycle(row)
            checkpoint_row = await _fetchone(
                db,
                "SELECT value FROM archive_meta WHERE key = 'discovery_checkpoint'",
            )
            checkpoint = None if checkpoint_row is None else str(checkpoint_row["value"])
            if checkpoint is not None and started < checkpoint:
                raise ValueError("cycle start precedes the discovery checkpoint")
            cursor = await db.execute(
                """
                INSERT INTO sync_cycles(started_at, checkpoint_from, status)
                VALUES (?, ?, 'active')
                """,
                (started, checkpoint),
            )
            cycle_id = int(cursor.lastrowid)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        row = await _fetchone(db, "SELECT * FROM sync_cycles WHERE id = ?", (cycle_id,))
        if row is None:
            raise RuntimeError("failed to create sync cycle")
        return _cycle(row)

    async def active_cycle(self) -> SyncCycle | None:
        """Return the recoverable active cycle, if one exists."""
        row = await _fetchone(
            self._connection,
            "SELECT * FROM sync_cycles WHERE status = 'active'",
        )
        return None if row is None else _cycle(row)

    async def start_maintenance_job(
        self,
        job_key: str,
        kind: str,
        requested_at: datetime,
        scope: dict[str, Any],
        tasks: Collection[TaskDraft],
    ) -> MaintenanceJob:
        """Create or resume an immutable refresh or backfill scope.

        Args:
            job_key: Stable identity for retrying this exact invocation.
            kind: ``refresh`` or ``backfill``.
            requested_at: Actual first invocation time.
            scope: Frozen target set and field-family contract.
            tasks: Complete deterministic work population.

        Returns:
            The existing identical job or the newly persisted job.

        Raises:
            RuntimeError: Another maintenance job remains active.
            ValueError: The key or task definitions conflict with durable state.
        """
        if not job_key or kind not in {"backfill", "refresh"}:
            raise ValueError("invalid maintenance job identity")
        requested = _iso(requested_at)
        scope_raw = _json_bytes(scope)
        scope_digest = _digest(scope_raw)
        prepared = tuple(sorted((_prepare_task(task) for task in tasks), key=lambda item: item[0].task_key))
        keys = [item[0].task_key for item in prepared]
        if len(keys) != len(set(keys)):
            raise ValueError("maintenance task keys must be unique")
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            existing = await _fetchone(
                db,
                "SELECT * FROM maintenance_jobs WHERE job_key = ?",
                (job_key,),
            )
            if existing is not None:
                await _verify_maintenance_job(
                    db,
                    existing,
                    kind,
                    requested,
                    scope_digest,
                    prepared,
                )
                await db.commit()
                return await _maintenance_job_from_row(db, existing)
            active = await _fetchone(
                db,
                "SELECT id FROM maintenance_jobs WHERE status = 'active'",
            )
            if active is not None:
                raise RuntimeError(f"maintenance job {active['id']} must finish first")
            await _put_encoded_blob(db, scope_digest, scope_raw)
            status = "complete" if not prepared else "active"
            cursor = await db.execute(
                """
                INSERT INTO maintenance_jobs(
                    job_key, kind, requested_at, completed_at, status, scope_digest,
                    total_tasks, completed_tasks
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    job_key,
                    kind,
                    requested,
                    requested if not prepared else None,
                    status,
                    scope_digest,
                    len(prepared),
                ),
            )
            job_id = int(cursor.lastrowid)
            for task, input_digest, raw in prepared:
                await _put_encoded_blob(db, input_digest, raw)
                await db.execute(
                    """
                    INSERT INTO maintenance_tasks(
                        job_id, task_key, kind, subject_key, resource_number,
                        input_digest
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        task.task_key,
                        task.kind,
                        task.subject_key,
                        task.resource_number,
                        input_digest,
                    ),
                )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        row = await _fetchone(db, "SELECT * FROM maintenance_jobs WHERE id = ?", (job_id,))
        if row is None:
            raise RuntimeError("maintenance job disappeared")
        return await _maintenance_job_from_row(db, row)

    async def active_maintenance_job(self) -> MaintenanceJob | None:
        """Return the sole resumable maintenance job, if one exists."""
        row = await _fetchone(
            self._connection,
            "SELECT * FROM maintenance_jobs WHERE status = 'active'",
        )
        return None if row is None else await _maintenance_job_from_row(self._connection, row)

    async def maintenance_job(self, job_id: int) -> MaintenanceJob:
        """Return one maintenance job by durable identity.

        Args:
            job_id: Refresh or backfill job identity.

        Raises:
            KeyError: The job does not exist.
        """
        row = await _fetchone(
            self._connection,
            "SELECT * FROM maintenance_jobs WHERE id = ?",
            (job_id,),
        )
        if row is None:
            raise KeyError(job_id)
        return await _maintenance_job_from_row(self._connection, row)

    async def maintenance_job_by_key(self, job_key: str) -> MaintenanceJob | None:
        """Return one maintenance job by idempotency key.

        Args:
            job_key: Stable invocation identity.
        """
        row = await _fetchone(
            self._connection,
            "SELECT * FROM maintenance_jobs WHERE job_key = ?",
            (job_key,),
        )
        return None if row is None else await _maintenance_job_from_row(self._connection, row)

    async def take_maintenance_tasks(
        self,
        job_id: int,
        limit: int,
        started_at: datetime,
        *,
        kind: str | None = None,
    ) -> tuple[MaintenanceTask, ...]:
        """Claim a durable batch from an active maintenance job.

        Args:
            job_id: Active job owning the work.
            limit: Maximum tasks returned.
            started_at: Actual start of this execution attempt.
            kind: Optional task kind used to enforce a maintenance stage barrier.

        Returns:
            Pending tasks in durable order with incremented attempt counts.
        """
        if limit < 1:
            raise ValueError("task limit must be positive")
        started = _iso(started_at)
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _active_maintenance_job_row(db, job_id)
            rows = await _fetchall(
                db,
                """
                SELECT * FROM maintenance_tasks
                WHERE job_id = ? AND completed_at IS NULL
                      AND (? IS NULL OR kind = ?)
                ORDER BY id
                LIMIT ?
                """,
                (job_id, kind, kind, limit),
            )
            if rows:
                await db.executemany(
                    """
                    UPDATE maintenance_tasks
                    SET attempts = attempts + 1, last_attempt_from = ?,
                        last_attempt_until = NULL, last_error = NULL
                    WHERE id = ?
                    """,
                    ((started, int(row["id"])) for row in rows),
                )
                rows = [
                    await _required_row(
                        db,
                        "SELECT * FROM maintenance_tasks WHERE id = ?",
                        (int(row["id"]),),
                        "maintenance task disappeared",
                    )
                    for row in rows
                ]
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return tuple([await _maintenance_task_from_row(db, row) for row in rows])

    async def record_maintenance_task_error(
        self,
        task_id: int,
        observed_until: datetime,
        error: str,
    ) -> None:
        """Persist one retryable failed attempt without publishing a fact.

        Args:
            task_id: Pending maintenance task.
            observed_until: Actual end of the failed attempt.
            error: Concise exception type and message.
        """
        if not error:
            raise ValueError("task error cannot be empty")
        ended = _iso(observed_until)
        cursor = await self._connection.execute(
            """
            UPDATE maintenance_tasks
            SET last_attempt_until = ?, last_error = ?
            WHERE id = ? AND completed_at IS NULL
                AND last_attempt_from IS NOT NULL AND last_attempt_from <= ?
            """,
            (ended, error, task_id, ended),
        )
        if cursor.rowcount != 1:
            await self._connection.rollback()
            raise RuntimeError("maintenance task is not pending")
        await self._connection.commit()

    async def publication(
        self,
        publication_key: str,
    ) -> tuple[FactObservation, ...] | None:
        """Return an idempotent publication when it already exists.

        Args:
            publication_key: Stable source-operation identity.

        Returns:
            The original atomic batch, or None before publication.
        """
        row = await _fetchone(
            self._connection,
            "SELECT id FROM fact_batches WHERE publication_key = ?",
            (publication_key,),
        )
        return None if row is None else await _batch_facts(self._connection, int(row["id"]))

    async def current_fact(
        self,
        family: str,
        subject_key: str,
    ) -> FactObservation | None:
        """Return one fact's latest observation by actual read time.

        Args:
            family: Versioned semantic fact family.
            subject_key: Stable identity within that family.

        Returns:
            The current observation, or None when it has never been observed.
        """
        row = await _fetchone(
            self._connection,
            _CURRENT_FACT_QUERY,
            (family, subject_key),
        )
        return None if row is None else _fact(row)

    async def latest_complete_fact(
        self,
        family: str,
        subject_key: str,
    ) -> FactObservation | None:
        """Return the newest complete observation for one fact identity.

        Args:
            family: Versioned semantic fact family.
            subject_key: Stable identity within that family.

        Returns:
            The latest complete observation, even when a newer non-complete
            conclusion is current, or None when completeness was never observed.
        """
        row = await _fetchone(
            self._connection,
            _LATEST_COMPLETE_FACT_QUERY,
            (family, subject_key),
        )
        return None if row is None else _fact(row)

    async def observation_cutoff(self) -> int:
        """Return the greatest globally replayable observation identity."""
        row = await _fetchone(
            self._connection,
            "SELECT COALESCE(MAX(id), 0) AS cutoff FROM fact_observations",
        )
        return 0 if row is None else int(row["cutoff"])

    async def iter_structured_commit_sources(
        self,
        cutoff: int,
        observation_ids: Collection[int] | None = None,
    ) -> AsyncIterator[FactObservation]:
        """Read complete raw facts covered by structured commit extraction.

        Args:
            cutoff: Inclusive source-observation identity frozen by the caller.
            observation_ids: Optional exact subset within the frozen range.

        Yields:
            Contract-covered source facts ordered by observation identity.
        """
        if cutoff < 0:
            raise ValueError("source observation cutoff cannot be negative")
        selected = None if observation_ids is None else tuple(sorted(set(observation_ids)))
        if selected is not None and any(
            type(observation_id) is not int
            or observation_id < 1
            or observation_id > cutoff
            for observation_id in selected
        ):
            raise ValueError("source observations must belong to the frozen range")
        if selected == ():
            return
        families = tuple(sorted(COMMIT_REFERENCE_SOURCE_FAMILIES))
        family_placeholders = ",".join("?" for _ in families)
        if selected is None:
            query = f"""
                {_FACT_SELECT}
                WHERE o.id <= ? AND o.coverage = 'complete'
                      AND o.family IN ({family_placeholders})
                ORDER BY o.id
            """
            async with self._connection.execute(query, (cutoff, *families)) as cursor:
                async for row in cursor:
                    yield _fact(row)
            return
        rows: dict[int, aiosqlite.Row] = {}
        for offset in range(0, len(selected), 500):
            chunk = selected[offset : offset + 500]
            id_placeholders = ",".join("?" for _ in chunk)
            query = f"""
                {_FACT_SELECT}
                WHERE o.id <= ? AND o.coverage = 'complete'
                      AND o.family IN ({family_placeholders})
                      AND o.id IN ({id_placeholders})
            """
            for row in await _fetchall(
                self._connection,
                query,
                (cutoff, *families, *chunk),
            ):
                rows[int(row["id"])] = row
        for _, row in sorted(rows.items()):
            yield _fact(row)

    async def commit_reference_scans(
        self,
        source_cutoff: int,
    ) -> set[tuple[int, str, str, str]]:
        """Return valid derived scans attached to a frozen raw-source range.

        Args:
            source_cutoff: Inclusive source-observation identity. A derived fact may
                have been published later than this boundary.

        Returns:
            Source identity, family, payload digest, and reference-list digest for
            every valid scan, including scans whose reference list is empty.
        """
        if source_cutoff < 0:
            raise ValueError("source observation cutoff cannot be negative")
        scans = set()
        query = f"""
            {_FACT_SELECT}
            WHERE o.family = 'commit-references' AND o.coverage = 'complete'
            ORDER BY o.id
        """
        async with self._connection.execute(query) as cursor:
            async for row in cursor:
                signature = _commit_reference_scan_signature(_fact(row))
                if signature is not None and signature[0] <= source_cutoff:
                    scans.add(signature)
        return scans

    async def iter_commit_references_by_source(
        self,
        source_cutoff: int,
        shas: Collection[str],
    ) -> AsyncIterator[dict[str, Any]]:
        """Read references selected by their raw source boundary.

        Args:
            source_cutoff: Inclusive raw source-observation identity.
            shas: Exact commit targets required by one verification task.

        Yields:
            Source-distinct provenance from the latest valid scan per raw fact.
        """
        if source_cutoff < 0:
            raise ValueError("source observation cutoff cannot be negative")
        selected = tuple(sorted(set(shas)))
        if not selected:
            return
        rows: dict[int, aiosqlite.Row] = {}
        for offset in range(0, len(selected), 500):
            chunk = selected[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            candidates = await _fetchall(
                self._connection,
                f"""
                SELECT o.id AS observation_id, o.resource_number, o.source_digest,
                       p.codec, p.raw_size, p.payload
                FROM (
                    SELECT DISTINCT observation_id
                    FROM commit_reference_index
                    WHERE sha IN ({placeholders})
                ) AS selected
                JOIN fact_observations AS o ON o.id = selected.observation_id
                JOIN payload_blobs AS p ON p.digest = o.payload_digest
                WHERE o.family = 'commit-references' AND o.coverage = 'complete'
                """,  # noqa: S608 - placeholders are generated from exact SHA values.
                chunk,
            )
            rows.update((int(row["observation_id"]), row) for row in candidates)
        chosen: dict[tuple[int, str, str], tuple[dict[str, Any], ...]] = {}
        selected_set = set(selected)
        for observation_id, row in sorted(rows.items(), reverse=True):
            payload = _decode_payload(row)
            identity = _commit_reference_source_payload_identity(
                payload,
                _optional_text(row["source_digest"]),
            )
            if identity is None or identity[0] > source_cutoff or identity in chosen:
                continue
            try:
                references = commit_reference_provenance(
                    observation_id,
                    (
                        None
                        if row["resource_number"] is None
                        else int(row["resource_number"])
                    ),
                    payload,
                )
            except ValueError:
                continue
            chosen[identity] = tuple(
                reference for reference in references if reference["sha"] in selected_set
            )
        for _, references in sorted(chosen.items()):
            for reference in references:
                yield reference

    async def iter_commit_references(
        self,
        cutoff: int,
        shas: Collection[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Read indexed structured references within a stable replay cutoff.

        Args:
            cutoff: Inclusive global observation identity.
            shas: Optional exact commit targets; None reads the complete range.

        Yields:
            Source-distinct provenance records in source observation order.
        """
        if cutoff < 0:
            raise ValueError("observation cutoff cannot be negative")
        selected = None if shas is None else tuple(sorted(set(shas)))
        if selected == ():
            return
        selected_set = None if selected is None else set(selected)
        if selected is None or len(selected) > 5_000:
            async with self._connection.execute(
                """
                SELECT o.id AS observation_id, o.resource_number,
                       p.codec, p.raw_size, p.payload
                FROM fact_observations AS o
                JOIN payload_blobs AS p ON p.digest = o.payload_digest
                WHERE o.family = 'commit-references' AND o.coverage = 'complete'
                      AND o.id <= ?
                ORDER BY o.id
                """,
                (cutoff,),
            ) as cursor:
                async for row in cursor:
                    for value in _indexed_references(row, selected_set):
                        yield value
            return
        rows: dict[int, aiosqlite.Row] = {}
        for offset in range(0, len(selected), 500):
            chunk = selected[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            candidates = await _fetchall(
                self._connection,
                f"""
                SELECT o.id AS observation_id, o.resource_number,
                       p.codec, p.raw_size, p.payload
                FROM (
                    SELECT DISTINCT observation_id
                    FROM commit_reference_index
                    WHERE observation_id <= ? AND sha IN ({placeholders})
                ) AS selected
                JOIN fact_observations AS o ON o.id = selected.observation_id
                JOIN payload_blobs AS p ON p.digest = o.payload_digest
                """,  # noqa: S608
                (cutoff, *chunk),
            )
            rows.update((int(row["observation_id"]), row) for row in candidates)
        for _, row in sorted(rows.items()):
            for value in _indexed_references(row, selected_set):
                yield value

    async def checked_commits(self, cutoff: int) -> set[str]:
        """Return targets with a version-two reconstruction outcome.

        Args:
            cutoff: Inclusive global observation identity.
        """
        if cutoff < 0:
            raise ValueError("observation cutoff cannot be negative")
        rows = await _fetchall(
            self._connection,
            """
            SELECT DISTINCT subject_key
            FROM fact_observations
            WHERE family = 'commit-object' AND schema_version = 2 AND id <= ?
            """,
            (cutoff,),
        )
        return {str(row["subject_key"]).removeprefix("commit:") for row in rows}

    async def discovery_checkpoint(self) -> datetime | None:
        """Return the last safely completed discovery boundary."""
        row = await _fetchone(
            self._connection,
            "SELECT value FROM archive_meta WHERE key = 'discovery_checkpoint'",
        )
        return None if row is None else _time(str(row["value"]))

    async def discovery_item(
        self,
        cycle_id: int,
        number: int,
    ) -> DiscoveryItem | None:
        """Return the latest catalog summary seen for a cycle-local parent.

        Args:
            cycle_id: Cycle whose traversal produced the summary.
            number: Repository-local Issue/PR number.

        Returns:
            Durable page evidence, or None for a comment-signal-only parent.
        """
        row = await _fetchone(
            self._connection,
            """
            SELECT d.*, p.codec, p.raw_size, p.payload
            FROM discovery_items AS d
            JOIN payload_blobs AS p ON p.digest = d.summary_digest
            WHERE d.cycle_id = ? AND d.number = ?
            """,
            (cycle_id, number),
        )
        return None if row is None else _discovery_item(row)

    async def record_discovery_signals(
        self,
        cycle_id: int,
        issue_comments: Collection[int],
        pull_comments: Collection[int],
    ) -> None:
        """Persist repository comment feeds before catalog traversal starts.

        Args:
            cycle_id: Active cycle receiving the discovery evidence.
            issue_comments: Parents selected by conversation-comment changes.
            pull_comments: PRs selected by review-comment changes.
        """
        issue_numbers = set(issue_comments)
        pull_numbers = set(pull_comments)
        if any(number < 1 for number in issue_numbers | pull_numbers):
            raise ValueError("discovery signal numbers must be positive")
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _active_cycle_row(db, cycle_id)
            await db.executemany(
                """
                INSERT INTO discovery_signals(
                    cycle_id, number, issue_comments, pull_comments
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(cycle_id, number) DO UPDATE SET
                    issue_comments = MAX(issue_comments, excluded.issue_comments),
                    pull_comments = MAX(pull_comments, excluded.pull_comments)
                """,
                (
                    (
                        cycle_id,
                        number,
                        int(number in issue_numbers),
                        int(number in pull_numbers),
                    )
                    for number in sorted(issue_numbers | pull_numbers)
                ),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    async def discovery_signals(self, cycle_id: int, number: int) -> tuple[bool, bool]:
        """Return durable conversation and review-comment selection signals.

        Args:
            cycle_id: Cycle whose repository feeds were read.
            number: Repository-local Issue/PR number.

        Returns:
            ``(issue_comments, pull_comments)`` flags; absent evidence is false.
        """
        row = await _fetchone(
            self._connection,
            """
            SELECT issue_comments, pull_comments
            FROM discovery_signals
            WHERE cycle_id = ? AND number = ?
            """,
            (cycle_id, number),
        )
        if row is None:
            return False, False
        return bool(row["issue_comments"]), bool(row["pull_comments"])

    async def begin_discovery(self, cycle_id: int, initial_cursor: str) -> str | None:
        """Persist the first discovery request or return the resume cursor.

        Args:
            cycle_id: Active cycle receiving the traversal.
            initial_cursor: Opaque path or cursor for a traversal not yet started.

        Returns:
            The request cursor to execute next, or None after terminal completion.
        """
        if not initial_cursor:
            raise ValueError("initial discovery cursor cannot be empty")
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            row = await _active_cycle_row(db, cycle_id)
            if bool(row["discovery_complete"]):
                await db.commit()
                return None
            if not bool(row["discovery_started"]):
                await db.execute(
                    """
                    UPDATE sync_cycles
                    SET discovery_started = 1, discovery_cursor = ?
                    WHERE id = ?
                    """,
                    (initial_cursor, cycle_id),
                )
                result = initial_cursor
            else:
                result = _optional_text(row["discovery_cursor"])
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        else:
            return result

    async def save_discovery_page(
        self,
        cycle_id: int,
        request_cursor: str,
        next_cursor: str | None,
        items: Collection[DiscoveryItemDraft],
        tasks: Collection[TaskDraft] = (),
    ) -> SyncCycle:
        """Atomically retain one consumed discovery page and its selected tasks.

        Args:
            cycle_id: Active cycle receiving the page.
            request_cursor: Cursor used for this response; it must equal durable state.
            next_cursor: Cursor for the next page, or None for the terminal response.
            items: Raw catalog entries accepted from this response.
            tasks: Idempotent work selected from the page.

        Returns:
            Updated cycle state. A terminal page marks discovery complete.
        """
        if not request_cursor:
            raise ValueError("request cursor cannot be empty")
        if next_cursor == "":
            raise ValueError("next cursor cannot be empty")
        page = tuple(_prepare_discovery_item(item) for item in items)
        prepared = tuple(_prepare_task(task) for task in tasks)
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            row = await _active_cycle_row(db, cycle_id)
            if not bool(row["discovery_started"]) or row["discovery_cursor"] != request_cursor:
                raise RuntimeError("discovery cursor does not match durable state")
            for item, summary_digest, raw in page:
                await _put_encoded_blob(db, summary_digest, raw)
                await db.execute(
                    """
                    INSERT INTO discovery_items(
                        cycle_id, number, kind, observed_from, observed_until,
                        summary_digest
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cycle_id, number) DO UPDATE SET
                        kind = excluded.kind,
                        observed_from = excluded.observed_from,
                        observed_until = excluded.observed_until,
                        summary_digest = excluded.summary_digest
                    WHERE discovery_items.observed_until <= excluded.observed_until
                    """,
                    (
                        cycle_id,
                        item.number,
                        item.kind,
                        _iso(item.observed_from),
                        _iso(item.observed_until),
                        summary_digest,
                    ),
                )
            for task, input_digest, raw in prepared:
                await _put_encoded_blob(db, input_digest, raw)
                await _insert_task(db, cycle_id, task, input_digest)
            await db.execute(
                """
                UPDATE sync_cycles
                SET discovery_cursor = ?,
                    discovery_complete = ?,
                    discovery_pages = discovery_pages + 1,
                    discovered_items = discovered_items + ?
                WHERE id = ?
                """,
                (next_cursor, int(next_cursor is None), len(page), cycle_id),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        row = await _fetchone(db, "SELECT * FROM sync_cycles WHERE id = ?", (cycle_id,))
        if row is None:
            raise RuntimeError("sync cycle disappeared")
        return _cycle(row)

    async def enqueue_tasks(
        self,
        cycle_id: int,
        tasks: Collection[TaskDraft],
    ) -> tuple[SyncTask, ...]:
        """Add idempotent work to an active cycle.

        Args:
            cycle_id: Active cycle that owns the work.
            tasks: Stable task definitions; reusing a key with another definition fails.

        Returns:
            Persisted tasks in caller order.
        """
        prepared = tuple(_prepare_task(task) for task in tasks)
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _active_cycle_row(db, cycle_id)
            rows = []
            for task, input_digest, raw in prepared:
                await _put_encoded_blob(db, input_digest, raw)
                rows.append(await _insert_task(db, cycle_id, task, input_digest))
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return tuple([await _task_from_row(db, row) for row in rows])

    async def task_completed(self, cycle_id: int, task_key: str) -> bool:
        """Report whether one durable cycle task has completed.

        Args:
            cycle_id: Cycle that owns the task.
            task_key: Stable task identity within the cycle.

        Returns:
            True only when the task exists and has a completion timestamp.
        """
        row = await _fetchone(
            self._connection,
            """
            SELECT completed_at
            FROM sync_tasks
            WHERE cycle_id = ? AND task_key = ?
            """,
            (cycle_id, task_key),
        )
        return row is not None and row["completed_at"] is not None

    async def take_tasks(
        self,
        cycle_id: int,
        limit: int,
        *,
        kinds: Collection[str] | None = None,
    ) -> tuple[SyncTask, ...]:
        """Claim pending tasks for the archive's single writer.

        Args:
            cycle_id: Active cycle whose tasks are requested.
            limit: Maximum number returned; each returned task increments attempts.
            kinds: Optional task kinds forming one independent execution lane.

        Returns:
            Pending tasks ordered by durable identity.
        """
        if limit < 1:
            raise ValueError("task limit must be positive")
        selected = None if kinds is None else tuple(sorted(set(kinds)))
        if selected is not None and (
            not selected
            or any(not isinstance(kind, str) or not kind for kind in selected)
        ):
            raise ValueError("task kinds must be non-empty strings")
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _active_cycle_row(db, cycle_id)
            if selected is None:
                rows = await _fetchall(
                    db,
                    """
                    SELECT * FROM sync_tasks
                    WHERE cycle_id = ? AND completed_at IS NULL
                    ORDER BY id
                    LIMIT ?
                    """,
                    (cycle_id, limit),
                )
            else:
                placeholders = ",".join("?" for _ in selected)
                rows = await _fetchall(
                    db,
                    f"""
                    SELECT * FROM sync_tasks
                    WHERE cycle_id = ? AND completed_at IS NULL
                          AND kind IN ({placeholders})
                    ORDER BY id
                    LIMIT ?
                    """,  # noqa: S608 - values use bound parameters.
                    (cycle_id, *selected, limit),
                )
            if rows:
                await db.executemany(
                    "UPDATE sync_tasks SET attempts = attempts + 1 WHERE id = ?",
                    ((int(row["id"]),) for row in rows),
                )
                updated = []
                for row in rows:
                    current = await _fetchone(
                        db,
                        "SELECT * FROM sync_tasks WHERE id = ?",
                        (int(row["id"]),),
                    )
                    if current is None:
                        raise RuntimeError("sync task disappeared")
                    updated.append(current)
                rows = updated
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return tuple([await _task_from_row(db, row) for row in rows])

    async def record_task_error(self, task_id: int, error: str) -> None:
        """Retain a retryable execution failure without publishing a fact.

        Args:
            task_id: Pending task that failed.
            error: Concise exception type and message for operators.
        """
        if not error:
            raise ValueError("task error cannot be empty")
        cursor = await self._connection.execute(
            "UPDATE sync_tasks SET last_error = ? WHERE id = ? AND completed_at IS NULL",
            (error, task_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("task is not pending")
        await self._connection.commit()

    async def complete_task(self, task_id: int, completed_at: datetime) -> SyncTask:
        """Close a no-op task without creating a fact observation.

        Args:
            task_id: Pending task whose source read requires no publication.
            completed_at: Actual time the no-op decision became durable.

        Returns:
            The completed task; repeating the call is idempotent.
        """
        completed = _iso(completed_at)
        db = self._connection
        row = await _fetchone(db, "SELECT * FROM sync_tasks WHERE id = ?", (task_id,))
        if row is None:
            raise RuntimeError("unknown sync task")
        if row["completed_at"] is None:
            await _active_cycle_row(db, int(row["cycle_id"]))
            await db.execute(
                "UPDATE sync_tasks SET completed_at = ?, last_error = NULL WHERE id = ?",
                (completed, task_id),
            )
            await db.commit()
            row = await _fetchone(db, "SELECT * FROM sync_tasks WHERE id = ?", (task_id,))
        if row is None:
            raise RuntimeError("sync task disappeared")
        return await _task_from_row(db, row)

    async def publish(
        self,
        publication_key: str,
        kind: str,
        published_at: datetime,
        facts: Collection[FactDraft],
        *,
        cycle_id: int | None = None,
        task_id: int | None = None,
        maintenance_task_id: int | None = None,
    ) -> tuple[FactObservation, ...]:
        """Atomically append one or more closed observations.

        Args:
            publication_key: Stable idempotency key for this source operation.
            kind: ``sync``, ``import``, or maintenance ``refresh`` publication.
            published_at: Actual durable publication time.
            facts: Distinct family/subject observations forming one atomic result.
            cycle_id: Owning active cycle for normal sync work.
            task_id: Optional pending task completed in the same transaction.
            maintenance_task_id: Optional maintenance task owning this publication.

        Returns:
            Immutable replay rows. Repeating the same publication returns the originals.

        Raises:
            ValueError: The publication or observation contract is invalid.
            RuntimeError: Durable state conflicts with the supplied idempotency key.
        """
        if not publication_key:
            raise ValueError("publication key cannot be empty")
        if kind not in {"sync", "import", "refresh"}:
            raise ValueError("invalid publication kind")
        if task_id is not None and cycle_id is None:
            raise ValueError("task publication requires its cycle")
        if task_id is not None and maintenance_task_id is not None:
            raise ValueError("publication cannot complete two task types")
        if maintenance_task_id is not None and cycle_id is not None:
            raise ValueError("maintenance publication cannot belong to a sync cycle")
        if maintenance_task_id is not None and kind != "refresh":
            raise ValueError("maintenance task requires a maintenance publication")
        published = _iso(published_at)
        prepared = tuple(_prepare_fact(fact) for fact in facts)
        if not prepared:
            raise ValueError("fact publication cannot be empty")
        identities = [(item[0].family, item[0].subject_key) for item in prepared]
        if len(identities) != len(set(identities)):
            raise ValueError("fact publication contains duplicate subjects")
        if any(item[2] > published for item in prepared):
            raise ValueError("fact cannot be published before its observation closes")

        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            existing = await _fetchone(
                db,
                "SELECT * FROM fact_batches WHERE publication_key = ?",
                (publication_key,),
            )
            if existing is not None:
                await _verify_existing_batch(
                    db,
                    existing,
                    kind,
                    cycle_id,
                    task_id,
                    maintenance_task_id,
                    prepared,
                )
                await db.commit()
                return await _batch_facts(db, int(existing["id"]))
            if cycle_id is not None:
                await _active_cycle_row(db, cycle_id)
            if task_id is not None:
                task = await _fetchone(db, "SELECT * FROM sync_tasks WHERE id = ?", (task_id,))
                if (
                    task is None
                    or int(task["cycle_id"]) != cycle_id
                    or task["completed_at"] is not None
                ):
                    raise RuntimeError("fact publication requires its pending task")
            maintenance_task = None
            if maintenance_task_id is not None:
                maintenance_task = await _fetchone(
                    db,
                    """
                    SELECT t.*, j.kind AS job_kind, j.status AS job_status
                    FROM maintenance_tasks AS t
                    JOIN maintenance_jobs AS j ON j.id = t.job_id
                    WHERE t.id = ?
                    """,
                    (maintenance_task_id,),
                )
                if (
                    maintenance_task is None
                    or maintenance_task["completed_at"] is not None
                    or maintenance_task["job_status"] != "active"
                ):
                    raise RuntimeError("fact publication requires its pending maintenance task")
            for _, payload_digest, _, raw in prepared:
                await _put_encoded_blob(db, payload_digest, raw)
            for fact, _, _, _ in prepared:
                if fact.source_digest is not None and not await _blob_exists(db, fact.source_digest):
                    raise ValueError(f"unknown source digest: {fact.source_digest}")
            cursor = await db.execute(
                """
                INSERT INTO fact_batches(
                    publication_key, kind, cycle_id, task_id, maintenance_task_id,
                    published_at, fact_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication_key,
                    kind,
                    cycle_id,
                    task_id,
                    maintenance_task_id,
                    published,
                    len(prepared),
                ),
            )
            batch_id = int(cursor.lastrowid)
            for ordinal, (fact, payload_digest, _, _) in enumerate(prepared):
                cursor = await db.execute(
                    """
                    INSERT INTO fact_observations(
                        batch_id, ordinal, family, schema_version, subject_key,
                        resource_number, source_digest, origin, observed_from,
                        observed_until, coverage, payload_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        ordinal,
                        fact.family,
                        fact.schema_version,
                        fact.subject_key,
                        fact.resource_number,
                        fact.source_digest,
                        fact.origin.value,
                        _iso(fact.observed_from),
                        _iso(fact.observed_until),
                        fact.coverage.value,
                        payload_digest,
                    ),
                )
                observation_id = int(cursor.lastrowid)
                if fact.family == "commit-references" and fact.coverage is Coverage.COMPLETE:
                    await db.executemany(
                        """
                        INSERT INTO commit_reference_index(
                            observation_id, ordinal, sha
                        ) VALUES (?, ?, ?)
                        """,
                        commit_reference_index_rows(
                            observation_id,
                            fact.resource_number,
                            fact.payload,
                        ),
                    )
                await _advance_head(db, observation_id, fact.family, fact.subject_key)
            if task_id is not None:
                await db.execute(
                    "UPDATE sync_tasks SET completed_at = ?, last_error = NULL WHERE id = ?",
                    (published, task_id),
                )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return await _batch_facts(db, batch_id)

    async def complete_maintenance_task(
        self,
        task_id: int,
        completed_at: datetime,
        outcome: Coverage,
    ) -> MaintenanceTask:
        """Complete a maintenance workflow after all its facts are durable.

        Args:
            task_id: Pending maintenance task.
            completed_at: Actual workflow completion time.
            outcome: Aggregate source coverage for this declared task.

        Returns:
            The completed task; repeating the call is idempotent.
        """
        if not isinstance(outcome, Coverage):
            raise TypeError("invalid maintenance task outcome")
        completed = _iso(completed_at)
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            row = await _fetchone(db, "SELECT * FROM maintenance_tasks WHERE id = ?", (task_id,))
            if row is None:
                raise RuntimeError("unknown maintenance task")
            if row["completed_at"] is None:
                await _active_maintenance_job_row(db, int(row["job_id"]))
                started = row["last_attempt_from"]
                if started is None or str(started) > completed:
                    raise RuntimeError("maintenance task has no active attempt")
                await db.execute(
                    """
                    UPDATE maintenance_tasks
                    SET completed_at = ?, outcome = ?, last_attempt_until = ?,
                        last_error = NULL
                    WHERE id = ?
                    """,
                    (completed, outcome.value, completed, task_id),
                )
                await _advance_maintenance_job(db, int(row["job_id"]), completed)
            elif row["outcome"] != outcome.value:
                raise RuntimeError("maintenance task already has another outcome")
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        current = await _required_row(
            db,
            "SELECT * FROM maintenance_tasks WHERE id = ?",
            (task_id,),
            "maintenance task disappeared",
        )
        return await _maintenance_task_from_row(db, current)

    async def add_maintenance_requests(self, job_id: int, count: int) -> None:
        """Add attempted HTTP operations to an active maintenance job.

        Args:
            job_id: Active refresh or backfill job.
            count: Non-negative number of newly attempted operations.
        """
        if count < 0:
            raise ValueError("request count cannot be negative")
        cursor = await self._connection.execute(
            """
            UPDATE maintenance_jobs
            SET request_count = request_count + ?
            WHERE id = ?
            """,
            (count, job_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("request accounting requires a maintenance job")
        await self._connection.commit()

    async def add_requests(self, cycle_id: int, count: int) -> None:
        """Add attempted HTTP operations to an active cycle.

        Args:
            cycle_id: Active cycle receiving the accounting.
            count: Non-negative number of newly attempted operations.
        """
        if count < 0:
            raise ValueError("request count cannot be negative")
        cursor = await self._connection.execute(
            """
            UPDATE sync_cycles
            SET request_count = request_count + ?
            WHERE id = ? AND status = 'active'
            """,
            (count, cycle_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("request accounting requires an active cycle")
        await self._connection.commit()

    async def complete_cycle(self, cycle_id: int, completed_at: datetime) -> SyncCycle:
        """Close a cycle and advance discovery only after all work is durable.

        Args:
            cycle_id: Active cycle with terminal discovery and no pending tasks.
            completed_at: Actual completion time of the operational cycle.

        Returns:
            The committed cycle. Repeating the call is idempotent.
        """
        completed = _iso(completed_at)
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            row = await _fetchone(db, "SELECT * FROM sync_cycles WHERE id = ?", (cycle_id,))
            if row is None:
                raise RuntimeError("unknown sync cycle")
            if row["status"] == "complete":
                await db.commit()
                return _cycle(row)
            if not bool(row["discovery_complete"]):
                raise RuntimeError("discovery must finish before the cycle")
            pending = await _fetchone(
                db,
                """
                SELECT COUNT(*) AS count
                FROM sync_tasks
                WHERE cycle_id = ? AND completed_at IS NULL
                """,
                (cycle_id,),
            )
            if pending is None or int(pending["count"]) != 0:
                raise RuntimeError("all sync tasks must finish before the cycle")
            started = str(row["started_at"])
            if completed < started:
                raise ValueError("cycle cannot complete before it starts")
            await db.execute(
                "UPDATE sync_cycles SET completed_at = ?, status = 'complete' WHERE id = ?",
                (completed, cycle_id),
            )
            await db.execute(
                """
                INSERT INTO archive_meta(key, value)
                VALUES ('discovery_checkpoint', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                WHERE archive_meta.value <= excluded.value
                """,
                (started,),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        row = await _fetchone(db, "SELECT * FROM sync_cycles WHERE id = ?", (cycle_id,))
        if row is None:
            raise RuntimeError("sync cycle disappeared")
        return _cycle(row)

    async def _initialize(self) -> None:
        db = self._connection
        table = await _fetchone(
            db,
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'archive_meta'",
        )
        if table is None:
            await db.executescript(SCHEMA)
            await db.executemany(
                "INSERT INTO archive_meta(key, value) VALUES (?, ?)",
                (
                    ("schema_version", ARCHIVE_SCHEMA_VERSION),
                    ("git_layout_version", GIT_LAYOUT_VERSION),
                    ("git_store", str(self.git_store)),
                    ("repository", self.repository),
                ),
            )
            await db.executemany(
                "INSERT INTO fact_schemas(family, version) VALUES (?, ?)",
                _fact_schema_rows(),
            )
            await db.commit()
            return
        metadata = {
            str(row["key"]): str(row["value"])
            for row in await _fetchall(db, "SELECT key, value FROM archive_meta")
        }
        if metadata.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
            raise ValueError("unsupported GitHub observation archive schema")
        if metadata.get("repository") != self.repository:
            raise ValueError("archive belongs to another GitHub repository")
        if metadata.get("git_layout_version") != GIT_LAYOUT_VERSION:
            raise ValueError("unsupported GitHub Git layout")
        if metadata.get("git_store") != str(self.git_store):
            raise ValueError("archive belongs to another Git object store")
        schemas = {
            (str(row["family"]), int(row["version"]))
            for row in await _fetchall(db, "SELECT family, version FROM fact_schemas")
        }
        if schemas != set(_fact_schema_rows()):
            raise ValueError("archive fact schema registry is inconsistent")

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("observation archive is not open")
        return self._db


async def iter_observations(
    path: Path,
    *,
    after: int = 0,
    family: str | None = None,
    subject_key: str | None = None,
) -> AsyncIterator[FactObservation]:
    """Replay immutable observations in publication order.

    Args:
        path: SQLite observation archive.
        after: Exclusive global observation cursor.
        family: Optional exact fact-family filter.
        subject_key: Optional exact subject filter.

    Yields:
        Matching observations ordered by stable integer identity.
    """
    if after < 0:
        raise ValueError("observation cursor cannot be negative")
    parameters = (after, family, family, subject_key, subject_key)
    async with _reader(path) as db, db.execute(_REPLAY_QUERY, parameters) as cursor:
        async for row in cursor:
            yield _fact(row)


async def iter_current_facts(
    path: Path,
    *,
    family: str | None = None,
    subject_key: str | None = None,
) -> AsyncIterator[FactObservation]:
    """Read the latest actual observation for each selected semantic fact.

    Args:
        path: SQLite observation archive.
        family: Optional exact fact-family filter.
        subject_key: Optional exact subject filter.

    Yields:
        Current facts ordered by family and subject identity.
    """
    parameters = (family, family, subject_key, subject_key)
    async with _reader(path) as db, db.execute(_CURRENT_QUERY, parameters) as cursor:
        async for row in cursor:
            yield _fact(row)


async def iter_facts_as_of(
    path: Path,
    at: datetime,
    *,
    family: str | None = None,
    subject_key: str | None = None,
) -> AsyncIterator[FactObservation]:
    """Read each fact's latest observation closed no later than a timestamp.

    Args:
        path: SQLite observation archive.
        at: Inclusive UTC-normalized observation boundary.
        family: Optional exact fact-family filter.
        subject_key: Optional exact subject filter.

    Yields:
        One independently timed observation per matching family and subject.
    """
    parameters = (_iso(at), family, family, subject_key, subject_key)
    async with _reader(path) as db, db.execute(_AS_OF_QUERY, parameters) as cursor:
        async for row in cursor:
            yield _fact(row)


class _Reader:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> aiosqlite.Connection:
        self.db = await aiosqlite.connect(f"file:{self.path}?mode=ro", uri=True)
        self.db.row_factory = aiosqlite.Row
        metadata = {
            str(row["key"]): str(row["value"])
            for row in await _fetchall(self.db, "SELECT key, value FROM archive_meta")
        }
        if metadata.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
            await self.db.close()
            self.db = None
            raise ValueError("unsupported GitHub observation archive schema")
        return self.db

    async def __aexit__(self, *_: object) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None


def _reader(path: Path) -> _Reader:
    return _Reader(path)


_FACT_SELECT = """
SELECT o.*
FROM fact_records AS o
"""

_REPLAY_QUERY = _FACT_SELECT + """
WHERE o.id > ?
  AND (? IS NULL OR o.family = ?)
  AND (? IS NULL OR o.subject_key = ?)
ORDER BY o.id
"""

_CURRENT_QUERY = _FACT_SELECT + """
JOIN fact_heads AS h ON h.observation_id = o.id
WHERE (? IS NULL OR o.family = ?)
  AND (? IS NULL OR o.subject_key = ?)
ORDER BY o.family, o.subject_key
"""

_CURRENT_FACT_QUERY = _FACT_SELECT + """
JOIN fact_heads AS h ON h.observation_id = o.id
WHERE o.family = ? AND o.subject_key = ?
"""

_LATEST_COMPLETE_FACT_QUERY = _FACT_SELECT + """
WHERE o.family = ? AND o.subject_key = ? AND o.coverage = 'complete'
ORDER BY o.observed_until DESC, o.observed_from DESC, o.id DESC
LIMIT 1
"""

_AS_OF_QUERY = """
SELECT * FROM (
    SELECT selected.*, ROW_NUMBER() OVER (
        PARTITION BY selected.family, selected.subject_key
        ORDER BY selected.observed_until DESC,
                 selected.observed_from DESC,
                 selected.id DESC
    ) AS fact_rank
    FROM (
        SELECT o.*
        FROM fact_records AS o
        WHERE o.observed_until <= ?
          AND (? IS NULL OR o.family = ?)
          AND (? IS NULL OR o.subject_key = ?)
    ) AS selected
)
WHERE fact_rank = 1
ORDER BY family, subject_key
"""


def _prepare_fact(fact: FactDraft) -> tuple[FactDraft, str, str, bytes]:
    versions = FACT_SCHEMAS.get(fact.family)
    if versions is None or fact.schema_version not in versions:
        raise ValueError(f"unsupported fact schema: {fact.family}@{fact.schema_version}")
    if not fact.subject_key:
        raise ValueError("fact subject cannot be empty")
    if fact.resource_number is not None and fact.resource_number < 1:
        raise ValueError("resource number must be positive")
    if not isinstance(fact.coverage, Coverage) or not isinstance(fact.origin, Origin):
        raise TypeError("invalid fact coverage or origin")
    observed_from = _iso(fact.observed_from)
    observed_until = _iso(fact.observed_until)
    if observed_from > observed_until:
        raise ValueError("fact observation window is reversed")
    if fact.source_digest is not None and not _is_digest(fact.source_digest):
        raise ValueError("invalid source digest")
    raw = _json_bytes(fact.payload)
    return fact, _digest(raw), observed_until, raw


def _prepare_task(task: TaskDraft) -> tuple[TaskDraft, str, bytes]:
    if not task.task_key or not task.kind or not task.subject_key:
        raise ValueError("task identity cannot be empty")
    if task.resource_number is not None and task.resource_number < 1:
        raise ValueError("resource number must be positive")
    raw = _json_bytes(task.payload)
    return task, _digest(raw), raw


def _prepare_discovery_item(
    item: DiscoveryItemDraft,
) -> tuple[DiscoveryItemDraft, str, bytes]:
    if item.number < 1 or item.kind not in {"issue", "pull"}:
        raise ValueError("invalid discovery item identity")
    observed_from = _iso(item.observed_from)
    observed_until = _iso(item.observed_until)
    if observed_from > observed_until:
        raise ValueError("discovery observation window is reversed")
    if item.summary.get("number") != item.number:
        raise ValueError("discovery summary has another number")
    raw = _json_bytes(item.summary)
    return item, _digest(raw), raw


async def _insert_task(
    db: aiosqlite.Connection,
    cycle_id: int,
    task: TaskDraft,
    input_digest: str,
) -> aiosqlite.Row:
    row = await _fetchone(
        db,
        "SELECT * FROM sync_tasks WHERE cycle_id = ? AND task_key = ?",
        (cycle_id, task.task_key),
    )
    definition = (task.kind, task.subject_key, task.resource_number, input_digest)
    if row is not None:
        stored = (
            str(row["kind"]),
            str(row["subject_key"]),
            None if row["resource_number"] is None else int(row["resource_number"]),
            str(row["input_digest"]),
        )
        if stored != definition:
            raise ValueError(f"task key has another definition: {task.task_key}")
        return row
    cursor = await db.execute(
        """
        INSERT INTO sync_tasks(
            cycle_id, task_key, kind, subject_key, resource_number, input_digest
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (cycle_id, task.task_key, *definition),
    )
    row = await _fetchone(db, "SELECT * FROM sync_tasks WHERE id = ?", (cursor.lastrowid,))
    if row is None:
        raise RuntimeError("failed to create sync task")
    return row


async def _verify_existing_batch(
    db: aiosqlite.Connection,
    batch: aiosqlite.Row,
    kind: str,
    cycle_id: int | None,
    task_id: int | None,
    maintenance_task_id: int | None,
    facts: tuple[tuple[FactDraft, str, str, bytes], ...],
) -> None:
    identity = (
        str(batch["kind"]),
        None if batch["cycle_id"] is None else int(batch["cycle_id"]),
        None if batch["task_id"] is None else int(batch["task_id"]),
        (
            None
            if batch["maintenance_task_id"] is None
            else int(batch["maintenance_task_id"])
        ),
        int(batch["fact_count"]),
    )
    if identity != (kind, cycle_id, task_id, maintenance_task_id, len(facts)):
        raise RuntimeError("publication key belongs to another operation")
    rows = await _fetchall(
        db,
        "SELECT * FROM fact_observations WHERE batch_id = ? ORDER BY ordinal",
        (int(batch["id"]),),
    )
    signatures = tuple(_stored_signature(row) for row in rows)
    supplied = tuple(_draft_signature(fact, digest) for fact, digest, _, _ in facts)
    if signatures != supplied:
        raise RuntimeError("publication key belongs to different facts")


def _stored_signature(row: aiosqlite.Row) -> tuple[object, ...]:
    return (
        str(row["family"]),
        int(row["schema_version"]),
        str(row["subject_key"]),
        None if row["resource_number"] is None else int(row["resource_number"]),
        _optional_text(row["source_digest"]),
        str(row["origin"]),
        str(row["observed_from"]),
        str(row["observed_until"]),
        str(row["coverage"]),
        str(row["payload_digest"]),
    )


def _draft_signature(fact: FactDraft, payload_digest: str) -> tuple[object, ...]:
    return (
        fact.family,
        fact.schema_version,
        fact.subject_key,
        fact.resource_number,
        fact.source_digest,
        fact.origin.value,
        _iso(fact.observed_from),
        _iso(fact.observed_until),
        fact.coverage.value,
        payload_digest,
    )


async def _advance_head(
    db: aiosqlite.Connection,
    observation_id: int,
    family: str,
    subject_key: str,
) -> None:
    current = await _fetchone(
        db,
        """
        SELECT o.id, o.observed_from, o.observed_until
        FROM fact_heads AS h
        JOIN fact_observations AS o ON o.id = h.observation_id
        WHERE h.family = ? AND h.subject_key = ?
        """,
        (family, subject_key),
    )
    candidate = await _fetchone(
        db,
        "SELECT id, observed_from, observed_until FROM fact_observations WHERE id = ?",
        (observation_id,),
    )
    if candidate is None:
        raise RuntimeError("fact observation disappeared")
    if current is not None and _observation_order(current) >= _observation_order(candidate):
        return
    await db.execute(
        """
        INSERT INTO fact_heads(family, subject_key, observation_id)
        VALUES (?, ?, ?)
        ON CONFLICT(family, subject_key)
        DO UPDATE SET observation_id = excluded.observation_id
        """,
        (family, subject_key, observation_id),
    )


async def _advance_maintenance_job(
    db: aiosqlite.Connection,
    job_id: int,
    completed_at: str,
) -> None:
    progress = await _fetchone(
        db,
        """
        SELECT COUNT(*) AS total,
               COUNT(CASE WHEN completed_at IS NOT NULL THEN 1 END) AS completed
        FROM maintenance_tasks WHERE job_id = ?
        """,
        (job_id,),
    )
    if progress is None:
        raise RuntimeError("maintenance job progress disappeared")
    total = int(progress["total"])
    completed = int(progress["completed"])
    await db.execute(
        """
        UPDATE maintenance_jobs
        SET completed_tasks = ?, status = ?, completed_at = ?
        WHERE id = ? AND status = 'active'
        """,
        (
            completed,
            "complete" if completed == total else "active",
            completed_at if completed == total else None,
            job_id,
        ),
    )


def _observation_order(row: aiosqlite.Row) -> tuple[str, str, int]:
    return str(row["observed_until"]), str(row["observed_from"]), int(row["id"])


async def _batch_facts(
    db: aiosqlite.Connection,
    batch_id: int,
) -> tuple[FactObservation, ...]:
    rows = await _fetchall(
        db,
        f"{_FACT_SELECT} WHERE o.batch_id = ? ORDER BY o.ordinal",
        (batch_id,),
    )
    return tuple(_fact(row) for row in rows)


async def _task_from_row(db: aiosqlite.Connection, row: aiosqlite.Row) -> SyncTask:
    payload_row = await _fetchone(
        db,
        "SELECT codec, raw_size, payload FROM payload_blobs WHERE digest = ?",
        (str(row["input_digest"]),),
    )
    if payload_row is None:
        raise RuntimeError("sync task input is missing")
    return SyncTask(
        id=int(row["id"]),
        cycle_id=int(row["cycle_id"]),
        task_key=str(row["task_key"]),
        kind=str(row["kind"]),
        subject_key=str(row["subject_key"]),
        resource_number=(
            None if row["resource_number"] is None else int(row["resource_number"])
        ),
        payload=_decode_payload(payload_row),
        completed_at=(
            None if row["completed_at"] is None else _time(str(row["completed_at"]))
        ),
        attempts=int(row["attempts"]),
        last_error=_optional_text(row["last_error"]),
    )


async def _maintenance_job_from_row(
    db: aiosqlite.Connection,
    row: aiosqlite.Row,
) -> MaintenanceJob:
    payload_row = await _required_row(
        db,
        "SELECT codec, raw_size, payload FROM payload_blobs WHERE digest = ?",
        (str(row["scope_digest"]),),
        "maintenance scope is missing",
    )
    return MaintenanceJob(
        id=int(row["id"]),
        job_key=str(row["job_key"]),
        kind=str(row["kind"]),
        requested_at=_time(str(row["requested_at"])),
        completed_at=(
            None if row["completed_at"] is None else _time(str(row["completed_at"]))
        ),
        status=str(row["status"]),
        scope_digest=str(row["scope_digest"]),
        scope=_decode_payload(payload_row),
        total_tasks=int(row["total_tasks"]),
        completed_tasks=int(row["completed_tasks"]),
        request_count=int(row["request_count"]),
    )


async def _maintenance_task_from_row(
    db: aiosqlite.Connection,
    row: aiosqlite.Row,
) -> MaintenanceTask:
    payload_row = await _required_row(
        db,
        "SELECT codec, raw_size, payload FROM payload_blobs WHERE digest = ?",
        (str(row["input_digest"]),),
        "maintenance task input is missing",
    )
    return MaintenanceTask(
        id=int(row["id"]),
        job_id=int(row["job_id"]),
        task_key=str(row["task_key"]),
        kind=str(row["kind"]),
        subject_key=str(row["subject_key"]),
        resource_number=(
            None if row["resource_number"] is None else int(row["resource_number"])
        ),
        payload=_decode_payload(payload_row),
        completed_at=(
            None if row["completed_at"] is None else _time(str(row["completed_at"]))
        ),
        outcome=(None if row["outcome"] is None else Coverage(str(row["outcome"]))),
        attempts=int(row["attempts"]),
        last_attempt_from=(
            None
            if row["last_attempt_from"] is None
            else _time(str(row["last_attempt_from"]))
        ),
        last_attempt_until=(
            None
            if row["last_attempt_until"] is None
            else _time(str(row["last_attempt_until"]))
        ),
        last_error=_optional_text(row["last_error"]),
    )


def _cycle(row: aiosqlite.Row) -> SyncCycle:
    return SyncCycle(
        id=int(row["id"]),
        started_at=_time(str(row["started_at"])),
        checkpoint_from=(
            None if row["checkpoint_from"] is None else _time(str(row["checkpoint_from"]))
        ),
        completed_at=(
            None if row["completed_at"] is None else _time(str(row["completed_at"]))
        ),
        status=str(row["status"]),
        discovery_started=bool(row["discovery_started"]),
        discovery_cursor=_optional_text(row["discovery_cursor"]),
        discovery_complete=bool(row["discovery_complete"]),
        discovery_pages=int(row["discovery_pages"]),
        discovered_items=int(row["discovered_items"]),
        request_count=int(row["request_count"]),
    )


def _discovery_item(row: aiosqlite.Row) -> DiscoveryItem:
    return DiscoveryItem(
        cycle_id=int(row["cycle_id"]),
        number=int(row["number"]),
        kind=str(row["kind"]),
        observed_from=_time(str(row["observed_from"])),
        observed_until=_time(str(row["observed_until"])),
        summary=_decode_payload(row),
    )


def _fact(row: aiosqlite.Row) -> FactObservation:
    return FactObservation(
        id=int(row["id"]),
        batch_id=int(row["batch_id"]),
        publication_key=str(row["publication_key"]),
        batch_kind=str(row["batch_kind"]),
        cycle_id=None if row["cycle_id"] is None else int(row["cycle_id"]),
        task_id=None if row["task_id"] is None else int(row["task_id"]),
        maintenance_job_id=(
            None
            if row["maintenance_job_id"] is None
            else int(row["maintenance_job_id"])
        ),
        maintenance_task_id=(
            None
            if row["maintenance_task_id"] is None
            else int(row["maintenance_task_id"])
        ),
        published_at=_time(str(row["published_at"])),
        ordinal=int(row["ordinal"]),
        family=str(row["family"]),
        schema_version=int(row["schema_version"]),
        subject_key=str(row["subject_key"]),
        resource_number=(
            None if row["resource_number"] is None else int(row["resource_number"])
        ),
        source_digest=_optional_text(row["source_digest"]),
        origin=Origin(str(row["origin"])),
        observed_from=_time(str(row["observed_from"])),
        observed_until=_time(str(row["observed_until"])),
        coverage=Coverage(str(row["coverage"])),
        payload_digest=str(row["payload_digest"]),
        payload=_decode_payload(row),
    )


def _decode_payload(row: aiosqlite.Row) -> dict[str, Any]:
    codec = str(row["codec"])
    if codec != _CODEC:
        raise ValueError(f"unsupported payload codec: {codec}")
    raw = zlib.decompress(bytes(row["payload"]))
    if len(raw) != int(row["raw_size"]):
        raise ValueError("payload size does not match its index")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("fact payload is not an object")
    return value


async def _put_encoded_blob(
    db: aiosqlite.Connection,
    digest: str,
    raw: bytes,
) -> None:
    await db.execute(
        """
        INSERT OR IGNORE INTO payload_blobs(digest, codec, raw_size, payload)
        VALUES (?, ?, ?, ?)
        """,
        (digest, _CODEC, len(raw), zlib.compress(raw, level=9)),
    )


async def _blob_exists(db: aiosqlite.Connection, digest: str) -> bool:
    return (
        await _fetchone(db, "SELECT 1 FROM payload_blobs WHERE digest = ?", (digest,))
        is not None
    )


async def _active_cycle_row(db: aiosqlite.Connection, cycle_id: int) -> aiosqlite.Row:
    row = await _fetchone(db, "SELECT * FROM sync_cycles WHERE id = ?", (cycle_id,))
    if row is None or row["status"] != "active":
        raise RuntimeError("operation requires an active sync cycle")
    return row


async def _active_maintenance_job_row(
    db: aiosqlite.Connection,
    job_id: int,
) -> aiosqlite.Row:
    row = await _fetchone(db, "SELECT * FROM maintenance_jobs WHERE id = ?", (job_id,))
    if row is None or row["status"] != "active":
        raise RuntimeError("operation requires an active maintenance job")
    return row


async def _verify_maintenance_job(
    db: aiosqlite.Connection,
    job: aiosqlite.Row,
    kind: str,
    requested_at: str,
    scope_digest: str,
    tasks: tuple[tuple[TaskDraft, str, bytes], ...],
) -> None:
    identity = (
        str(job["kind"]),
        str(job["requested_at"]),
        str(job["scope_digest"]),
        int(job["total_tasks"]),
    )
    if identity != (kind, requested_at, scope_digest, len(tasks)):
        raise ValueError("maintenance job key has another definition")
    rows = await _fetchall(
        db,
        "SELECT * FROM maintenance_tasks WHERE job_id = ? ORDER BY task_key",
        (int(job["id"]),),
    )
    stored = tuple(
        (
            str(row["task_key"]),
            str(row["kind"]),
            str(row["subject_key"]),
            None if row["resource_number"] is None else int(row["resource_number"]),
            str(row["input_digest"]),
        )
        for row in rows
    )
    supplied = tuple(
        (
            task.task_key,
            task.kind,
            task.subject_key,
            task.resource_number,
            input_digest,
        )
        for task, input_digest, _ in tasks
    )
    if stored != supplied:
        raise ValueError("maintenance job key has other tasks")


def _fact_schema_rows() -> list[tuple[str, int]]:
    return sorted(
        (family, version)
        for family, versions in FACT_SCHEMAS.items()
        for version in versions
    )


def _commit_reference_scan_signature(
    fact: FactObservation,
) -> tuple[int, str, str, str] | None:
    identity = _commit_reference_source_identity(fact)
    if identity is None:
        return None
    try:
        commit_reference_provenance(
            fact.id,
            fact.resource_number,
            fact.payload,
        )
    except ValueError:
        return None
    return (*identity, _value_digest(fact.payload.get("references")))


def _commit_reference_source_identity(
    fact: FactObservation,
) -> tuple[int, str, str] | None:
    return _commit_reference_source_payload_identity(fact.payload, fact.source_digest)


def _commit_reference_source_payload_identity(
    payload: dict[str, Any],
    source_digest: str | None,
) -> tuple[int, str, str] | None:
    observation_id = payload.get("source_observation_id")
    family = payload.get("source_family")
    payload_digest = payload.get("source_payload_digest")
    if (
        type(observation_id) is not int
        or observation_id < 1
        or family not in COMMIT_REFERENCE_SOURCE_FAMILIES
        or not isinstance(payload_digest, str)
        or not _is_digest(payload_digest)
        or source_digest != payload_digest
    ):
        return None
    return observation_id, str(family), payload_digest


def _indexed_references(
    row: aiosqlite.Row,
    selected: Collection[str] | None,
) -> tuple[dict[str, Any], ...]:
    observation_id = int(row["observation_id"])
    values = commit_reference_provenance(
        observation_id,
        None if row["resource_number"] is None else int(row["resource_number"]),
        _decode_payload(row),
    )
    if selected is None:
        return values
    return tuple(value for value in values if value["sha"] in selected)


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _value_digest(value: object) -> str:
    raw = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return _digest(raw)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored timestamp has no timezone")
    return parsed.astimezone(UTC)


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


async def _fetchone(
    db: aiosqlite.Connection,
    query: str,
    parameters: Iterable[object] = (),
) -> aiosqlite.Row | None:
    async with db.execute(query, tuple(parameters)) as cursor:
        return await cursor.fetchone()


async def _fetchall(
    db: aiosqlite.Connection,
    query: str,
    parameters: Iterable[object] = (),
) -> list[aiosqlite.Row]:
    async with db.execute(query, tuple(parameters)) as cursor:
        return await cursor.fetchall()


async def _required_row(
    db: aiosqlite.Connection,
    query: str,
    parameters: Iterable[object],
    error: str,
) -> aiosqlite.Row:
    row = await _fetchone(db, query, parameters)
    if row is None:
        raise RuntimeError(error)
    return row
