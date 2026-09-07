"""Incrementally observe and archive GitHub issues, pull requests, and Git objects.

Each complete fact collection is appended after its real read window closes. Sync
cycles own discovery and recovery checkpoints, while maintenance jobs perform targeted
refreshes. Historical reads select observations by time instead of overwriting a
repository snapshot. Silent deletions and changes without GitHub signals are best
effort; once a parent item is selected, all promised collections are observed again.
"""

from .client import GitHubAPI
from .errors import GitHubAPIError
from .git_store import GitStoreError, TransientGitStoreError, git_store_path
from .maintenance import (
    REFRESH_FAMILIES,
    GitHubMaintainer,
    MaintenanceResult,
    backfill,
    refresh,
)
from .observations import (
    Coverage,
    FactObservation,
    MaintenanceJob,
    MaintenanceTask,
    ObservationArchive,
    Origin,
    SyncCycle,
    SyncTask,
    iter_current_facts,
    iter_facts_as_of,
    iter_observations,
)
from .progress import ConsoleProgress, ProgressObserver, RateQuota, SyncProgress
from .syncer import (
    GitHubSyncConfig,
    GitHubSyncer,
    IncompleteGitHubDataError,
    SyncResult,
    sync,
)

__all__ = [
    "REFRESH_FAMILIES",
    "ConsoleProgress",
    "Coverage",
    "FactObservation",
    "GitHubAPI",
    "GitHubAPIError",
    "GitHubMaintainer",
    "GitHubSyncConfig",
    "GitHubSyncer",
    "GitStoreError",
    "IncompleteGitHubDataError",
    "MaintenanceJob",
    "MaintenanceResult",
    "MaintenanceTask",
    "ObservationArchive",
    "Origin",
    "ProgressObserver",
    "RateQuota",
    "SyncCycle",
    "SyncProgress",
    "SyncResult",
    "SyncTask",
    "TransientGitStoreError",
    "backfill",
    "git_store_path",
    "iter_current_facts",
    "iter_facts_as_of",
    "iter_observations",
    "refresh",
    "sync",
]
