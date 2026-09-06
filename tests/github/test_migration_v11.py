"""Test the one-time v10 to v11 observation-archive migration."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from typing import TYPE_CHECKING

import pytest

from gh_puller.github.observations import ObservationArchive
from gh_puller.github.v10.schema import FACT_SCHEMAS as V10_FACT_SCHEMAS
from gh_puller.github.v10.schema import SCHEMA as V10_SCHEMA
from gh_puller.github.v11.migrate import migrate_archive

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_v11_migration_preserves_pending_cycle_and_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "facts.sqlite3"
    git_store = tmp_path / "facts.sqlite3.git"
    raw = json.dumps({"number": 7}, separators=(",", ":"), sort_keys=True).encode()
    digest = hashlib.sha256(raw).hexdigest()
    sha = "a" * 40
    reference_raw = json.dumps(
        {
            "source_family": "pull-review-comments",
            "source_observation_id": 12,
            "source_payload_digest": "f" * 64,
            "references": [
                {
                    "field_path": "/value/0/commit_id",
                    "sha": sha,
                    "source_id": 90,
                    "source_kind": "review_comment",
                },
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    reference_digest = hashlib.sha256(reference_raw).hexdigest()
    with sqlite3.connect(database) as connection:
        connection.executescript(V10_SCHEMA)
        connection.executemany(
            "INSERT INTO archive_meta(key, value) VALUES (?, ?)",
            (
                ("schema_version", "10"),
                ("git_layout_version", "0"),
                ("git_store", str(git_store.resolve())),
                ("repository", "acme/widgets"),
            ),
        )
        connection.executemany(
            "INSERT INTO fact_schemas(family, version) VALUES (?, ?)",
            sorted(V10_FACT_SCHEMAS.items()),
        )
        connection.execute(
            "INSERT INTO payload_blobs VALUES (?, 'zlib-json-v1', ?, ?)",
            (digest, len(raw), zlib.compress(raw, level=9)),
        )
        connection.execute(
            "INSERT INTO payload_blobs VALUES (?, 'zlib-json-v1', ?, ?)",
            (
                reference_digest,
                len(reference_raw),
                zlib.compress(reference_raw, level=9),
            ),
        )
        connection.execute(
            "INSERT INTO sync_cycles(id, started_at, status) VALUES (1, ?, 'active')",
            ("2026-09-05T12:00:00.000000Z",),
        )
        connection.execute(
            """
            INSERT INTO sync_tasks(
                cycle_id, task_key, kind, subject_key, resource_number, input_digest
            ) VALUES (1, 'issue:7', 'parent', 'issue:7', 7, ?)
            """,
            (digest,),
        )
        connection.execute(
            """
            INSERT INTO fact_batches(
                id, publication_key, kind, published_at, fact_count
            ) VALUES (1, 'import:references', 'import', ?, 1)
            """,
            ("2026-09-05T12:00:00.000000Z",),
        )
        connection.execute(
            """
            INSERT INTO fact_observations(
                id, batch_id, ordinal, family, schema_version, subject_key,
                resource_number, origin, observed_from, observed_until, coverage,
                payload_digest
            ) VALUES (1, 1, 0, 'commit-references', 1, 'payload:test', 7,
                      'import', ?, ?, 'complete', ?)
            """,
            (
                "2026-09-05T12:00:00.000000Z",
                "2026-09-05T12:00:00.000000Z",
                reference_digest,
            ),
        )
        connection.execute(
            "INSERT INTO fact_heads VALUES ('commit-references', 'payload:test', 1)",
        )

    first = await migrate_archive(database)
    repeated = await migrate_archive(database)

    assert (first.previous_version, first.version, first.changed) == ("10", "11", True)
    assert repeated.changed is False
    with sqlite3.connect(database) as connection:
        assert dict(connection.execute("SELECT key, value FROM archive_meta"))["schema_version"] == "11"
        assert connection.execute("SELECT task_key FROM sync_tasks").fetchone() == ("issue:7",)
        assert connection.execute(
            "SELECT version FROM fact_schemas WHERE family = 'commit-object' ORDER BY version",
        ).fetchall() == [(1,), (2,)]
        indexed = connection.execute(
            "SELECT sha FROM commit_reference_index",
        ).fetchone()
        assert indexed == (sha,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    async with ObservationArchive(database, "acme/widgets", git_store) as archive:
        cycle = await archive.active_cycle()
        assert cycle is not None and cycle.id == 1
        references = [
            value
            async for value in archive.iter_commit_references(1, {sha})
        ]
        assert references[0]["field_path"] == "/value/0/commit_id"
