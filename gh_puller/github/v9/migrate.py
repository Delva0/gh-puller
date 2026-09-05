"""Upgrade a stopped version-eight archive to additive fact publication.

Migration is local and network-free. Existing payloads, resource rows, pending pull
work, and Git refs remain unchanged; only the version-nine tables are added.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..locking import archive_lock
from .schema import GIT_LAYOUT_VERSION, SCHEMA, VERSION


class ArchiveMigrationError(RuntimeError):
    """The archive cannot be migrated without violating its stored contract."""


@dataclass(frozen=True, slots=True)
class MigrationResult:
    database: Path  # Canonical SQLite path.
    repository: str  # Bound GitHub owner/repo.
    changed: bool  # False when the archive was already current.


async def migrate_archive(database: Path) -> MigrationResult:
    """Migrate one stopped version-eight archive without rewriting stored facts.

    Args:
        database: SQLite archive whose pending work and payload identities are kept.

    Returns:
        Canonical archive identity and whether schema metadata changed.

    Raises:
        ArchiveMigrationError: The source schema, repository, or Git layout is invalid.
        ArchiveLockedError: A puller or another migration owns the archive writer lock.
    """
    destination = await asyncio.to_thread(Path(database).resolve)
    async with archive_lock(destination, wait=False):
        return await asyncio.to_thread(_migrate_archive, destination)


def _migrate_archive(database: Path) -> MigrationResult:
    if not database.is_file():
        raise ArchiveMigrationError(f"archive database does not exist: {database}")
    connection = sqlite3.connect(database)
    try:
        metadata = dict(connection.execute("SELECT key, value FROM archive_meta"))
        repository = metadata.get("repository")
        if not isinstance(repository, str):
            raise ArchiveMigrationError("archive has no repository identity")
        source_version = metadata.get("schema_version")
        if source_version == VERSION:
            _validate_layout(metadata)
            connection.executescript(SCHEMA)
            return MigrationResult(database, repository, False)
        if source_version != "8":
            raise ArchiveMigrationError(f"unsupported source schema {source_version!r}")
        _validate_layout(metadata)
        connection.executescript(SCHEMA)
        connection.execute(
            "UPDATE archive_meta SET value = ? WHERE key = 'schema_version'",
            (VERSION,),
        )
        connection.commit()
        return MigrationResult(database, repository, True)
    except sqlite3.Error as exc:
        raise ArchiveMigrationError(str(exc)) from exc
    finally:
        connection.close()


def _validate_layout(metadata: dict[str, str]) -> None:
    layout = metadata.get("git_layout_version")
    if layout != GIT_LAYOUT_VERSION:
        raise ArchiveMigrationError(f"unsupported Git layout {layout!r}")
