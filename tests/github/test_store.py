"""Test GitHub archive payload storage and schema compatibility."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import aiosqlite
import pytest

if TYPE_CHECKING:
    from pathlib import Path

from tests.github._puller_support import (
    _T0,
    Clock,
    FakeAPI,
    _config,
    _puller,
    _rows,
)


@pytest.mark.asyncio
async def test_payload_blobs_are_compressed_and_content_addressed(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    clock = Clock(_T0)
    puller = _puller(_config(archive), api=api, now=clock, sleep=clock.sleep)
    await puller.pull(_T0)
    clock.current += timedelta(minutes=1)

    await puller.pull(clock.current)

    blobs = await _rows(
        archive,
        "SELECT digest, codec, raw_size, length(payload) AS stored_size FROM payload_blobs",
    )
    versions = await _rows(archive, "SELECT id FROM resource_versions")
    assert len(blobs) == 2
    assert len(versions) == 1
    assert {row["codec"] for row in blobs} == {"zlib-json-v1"}
    assert all(len(row["digest"]) == 64 for row in blobs)
    assert any(row["stored_size"] < row["raw_size"] for row in blobs)
    assert await _rows(archive, "PRAGMA integrity_check") == [{"integrity_check": "ok"}]


@pytest.mark.asyncio
async def test_incompatible_fact_archive_schema_is_rejected(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    await _puller(_config(archive), api=api, now=lambda: _T0).pull(_T0)

    db = await aiosqlite.connect(archive)
    try:
        await db.execute(
            "UPDATE archive_meta SET value = '7' WHERE key = 'schema_version'",
        )
        await db.commit()
    finally:
        await db.close()

    with pytest.raises(ValueError, match="unsupported GitHub archive schema"):
        await _puller(
            _config(archive),
            api=api,
            now=lambda: _T0 + timedelta(hours=1),
        ).pull(_T0 + timedelta(hours=1))

    assert await _rows(archive, "SELECT value FROM archive_meta WHERE key = 'schema_version'") == [
        {"value": "7"},
    ]
