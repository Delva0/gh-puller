"""增量观测并离线保存 GitHub Issue、PR 与关联 Git 对象。

每个语义完整的事实集合在真实读取窗口闭合后立即追加到 SQLite；同步 cycle 只负责
发现游标、任务恢复和内部 checkpoint，maintenance job 独立承载定向刷新与补采。
历史读取按观测时刻选择事实，不存在仓库级 target 快照或覆盖更新。静默删除及
GitHub 未暴露变更信号的旧子资源采用尽力而为语义；一旦 Issue/PR 被发现，全部
已承诺集合都会重新观测。
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
from .v11.migrate import MigrationResult, migrate_archive

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
    "MigrationResult",
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
    "migrate_archive",
    "refresh",
    "sync",
]
