"""Expose stable CBM contracts while keeping transport mechanics private.

The package facade is the supported integration boundary. MCP, CLI, native
helper, and build-runner implementations remain private sibling modules.
"""

from ._runner import CBMRunner
from .binary import CBMBinary, CBMBinaryError, resolve_cbm_binary
from .client import (
    CBMClient,
    CBMTransportError,
    GraphHandle,
    default_cbm_cache,
)
from .plan import BuildPlan, IncrementalConfig, IncrementalConfigError

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
