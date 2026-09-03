"""提供第七版 GitHub SQLite schema 与一次性前序版本迁移。"""

from ..v6 import schema as _v6

VERSION = "7"
SCHEMA = _v6.SCHEMA

MIGRATE_V6 = """
BEGIN IMMEDIATE;
DELETE FROM resource_versions
WHERE run_id IN (SELECT id FROM pull_runs WHERE status = 'pending');
UPDATE pull_tasks
SET completed = 0
WHERE run_id IN (SELECT id FROM pull_runs WHERE status = 'pending');
UPDATE archive_meta SET value = '7' WHERE key = 'schema_version' AND value = '6';
COMMIT;
"""
