"""Incrementally observe and archive GitHub issues, pull requests, and Git objects.

Each complete fact collection is appended after its real read window closes. Sync
cycles own discovery and recovery checkpoints, while maintenance jobs perform targeted
refreshes. Historical reads select observations by time instead of overwriting a
repository snapshot. Silent deletions and changes without GitHub signals are best
effort; once a parent item is selected, all promised collections are observed again.
"""

from .client import GitHubAPI, GitHubAPIError
from .git_store import GitStoreError, TransientGitStoreError
from .maintenance import (
    REFRESH_FAMILIES,
    MaintenanceResult,
    backfill,
    refresh,
)
from .observations import (
    Coverage,
    CurrentFactsView,
    FactObservation,
    Origin,
    iter_current_facts,
    iter_facts_as_of,
    iter_observations,
    open_current_facts,
)
from .syncer import (
    GitHubSyncConfig,
    IncompleteGitHubDataError,
    SyncResult,
    sync,
)
from .utils import ProgressObserver, RateQuota, SyncProgress

__all__ = [
    "REFRESH_FAMILIES",
    "Coverage",
    "CurrentFactsView",
    "FactObservation",
    "GitHubAPI",
    "GitHubAPIError",
    "GitHubSyncConfig",
    "GitStoreError",
    "IncompleteGitHubDataError",
    "MaintenanceResult",
    "Origin",
    "ProgressObserver",
    "RateQuota",
    "SyncProgress",
    "SyncResult",
    "TransientGitStoreError",
    "backfill",
    "iter_current_facts",
    "iter_facts_as_of",
    "iter_observations",
    "open_current_facts",
    "refresh",
    "sync",
]
