"""Define the GitHub observation archive and independent maintenance jobs.

Fact observations are immutable and globally replayable. Sync cycles own discovery
work; maintenance jobs own explicit refresh and backfill work without advancing the
discovery checkpoint.
"""

VERSION = "11"
GIT_LAYOUT_VERSION = "0"

FACT_SCHEMAS = {
    "catalog-item": frozenset({1}),
    "commit-object": frozenset({1, 2}),
    "commit-references": frozenset({1}),
    "git-refs": frozenset({1}),
    "issue": frozenset({1}),
    "issue-comment-reactions": frozenset({1}),
    "issue-comments": frozenset({1}),
    "issue-events": frozenset({1}),
    "issue-reactions": frozenset({1}),
    "issue-relations": frozenset({1}),
    "issue-timeline": frozenset({1}),
    "pull": frozenset({1}),
    "pull-closing-issues": frozenset({1}),
    "pull-commits": frozenset({1}),
    "pull-git": frozenset({1}),
    "pull-requested-reviewers": frozenset({1}),
    "pull-review-comments": frozenset({1}),
    "pull-review-comment-reactions": frozenset({1}),
    "pull-review-threads": frozenset({1}),
    "pull-reviews": frozenset({1}),
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

CREATE TABLE discovery_items (
    cycle_id INTEGER NOT NULL REFERENCES sync_cycles(id) ON DELETE CASCADE,
    number INTEGER NOT NULL CHECK (number > 0),
    kind TEXT NOT NULL CHECK (kind IN ('issue', 'pull')),
    observed_from TEXT NOT NULL,
    observed_until TEXT NOT NULL,
    summary_digest TEXT NOT NULL REFERENCES payload_blobs(digest),
    PRIMARY KEY(cycle_id, number),
    CHECK (observed_from <= observed_until)
) WITHOUT ROWID;

CREATE TABLE discovery_signals (
    cycle_id INTEGER NOT NULL REFERENCES sync_cycles(id) ON DELETE CASCADE,
    number INTEGER NOT NULL CHECK (number > 0),
    issue_comments INTEGER NOT NULL DEFAULT 0 CHECK (issue_comments IN (0, 1)),
    pull_comments INTEGER NOT NULL DEFAULT 0 CHECK (pull_comments IN (0, 1)),
    PRIMARY KEY(cycle_id, number)
) WITHOUT ROWID;

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

CREATE TABLE fact_batches (
    id INTEGER PRIMARY KEY,
    publication_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('sync', 'import', 'refresh')),
    cycle_id INTEGER REFERENCES sync_cycles(id),
    task_id INTEGER REFERENCES sync_tasks(id),
    maintenance_task_id INTEGER REFERENCES maintenance_tasks(id),
    published_at TEXT NOT NULL,
    fact_count INTEGER NOT NULL CHECK (fact_count > 0),
    CHECK (task_id IS NULL OR cycle_id IS NOT NULL),
    CHECK (task_id IS NULL OR maintenance_task_id IS NULL),
    CHECK (maintenance_task_id IS NULL OR cycle_id IS NULL),
    CHECK (maintenance_task_id IS NULL OR kind = 'refresh')
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

CREATE TABLE commit_reference_index (
    observation_id INTEGER NOT NULL REFERENCES fact_observations(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    sha TEXT NOT NULL CHECK (length(sha) IN (40, 64)),
    PRIMARY KEY(observation_id, ordinal)
) WITHOUT ROWID;

CREATE INDEX commit_reference_index_sha
ON commit_reference_index(sha, observation_id, ordinal);

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
