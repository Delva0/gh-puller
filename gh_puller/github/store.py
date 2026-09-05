"""持久化 GitHub 语义事实、拉取水位与离线版本流。

SQLite 保存 API 事实与执行状态；PR 代码对象属于配套 Git 对象库，见
``gh_puller.github``。未完成 run 可恢复但不进入公共读取流。与 bundle 摘要配对的
HTTP cache 可丢弃且不进入版本流。拉取算法与 GitHub HTTP 契约分别见 puller 和
client。
"""

from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import aiosqlite

from .archive_format import PullGitSnapshot, pull_git_snapshot
from .v9 import GIT_LAYOUT_VERSION, SCHEMA, VERSION

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection, Iterable

_CODEC = "zlib-json-v1"


@dataclass(frozen=True, slots=True)
class PullRun:
    id: int  # Stable local invocation identity.
    target_at: str  # Requested observation watermark.
    started_at: str  # First attempt start time.
    observed_until: str | None  # Durable staged observation watermark.
    request_count: int  # Attempts accumulated across crash recovery.


@dataclass(frozen=True, slots=True)
class PullPass:
    run_id: int  # Owning pending run.
    name: str  # prefetch or closing.
    cutoff_at: str  # Observation cutoff of this pass.
    mode: str  # delta or full catalog traversal.
    prepared: bool  # Discovery signals and count are durable.
    catalog_started: bool  # At least one catalog page is durable.
    catalog_complete: bool  # The terminal catalog page is durable.
    next_url: str | None  # GitHub cursor URL for the next page.
    catalog_pages: int  # Durable pages accepted in the active traversal.
    catalog_items: int  # Unique durable root rows in the active traversal.
    expected_count: int | None  # Progress estimate sampled before a cold traversal.


@dataclass(frozen=True, slots=True)
class CatalogItem:
    number: int  # Repository-local Issue/PR number.
    github_id: int  # Immutable GitHub object identity.
    kind: str  # issue or pull.
    created_at: str  # GitHub creation timestamp.
    updated_at: str  # GitHub root update timestamp.
    summary: dict[str, Any]  # Unprojected catalog response.


@dataclass(frozen=True, slots=True)
class PullTask:
    number: int  # Repository-local Issue/PR number.
    github_id: int | None  # Catalog identity; None for a signal-only task.
    kind: str | None  # issue, pull, or None before catalog association.
    created_at: str | None  # Catalog creation time.
    updated_at: str | None  # Catalog root update time.
    summary_digest: str | None  # Durable raw catalog payload.
    summary: dict[str, Any] | None  # Decoded raw catalog payload.
    catalog_member: bool  # Seen in the active catalog traversal.
    force_comments: bool  # Selected by a repository comment feed.
    completed: bool  # Fetch or intentional reuse is durable.


@dataclass(frozen=True, slots=True)
class StoredHead:
    number: int  # Repository-local Issue/PR number.
    github_id: int  # Immutable GitHub object identity.
    kind: str  # issue or pull.
    created_at: str  # GitHub creation timestamp.
    updated_at: str  # GitHub root update timestamp.
    summary_digest: str  # Content-addressed raw catalog summary.
    bundle_digest: str | None  # Content-addressed Issue/PR observation record.
    present: bool  # Last directly observed availability.
    missing_since: str | None  # First directly observed absence watermark.


@dataclass(frozen=True, slots=True)
class StagedFact:
    fact_kind: str  # Versioned supplemental fact family.
    schema_version: int  # Payload contract version within the fact family.
    subject_key: str  # Stable identity within the fact family.
    resource_number: int | None  # Related Issue/PR number, when applicable.
    source_digest: str | None  # Payload digest from which this observation derives.
    observed_from: str  # Start of the actual API or Git read window.
    observed_until: str  # End of the actual API or Git read window.
    status: str  # Coverage result for this observation.
    payload: dict[str, Any]  # Canonical source facts and coverage evidence.


@dataclass(frozen=True, slots=True)
class StagedResource:
    head: StoredHead  # State to publish with the run.
    observed_at: str  # Pass watermark associated with this observation.
    summary: dict[str, Any] | None  # None reuses head.summary_digest.
    bundle: dict[str, Any] | None  # None reuses head.bundle_digest.
    http_cache: dict[str, Any] | None = None  # Recoverable transport metadata for bundle.
    facts: tuple[StagedFact, ...] = ()  # Independently versioned observations to publish.


@dataclass(frozen=True, slots=True)
class ArchivedVersion:
    run_id: int  # Committed pull run that observed this change.
    target_at: str  # Run observation watermark.
    completed_at: str  # Actual completion time C.
    observed_at: str  # Pass watermark associated with this observation.
    number: int  # Repository-local Issue/PR number.
    github_id: int  # Immutable GitHub object identity.
    kind: str  # issue or pull.
    created_at: str  # GitHub creation timestamp.
    updated_at: str  # GitHub root update timestamp.
    present: bool  # False represents a directly observed tombstone.
    missing_since: str | None  # First target that directly observed absence.
    summary: dict[str, Any]  # Unprojected GitHub catalog response.
    bundle: dict[str, Any] | None  # Last observed Issue/PR record.


@dataclass(frozen=True, slots=True)
class ArchivedHead:
    number: int  # Repository-local Issue/PR number.
    github_id: int  # Immutable GitHub object identity.
    kind: str  # issue or pull.
    created_at: str  # GitHub creation timestamp.
    updated_at: str  # GitHub root update timestamp.
    present: bool  # False represents the latest directly observed tombstone.
    missing_since: str | None  # First target that directly observed absence.
    summary: dict[str, Any]  # Unprojected current catalog response.
    bundle: dict[str, Any] | None  # Last observed Issue/PR record.


@dataclass(frozen=True, slots=True)
class ArchivedRun:
    id: int  # Stable local invocation identity.
    target_at: str  # Requested observation watermark T.
    started_at: str  # First attempt start time.
    observed_until: str  # Durable observation-pass watermark.
    completed_at: str  # Actual completion time C.
    request_count: int  # HTTP attempts accumulated across recovery.
    changed_items: int  # Object versions or tombstones published.
    catalog_items: int  # Objects present after publication.


@dataclass(frozen=True, slots=True)
class ArchivedFact:
    id: int  # Global replay cursor.
    batch_id: int  # Atomic publication unit.
    batch_kind: str  # pull, backfill, or refresh.
    pull_run_id: int | None  # Normal pull publication identity.
    job_id: int | None  # Backfill or refresh publication identity.
    task_id: int | None  # Backfill or refresh work identity.
    published_at: str  # Batch publication time.
    ordinal: int  # Stable position inside the batch.
    fact_kind: str  # Supplemental fact family.
    schema_version: int  # Payload contract version within the family.
    subject_key: str  # Stable identity within the family.
    resource_number: int | None  # Related Issue/PR number, when applicable.
    source_digest: str | None  # Payload digest from which this observation derives.
    observed_from: str  # Start of the actual read window.
    observed_until: str  # End of the actual read window.
    status: str  # Coverage result for this observation.
    payload: dict[str, Any]  # Canonical source facts and coverage evidence.


@dataclass(frozen=True, slots=True)
class FactTaskSpec:
    fact_kind: str  # Versioned supplemental fact family.
    subject_key: str  # Stable identity within the fact family.
    resource_number: int | None  # Related Issue/PR number, when applicable.
    source_digest: str | None  # Existing payload that selected this task.


@dataclass(frozen=True, slots=True)
class FactTask:
    id: int  # Durable job-local work identity.
    job_id: int  # Owning backfill or refresh job.
    fact_kind: str  # Versioned supplemental fact family.
    subject_key: str  # Stable identity within the fact family.
    resource_number: int | None  # Related Issue/PR number, when applicable.
    source_digest: str | None  # Existing payload that selected this task.
    completed: bool  # A terminal observation attempt was published.
    outcome: str | None  # Published coverage status; None while pending.
    attempts: int  # Worker starts persisted across recovery.
    last_error: str | None  # Most recent retryable execution error.


