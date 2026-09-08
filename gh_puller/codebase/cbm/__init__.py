"""Expose stable CBM contracts while keeping backend mechanics private.

The package facade is the supported integration boundary. MCP, CLI, native
helper, and build-runner implementations remain private sibling modules.
"""

from ._runner import CBMRunner
from .binary import CBMBinary, CBMBinaryError, resolve_cbm_binary
from .client import ArchiveGraph, CBMClient, CBMTransportError, GraphTarget, default_cbm_cache
from .plan import BuildPlan, IncrementalConfig, IncrementalConfigError

__all__ = [
    "ArchiveGraph",
    "BuildPlan",
    "CBMBinary",
    "CBMBinaryError",
    "CBMClient",
    "CBMRunner",
    "CBMTransportError",
    "GraphTarget",
    "IncrementalConfig",
    "IncrementalConfigError",
    "default_cbm_cache",
    "resolve_cbm_binary",
]
