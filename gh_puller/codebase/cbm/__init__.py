"""Expose the stable CBM client, build policy, and runner contracts.

The package knows only CBM concepts. Native, MCP, and CLI access paths live in
``transports``; archive formats and adapters remain outside this package.
"""

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
