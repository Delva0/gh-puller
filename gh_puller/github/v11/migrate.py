"""Migrate a stopped version-ten observation archive to version eleven.

The migration preserves every fact, payload, discovery cursor, and pending sync task.
It only adds independent maintenance state and the stronger commit-object schema.
"""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from ..commit_references import commit_reference_index_rows
from ..locking import archive_lock
from .schema import VERSION


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """Summary of one idempotent archive migration."""

    path: Path
    previous_version: str
    version: str
    changed: bool


async def migrate_archive(path: Path) -> MigrationResult:
    """Upgrade one stopped v10 SQLite archive in place.

    Args:
        path: SQLite archive whose paired Git layout remains unchanged.

    Returns:
        Source and resulting versions, including whether this call changed storage.

    Raises:
        ValueError: The archive is neither v10 nor an already migrated v11 archive.
    """
    destination = Path(path)
    async with archive_lock(destination), aiosqlite.connect(destination) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        metadata = dict(await _rows(db, "SELECT key, value FROM archive_meta"))
        previous = str(metadata.get("schema_version", ""))
        if previous == VERSION:
            return MigrationResult(destination, previous, VERSION, False)
        if previous != "10":
            raise ValueError(f"cannot migrate GitHub archive schema {previous or 'unknown'}")
        try:
            await db.executescript(f"BEGIN IMMEDIATE;\n{_MAINTENANCE_SCHEMA}")
            await db.execute(
                """
                ALTER TABLE fact_batches
                ADD COLUMN maintenance_task_id INTEGER REFERENCES maintenance_tasks(id)
                """,
            )
            await db.execute(
                "INSERT INTO fact_schemas(family, version) VALUES ('commit-object', 2)",
            )
            await _populate_commit_reference_index(db)
            await db.execute("DROP VIEW fact_records")
            await db.execute(_FACT_RECORDS_VIEW)
            await db.execute(
                "UPDATE archive_meta SET value = ? WHERE key = 'schema_version'",
                (VERSION,),
            )
            violations = await _rows(db, "PRAGMA foreign_key_check")
            if violations:
                raise RuntimeError("migration produced foreign-key violations")
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return MigrationResult(destination, previous, VERSION, True)


async def _rows(
    db: aiosqlite.Connection,
    query: str,
) -> list[tuple[str, str]]:
    async with db.execute(query) as cursor:
        return await cursor.fetchall()


async def _populate_commit_reference_index(db: aiosqlite.Connection) -> None:
    async with db.execute(
        """
        SELECT o.id, o.resource_number, o.coverage, p.codec, p.raw_size, p.payload
        FROM fact_observations AS o
        JOIN payload_blobs AS p ON p.digest = o.payload_digest
        WHERE o.family = 'commit-references'
        ORDER BY o.id
        """,
    ) as cursor:
        while rows := await cursor.fetchmany(512):
            indexed = []
            for observation_id, resource_number, coverage, codec, raw_size, compressed in rows:
                if coverage != "complete":
                    continue
                if codec != "zlib-json-v1":
                    raise ValueError(f"unsupported payload codec {codec}")
                raw = zlib.decompress(compressed)
                if len(raw) != raw_size:
                    raise ValueError(f"payload {observation_id} has an invalid size")
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise TypeError(f"payload {observation_id} is not an object")
                indexed.extend(
                    commit_reference_index_rows(
                        int(observation_id),
                        None if resource_number is None else int(resource_number),
                        payload,
                    ),
                )
            await db.executemany(
                """
                INSERT INTO commit_reference_index(
                    observation_id, ordinal, sha
                ) VALUES (?, ?, ?)
                """,
                indexed,
            )


_MAINTENANCE_SCHEMA = """
CREATE TABLE maintenance_jobs (
    id INTEGER PRIMARY KEY,
    job_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('backfill', 'refresh')),
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('active', 'complete')),
    scope_digest TEXT NOT NULL REFERENCES payload_blobs(digest),
    total_tasks INTEGER NOT NULL CHECK (total_tasks >= 0),
    completed_tasks INTEGER NOT NULL DEFAULT 0
        CHECK (completed_tasks >= 0 AND completed_tasks <= total_tasks),
    request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    CHECK ((status = 'active' AND completed_at IS NULL) OR (status = 'complete' AND completed_at IS NOT NULL))
);

CREATE UNIQUE INDEX one_active_maintenance_job
ON maintenance_jobs(status) WHERE status = 'active';

CREATE TABLE maintenance_tasks (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES maintenance_jobs(id) ON DELETE CASCADE,
    task_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    resource_number INTEGER CHECK (resource_number IS NULL OR resource_number > 0),
    input_digest TEXT NOT NULL REFERENCES payload_blobs(digest),
    completed_at TEXT,
    outcome TEXT CHECK (
        outcome IN ('complete', 'null', 'partial', 'forbidden', 'unavailable')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_attempt_from TEXT,
    last_attempt_until TEXT,
    last_error TEXT,
    UNIQUE(job_id, task_key),
    CHECK ((completed_at IS NULL AND outcome IS NULL) OR (completed_at IS NOT NULL AND outcome IS NOT NULL)),
    CHECK (last_attempt_from IS NOT NULL OR last_attempt_until IS NULL),
    CHECK (last_attempt_until IS NULL OR last_attempt_from <= last_attempt_until)
);

CREATE INDEX pending_maintenance_tasks
ON maintenance_tasks(job_id, completed_at, id);

CREATE TABLE commit_reference_index (
    observation_id INTEGER NOT NULL REFERENCES fact_observations(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    sha TEXT NOT NULL CHECK (length(sha) IN (40, 64)),
    PRIMARY KEY(observation_id, ordinal)
) WITHOUT ROWID;

CREATE INDEX commit_reference_index_sha
ON commit_reference_index(sha, observation_id, ordinal);
"""

_FACT_RECORDS_VIEW = """
CREATE VIEW fact_records AS
SELECT
    o.id,
    o.batch_id,
    b.publication_key,
    b.kind AS batch_kind,
    b.cycle_id,
    b.task_id,
    mt.job_id AS maintenance_job_id,
    b.maintenance_task_id,
    b.published_at,
    o.ordinal,
    o.family,
    o.schema_version,
    o.subject_key,
    o.resource_number,
    o.source_digest,
    o.origin,
    o.observed_from,
    o.observed_until,
    o.coverage,
    o.payload_digest,
    p.codec,
    p.raw_size,
    p.payload
FROM fact_observations AS o
JOIN fact_batches AS b ON b.id = o.batch_id
LEFT JOIN maintenance_tasks AS mt ON mt.id = b.maintenance_task_id
JOIN payload_blobs AS p ON p.digest = o.payload_digest
"""
