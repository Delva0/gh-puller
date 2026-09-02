"""持久化 GitHub 原始事实、拉取水位与离线版本流。

SQLite 是本模块的唯一事实边界：未完成 run 可恢复但不进入公共读取流，committed
run 中的内容寻址 payload 与对象版本足以在无网络条件下重建任意下游数据。
拉取算法与 GitHub HTTP 契约分别见 puller 和 client。
"""

from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

_SCHEMA_VERSION = "1"
_CODEC = "zlib-json-v1"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS payload_blobs (
    digest TEXT PRIMARY KEY,
    codec TEXT NOT NULL,
    raw_size INTEGER NOT NULL,
    payload BLOB NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS pull_runs (
    id INTEGER PRIMARY KEY,
    target_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    observed_until TEXT,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'committed')),
    series TEXT,
    request_count INTEGER NOT NULL DEFAULT 0,
    changed_items INTEGER,
    catalog_items INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS one_pending_pull
ON pull_runs(status) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS pull_runs_series_target
ON pull_runs(series, target_at) WHERE status = 'committed';

CREATE UNIQUE INDEX IF NOT EXISTS one_unlabeled_committed_target
ON pull_runs(target_at) WHERE status = 'committed' AND series IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS one_labeled_committed_target
ON pull_runs(series, target_at) WHERE status = 'committed' AND series IS NOT NULL;

CREATE TABLE IF NOT EXISTS resource_heads (
    number INTEGER PRIMARY KEY,
    github_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('issue', 'pull')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    summary_digest TEXT NOT NULL REFERENCES payload_blobs(digest),
    bundle_digest TEXT REFERENCES payload_blobs(digest),
    present INTEGER NOT NULL CHECK (present IN (0, 1)),
    missing_since TEXT
);

CREATE TABLE IF NOT EXISTS resource_versions (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES pull_runs(id),
    observed_at TEXT NOT NULL,
    number INTEGER NOT NULL,
    github_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('issue', 'pull')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    summary_digest TEXT NOT NULL REFERENCES payload_blobs(digest),
    bundle_digest TEXT REFERENCES payload_blobs(digest),
    present INTEGER NOT NULL CHECK (present IN (0, 1)),
    missing_since TEXT
);

CREATE INDEX IF NOT EXISTS resource_versions_number
ON resource_versions(number, id);

