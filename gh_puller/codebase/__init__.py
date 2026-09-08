"""Expose public APIs for building and querying durable code graph archives.

The package owns KGA persistence and repository build orchestration. Its
``cbm`` subpackage contains the unified client boundary while hiding daemon and
native protocol implementations.
"""

from .archive import Archive, ArchiveError, ArchiveWriter, KGARecorder
from .build import (
    BuildError,
    BuildOptions,
    CommitBuildResult,
    CommitTarget,
    build_archive,
    build_commit,
    build_repository,
)
from .cbm import (
    ArchiveGraph,
    BuildPlan,
    CBMBinary,
    CBMBinaryError,
    CBMClient,
    CBMRunner,
    CBMTransportError,
    GraphTarget,
    IncrementalConfig,
    IncrementalConfigError,
    default_cbm_cache,
    resolve_cbm_binary,
)
from .store import GraphReader

__all__ = [
    "Archive",
    "ArchiveError",
    "ArchiveGraph",
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
    "GraphTarget",
    "IncrementalConfig",
    "IncrementalConfigError",
    "KGARecorder",
    "build_archive",
    "build_commit",
    "build_repository",
    "default_cbm_cache",
    "resolve_cbm_binary",
]
