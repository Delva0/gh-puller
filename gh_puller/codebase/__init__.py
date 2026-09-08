"""Build and read durable, random-access code graph archives.

The package owns the archive format and user-facing build API.  CBM executable
optimization and acceptance live in the persistent-digraph laboratory; this package
only resolves a promoted, content-addressed executable through :mod:`.binary`.
"""

from .archive import Archive, ArchiveError, ArchiveWriter, KGARecorder
from .binary import CBMBinary, CBMBinaryError, resolve_cbm_binary
from .build_plan import BuildPlan, IncrementalConfig, IncrementalConfigError
from .cbm_build import (
    BuildError,
    BuildOptions,
    CommitBuildResult,
    CommitTarget,
    build_archive,
    build_commit,
    build_repository,
)
from .cbm_runner import CBMRunner
from .cbm_sdk import CBMClient, CBMTransportError, default_cbm_cache
from .store import GraphReader

__all__ = [
    "Archive",
    "ArchiveError",
    "ArchiveWriter",
    "BuildError",
    "BuildOptions",
    "BuildPlan",
    "CBMBinary",
    "CBMBinaryError",
    "CBMClient",
    "CBMRunner",
    "CBMTransportError",
    "CommitBuildResult",
    "CommitTarget",
    "GraphReader",
    "IncrementalConfig",
    "IncrementalConfigError",
    "KGARecorder",
    "build_archive",
    "build_commit",
    "build_repository",
    "default_cbm_cache",
    "resolve_cbm_binary",
]
