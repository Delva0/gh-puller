"""Serialize writers that mutate one SQLite and Git archive pair.

The lock identity is derived only from the canonical SQLite destination. Pulling and
format migration share this boundary; readers remain outside it.
"""

from __future__ import annotations

import asyncio
import fcntl
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class ArchiveLockedError(RuntimeError):
    """Another process owns the archive writer lock."""


@asynccontextmanager
async def archive_lock(destination: Path, *, wait: bool = True) -> AsyncIterator[None]:
    """Acquire the single-writer lock for an archive pair.

    Args:
        destination: SQLite archive path that identifies the pair.
        wait: Wait for the active writer when true; otherwise fail immediately.

    Raises:
        ArchiveLockedError: Another writer owns the lock and waiting is disabled.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    path = destination.parent / f".{destination.name}.lock"
    file = path.open("a+")
    try:
        while True:
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait:
                    raise ArchiveLockedError(f"archive writer is active: {destination}") from None
                await asyncio.sleep(0.1)
        yield
    finally:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        file.close()
