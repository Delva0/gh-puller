"""GitHub Issue/PR 原始事实的增量全量化归档。

“拉取到目标时刻 T”表示完成覆盖闭合，不承诺 T 的历史快照；实际完成时刻 C 单独
记录。每个 T 首次成功时原子发布一个 SQLite run，重试返回原 run；可检测的不完整
响应不推进水位。事实库保留原始对象变化与 tombstone，可由
``iter_versions`` 完全离线重建下游数据，并由 ``iter_heads`` 直接读取当前状态。

拉取算法、数据布局、恢复语义和可执行入口见 ``docs/github-puller.md``。
"""

from .client import GitHubAPI, GitHubAPIError
from .progress import ConsoleProgress, ProgressObserver, PullProgress
from .puller import (
    GitHubPullConfig,
    GitHubPuller,
    IncompleteGitHubDataError,
    PullResult,
    incremental_pull,
)
from .store import ArchivedHead, ArchivedRun, ArchivedVersion, iter_heads, iter_runs, iter_versions

__all__ = [
    "ArchivedHead",
    "ArchivedRun",
    "ArchivedVersion",
    "ConsoleProgress",
    "GitHubAPI",
    "GitHubAPIError",
    "GitHubPullConfig",
    "GitHubPuller",
    "IncompleteGitHubDataError",
    "ProgressObserver",
    "PullProgress",
    "PullResult",
    "incremental_pull",
    "iter_heads",
    "iter_runs",
    "iter_versions",
]
