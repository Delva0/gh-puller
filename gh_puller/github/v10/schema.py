"""Define the fine-grained GitHub observation archive schema.

The format separates immediately published semantic observations from recoverable
sync-cycle state. Observation time describes source reads; cycle checkpoints only
bound later discovery and never act as public snapshot versions.
"""

VERSION = "10"
GIT_LAYOUT_VERSION = "0"

FACT_SCHEMAS = {
    "catalog-item": 1,
    "commit-object": 1,
    "commit-references": 1,
    "git-refs": 1,
    "issue": 1,
    "issue-comment-reactions": 1,
    "issue-comments": 1,
    "issue-events": 1,
    "issue-reactions": 1,
    "issue-relations": 1,
    "issue-timeline": 1,
    "pull": 1,
    "pull-closing-issues": 1,
    "pull-commits": 1,
    "pull-git": 1,
    "pull-requested-reviewers": 1,
    "pull-review-comment-reactions": 1,
    "pull-review-threads": 1,
    "pull-reviews": 1,
}

SCHEMA = """
CREATE TABLE archive_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE payload_blobs (
    digest TEXT PRIMARY KEY,
    codec TEXT NOT NULL CHECK (codec = 'zlib-json-v1'),
    raw_size INTEGER NOT NULL CHECK (raw_size >= 0),
    payload BLOB NOT NULL
) WITHOUT ROWID;

CREATE TABLE fact_schemas (
    family TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    PRIMARY KEY(family, version)
) WITHOUT ROWID;

CREATE TABLE sync_cycles (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    checkpoint_from TEXT,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('active', 'complete')),
    discovery_started INTEGER NOT NULL DEFAULT 0 CHECK (discovery_started IN (0, 1)),
    discovery_cursor TEXT,
    discovery_complete INTEGER NOT NULL DEFAULT 0 CHECK (discovery_complete IN (0, 1)),
    discovery_pages INTEGER NOT NULL DEFAULT 0 CHECK (discovery_pages >= 0),
    discovered_items INTEGER NOT NULL DEFAULT 0 CHECK (discovered_items >= 0),
    request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    CHECK ((status = 'active' AND completed_at IS NULL) OR (status = 'complete' AND completed_at IS NOT NULL)),
    CHECK (discovery_complete = 0 OR discovery_started = 1)
);

CREATE UNIQUE INDEX one_active_sync_cycle
ON sync_cycles(status) WHERE status = 'active';

CREATE TABLE sync_tasks (
    id INTEGER PRIMARY KEY,
    cycle_id INTEGER NOT NULL REFERENCES sync_cycles(id) ON DELETE CASCADE,
    task_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    resource_number INTEGER CHECK (resource_number IS NULL OR resource_number > 0),
    input_digest TEXT NOT NULL REFERENCES payload_blobs(digest),
    completed_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    UNIQUE(cycle_id, task_key)
);

CREATE INDEX pending_sync_tasks
ON sync_tasks(cycle_id, completed_at, id);

CREATE TABLE fact_batches (
    id INTEGER PRIMARY KEY,
    publication_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('sync', 'import', 'refresh')),
    cycle_id INTEGER REFERENCES sync_cycles(id),
    task_id INTEGER REFERENCES sync_tasks(id),
    published_at TEXT NOT NULL,
    fact_count INTEGER NOT NULL CHECK (fact_count > 0),
    CHECK (task_id IS NULL OR cycle_id IS NOT NULL)
);

CREATE UNIQUE INDEX one_fact_batch_per_task
ON fact_batches(task_id) WHERE task_id IS NOT NULL;

CREATE TABLE fact_observations (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES fact_batches(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    family TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    subject_key TEXT NOT NULL,
    resource_number INTEGER CHECK (resource_number IS NULL OR resource_number > 0),
    source_digest TEXT REFERENCES payload_blobs(digest),
    origin TEXT NOT NULL CHECK (origin IN ('api', 'git', 'derived', 'import')),
    observed_from TEXT NOT NULL,
    observed_until TEXT NOT NULL,
    coverage TEXT NOT NULL CHECK (
        coverage IN ('complete', 'null', 'partial', 'forbidden', 'unavailable')
    ),
    payload_digest TEXT NOT NULL REFERENCES payload_blobs(digest),
    FOREIGN KEY(family, schema_version) REFERENCES fact_schemas(family, version),
    UNIQUE(batch_id, ordinal),
    UNIQUE(batch_id, family, subject_key),
    CHECK (observed_from <= observed_until)
);

CREATE INDEX fact_observations_subject
ON fact_observations(family, subject_key, observed_until DESC, observed_from DESC, id DESC);

CREATE INDEX fact_observations_resource
ON fact_observations(resource_number, id) WHERE resource_number IS NOT NULL;

CREATE TABLE fact_heads (
    family TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    observation_id INTEGER NOT NULL REFERENCES fact_observations(id),
    PRIMARY KEY(family, subject_key)
) WITHOUT ROWID;

CREATE VIEW fact_records AS
SELECT
    o.id,
    o.batch_id,
    b.publication_key,
    b.kind AS batch_kind,
    b.cycle_id,
    b.task_id,
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
JOIN payload_blobs AS p ON p.digest = o.payload_digest;

CREATE VIEW current_facts AS
SELECT
    h.family,
    h.subject_key,
    o.id AS observation_id,
    o.batch_id,
    o.schema_version,
    o.resource_number,
    o.source_digest,
    o.origin,
    o.observed_from,
    o.observed_until,
    o.coverage,
    o.payload_digest
FROM fact_heads AS h
JOIN fact_observations AS o ON o.id = h.observation_id;
"""
