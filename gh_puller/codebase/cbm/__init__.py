"""Expose stable CBM contracts while keeping backend mechanics private.

The package facade is the supported integration boundary. MCP, CLI, native
helper, and build-runner implementations remain private sibling modules.
"""

from ._runner import CBMRunner
from .client import ArchiveGraph, CBMClient, CBMTransportError, GraphTarget, default_cbm_cache

__all__ = [
    "ArchiveGraph",
    "CBMClient",
    "CBMRunner",
    "CBMTransportError",
    "GraphTarget",
    "default_cbm_cache",
]