CREATE INDEX IF NOT EXISTS resource_versions_run_number
ON resource_versions(run_id, number, id);
"""


@dataclass(frozen=True, slots=True)
class PullRun:
    id: int  # Stable local invocation identity.
    target_at: str  # Requested coverage watermark.
    started_at: str  # First attempt start time.
    observed_until: str | None  # Durable staged observation watermark.
    series: str | None  # Opaque scheduler series label.
    request_count: int  # Attempts accumulated across crash recovery.


@dataclass(frozen=True, slots=True)
class StoredHead:
    number: int  # Repository-local Issue/PR number.
    github_id: int  # Immutable GitHub object identity.
    kind: str  # issue or pull.
    created_at: str  # GitHub creation timestamp.
    updated_at: str  # GitHub root update timestamp.
    summary_digest: str  # Content-addressed raw catalog summary.
    bundle_digest: str | None  # Content-addressed complete bundle.
    present: bool  # Current catalog membership.
    missing_since: str | None  # First observed absence watermark.


@dataclass(frozen=True, slots=True)
class StagedResource:
    head: StoredHead  # State to publish with the run.
    observed_at: str  # Pass watermark associated with this observation.
    summary: dict[str, Any] | None  # None reuses head.summary_digest.
    bundle: dict[str, Any] | None  # None reuses head.bundle_digest.


@dataclass(frozen=True, slots=True)
class ArchivedVersion:
    run_id: int  # Committed pull run that observed this change.
    target_at: str  # Run coverage watermark.
    completed_at: str  # Actual completion time C.
    observed_at: str  # Pass watermark associated with this observation.
    number: int  # Repository-local Issue/PR number.
    github_id: int  # Immutable GitHub object identity.
    kind: str  # issue or pull.
    created_at: str  # GitHub creation timestamp.
    updated_at: str  # GitHub root update timestamp.
    present: bool  # False represents a tombstone.
    missing_since: str | None  # First target that certified absence.
    summary: dict[str, Any]  # Unprojected GitHub catalog response.
    bundle: dict[str, Any] | None  # Complete last observed bundle.


@dataclass(frozen=True, slots=True)
class ArchivedRun:
    id: int  # Stable local invocation identity.
    target_at: str  # Requested coverage watermark T.
    started_at: str  # First attempt start time.
    observed_until: str  # Durable closed observation watermark.
    completed_at: str  # Actual completion time C.
    series: str | None  # Opaque scheduler series label.
    request_count: int  # HTTP attempts accumulated across recovery.
    changed_items: int  # Object versions or tombstones published.
    catalog_items: int  # Objects present after publication.


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
            await self._db.executescript(_SCHEMA)
            await self._bind_archive()
        except BaseException:
            await self._db.close()
            self._db = None
            raise
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._connection.close()
        self._db = None

    async def start_run(self, target_at: str, started_at: str, series: str | None) -> PullRun:
        """创建或恢复唯一 pending run。

        Args:
            target_at: 本次覆盖水位。
            started_at: 新 run 的首次调用时刻。
            series: 调度器提供的可选系列标签。

        Returns:
            新建或同水位恢复的 run。

        Raises:
            RuntimeError: 数据库存在另一水位或系列的 pending run。
        """
        db = self._connection
        row = await _fetchone(db, "SELECT * FROM pull_runs WHERE status = 'pending'")
        if row is not None:
            if row["target_at"] != target_at or row["series"] != series:
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
                target_at, started_at, observed_until, status, series
            ) VALUES (?, ?, ?, 'pending', ?)
            """,
            (target_at, started_at, observed, series),
        )
        await db.commit()
        return PullRun(
            id=int(cursor.lastrowid),
            target_at=target_at,
            started_at=started_at,
            observed_until=observed,
            series=series,
            request_count=0,
        )

    async def committed_run(
        self,
        target_at: str,
        series: str | None,
    ) -> ArchivedRun | None:
        """读取幂等键对应的 committed run。

        Args:
            target_at: 规范化后的覆盖水位。
            series: 调度系列标签；None 也是独立的键空间。

        Returns:
            已发布的原始运行结果；键不存在时为 None。
        """
        row = await _fetchone(
            self._connection,
            """
            SELECT *
            FROM pull_runs
            WHERE status = 'committed' AND target_at = ? AND series IS ?
            ORDER BY id
            LIMIT 1
            """,
            (target_at, series),
        )
        return None if row is None else _archived_run(row)

    async def load_heads(self, run_id: int) -> dict[int, StoredHead]:
        """读取 committed heads，并叠加指定 run 的 durable stage。

        Args:
            run_id: 当前 pending run。

        Returns:
            按 Issue/PR number 索引的逻辑当前状态。
        """
        rows = await _fetchall(self._connection, "SELECT * FROM resource_heads")
        heads = {int(row["number"]): _head(row) for row in rows}
        staged = await _fetchall(
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
            """,
            (run_id,),
        )
        heads.update({int(row["number"]): _head(row) for row in staged})
        return heads

    async def stage(self, run_id: int, resources: Iterable[StagedResource]) -> None:
        """持久化一批可恢复对象，但不向 committed 读取者发布。

        Args:
            run_id: 当前 pending run。
            resources: 完整的新 head 及可选的新 payload。
        """
        db = self._connection
        await db.execute("BEGIN IMMEDIATE")
        try:
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
                previous = await _current_head(db, run_id, head.number)
                if previous == head:
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
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    async def update_observed(self, run_id: int, observed_until: str) -> None:
        """推进 pending run 已完成的 staged observation 水位。

        Args:
            run_id: 当前 pending run。
            observed_until: 已完整闭合的观察时刻。
        """
        await self._connection.execute(
            "UPDATE pull_runs SET observed_until = ? WHERE id = ? AND status = 'pending'",
            (observed_until, run_id),
        )
        await self._connection.commit()

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
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        else:
            return changed, catalog, requests

    async def _bind_archive(self) -> None:
        db = self._connection
        schema = await _fetchone(
            db,
            "SELECT value FROM archive_meta WHERE key = 'schema_version'",
        )
        repository = await _fetchone(
            db,
            "SELECT value FROM archive_meta WHERE key = 'repository'",
        )
        if schema is None and repository is None:
            await db.executemany(
                "INSERT INTO archive_meta(key, value) VALUES (?, ?)",
                (("schema_version", _SCHEMA_VERSION), ("repository", self.repository)),
            )
            await db.commit()
            return
        if schema is None or schema["value"] != _SCHEMA_VERSION:
            raise ValueError("unsupported GitHub archive schema")
        if repository is None or repository["value"] != self.repository:
            raise ValueError("archive belongs to a different GitHub repository")

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("archive is not open")
        return self._db


async def schedule_state(
    path: Path,
    series: str,
) -> tuple[str | None, str | None]:
    """读取调度系列的 committed 与 pending 水位。

    Args:
        path: SQLite 事实库。
        series: 调度器的稳定系列标签。

    Returns:
        最近 committed target 与同系列 pending target；数据库不存在时均为 None。
    """
    path = Path(path)
    if not path.exists():
        return None, None
    uri = _readonly_uri(path)
    db = await aiosqlite.connect(uri, uri=True)
    db.row_factory = aiosqlite.Row
    try:
        committed = await _fetchone(
            db,
            """
            SELECT target_at
            FROM pull_runs
            WHERE status = 'committed' AND series = ?
            ORDER BY target_at DESC, id DESC
            LIMIT 1
            """,
            (series,),
        )
        pending = await _fetchone(
            db,
            """
            SELECT target_at, series
            FROM pull_runs
            WHERE status = 'pending'
            ORDER BY id DESC
            LIMIT 1
            """,
        )
        if pending is not None and pending["series"] != series:
            raise RuntimeError(
                f"pending pull belongs to series {pending['series']!r}, not {series!r}",
            )
        return (
            None if committed is None else str(committed["target_at"]),
            None if pending is None else str(pending["target_at"]),
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
            ORDER BY v.id
            """,
        )
        async for row in cursor:
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
                raise ValueError("archive version payload is not a JSON object")
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


async def iter_runs(path: Path) -> AsyncIterator[ArchivedRun]:
    """按发布顺序流式读取 committed run。

    Args:
        path: SQLite 事实库。

    Yields:
        包含 T、C、调度标签和调用统计的已完成 run。
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


def _decode_row(digest: str, codec: str, raw_size: int, payload: bytes) -> Any:
    if codec != _CODEC:
        raise ValueError(f"unsupported payload codec {codec}")
    raw = zlib.decompress(payload)
    if len(raw) != raw_size or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"corrupt payload blob {digest}")
    return json.loads(raw)


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
        series=None if row["series"] is None else str(row["series"]),
        request_count=int(row["request_count"]),
    )


def _archived_run(row: aiosqlite.Row) -> ArchivedRun:
    return ArchivedRun(
        id=int(row["id"]),
        target_at=str(row["target_at"]),
        started_at=str(row["started_at"]),
        observed_until=str(row["observed_until"]),
        completed_at=str(row["completed_at"]),
        series=None if row["series"] is None else str(row["series"]),
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
