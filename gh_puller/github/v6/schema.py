"""提供第六版 GitHub SQLite schema 与一次性前序版本迁移。"""

VERSION = "6"

SCHEMA = """
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

CREATE TABLE IF NOT EXISTS bundle_http_cache (
    bundle_digest TEXT PRIMARY KEY REFERENCES payload_blobs(digest),
    cache_digest TEXT NOT NULL,
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
    request_count INTEGER NOT NULL DEFAULT 0,
    changed_items INTEGER,
    catalog_items INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS one_pending_pull
ON pull_runs(status) WHERE status = 'pending';

CREATE UNIQUE INDEX IF NOT EXISTS one_pull_target
ON pull_runs(target_at);

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

CREATE TABLE IF NOT EXISTS pull_passes (
    run_id INTEGER PRIMARY KEY REFERENCES pull_runs(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (name IN ('prefetch', 'closing')),
    cutoff_at TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('delta', 'full')),
    prepared INTEGER NOT NULL DEFAULT 0 CHECK (prepared IN (0, 1)),
    catalog_started INTEGER NOT NULL DEFAULT 0 CHECK (catalog_started IN (0, 1)),
    catalog_complete INTEGER NOT NULL DEFAULT 0 CHECK (catalog_complete IN (0, 1)),
    next_url TEXT,
    catalog_pages INTEGER NOT NULL DEFAULT 0,
    catalog_items INTEGER NOT NULL DEFAULT 0,
    expected_count INTEGER
);

CREATE TABLE IF NOT EXISTS pull_tasks (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES pull_passes(run_id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    github_id INTEGER,
    kind TEXT CHECK (kind IN ('issue', 'pull')),
    created_at TEXT,
    updated_at TEXT,
    summary_digest TEXT REFERENCES payload_blobs(digest),
    catalog_member INTEGER NOT NULL DEFAULT 0 CHECK (catalog_member IN (0, 1)),
    force_comments INTEGER NOT NULL DEFAULT 0 CHECK (force_comments IN (0, 1)),
    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
    UNIQUE(run_id, number)
);

CREATE INDEX IF NOT EXISTS pull_tasks_pending
ON pull_tasks(run_id, catalog_member, completed, id);
"""

MIGRATE_V5 = """
BEGIN IMMEDIATE;
UPDATE archive_meta SET value = '6' WHERE key = 'schema_version' AND value = '5';
COMMIT;
"""
