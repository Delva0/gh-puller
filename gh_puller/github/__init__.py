"""增量观测并离线保存 GitHub Issue、PR 与关联 Git 对象。

每个语义完整的事实集合在真实读取窗口闭合后立即追加到 SQLite；同步 cycle 只负责
发现游标、任务恢复和内部 checkpoint。历史读取按观测时刻选择事实，不存在仓库级
target 快照或覆盖更新。静默删除及 GitHub 未暴露变更信号的旧子资源采用尽力而为
语义；一旦 Issue/PR 被发现，全部已承诺集合都会重新观测。
"""

from .client import GitHubAPI
from .errors import GitHubAPIError
from .git_store import GitStoreError, TransientGitStoreError, git_store_path
from .observations import (
    Coverage,
    FactObservation,
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
    "ConsoleProgress",
    "Coverage",
    "FactObservation",
    "GitHubAPI",
    "GitHubAPIError",
    "GitHubSyncConfig",
    "GitHubSyncer",
    "GitStoreError",
    "IncompleteGitHubDataError",
    "ObservationArchive",
    "Origin",
    "ProgressObserver",
    "RateQuota",
    "SyncCycle",
    "SyncProgress",
    "SyncResult",
    "SyncTask",
    "TransientGitStoreError",
    "git_store_path",
    "iter_current_facts",
    "iter_facts_as_of",
    "iter_observations",
    "sync",
]