@dataclass(frozen=True, slots=True)
class FactJob:
    id: int  # Durable backfill or explicit-refresh identity.
    job_key: str  # Caller-supplied idempotency key.
    kind: str  # backfill or refresh.
    target_at: str  # Requested observation target.
    requested_at: str  # First request time.
    completed_at: str | None  # Time all scoped tasks reached an outcome.
    status: str  # pending or complete.
    scope_digest: str  # Canonical coverage-scope payload identity.
    scope: dict[str, Any]  # Fact versions, resource cutoff, and requested targets.
    total_tasks: int  # Frozen task population.
    completed_tasks: int  # Tasks with a published terminal attempt.


@dataclass(frozen=True, slots=True)
class StagedTaskFact:
    task_id: int  # Durable task receiving this result.
    fact: StagedFact  # Complete attempt to publish.


@dataclass(frozen=True, slots=True)
class ScheduleState:
    committed_target: str | None  # Greatest committed observation target.
    pending_target: str | None  # Target of the archive-wide pending run.


class SQLiteArchive:
    """单写者 SQLite 原始事实库。

    Args:
        path: SQLite 文件路径；父目录按需创建。
        repository: 固定绑定的 GitHub owner/repo。
    """

    def __init__(self, path: Path, repository: str) -> None:
        self.path = Path(path)
        self.repository = repository
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
            await self._initialize_archive()
        except BaseException:
            await self._db.close()
            self._db = None
            raise
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._connection.close()
        self._db = None

    async def start_run(self, target_at: str, started_at: str) -> PullRun:
        """创建或恢复唯一 pending run。

        Args:
            target_at: 本次观测水位。
            started_at: 新 run 的首次调用时刻。

        Returns:
            新建或同水位恢复的 run。

        Raises:
            RuntimeError: 数据库存在另一水位的 pending run。
        """
        db = self._connection
        row = await _fetchone(db, "SELECT * FROM pull_runs WHERE status = 'pending'")
        if row is not None:
            if row["target_at"] != target_at:
                raise RuntimeError(
                    f"pending pull {row['target_at']} must finish before {target_at}",
                )
            return _run(row)
        previous = await _fetchone(
            db,
            """
            SELECT observed_until
            FROM pull_runs
            WHERE status = 'committed'
            ORDER BY id DESC
            LIMIT 1
            """,
        )
        observed = None if previous is None else previous["observed_until"]
        cursor = await db.execute(
            """
            INSERT INTO pull_runs(
                target_at, started_at, observed_until, status
            ) VALUES (?, ?, ?, 'pending')
            """,
            (target_at, started_at, observed),
        )
        await db.commit()
        return PullRun(
            id=int(cursor.lastrowid),
            target_at=target_at,
            started_at=started_at,
            observed_until=observed,
            request_count=0,
        )

    async def committed_run(self, target_at: str) -> ArchivedRun | None:
        """读取幂等键对应的 committed run。

        Args:
            target_at: 规范化后的观测水位。

        Returns:
            已发布的原始运行结果；键不存在时为 None。
        """
        row = await _fetchone(
            self._connection,
            """
            SELECT *
            FROM pull_runs
            WHERE status = 'committed' AND target_at = ?
            ORDER BY id
            LIMIT 1
            """,
            (target_at,),
        )
        return None if row is None else _archived_run(row)

    async def active_pass(self, run_id: int) -> PullPass | None:
        """读取一个 pending run 尚未闭合的 observation pass。

        Args:
            run_id: 当前 pending run。

        Returns:
            可恢复 pass；尚未开始或已经闭合时为 None。
        """
        row = await _fetchone(
            self._connection,
            "SELECT * FROM pull_passes WHERE run_id = ?",
            (run_id,),
        )
        return None if row is None else _pass(row)

    async def start_pass(
        self,
        run_id: int,
        name: str,
        cutoff_at: str,
        mode: str,
    ) -> PullPass:
        """创建或恢复 run 内唯一的 observation pass。

        Args:
            run_id: 当前 pending run。
            name: ``prefetch`` 或 ``closing``。
            cutoff_at: 本 pass 的固定观测时刻。
            mode: ``delta`` 或 ``full`` 目录遍历。

        Returns:
            新建或与参数完全一致的活动 pass。

        Raises:
            RuntimeError: run 不可写或已有另一活动 pass。
        """
        db = self._connection
        row = await _fetchone(db, "SELECT * FROM pull_passes WHERE run_id = ?", (run_id,))
        if row is not None:
            current = _pass(row)
            if (current.name, current.cutoff_at, current.mode) != (name, cutoff_at, mode):
                raise RuntimeError("another observation pass must finish first")
            return current
        await db.execute("BEGIN IMMEDIATE")
        try:
            run = await _fetchone(db, "SELECT status FROM pull_runs WHERE id = ?", (run_id,))
            if run is None or run["status"] != "pending":
                raise RuntimeError("observation passes require a pending pull")
            await db.execute(
                """
                INSERT INTO pull_passes(run_id, name, cutoff_at, mode)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, name, cutoff_at, mode),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        row = await _fetchone(db, "SELECT * FROM pull_passes WHERE run_id = ?", (run_id,))
        if row is None:
            raise RuntimeError("failed to create observation pass")
        return _pass(row)

    async def prepare_pass(
        self,
        run_id: int,
        signals: Iterable[int],
        expected_count: int | None,
    ) -> PullPass:
        """原子保存 discovery 信号和目录计数。

        Args:
            run_id: 当前 pending run。
            signals: 需要强制复查评论的 parent numbers。
            expected_count: 冷启动目录遍历开始前的进度估计；不可用时为 None。

        Returns:
            prepared 活动 pass。重复调用不改变已冻结的输入。
        """
        db = self._connection
        row = await _fetchone(db, "SELECT * FROM pull_passes WHERE run_id = ?", (run_id,))
        if row is None:
            raise RuntimeError("observation pass does not exist")
        if bool(row["prepared"]):
            return _pass(row)
        await db.execute("BEGIN IMMEDIATE")
        try:
            for number in sorted(set(signals)):
                await db.execute(
                    """
                    INSERT INTO pull_tasks(run_id, number, force_comments)
                    VALUES (?, ?, 1)
                    ON CONFLICT(run_id, number) DO UPDATE SET force_comments = 1
                    """,
                    (run_id, number),
                )
            cursor = await db.execute(
                """
                UPDATE pull_passes
                SET prepared = 1, expected_count = ?
                WHERE run_id = ? AND prepared = 0
                """,
                (expected_count, run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("observation pass changed while preparing")
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        row = await _fetchone(db, "SELECT * FROM pull_passes WHERE run_id = ?", (run_id,))
        if row is None:
            raise RuntimeError("prepared observation pass disappeared")
        return _pass(row)

    async def stage_catalog_page(
        self,
        run_id: int,
        items: Iterable[CatalogItem],
        next_url: str | None,
    ) -> PullPass:
        """原子保存一个目录页、对象任务与下一页 cursor。

        Args:
            run_id: 当前 pending run。
            items: 本页已验证的原始 Issue/PR 条目。
            next_url: GitHub ``Link`` 给出的下一页 URL；末页为 None。

        Returns:
            页提交后的活动 pass。

        Raises:
            ValueError: 同一 number 对应的不可变 GitHub identity 发生冲突。
        """
        page = list(items)
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            state = await _fetchone(
                db,
                "SELECT * FROM pull_passes WHERE run_id = ?",
                (run_id,),
            )
            if state is None or not bool(state["prepared"]) or bool(state["catalog_complete"]):
                raise RuntimeError("catalog page requires an active prepared traversal")
            discovered = 0
            for item in page:
                digest = await _put_json(db, item.summary)
                existing = await _fetchone(
                    db,
                    "SELECT * FROM pull_tasks WHERE run_id = ? AND number = ?",
                    (run_id, item.number),
                )
                if existing is not None and bool(existing["catalog_member"]):
                    identity = (
                        int(existing["github_id"]),
                        str(existing["kind"]),
                        str(existing["created_at"]),
                    )
                    if identity != (item.github_id, item.kind, item.created_at):
                        raise ValueError(f"catalog identity conflict for parent #{item.number}")
                else:
                    discovered += 1
                if existing is None:
                    await db.execute(
                        """
                        INSERT INTO pull_tasks(
                            run_id, number, github_id, kind, created_at, updated_at,
                            summary_digest, catalog_member
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            run_id,
                            item.number,
                            item.github_id,
                            item.kind,
                            item.created_at,
                            item.updated_at,
                            digest,
                        ),
                    )
                else:
                    completed = bool(existing["completed"]) and existing["summary_digest"] == digest
                    await db.execute(
                        """
                        UPDATE pull_tasks
                        SET github_id = ?, kind = ?, created_at = ?, updated_at = ?,
                            summary_digest = ?, catalog_member = 1, completed = ?
                        WHERE run_id = ? AND number = ?
                        """,
                        (
                            item.github_id,
                            item.kind,
                            item.created_at,
                            item.updated_at,
                            digest,
                            int(completed),
                            run_id,
                            item.number,
                        ),
                    )
            await db.execute(
                """
                UPDATE pull_passes
                SET catalog_started = 1, catalog_complete = ?, next_url = ?,
                    catalog_pages = catalog_pages + 1,
                    catalog_items = catalog_items + ?
                WHERE run_id = ?
                """,
                (int(next_url is None), next_url, discovered, run_id),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        row = await _fetchone(db, "SELECT * FROM pull_passes WHERE run_id = ?", (run_id,))
        if row is None:
            raise RuntimeError("catalog traversal disappeared")
        return _pass(row)

    async def restart_catalog(self, run_id: int) -> PullPass:
        """在保留已完成任务的前提下丢弃失效的分页 cursor。

        Args:
            run_id: 当前 pending run。

        Returns:
            从首页重新遍历的活动 pass。
        """
        await self._connection.execute(
            """
            UPDATE pull_passes
            SET catalog_started = 0, catalog_complete = 0,
                next_url = NULL, catalog_pages = 0
            WHERE run_id = ?
            """,
            (run_id,),
        )
        await self._connection.commit()
        row = await _fetchone(
            self._connection,
            "SELECT * FROM pull_passes WHERE run_id = ?",
            (run_id,),
        )
        if row is None:
            raise RuntimeError("catalog traversal disappeared")
        return _pass(row)

    async def load_head_state(
        self,
        run_id: int,
    ) -> tuple[dict[int, StoredHead], dict[int, StoredHead]]:
        """读取 committed heads 与指定 run 的最新 durable stage。

        Args:
            run_id: 当前 pending run。

        Returns:
            叠加 stage 后的逻辑当前状态，以及仅含该 run 最新 stage 的状态；
            两者均按 Issue/PR number 索引。
        """
        rows = await _fetchall(self._connection, "SELECT * FROM resource_heads")
        heads = {int(row["number"]): _head(row) for row in rows}
        rows = await _fetchall(
            self._connection,
            """
            SELECT v.*
            FROM resource_versions AS v
            JOIN (
                SELECT number, max(id) AS id
                FROM resource_versions
                WHERE run_id = ?
                GROUP BY number
            ) AS latest ON latest.id = v.id
            ORDER BY v.id
            """,
            (run_id,),
        )
        staged = {int(row["number"]): _head(row) for row in rows}
        heads.update(staged)
        return heads, staged

    async def pending_catalog_tasks(
        self,
        run_id: int,
        *,
        exclude: Collection[int] = (),
    ) -> list[PullTask]:
        """读取当前已落盘目录页中尚未处理的对象。

        Args:
            run_id: 当前 pending run。
            exclude: 本进程暂缓重试的 PR numbers。

        Returns:
            最多一个 GitHub 页大小、按发现顺序排列的任务。
        """
        rows = await _fetchall(
            self._connection,
            """
            SELECT t.*, b.codec, b.raw_size, b.payload
            FROM pull_tasks AS t
            JOIN payload_blobs AS b ON b.digest = t.summary_digest
            WHERE t.run_id = ? AND t.catalog_member = 1 AND t.completed = 0
            ORDER BY t.id
            LIMIT ?
            """,
            (run_id, 100 + len(exclude)),
        )
        return [
            _task(row, decode_summary=True)
            for row in rows
            if int(row["number"]) not in exclude
        ][:100]

    async def pending_signal_tasks(
        self,
        run_id: int,
        *,
        exclude: Collection[int] = (),
    ) -> list[PullTask]:
        """读取目录闭合后仍需处理的 signal-only parents。

        Args:
            run_id: 当前 pending run。
            exclude: 本进程暂缓重试的 PR numbers。

        Returns:
            至多 100 个按 number 排列的任务。
        """
        rows = await _fetchall(
            self._connection,
            """
            SELECT *
            FROM pull_tasks
            WHERE run_id = ? AND catalog_member = 0
                AND force_comments = 1 AND completed = 0
            ORDER BY number
            LIMIT ?
            """,
            (run_id, 100 + len(exclude)),
        )
        return [
            _task(row, decode_summary=False)
            for row in rows
            if int(row["number"]) not in exclude
        ][:100]

    async def task_progress(self, run_id: int) -> tuple[int, int]:
        """读取活动 pass 已完成和全部 Issue/PR 任务数。

        Args:
            run_id: 当前 pending run。

        Returns:
            已完成任务数与全部任务数。
        """
        row = await _fetchone(
            self._connection,
            """
            SELECT count(*) AS total, count(CASE WHEN completed = 1 THEN 1 END) AS completed
            FROM pull_tasks
            WHERE run_id = ?
            """,
            (run_id,),
        )
        if row is None:
            raise RuntimeError("failed to read task progress")
        return int(row["completed"]), int(row["total"])

    async def complete_task(
        self,
        run_id: int,
        number: int,
        summary_digest: str | None,
    ) -> bool:
        """将一个仍匹配发现输入的复用任务标记为完成。

        Args:
            run_id: 当前 pending run。
            number: Repository-local Issue/PR number。
            summary_digest: 消费者读取任务时绑定的目录 payload；signal-only 为 None。

        Returns:
            任务仍匹配并已完成时为 True；生产者已更新任务时为 False。
        """
        db = self._connection
        cursor = await db.execute(
            """
            UPDATE pull_tasks
            SET completed = 1
            WHERE run_id = ? AND number = ? AND completed = 0
                AND summary_digest IS ?
            """,
            (run_id, number, summary_digest),
        )
        await db.commit()
        return cursor.rowcount == 1

    async def stage_task(
        self,
        run_id: int,
        number: int,
        summary_digest: str | None,
        resource: StagedResource,
    ) -> bool:
        """原子保存一个 bundle 并完成其目录任务。

        Args:
            run_id: 当前 pending run。
            number: 与 resource 对应的 parent number。
            summary_digest: 消费者读取任务时绑定的目录 payload；signal-only 为 None。
            resource: 完整的新 head、事实 payload 与传输 cache。

        Returns:
            任务仍匹配且 bundle 已持久化时为 True；生产者已更新任务时为 False。
        """
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            task = await _fetchone(
                db,
                "SELECT completed, summary_digest FROM pull_tasks WHERE run_id = ? AND number = ?",
                (run_id, number),
            )
            if task is None:
                raise RuntimeError("bundle completion requires a pending discovery task")
            if bool(task["completed"]):
                await db.rollback()
                return True
            if task["summary_digest"] != summary_digest:
                await db.rollback()
                return False
            await _stage_resources(db, run_id, (resource,))
            await db.execute(
                "UPDATE pull_tasks SET completed = 1 WHERE run_id = ? AND number = ?",
                (run_id, number),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return True

    async def stage_facts(self, run_id: int, facts: Iterable[StagedFact]) -> None:
        """Durably stage supplemental observations for a normal pull.

        Args:
            run_id: Current pending pull run.
            facts: Complete observation attempts to publish with that run.
        """
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _stage_facts(db, run_id, facts)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    async def start_fact_job(
        self,
        job_key: str,
        kind: str,
        target_at: str,
        requested_at: str,
        scope: dict[str, Any],
        tasks: Iterable[FactTaskSpec],
    ) -> FactJob:
        """Create or resume one immutable backfill or refresh scope.

        Args:
            job_key: Idempotency identity; a new observation uses a new key.
            kind: ``backfill`` or ``refresh``.
            target_at: Requested observation target for this job.
            requested_at: First request time.
            scope: Explicit fact-set versions, resource cutoff, and selected targets.
            tasks: Frozen work items covered by the scope.

        Returns:
            The existing identical job or a newly durable job.

        Raises:
            RuntimeError: Another fact job is pending.
            ValueError: The key already names a different immutable scope.
        """
        definitions = tuple(sorted(tasks, key=lambda task: (task.fact_kind, task.subject_key)))
        identities = {(task.fact_kind, task.subject_key) for task in definitions}
        if len(identities) != len(definitions):
            raise ValueError("fact task identities must be unique within a job")
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            scope_digest = await _put_json(db, scope)
            existing = await _fact_job_by_key(db, job_key)
            if existing is not None:
                await _verify_fact_job(db, existing, kind, target_at, scope_digest, definitions)
                await db.commit()
                return _fact_job(existing)
            pending = await _fetchone(db, "SELECT id FROM fact_jobs WHERE status = 'pending'")
            if pending is not None:
                raise RuntimeError(f"fact job {pending['id']} must finish first")
            complete = not definitions
            cursor = await db.execute(
                """
                INSERT INTO fact_jobs(
                    job_key, kind, target_at, requested_at, completed_at, status,
                    scope_payload_digest, total_tasks, completed_tasks
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    job_key,
                    kind,
                    target_at,
                    requested_at,
                    requested_at if complete else None,
                    "complete" if complete else "pending",
                    scope_digest,
                    len(definitions),
                ),
            )
            job_id = int(cursor.lastrowid)
            await db.executemany(
                """
                INSERT INTO fact_tasks(
                    job_id, fact_kind, subject_key, resource_number, source_digest
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (
                        job_id,
                        task.fact_kind,
                        task.subject_key,
                        task.resource_number,
                        task.source_digest,
                    )
                    for task in definitions
                ),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        row = await _fact_job_by_id(db, job_id)
        if row is None:
            raise RuntimeError("fact job disappeared after creation")
        return _fact_job(row)

    async def fact_job(self, job_id: int) -> FactJob:
        """Read one durable supplemental-fact job.

        Args:
            job_id: Backfill or refresh job identity.

        Returns:
            Job scope and independently tracked task completion.

        Raises:
            KeyError: The job does not exist.
        """
        row = await _fact_job_by_id(self._connection, job_id)
        if row is None:
            raise KeyError(job_id)
        return _fact_job(row)

    async def take_fact_tasks(self, job_id: int, limit: int = 100) -> list[FactTask]:
        """Start a recoverable batch of pending fact tasks.

        Args:
            job_id: Pending backfill or refresh job.
            limit: Maximum tasks returned in durable ID order.

        Returns:
            Pending tasks with their incremented attempt counts.
        """
        if limit < 1:
            raise ValueError("fact task limit must be positive")
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            job = await _fetchone(db, "SELECT status FROM fact_jobs WHERE id = ?", (job_id,))
            if job is None:
                raise KeyError(job_id)
            if job["status"] == "complete":
                await db.commit()
                return []
            rows = await _fetchall(
                db,
                """
                SELECT * FROM fact_tasks
                WHERE job_id = ? AND completed = 0
                ORDER BY id
                LIMIT ?
                """,
                (job_id, limit),
            )
            await db.executemany(
                "UPDATE fact_tasks SET attempts = attempts + 1, last_error = NULL WHERE id = ?",
                ((int(row["id"]),) for row in rows),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return [_fact_task(row, attempts=int(row["attempts"]) + 1) for row in rows]

    async def record_fact_task_error(self, job_id: int, task_id: int, error: str) -> None:
        """Persist a retryable task error without publishing a fact result.

        Args:
            job_id: Pending backfill or refresh job.
            task_id: Incomplete task identity.
            error: Concise diagnostic for status reporting.

        Raises:
            KeyError: The named pending task does not exist.
        """
        cursor = await self._connection.execute(
            """
            UPDATE fact_tasks SET last_error = ?
            WHERE id = ? AND job_id = ? AND completed = 0
            """,
            (error, task_id, job_id),
        )
        if cursor.rowcount != 1:
            await self._connection.rollback()
            raise KeyError(task_id)
        await self._connection.commit()

    async def publish_fact_batch(
        self,
        job_id: int,
        published_at: str,
        results: Iterable[StagedTaskFact],
    ) -> FactJob:
        """Atomically publish completed job tasks as one replay unit.

        Args:
            job_id: Backfill or refresh job receiving the results.
            published_at: Actual batch publication time.
            results: One terminal observation attempt per durable task.

        Returns:
            Updated job progress and completion state.

        Raises:
            KeyError: A result does not belong to the job.
            ValueError: A result changes its task identity.
        """
        staged = tuple(results)
        if len({result.task_id for result in staged}) != len(staged):
            raise ValueError("a fact task can appear only once per batch")
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            job = await _fetchone(db, "SELECT * FROM fact_jobs WHERE id = ?", (job_id,))
            if job is None:
                raise KeyError(job_id)
            pending: list[tuple[aiosqlite.Row, StagedFact]] = []
            for result in staged:
                task = await _fetchone(
                    db,
                    "SELECT * FROM fact_tasks WHERE id = ? AND job_id = ?",
                    (result.task_id, job_id),
                )
                if task is None:
                    raise KeyError(result.task_id)
                fact = result.fact
                if (task["fact_kind"], task["subject_key"], task["resource_number"]) != (
                    fact.fact_kind,
                    fact.subject_key,
                    fact.resource_number,
                ):
                    raise ValueError(f"fact result changes task {result.task_id} identity")
                if not bool(task["completed"]):
                    pending.append((task, fact))
            if not pending:
                await db.commit()
                return await self.fact_job(job_id)
            cursor = await db.execute(
                """
                INSERT INTO fact_batches(kind, job_id, published_at, fact_count)
                VALUES (?, ?, ?, ?)
                """,
                (job["kind"], job_id, published_at, len(pending)),
            )
            batch_id = int(cursor.lastrowid)
            for ordinal, (task, fact) in enumerate(pending):
                payload_digest = await _put_json(db, fact.payload)
                await _insert_fact_version(
                    db,
                    batch_id,
                    ordinal,
                    int(task["id"]),
                    fact.fact_kind,
                    fact.schema_version,
                    fact.subject_key,
                    fact.resource_number,
                    fact.source_digest,
                    fact.observed_from,
                    fact.observed_until,
                    fact.status,
                    payload_digest,
                )
                await db.execute(
                    """
                    UPDATE fact_tasks
                    SET completed = 1, outcome = ?
                    WHERE id = ?
                    """,
                    (fact.status, task["id"]),
                )
            progress = await _fetchone(
                db,
                """
                SELECT count(*) AS total,
                    count(CASE WHEN completed = 1 THEN 1 END) AS completed
                FROM fact_tasks WHERE job_id = ?
                """,
                (job_id,),
            )
            if progress is None:
                raise RuntimeError("failed to summarize fact job")
            completed = int(progress["completed"])
            total = int(progress["total"])
            await db.execute(
                """
                UPDATE fact_jobs
                SET completed_tasks = ?, status = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    completed,
                    "complete" if completed == total else "pending",
                    published_at if completed == total else None,
                    job_id,
                ),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        row = await _fact_job_by_id(db, job_id)
        if row is None:
            raise RuntimeError("fact job disappeared after publication")
        return _fact_job(row)

    async def finish_pass(self, run_id: int, observed_until: str) -> None:
        """原子闭合 pass、推进 staged 水位并清理途径状态。

        Args:
            run_id: 当前 pending run。
            observed_until: 已完成 observation pass 的 cutoff。
        """
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            state = await _fetchone(
                db,
                """
                SELECT
                    p.catalog_complete,
                    count(CASE WHEN t.completed = 0 THEN 1 END) AS pending
                FROM pull_passes AS p
                LEFT JOIN pull_tasks AS t ON t.run_id = p.run_id
                WHERE p.run_id = ?
                GROUP BY p.run_id
                """,
                (run_id,),
            )
            if (
                state is None
                or not bool(state["catalog_complete"])
                or int(state["pending"]) != 0
            ):
                raise RuntimeError("observation pass still has unfinished discovery work")
            await db.execute(
                "UPDATE pull_runs SET observed_until = ? WHERE id = ? AND status = 'pending'",
                (observed_until, run_id),
            )
            await db.execute("DELETE FROM pull_passes WHERE run_id = ?", (run_id,))
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    async def load_bundle_state(
        self,
        bundle_digest: str | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """读取与一个 bundle 摘要原子配对的事实和传输缓存。

        Args:
            bundle_digest: committed 或当前 run staged head 引用的 bundle 摘要。

        Returns:
            已存记录与可丢弃的 HTTP validator 元数据；无摘要时均为 None。
        """
        if bundle_digest is None:
            return None, None
        row = await _fetchone(
            self._connection,
            """
            SELECT
                b.digest, b.codec, b.raw_size, b.payload,
                c.cache_digest, c.codec AS cache_codec,
                c.raw_size AS cache_size, c.payload AS cache_payload
            FROM payload_blobs AS b
            LEFT JOIN bundle_http_cache AS c ON c.bundle_digest = b.digest
            WHERE b.digest = ?
            """,
            (bundle_digest,),
        )
        if row is None:
            raise ValueError(f"missing bundle payload {bundle_digest}")
        bundle = _decode_row(
            str(row["digest"]),
            str(row["codec"]),
            int(row["raw_size"]),
            bytes(row["payload"]),
        )
        cache = None
        if row["cache_digest"] is not None:
            cache = _decode_row(
                str(row["cache_digest"]),
                str(row["cache_codec"]),
                int(row["cache_size"]),
                bytes(row["cache_payload"]),
            )
        if not isinstance(bundle, dict) or (cache is not None and not isinstance(cache, dict)):
            raise ValueError("bundle state is not a JSON object")
        return bundle, cache

    async def add_requests(self, run_id: int, count: int) -> None:
        """累计一次进程尝试发出的 HTTP 请求数。

        Args:
            run_id: 当前 pending run。
            count: 本次尝试新增的请求数。
        """
        await self._connection.execute(
            "UPDATE pull_runs SET request_count = request_count + ? WHERE id = ?",
            (count, run_id),
        )
        await self._connection.commit()

    async def finalize(
        self,
        run_id: int,
        completed_at: str,
    ) -> tuple[int, int, int]:
        """原子发布 pending 版本并更新 committed heads。

        Args:
            run_id: 当前 pending run。
            completed_at: 实际闭合时刻 C。

        Returns:
            变化版本数、当前目录对象数及该 run 累计请求数。
        """
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
            run = await _fetchone(
                db,
                "SELECT status, observed_until FROM pull_runs WHERE id = ?",
                (run_id,),
            )
            if run is None or run["status"] != "pending" or run["observed_until"] is None:
                raise RuntimeError("only a closed pending pull can be finalized")
            await db.execute(
                """
                INSERT INTO resource_heads(
                    number, github_id, kind, created_at, updated_at,
                    summary_digest, bundle_digest, present, missing_since
                )
                SELECT
                    v.number, v.github_id, v.kind, v.created_at, v.updated_at,
                    v.summary_digest, v.bundle_digest, v.present, v.missing_since
                FROM resource_versions AS v
                JOIN (
                    SELECT number, max(id) AS id
                    FROM resource_versions
                    WHERE run_id = ?
                    GROUP BY number
                ) AS latest ON latest.id = v.id
                WHERE 1
                ON CONFLICT(number) DO UPDATE SET
                    github_id = excluded.github_id,
                    kind = excluded.kind,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    summary_digest = excluded.summary_digest,
                    bundle_digest = excluded.bundle_digest,
                    present = excluded.present,
                    missing_since = excluded.missing_since
                """,
                (run_id,),
            )
            changed_row = await _fetchone(
                db,
                "SELECT count(*) AS count FROM resource_versions WHERE run_id = ?",
                (run_id,),
            )
            catalog_row = await _fetchone(
                db,
                "SELECT count(*) AS count FROM resource_heads WHERE present = 1",
            )
            run_row = await _fetchone(
                db,
                "SELECT request_count FROM pull_runs WHERE id = ?",
                (run_id,),
            )
            if changed_row is None or catalog_row is None or run_row is None:
                raise RuntimeError("failed to summarize pending pull")
            changed = int(changed_row["count"])
            catalog = int(catalog_row["count"])
            requests = int(run_row["request_count"])
            cursor = await db.execute(
                """
                UPDATE pull_runs
                SET completed_at = ?, status = 'committed',
                    changed_items = ?, catalog_items = ?
                WHERE id = ? AND status = 'pending'
                """,
                (completed_at, changed, catalog, run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("pending pull changed while finalizing")
            await _publish_run_facts(db, run_id, completed_at)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        else:
            return changed, catalog, requests

    async def _initialize_archive(self) -> None:
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
                    ("repository", self.repository),
                ),
            )
            await db.commit()
            return
        schema = await _fetchone(
            db,
            "SELECT value FROM archive_meta WHERE key = 'schema_version'",
        )
        repository = await _fetchone(
            db,
            "SELECT value FROM archive_meta WHERE key = 'repository'",
        )
        git_layout = await _fetchone(
            db,
            "SELECT value FROM archive_meta WHERE key = 'git_layout_version'",
        )
        if repository is None or repository["value"] != self.repository:
            raise ValueError("archive belongs to a different GitHub repository")
        if schema is None or schema["value"] != VERSION:
            raise ValueError(
                "unsupported GitHub archive schema; run "
                f"'uv run -m gh_puller.github migrate {self.path}'",
            )
        if git_layout is None or git_layout["value"] != GIT_LAYOUT_VERSION:
            raise ValueError(
                "unsupported GitHub Git layout; run "
                f"'uv run -m gh_puller.github migrate {self.path}'",
            )
        await db.executescript(SCHEMA)
        await db.commit()

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("archive is not open")
        return self._db


async def schedule_state(path: Path) -> ScheduleState:
    """读取最大 committed 水位与 archive-wide pending run。

    Args:
        path: SQLite 事实库。

    Returns:
        最大 committed target 与唯一 pending target；数据库不存在时均为 None。
    """
    path = Path(path)
    if not path.exists():
        return ScheduleState(None, None)
    uri = _readonly_uri(path)
    db = await aiosqlite.connect(uri, uri=True)
    db.row_factory = aiosqlite.Row
    try:
        committed = await _fetchall(
            db,
            """
            SELECT target_at
            FROM pull_runs
            WHERE status = 'committed'
            """,
        )
        pending = await _fetchone(
            db,
            """
            SELECT target_at
            FROM pull_runs
            WHERE status = 'pending'
            ORDER BY id DESC
            LIMIT 1
            """,
        )
        committed_target = max(
            (str(row["target_at"]) for row in committed),
            key=datetime.fromisoformat,
            default=None,
        )
        return ScheduleState(
            committed_target=committed_target,
            pending_target=None if pending is None else str(pending["target_at"]),
        )
    finally:
        await db.close()


async def iter_versions(path: Path) -> AsyncIterator[ArchivedVersion]:
    """按 committed run 顺序流式读取全部无损对象版本。

    Args:
        path: SQLite 事实库。

    Yields:
        可离线重建下游存储的对象变化或 tombstone。
    """
    uri = _readonly_uri(Path(path))
    db = await aiosqlite.connect(uri, uri=True)
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            """
            SELECT
                v.run_id, r.target_at, r.completed_at, v.observed_at,
                v.number, v.github_id, v.kind, v.created_at, v.updated_at,
                v.present, v.missing_since,
                v.summary_digest, v.bundle_digest,
                s.codec AS summary_codec, s.raw_size AS summary_size,
                s.payload AS summary_payload,
                b.codec AS bundle_codec, b.raw_size AS bundle_size,
                b.payload AS bundle_payload
            FROM resource_versions AS v
            JOIN pull_runs AS r ON r.id = v.run_id AND r.status = 'committed'
            JOIN payload_blobs AS s ON s.digest = v.summary_digest
            LEFT JOIN payload_blobs AS b ON b.digest = v.bundle_digest
            ORDER BY v.run_id, v.number, v.id
            """,
        )
        async for row in cursor:
            summary, bundle = _decode_payloads(row)
            yield ArchivedVersion(
                run_id=int(row["run_id"]),
                target_at=str(row["target_at"]),
                completed_at=str(row["completed_at"]),
                observed_at=str(row["observed_at"]),
                number=int(row["number"]),
                github_id=int(row["github_id"]),
                kind=str(row["kind"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
                present=bool(row["present"]),
                missing_since=(None if row["missing_since"] is None else str(row["missing_since"])),
                summary=summary,
                bundle=bundle,
            )
        await cursor.close()
    finally:
        await db.close()


async def iter_heads(
    path: Path,
    *,
    present_only: bool = False,
) -> AsyncIterator[ArchivedHead]:
    """按 number 流式读取已发布的当前对象状态。

    Args:
        path: SQLite 事实库。
        present_only: True 时跳过 tombstone，仅返回当前可见对象。

    Yields:
        无需回放版本流即可读取的 committed head。
    """
    db = await aiosqlite.connect(_readonly_uri(Path(path)), uri=True)
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            """
            SELECT
                h.number, h.github_id, h.kind, h.created_at, h.updated_at,
                h.present, h.missing_since,
                h.summary_digest, h.bundle_digest,
                s.codec AS summary_codec, s.raw_size AS summary_size,
                s.payload AS summary_payload,
                b.codec AS bundle_codec, b.raw_size AS bundle_size,
                b.payload AS bundle_payload
            FROM resource_heads AS h
            JOIN payload_blobs AS s ON s.digest = h.summary_digest
            LEFT JOIN payload_blobs AS b ON b.digest = h.bundle_digest
            WHERE ? = 0 OR h.present = 1
            ORDER BY h.number
            """,
            (int(present_only),),
        )
        async for row in cursor:
            summary, bundle = _decode_payloads(row)
            yield ArchivedHead(
                number=int(row["number"]),
                github_id=int(row["github_id"]),
                kind=str(row["kind"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
                present=bool(row["present"]),
                missing_since=(None if row["missing_since"] is None else str(row["missing_since"])),
                summary=summary,
                bundle=bundle,
            )
        await cursor.close()
    finally:
        await db.close()


async def iter_runs(path: Path) -> AsyncIterator[ArchivedRun]:
    """按发布顺序流式读取 committed run。

    Args:
        path: SQLite 事实库。

    Yields:
        包含 T、C 和调用统计的已完成 run。
    """
    db = await aiosqlite.connect(_readonly_uri(Path(path)), uri=True)
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            """
            SELECT *
            FROM pull_runs
            WHERE status = 'committed'
            ORDER BY id
            """,
        )
        async for row in cursor:
            yield _archived_run(row)
        await cursor.close()
    finally:
        await db.close()


async def iter_facts(
    path: Path,
    *,
    after: int = 0,
) -> AsyncIterator[ArchivedFact]:
    """Stream published supplemental observations in stable replay order.

    Args:
        path: SQLite fact archive.
        after: Exclusive ``ArchivedFact.id`` cursor; zero starts from the beginning.

    Yields:
        Published observations with their atomic batch and source identity.
    """
    db = await aiosqlite.connect(_readonly_uri(Path(path)), uri=True)
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            """
            SELECT
                v.*, b.kind AS batch_kind, b.pull_run_id, b.job_id, b.published_at,
                p.codec, p.raw_size, p.payload
            FROM fact_versions AS v
            JOIN fact_batches AS b ON b.id = v.batch_id
            LEFT JOIN pull_runs AS r ON r.id = b.pull_run_id
            JOIN payload_blobs AS p ON p.digest = v.payload_digest
            WHERE v.id > ? AND (b.pull_run_id IS NULL OR r.status = 'committed')
            ORDER BY v.id
            """,
            (after,),
        )
        async for row in cursor:
            payload = _decode_row(
                str(row["payload_digest"]),
                str(row["codec"]),
                int(row["raw_size"]),
                bytes(row["payload"]),
            )
            if not isinstance(payload, dict):
                raise TypeError(f"fact payload {row['payload_digest']} is not a JSON object")
            yield ArchivedFact(
                id=int(row["id"]),
                batch_id=int(row["batch_id"]),
                batch_kind=str(row["batch_kind"]),
                pull_run_id=None if row["pull_run_id"] is None else int(row["pull_run_id"]),
                job_id=None if row["job_id"] is None else int(row["job_id"]),
                task_id=None if row["task_id"] is None else int(row["task_id"]),
                published_at=str(row["published_at"]),
                ordinal=int(row["ordinal"]),
                fact_kind=str(row["fact_kind"]),
                schema_version=int(row["schema_version"]),
                subject_key=str(row["subject_key"]),
                resource_number=(
                    None if row["resource_number"] is None else int(row["resource_number"])
                ),
                source_digest=None if row["source_digest"] is None else str(row["source_digest"]),
                observed_from=str(row["observed_from"]),
                observed_until=str(row["observed_until"]),
                status=str(row["status"]),
                payload=payload,
            )
        await cursor.close()
    finally:
        await db.close()


async def _put_json(db: aiosqlite.Connection, value: Any) -> str:
    raw = _json_bytes(value)
    digest = hashlib.sha256(raw).hexdigest()
    await db.execute(
        """
        INSERT OR IGNORE INTO payload_blobs(digest, codec, raw_size, payload)
        VALUES (?, ?, ?, ?)
        """,
        (digest, _CODEC, len(raw), zlib.compress(raw)),
    )
    return digest


async def _put_http_cache(
    db: aiosqlite.Connection,
    bundle_digest: str,
    value: dict[str, Any],
) -> None:
    raw = _json_bytes(value)
    digest = hashlib.sha256(raw).hexdigest()
    await db.execute(
        """
        INSERT INTO bundle_http_cache(
            bundle_digest, cache_digest, codec, raw_size, payload
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(bundle_digest) DO UPDATE SET
            cache_digest = excluded.cache_digest,
            codec = excluded.codec,
            raw_size = excluded.raw_size,
            payload = excluded.payload
        """,
        (bundle_digest, digest, _CODEC, len(raw), zlib.compress(raw)),
    )


def _decode_row(digest: str, codec: str, raw_size: int, payload: bytes) -> Any:
    if codec != _CODEC:
        raise ValueError(f"unsupported payload codec {codec}")
    raw = zlib.decompress(payload)
    if len(raw) != raw_size or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"corrupt payload blob {digest}")
    return json.loads(raw)


def _decode_payloads(row: aiosqlite.Row) -> tuple[dict[str, Any], dict[str, Any] | None]:
    summary = _decode_row(
        str(row["summary_digest"]),
        str(row["summary_codec"]),
        int(row["summary_size"]),
        bytes(row["summary_payload"]),
    )
    bundle = None
    if row["bundle_digest"] is not None:
        bundle = _decode_row(
            str(row["bundle_digest"]),
            str(row["bundle_codec"]),
            int(row["bundle_size"]),
            bytes(row["bundle_payload"]),
        )
    if not isinstance(summary, dict) or (bundle is not None and not isinstance(bundle, dict)):
        raise ValueError("archive payload is not a JSON object")
    return summary, bundle


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def json_digest(value: Any) -> str:
    """返回事实库 canonical JSON 编码的 SHA-256 摘要。

    Args:
        value: 可 JSON 序列化的原始 API 值。

    Returns:
        与 payload_blobs 主键一致的十六进制摘要。
    """
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _readonly_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


async def _stage_resources(
    db: aiosqlite.Connection,
    run_id: int,
    resources: Iterable[StagedResource],
) -> None:
    run = await _fetchone(
        db,
        "SELECT status FROM pull_runs WHERE id = ?",
        (run_id,),
    )
    if run is None or run["status"] != "pending":
        raise RuntimeError("resource versions require a pending pull")
    for resource in resources:
        head = resource.head
        summary_digest = head.summary_digest
        bundle_digest = head.bundle_digest
        if resource.summary is not None:
            summary_digest = await _put_json(db, resource.summary)
        if resource.bundle is not None:
            bundle_digest = await _put_json(db, resource.bundle)
            await _put_pull_git_index(db, bundle_digest, resource.bundle)
            if resource.http_cache is not None:
                await _put_http_cache(db, bundle_digest, resource.http_cache)
        if summary_digest != head.summary_digest or bundle_digest != head.bundle_digest:
            head = StoredHead(
                number=head.number,
                github_id=head.github_id,
                kind=head.kind,
                created_at=head.created_at,
                updated_at=head.updated_at,
                summary_digest=summary_digest,
                bundle_digest=bundle_digest,
                present=head.present,
                missing_since=head.missing_since,
            )
        await _stage_facts(db, run_id, resource.facts)
        if await _current_head(db, run_id, head.number) == head:
            continue
        await db.execute(
            """
            INSERT INTO resource_versions(
                run_id, observed_at, number, github_id, kind, created_at, updated_at,
                summary_digest, bundle_digest, present, missing_since
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, resource.observed_at, *_head_values(head)),
        )


async def _stage_facts(
    db: aiosqlite.Connection,
    run_id: int,
    facts: Iterable[StagedFact],
) -> None:
    run = await _fetchone(db, "SELECT status FROM pull_runs WHERE id = ?", (run_id,))
    if run is None or run["status"] != "pending":
        raise RuntimeError("supplemental facts require a pending pull")
    for fact in facts:
        payload_digest = await _put_json(db, fact.payload)
        await db.execute(
            """
            INSERT OR IGNORE INTO pending_fact_versions(
                run_id, fact_kind, schema_version, subject_key, resource_number,
                source_digest, observed_from, observed_until, status, payload_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                fact.fact_kind,
                fact.schema_version,
                fact.subject_key,
                fact.resource_number,
                fact.source_digest,
                fact.observed_from,
                fact.observed_until,
                fact.status,
                payload_digest,
            ),
        )


async def _publish_run_facts(
    db: aiosqlite.Connection,
    run_id: int,
    published_at: str,
) -> None:
    rows = await _fetchall(
        db,
        "SELECT * FROM pending_fact_versions WHERE run_id = ? ORDER BY id",
        (run_id,),
    )
    if not rows:
        return
    cursor = await db.execute(
        """
        INSERT INTO fact_batches(kind, pull_run_id, published_at, fact_count)
        VALUES ('pull', ?, ?, ?)
        """,
        (run_id, published_at, len(rows)),
    )
    batch_id = int(cursor.lastrowid)
    for ordinal, row in enumerate(rows):
        await _insert_fact_version(
            db,
            batch_id,
            ordinal,
            None,
            str(row["fact_kind"]),
            int(row["schema_version"]),
            str(row["subject_key"]),
            None if row["resource_number"] is None else int(row["resource_number"]),
            None if row["source_digest"] is None else str(row["source_digest"]),
            str(row["observed_from"]),
            str(row["observed_until"]),
            str(row["status"]),
            str(row["payload_digest"]),
        )
    await db.execute("DELETE FROM pending_fact_versions WHERE run_id = ?", (run_id,))


async def _insert_fact_version(
    db: aiosqlite.Connection,
    batch_id: int,
    ordinal: int,
    task_id: int | None,
    fact_kind: str,
    schema_version: int,
    subject_key: str,
    resource_number: int | None,
    source_digest: str | None,
    observed_from: str,
    observed_until: str,
    status: str,
    payload_digest: str,
) -> None:
    cursor = await db.execute(
        """
        INSERT INTO fact_versions(
            batch_id, task_id, ordinal, fact_kind, schema_version, subject_key,
            resource_number, source_digest, observed_from, observed_until,
            status, payload_digest
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            task_id,
            ordinal,
            fact_kind,
            schema_version,
            subject_key,
            resource_number,
            source_digest,
            observed_from,
            observed_until,
            status,
            payload_digest,
        ),
    )
    version_id = int(cursor.lastrowid)
    successful = version_id if status in {"complete", "null"} else None
    await db.execute(
        """
        INSERT INTO fact_heads(
            fact_kind, subject_key, latest_version_id, successful_version_id
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(fact_kind, subject_key) DO UPDATE SET
            latest_version_id = excluded.latest_version_id,
            successful_version_id = coalesce(
                excluded.successful_version_id,
                fact_heads.successful_version_id
            )
        """,
        (fact_kind, subject_key, version_id, successful),
    )


async def _fact_job_by_id(
    db: aiosqlite.Connection,
    job_id: int,
) -> aiosqlite.Row | None:
    return await _fetchone(
        db,
        """
        SELECT
            j.*, p.codec AS scope_codec, p.raw_size AS scope_size,
            p.payload AS scope_payload
        FROM fact_jobs AS j
        JOIN payload_blobs AS p ON p.digest = j.scope_payload_digest
        WHERE j.id = ?
        """,
        (job_id,),
    )


async def _fact_job_by_key(
    db: aiosqlite.Connection,
    job_key: str,
) -> aiosqlite.Row | None:
    return await _fetchone(
        db,
        """
        SELECT
            j.*, p.codec AS scope_codec, p.raw_size AS scope_size,
            p.payload AS scope_payload
        FROM fact_jobs AS j
        JOIN payload_blobs AS p ON p.digest = j.scope_payload_digest
        WHERE j.job_key = ?
        """,
        (job_key,),
    )


async def _verify_fact_job(
    db: aiosqlite.Connection,
    row: aiosqlite.Row,
    kind: str,
    target_at: str,
    scope_digest: str,
    definitions: tuple[FactTaskSpec, ...],
) -> None:
    if (row["kind"], row["target_at"], row["scope_payload_digest"]) != (
        kind,
        target_at,
        scope_digest,
    ):
        raise ValueError("fact job key already names a different scope")
    tasks = await _fetchall(
        db,
        """
        SELECT fact_kind, subject_key, resource_number, source_digest
        FROM fact_tasks WHERE job_id = ?
        ORDER BY fact_kind, subject_key
        """,
        (row["id"],),
    )
    stored = tuple(
        FactTaskSpec(
            fact_kind=str(task["fact_kind"]),
            subject_key=str(task["subject_key"]),
            resource_number=(
                None if task["resource_number"] is None else int(task["resource_number"])
            ),
            source_digest=(None if task["source_digest"] is None else str(task["source_digest"])),
        )
        for task in tasks
    )
    if stored != definitions:
        raise ValueError("fact job key already names a different task population")


def _fact_job(row: aiosqlite.Row) -> FactJob:
    scope = _decode_row(
        str(row["scope_payload_digest"]),
        str(row["scope_codec"]),
        int(row["scope_size"]),
        bytes(row["scope_payload"]),
    )
    if not isinstance(scope, dict):
        raise TypeError("fact job scope is not a JSON object")
    return FactJob(
        id=int(row["id"]),
        job_key=str(row["job_key"]),
        kind=str(row["kind"]),
        target_at=str(row["target_at"]),
        requested_at=str(row["requested_at"]),
        completed_at=None if row["completed_at"] is None else str(row["completed_at"]),
        status=str(row["status"]),
        scope_digest=str(row["scope_payload_digest"]),
        scope=scope,
        total_tasks=int(row["total_tasks"]),
        completed_tasks=int(row["completed_tasks"]),
    )


def _fact_task(row: aiosqlite.Row, *, attempts: int | None = None) -> FactTask:
    return FactTask(
        id=int(row["id"]),
        job_id=int(row["job_id"]),
        fact_kind=str(row["fact_kind"]),
        subject_key=str(row["subject_key"]),
        resource_number=None if row["resource_number"] is None else int(row["resource_number"]),
        source_digest=None if row["source_digest"] is None else str(row["source_digest"]),
        completed=bool(row["completed"]),
        outcome=None if row["outcome"] is None else str(row["outcome"]),
        attempts=int(row["attempts"]) if attempts is None else attempts,
        last_error=None if row["last_error"] is None else str(row["last_error"]),
    )


async def _put_pull_git_index(
    db: aiosqlite.Connection,
    bundle_digest: str,
    bundle: dict[str, Any],
) -> None:
    snapshot = pull_git_snapshot(bundle_digest, bundle)
    if snapshot is None:
        return
    await db.execute(
        """
        INSERT OR IGNORE INTO git_pull_snapshots(
            bundle_digest, number, merged, base_sha, head_sha,
            comparison_kind, comparison_sha, base_ref, head_ref,
            comparison_ref, landing_sha, landing_ref, history_preserved
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        _pull_git_values(snapshot),
    )
    await db.executemany(
        """
        INSERT OR IGNORE INTO git_pull_commits(bundle_digest, ordinal, sha)
        VALUES (?, ?, ?)
        """,
        (
            (snapshot.bundle_digest, ordinal, sha)
            for ordinal, sha in enumerate(snapshot.commits)
        ),
    )


def _pull_git_values(snapshot: PullGitSnapshot) -> tuple[Any, ...]:
    return (
        snapshot.bundle_digest,
        snapshot.number,
        int(snapshot.merged),
        snapshot.base_sha,
        snapshot.head_sha,
        snapshot.comparison_kind,
        snapshot.comparison_sha,
        snapshot.base_ref,
        snapshot.head_ref,
        snapshot.comparison_ref,
        snapshot.landing_sha,
        snapshot.landing_ref,
        None if snapshot.history_preserved is None else int(snapshot.history_preserved),
    )


async def _current_head(
    db: aiosqlite.Connection,
    run_id: int,
    number: int,
) -> StoredHead | None:
    row = await _fetchone(
        db,
        """
        SELECT *
        FROM resource_versions
        WHERE run_id = ? AND number = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (run_id, number),
    )
    if row is None:
        row = await _fetchone(
            db,
            "SELECT * FROM resource_heads WHERE number = ?",
            (number,),
        )
    return None if row is None else _head(row)


def _head(row: aiosqlite.Row) -> StoredHead:
    return StoredHead(
        number=int(row["number"]),
        github_id=int(row["github_id"]),
        kind=str(row["kind"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        summary_digest=str(row["summary_digest"]),
        bundle_digest=None if row["bundle_digest"] is None else str(row["bundle_digest"]),
        present=bool(row["present"]),
        missing_since=None if row["missing_since"] is None else str(row["missing_since"]),
    )


def _head_values(head: StoredHead) -> tuple[Any, ...]:
    return (
        head.number,
        head.github_id,
        head.kind,
        head.created_at,
        head.updated_at,
        head.summary_digest,
        head.bundle_digest,
        int(head.present),
        head.missing_since,
    )


def _run(row: aiosqlite.Row) -> PullRun:
    return PullRun(
        id=int(row["id"]),
        target_at=str(row["target_at"]),
        started_at=str(row["started_at"]),
        observed_until=None if row["observed_until"] is None else str(row["observed_until"]),
        request_count=int(row["request_count"]),
    )


def _pass(row: aiosqlite.Row) -> PullPass:
    return PullPass(
        run_id=int(row["run_id"]),
        name=str(row["name"]),
        cutoff_at=str(row["cutoff_at"]),
        mode=str(row["mode"]),
        prepared=bool(row["prepared"]),
        catalog_started=bool(row["catalog_started"]),
        catalog_complete=bool(row["catalog_complete"]),
        next_url=None if row["next_url"] is None else str(row["next_url"]),
        catalog_pages=int(row["catalog_pages"]),
        catalog_items=int(row["catalog_items"]),
        expected_count=None if row["expected_count"] is None else int(row["expected_count"]),
    )


def _task(row: aiosqlite.Row, *, decode_summary: bool) -> PullTask:
    summary = None
    digest = None if row["summary_digest"] is None else str(row["summary_digest"])
    if decode_summary and digest is not None:
        summary = _decode_row(
            digest,
            str(row["codec"]),
            int(row["raw_size"]),
            bytes(row["payload"]),
        )
        if not isinstance(summary, dict):
            raise ValueError(f"catalog payload for #{row['number']} is not a JSON object")
    return PullTask(
        number=int(row["number"]),
        github_id=None if row["github_id"] is None else int(row["github_id"]),
        kind=None if row["kind"] is None else str(row["kind"]),
        created_at=None if row["created_at"] is None else str(row["created_at"]),
        updated_at=None if row["updated_at"] is None else str(row["updated_at"]),
        summary_digest=digest,
        summary=summary,
        catalog_member=bool(row["catalog_member"]),
        force_comments=bool(row["force_comments"]),
        completed=bool(row["completed"]),
    )


def _archived_run(row: aiosqlite.Row) -> ArchivedRun:
    return ArchivedRun(
        id=int(row["id"]),
        target_at=str(row["target_at"]),
        started_at=str(row["started_at"]),
        observed_until=str(row["observed_until"]),
        completed_at=str(row["completed_at"]),
        request_count=int(row["request_count"]),
        changed_items=int(row["changed_items"]),
        catalog_items=int(row["catalog_items"]),
    )


async def _fetchone(
    db: aiosqlite.Connection,
    sql: str,
    parameters: tuple[Any, ...] = (),
) -> aiosqlite.Row | None:
    cursor = await db.execute(sql, parameters)
    try:
        return await cursor.fetchone()
    finally:
        await cursor.close()


async def _fetchall(
    db: aiosqlite.Connection,
    sql: str,
    parameters: tuple[Any, ...] = (),
) -> list[aiosqlite.Row]:
    cursor = await db.execute(sql, parameters)
    try:
        return await cursor.fetchall()
    finally:
        await cursor.close()
