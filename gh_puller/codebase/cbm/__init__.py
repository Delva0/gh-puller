"""Expose the CBM backend and its KGA integration.

The client, runner, and transport modules model CBM independently of KGA.
The archive module adapts shared KGA snapshots into immutable CBM stores.
"""

from .archive import CBMArchiveAdapter, CBMArchiveStore
from .binary import CBMBinary, CBMBinaryError, resolve_cbm_binary
from .client import (
    CBMClient,
    CBMTransportError,
    GraphHandle,
    default_cbm_cache,
)
from .plan import BuildPlan, IncrementalConfig, IncrementalConfigError
from .runner import CBMRunner

__all__ = [
    "BuildPlan",
    "CBMArchiveAdapter",
    "CBMArchiveStore",
    "CBMBinary",
    "CBMBinaryError",
    "CBMClient",
    "CBMRunner",
    "CBMTransportError",
    "GraphHandle",
    "IncrementalConfig",
    "IncrementalConfigError",
    "default_cbm_cache",
    "resolve_cbm_binary",
]
