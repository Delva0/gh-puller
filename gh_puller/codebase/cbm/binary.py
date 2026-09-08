"""Resolve immutable, provenance-bearing executables for CBM client backends.

The accepted manifest is a local deployment pointer, not a build result.  It names
one content-addressed executable that passed the laboratory gates.  A build resolves
that pointer once and records the resulting digest; it never follows manifest updates
while running.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SCHEMA_VERSION = 1
_BINARY_ENV = "GH_PULLER_CODEBASE_CBM_BINARY"
_MANIFEST_ENV = "GH_PULLER_CODEBASE_CBM_MANIFEST"


class CBMBinaryError(RuntimeError):
    """The configured CBM executable cannot be resolved or authenticated."""


@dataclass(frozen=True, slots=True)
class CBMBinary:
    """A CBM executable pinned to the bytes resolved at build startup.

    Attributes:
        path: Absolute executable path used for child processes.
        sha256: Digest of the executable bytes.
        size: Executable size in bytes.
        version: First line reported by ``--version``.
        source: Resolution source such as an explicit path or accepted manifest.
        capabilities: Capabilities attested by an accepted manifest. Runtime probing
            independently checks capabilities needed by a build.
        source_commit: CBM source commit recorded by the accepted manifest.
        validation_commit: Laboratory commit that accepted the executable.
        manifest_path: Accepted manifest, when resolution used one.
    """

    path: Path
    sha256: str
    size: int
    version: str
    source: str
    capabilities: frozenset[str] = frozenset()
    source_commit: str | None = None
    validation_commit: str | None = None
    manifest_path: Path | None = None
    _device: int = 0
    _inode: int = 0
    _mtime_ns: int = 0

    def verify_unchanged(self) -> None:
        """Reject replacement of the executable after startup resolution."""
        try:
            stat = self.path.stat()
        except OSError as exc:
            raise CBMBinaryError(f"CBM binary disappeared after resolution: {self.path}") from exc
        identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        expected = (self._device, self._inode, self.size, self._mtime_ns)
        if identity != expected:
            raise CBMBinaryError(f"CBM binary changed after resolution: {self.path}")

    def provenance(self) -> dict[str, Any]:
        """Return stable build metadata suitable for archive and summary JSON."""
        result: dict[str, Any] = {
            "path": str(self.path),
            "sha256": self.sha256,
            "bytes": self.size,
            "version": self.version,
            "source": self.source,
            "capabilities": sorted(self.capabilities),
        }
        if self.source_commit is not None:
            result["source_commit"] = self.source_commit
        if self.validation_commit is not None:
            result["validation_commit"] = self.validation_commit
        if self.manifest_path is not None:
            result["manifest_path"] = str(self.manifest_path)
        return result


def default_registry(environ: Mapping[str, str] | None = None) -> Path:
    """Return the per-user accepted-CBM registry.

    Args:
        environ: Environment mapping used to resolve ``XDG_DATA_HOME``. ``None`` uses
            the process environment.
    """
    values = os.environ if environ is None else environ
    data_home = values.get("XDG_DATA_HOME")
    root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return root / "gh-puller" / "cbm"


def resolve_cbm_binary(
    binary: str | Path | None = None,
    *,
    manifest: str | Path | None = None,
    registry: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> CBMBinary:
    """Resolve and authenticate one CBM executable using deterministic precedence.

    Args:
        binary: Explicit executable path or command name. This has highest priority.
        manifest: Explicit accepted-manifest path, used when ``binary`` is absent.
        registry: Registry root containing the default ``accepted.json``.
        environ: Environment mapping for resolution. ``None`` uses the process
            environment.

    Returns:
        The executable identity pinned for this process.

    Raises:
        CBMBinaryError: No executable can be found or its identity is invalid.
    """
    values = os.environ if environ is None else environ
    if binary is not None:
        return _from_executable(binary, "explicit")
    if manifest is not None:
        return _from_manifest(Path(manifest).expanduser(), "explicit-manifest")
    if configured := values.get(_BINARY_ENV):
        return _from_executable(configured, f"environment:{_BINARY_ENV}")
    if configured := values.get(_MANIFEST_ENV):
        return _from_manifest(Path(configured).expanduser(), f"environment:{_MANIFEST_ENV}")
    registry_path = Path(registry).expanduser() if registry is not None else default_registry(values)
    accepted = registry_path / "accepted.json"
    if accepted.exists():
        return _from_manifest(accepted, "default-manifest")
    command = shutil.which("codebase-memory-mcp")
    if command is not None:
        return _from_executable(command, "PATH")
    raise CBMBinaryError(
        "no accepted CBM binary: pass --binary, set GH_PULLER_CODEBASE_CBM_BINARY, "
        "or promote one into the local registry",
    )


def _from_executable(value: str | Path, source: str) -> CBMBinary:
    raw = os.fspath(value)
    resolved = shutil.which(raw) if os.sep not in raw else None
    path = Path(resolved or raw).expanduser().resolve()
    return _inspect(path, source=source)


def _from_manifest(path: Path, source: str) -> CBMBinary:
    path = path.resolve()
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CBMBinaryError(f"cannot read CBM manifest {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != _SCHEMA_VERSION:
        raise CBMBinaryError(f"unsupported CBM manifest schema: {path}")
    binary = document.get("binary")
    if not isinstance(binary, dict):
        raise CBMBinaryError(f"CBM manifest has no binary object: {path}")
    relative = binary.get("path")
    expected_digest = binary.get("sha256")
    expected_size = binary.get("bytes")
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise CBMBinaryError(f"CBM manifest binary path must be relative: {path}")
    if not isinstance(expected_digest, str) or _DIGEST.fullmatch(expected_digest) is None:
        raise CBMBinaryError(f"CBM manifest has an invalid SHA-256: {path}")
    if not isinstance(expected_size, int) or expected_size < 1:
        raise CBMBinaryError(f"CBM manifest has an invalid binary size: {path}")
    target = (path.parent / relative).resolve()
    try:
        target.relative_to(path.parent)
    except ValueError as exc:
        raise CBMBinaryError(f"CBM manifest binary escapes its registry: {path}") from exc
    capabilities = document.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        raise CBMBinaryError(f"CBM manifest has invalid capabilities: {path}")
    source_data = document.get("source", {})
    validation = document.get("validation", {})
    if not isinstance(source_data, dict) or not isinstance(validation, dict):
        raise CBMBinaryError(f"CBM manifest has invalid provenance: {path}")
    return _inspect(
        target,
        source=source,
        expected_digest=expected_digest,
        expected_size=expected_size,
        capabilities=frozenset(capabilities),
        source_commit=_optional_string(source_data.get("commit")),
        validation_commit=_optional_string(validation.get("commit")),
        manifest_path=path,
    )


def _inspect(
    path: Path,
    *,
    source: str,
    expected_digest: str | None = None,
    expected_size: int | None = None,
    capabilities: frozenset[str] = frozenset(),
    source_commit: str | None = None,
    validation_commit: str | None = None,
    manifest_path: Path | None = None,
) -> CBMBinary:
    try:
        stat = path.stat()
    except OSError as exc:
        raise CBMBinaryError(f"CBM binary does not exist: {path}") from exc
    if not path.is_file() or not os.access(path, os.X_OK):
        raise CBMBinaryError(f"CBM binary is not an executable file: {path}")
    digest = _hash_file(path)
    if expected_digest is not None and digest != expected_digest:
        raise CBMBinaryError(f"CBM binary digest mismatch: expected {expected_digest}, got {digest}")
    if expected_size is not None and stat.st_size != expected_size:
        raise CBMBinaryError(f"CBM binary size mismatch: expected {expected_size}, got {stat.st_size}")
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CBMBinaryError(f"cannot execute CBM binary {path}: {exc}") from exc
    version = result.stdout.strip().splitlines()
    if result.returncode or not version or "codebase-memory-mcp" not in version[0]:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise CBMBinaryError(f"invalid CBM binary {path}: {detail}")
    return CBMBinary(
        path=path,
        sha256=digest,
        size=stat.st_size,
        version=version[0],
        source=source,
        capabilities=capabilities,
        source_commit=source_commit,
        validation_commit=validation_commit,
        manifest_path=manifest_path,
        _device=stat.st_dev,
        _inode=stat.st_ino,
        _mtime_ns=stat.st_mtime_ns,
    )


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None
