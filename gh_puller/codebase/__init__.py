"""Expose public APIs for building and querying durable code graph archives.

The package owns KGA persistence and repository build orchestration. Its
``cbm`` subpackage contains the format-independent CBM client boundary.
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
    BuildPlan,
    CBMBinary,
    CBMBinaryError,
    CBMClient,
    CBMRunner,
    CBMTransportError,
    GraphHandle,
    IncrementalConfig,
    IncrementalConfigError,
    default_cbm_cache,
    resolve_cbm_binary,
)
from .cbm_archive import CBMArchiveAdapter, CBMArchiveStore
from .store import GraphReader

__all__ = [
    "Archive",
    "ArchiveError",
    "ArchiveWriter",
    "BuildError",
    "BuildOptions",
    "BuildPlan",
    "CBMArchiveAdapter",
    "CBMArchiveStore",
    "CBMBinary",
    "CBMBinaryError",
    "CBMClient",
    "CBMRunner",
    "CBMTransportError",
    "CommitBuildResult",
    "CommitTarget",
    "GraphHandle",
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
