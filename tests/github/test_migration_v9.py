"""Test the additive version-nine archive migration."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from typing import TYPE_CHECKING

import pytest

from gh_puller.github.locking import ArchiveLockedError, archive_lock
from gh_puller.github.v8.schema import GIT_LAYOUT_VERSION
from gh_puller.github.v8.schema import SCHEMA as V8_SCHEMA
from gh_puller.github.v9 import ArchiveMigrationError, migrate_archive

if TYPE_CHECKING:
    from pathlib import Path

_REPOSITORY = "acme/widgets"
_TIME = "2026-09-05T12:00:00Z"


def _v8_archive(path: Path) -> tuple[str, bytes]:
    payload = {"kind": "issue", "number": 7, "schema_version": 7}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    with sqlite3.connect(path) as connection:
        connection.executescript(V8_SCHEMA)
        connection.executemany(
            "INSERT INTO archive_meta(key, value) VALUES (?, ?)",
            (
                ("schema_version", "8"),
                ("git_layout_version", GIT_LAYOUT_VERSION),
                ("repository", _REPOSITORY),
            ),
        )
        connection.execute(
            "INSERT INTO payload_blobs VALUES (?, 'zlib-json-v1', ?, ?)",
            (digest, len(raw), zlib.compress(raw)),
        )
        connection.execute(
            """
            INSERT INTO pull_runs(id, target_at, started_at, status)
            VALUES (1, ?, ?, 'pending')
            """,
            (_TIME, _TIME),
        )
        connection.execute(
            """
            INSERT INTO pull_passes(
                run_id, name, cutoff_at, mode, prepared, catalog_started,
                catalog_complete, next_url, catalog_pages, catalog_items
            ) VALUES (1, 'closing', ?, 'full', 1, 1, 0, '/next', 3, 300)
            """,
            (_TIME,),
        )
        connection.execute(
            """
            INSERT INTO pull_tasks(
                run_id, number, github_id, kind, created_at, updated_at,
                summary_digest, catalog_member, completed
            ) VALUES (1, 7, 70, 'issue', ?, ?, ?, 1, 1)
            """,
            (_TIME, _TIME, digest),
        )
    return digest, raw


@pytest.mark.asyncio
async def test_v9_migration_preserves_payload_and_pending_work(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite3"
    digest, raw = _v8_archive(database)

    result = await migrate_archive(database)

    assert result.changed is True
    assert result.repository == _REPOSITORY
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        metadata = dict(connection.execute("SELECT key, value FROM archive_meta"))
        assert metadata["schema_version"] == "9"
        payload = connection.execute(
            "SELECT digest, raw_size, payload FROM payload_blobs WHERE digest = ?",
            (digest,),
        ).fetchone()
        assert payload is not None
        assert payload["raw_size"] == len(raw)
        assert zlib.decompress(payload["payload"]) == raw
        assert dict(connection.execute("SELECT * FROM pull_passes").fetchone())["next_url"] == "/next"
        assert connection.execute("SELECT completed FROM pull_tasks").fetchone()[0] == 1
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            )
        }
        assert {"fact_jobs", "fact_tasks", "fact_batches", "fact_versions", "fact_heads"} <= tables
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    assert (await migrate_archive(database)).changed is False


@pytest.mark.asyncio
async def test_v9_migration_refuses_active_writer(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite3"
    _v8_archive(database)

    async with archive_lock(database):
        with pytest.raises(ArchiveLockedError, match="writer is active"):
            await migrate_archive(database)


@pytest.mark.asyncio
async def test_v9_migration_rejects_noncurrent_source(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite3"
    _v8_archive(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE archive_meta SET value = '7' WHERE key = 'schema_version'",
        )

    with pytest.raises(ArchiveMigrationError, match="unsupported source schema"):
        await migrate_archive(database)
