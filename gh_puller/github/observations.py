"""持久化细粒度 GitHub 事实观测与可恢复的同步执行状态。

一条观测对应一次语义闭合的 API、Git 或派生读取，而不是单字段、HTTP 页或仓库级
快照。``observed_from`` 与 ``observed_until`` 给出真实读取窗口；同一事实后续观测
只追加，不覆盖历史。当前状态按读取窗口结束时刻选择，写入较晚的旧观测不会倒退
事实头。

同步 cycle 只承载发现游标、任务恢复和内部 checkpoint。事实一经闭合便独立发布，
无需等待 cycle 完成；checkpoint 仅在发现遍历和全部任务完成后推进。请求失败属于任务
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

from .v10 import FACT_SCHEMAS, GIT_LAYOUT_VERSION, SCHEMA, VERSION

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
    """A durable unit whose successful execution publishes at most one fact batch."""

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
class FactObservation:
    """One immutable fact observation in the global replay stream."""

    id: int
    batch_id: int
    publication_key: str
    batch_kind: str
    cycle_id: int | None
    task_id: int | None
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

    async def take_tasks(self, cycle_id: int, limit: int) -> tuple[SyncTask, ...]:
        """Claim pending tasks for the archive's single writer.

        Args:
            cycle_id: Active cycle whose tasks are requested.
            limit: Maximum number returned; each returned task increments attempts.

        Returns:
            Pending tasks ordered by durable identity.
        """
        if limit < 1:
            raise ValueError("task limit must be positive")
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _active_cycle_row(db, cycle_id)
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
    ) -> tuple[FactObservation, ...]:
        """Atomically append one or more closed observations.

        Args:
            publication_key: Stable idempotency key for this source operation.
            kind: ``sync``, ``import``, or explicit ``refresh`` publication.
            published_at: Actual durable publication time.
            facts: Distinct family/subject observations forming one atomic result.
            cycle_id: Owning active cycle for normal sync work.
            task_id: Optional pending task completed in the same transaction.

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
                await _verify_existing_batch(db, existing, kind, cycle_id, task_id, prepared)
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
            for _, payload_digest, _, raw in prepared:
                await _put_encoded_blob(db, payload_digest, raw)
            for fact, _, _, _ in prepared:
                if fact.source_digest is not None and not await _blob_exists(db, fact.source_digest):
                    raise ValueError(f"unknown source digest: {fact.source_digest}")
            cursor = await db.execute(
                """
                INSERT INTO fact_batches(
                    publication_key, kind, cycle_id, task_id, published_at, fact_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (publication_key, kind, cycle_id, task_id, published, len(prepared)),
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
                await _advance_head(db, int(cursor.lastrowid), fact.family, fact.subject_key)
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
                    ("schema_version", VERSION),
                    ("git_layout_version", GIT_LAYOUT_VERSION),
                    ("git_store", str(self.git_store)),
                    ("repository", self.repository),
                ),
            )
            await db.executemany(
                "INSERT INTO fact_schemas(family, version) VALUES (?, ?)",
                sorted(FACT_SCHEMAS.items()),
            )
            await db.commit()
            return
        metadata = {
            str(row["key"]): str(row["value"])
            for row in await _fetchall(db, "SELECT key, value FROM archive_meta")
        }
        if metadata.get("schema_version") != VERSION:
            raise ValueError("unsupported GitHub observation archive schema")
        if metadata.get("repository") != self.repository:
            raise ValueError("archive belongs to another GitHub repository")
        if metadata.get("git_layout_version") != GIT_LAYOUT_VERSION:
            raise ValueError("unsupported GitHub Git layout")
        if metadata.get("git_store") != str(self.git_store):
            raise ValueError("archive belongs to another Git object store")
        schemas = {
            str(row["family"]): int(row["version"])
            for row in await _fetchall(db, "SELECT family, version FROM fact_schemas")
        }
        if schemas != FACT_SCHEMAS:
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
        path: Version-ten SQLite archive.
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
        path: Version-ten SQLite archive.
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
        path: Version-ten SQLite archive.
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
        if metadata.get("schema_version") != VERSION:
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
    version = FACT_SCHEMAS.get(fact.family)
    if version is None or fact.schema_version != version:
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
    facts: tuple[tuple[FactDraft, str, str, bytes], ...],
) -> None:
    identity = (
        str(batch["kind"]),
        None if batch["cycle_id"] is None else int(batch["cycle_id"]),
        None if batch["task_id"] is None else int(batch["task_id"]),
        int(batch["fact_count"]),
    )
    if identity != (kind, cycle_id, task_id, len(facts)):
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


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


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
