"""Provide small utilities shared by codebase adapters.

The module contains shared executable identity and resource-observation
contracts. Domain protocols and lifecycles remain with their owning adapters.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping


class NativeExecutableError(RuntimeError):
    """Report failed discovery or authentication of a native executable."""


class ResourceMonitorLike(Protocol):
    """Receive process and resource observations from an adapter."""

    exceeded: bool

    def add_child(self, pid: int) -> None: ...

    def remove_child(self, pid: int) -> None: ...

    def sample(self) -> None: ...


class NullResourceMonitor:
    """Discard process and resource observations."""

    exceeded = False

    def add_child(self, pid: int) -> None:
        return

    def remove_child(self, pid: int) -> None:
        return

    def sample(self) -> None:
        return


@dataclass(frozen=True, slots=True)
class NativeExecutable:
    """An executable pinned to the bytes inspected before startup."""

    path: Path
    sha256: str
    size: int
    version: str
    source: str
    _device: int
    _inode: int
    _mtime_ns: int

    def verify_unchanged(self) -> None:
        """Reject replacement of the executable before it is started."""
        try:
            status = self.path.stat()
        except OSError as exc:
            raise NativeExecutableError(f"native executable disappeared: {self.path}") from exc
        identity = (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)
        expected = (self._device, self._inode, self.size, self._mtime_ns)
        if identity != expected:
            raise NativeExecutableError(f"native executable changed after resolution: {self.path}")

    def provenance(self) -> dict[str, object]:
        """Return stable executable identity for downstream metadata."""
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "bytes": self.size,
            "version": self.version,
            "source": self.source,
        }


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def resolve_native_executable(
    executable: str | Path | NativeExecutable | None,
    *,
    environ: Mapping[str, str],
    environment_key: str,
    local_name: str,
    version_prefix: str,
) -> NativeExecutable:
    """Resolve and authenticate one native executable.

    Args:
        executable: Explicit path, command name, pinned identity, or ``None``.
        environ: Complete child environment used for lookup and inspection.
        environment_key: Variable naming the executable when no override is supplied.
        local_name: Executable name used for local-build and ``PATH`` lookup.
        version_prefix: Required first-line prefix from ``--version``.

    Returns:
        Executable identity pinned to its current inode and bytes.

    Raises:
        NativeExecutableError: No valid executable can be resolved.
    """
    if isinstance(executable, NativeExecutable):
        executable.verify_unchanged()
        return executable
    configured = executable if executable is not None else environ.get(environment_key)
    source = "explicit" if executable is not None else f"environment:{environment_key}"
    candidate: Path | None = None
    if configured is not None:
        raw = os.fspath(configured)
        located = shutil.which(raw, path=environ.get("PATH")) if os.sep not in raw else None
        candidate = Path(located or raw).expanduser().resolve()
    else:
        local = Path(__file__).resolve().parents[2] / "build" / "native" / "bin" / local_name
        located = shutil.which(local_name, path=environ.get("PATH"))
        candidate = local if local.exists() else Path(located).resolve() if located else None
        source = "local-build" if local.exists() else "PATH"
    if candidate is None:
        raise NativeExecutableError(
            f"no native executable: pass its path, set {environment_key}, or run make native",
        )
    try:
        status = candidate.stat()
    except OSError as exc:
        raise NativeExecutableError(f"native executable does not exist: {candidate}") from exc
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise NativeExecutableError(f"native executable is not executable: {candidate}")
    try:
        result = subprocess.run(
            [str(candidate), "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
            env=dict(environ),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeExecutableError(f"cannot execute native executable {candidate}: {exc}") from exc
    version = result.stdout.strip().splitlines()
    if result.returncode or not version or not version[0].startswith(version_prefix):
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise NativeExecutableError(f"invalid native executable {candidate}: {detail}")
    return NativeExecutable(
        candidate,
        _hash_file(candidate),
        status.st_size,
        version[0],
        source,
        status.st_dev,
        status.st_ino,
        status.st_mtime_ns,
    )
