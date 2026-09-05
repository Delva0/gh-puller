"""Define additive publication tables for independently observed fact sets.

The version-eight resource archive remains byte-for-byte intact. These relations add
atomic fact batches, replayable fact versions, successful heads, and durable jobs for
backfill or explicit refresh operations.
"""

from ..v8 import schema as _resource_schema

GIT_LAYOUT_VERSION = _resource_schema.GIT_LAYOUT_VERSION
RESOURCE_SCHEMA = _resource_schema.SCHEMA

VERSION = "9"

SCHEMA = RESOURCE_SCHEMA + """

CREATE TABLE IF NOT EXISTS fact_jobs (
    id INTEGER PRIMARY KEY,
    job_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('backfill', 'refresh')),
    target_at TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'complete')),
    scope_payload_digest TEXT NOT NULL REFERENCES payload_blobs(digest),
    total_tasks INTEGER NOT NULL CHECK (total_tasks >= 0),
    completed_tasks INTEGER NOT NULL DEFAULT 0
        CHECK (completed_tasks >= 0 AND completed_tasks <= total_tasks)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_pending_fact_job
ON fact_jobs(status) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS fact_tasks (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES fact_jobs(id) ON DELETE CASCADE,
    fact_kind TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    resource_number INTEGER,
    source_digest TEXT REFERENCES payload_blobs(digest),
    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
    outcome TEXT CHECK (
        outcome IN ('complete', 'null', 'partial', 'forbidden', 'unavailable', 'failed')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    CHECK ((completed = 0 AND outcome IS NULL) OR (completed = 1 AND outcome IS NOT NULL)),
    UNIQUE(job_id, fact_kind, subject_key)
);

CREATE INDEX IF NOT EXISTS fact_tasks_pending
ON fact_tasks(job_id, completed, id);

CREATE TABLE IF NOT EXISTS fact_batches (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('pull', 'backfill', 'refresh')),
    pull_run_id INTEGER REFERENCES pull_runs(id),
    job_id INTEGER REFERENCES fact_jobs(id),
    published_at TEXT NOT NULL,
    fact_count INTEGER NOT NULL CHECK (fact_count > 0),
    CHECK ((pull_run_id IS NULL) <> (job_id IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS one_pull_fact_batch
ON fact_batches(pull_run_id) WHERE pull_run_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS fact_versions (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES fact_batches(id),
    task_id INTEGER REFERENCES fact_tasks(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    fact_kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    subject_key TEXT NOT NULL,
    resource_number INTEGER,
    source_digest TEXT REFERENCES payload_blobs(digest),
    observed_from TEXT NOT NULL,
    observed_until TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('complete', 'null', 'partial', 'forbidden', 'unavailable', 'failed')
    ),
    payload_digest TEXT NOT NULL REFERENCES payload_blobs(digest),
    UNIQUE(batch_id, ordinal)
);

CREATE INDEX IF NOT EXISTS fact_versions_subject
ON fact_versions(fact_kind, subject_key, id);

CREATE INDEX IF NOT EXISTS fact_versions_resource
ON fact_versions(resource_number, id) WHERE resource_number IS NOT NULL;

DROP INDEX IF EXISTS one_fact_version_per_task;

CREATE TABLE IF NOT EXISTS fact_heads (
    fact_kind TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    latest_version_id INTEGER NOT NULL REFERENCES fact_versions(id),
    successful_version_id INTEGER REFERENCES fact_versions(id),
    PRIMARY KEY(fact_kind, subject_key)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS pending_fact_versions (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES pull_runs(id) ON DELETE CASCADE,
    fact_kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    subject_key TEXT NOT NULL,
    resource_number INTEGER,
    source_digest TEXT REFERENCES payload_blobs(digest),
    observed_from TEXT NOT NULL,
    observed_until TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('complete', 'null', 'partial', 'forbidden', 'unavailable', 'failed')
    ),
    payload_digest TEXT NOT NULL REFERENCES payload_blobs(digest),
    UNIQUE(
        run_id, fact_kind, subject_key, observed_from, observed_until,
        status, payload_digest
    )
);

CREATE INDEX IF NOT EXISTS pending_fact_versions_run
ON pending_fact_versions(run_id, id);

CREATE VIEW IF NOT EXISTS current_facts AS
SELECT
    h.fact_kind,
    h.subject_key,
    v.id AS version_id,
    v.batch_id,
    v.schema_version,
    v.resource_number,
    v.source_digest,
    v.observed_from,
    v.observed_until,
    v.status,
    v.payload_digest
FROM fact_heads AS h
JOIN fact_versions AS v ON v.id = h.successful_version_id;

CREATE VIEW IF NOT EXISTS latest_fact_attempts AS
SELECT
    h.fact_kind,
    h.subject_key,
    v.id AS version_id,
    v.batch_id,
    v.schema_version,
    v.resource_number,
    v.source_digest,
    v.observed_from,
    v.observed_until,
    v.status,
    v.payload_digest
FROM fact_heads AS h
JOIN fact_versions AS v ON v.id = h.latest_version_id;
"""
