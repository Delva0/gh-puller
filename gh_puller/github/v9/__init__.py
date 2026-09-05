"""Expose the current GitHub archive schema and migration contract."""

from .migrate import ArchiveMigrationError, MigrationResult, migrate_archive
from .schema import GIT_LAYOUT_VERSION, SCHEMA, VERSION

__all__ = [
    "GIT_LAYOUT_VERSION",
    "SCHEMA",
    "VERSION",
    "ArchiveMigrationError",
    "MigrationResult",
    "migrate_archive",
]
