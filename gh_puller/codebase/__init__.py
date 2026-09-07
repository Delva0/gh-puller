"""Build and read durable, random-access code graph archives.

The package owns the archive format and user-facing build API.  CBM executable
optimization and acceptance live in the persistent-digraph laboratory; this package
only resolves a promoted, content-addressed executable through :mod:`.binary`.
"""

from .archive import Archive, ArchiveError, ArchiveWriter
from .binary import CBMBinary, CBMBinaryError, resolve_cbm_binary
from .cbm_build import BuildError, BuildOptions, build_archive
from .cbm_sdk import CBMClient, default_cbm_cache
from .cbm_transport import CBMTransportError
from .incremental_config import IncrementalConfig, IncrementalConfigError

__all__ = [
    "Archive",
    "ArchiveError",
    "ArchiveWriter",
    "BuildError",
    "BuildOptions",
    "CBMBinary",
    "CBMBinaryError",
    "CBMClient",
    "CBMTransportError",
    "IncrementalConfig",
    "IncrementalConfigError",
    "build_archive",
    "default_cbm_cache",
    "resolve_cbm_binary",
]
