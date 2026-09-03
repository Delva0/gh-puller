"""GitHub Issue/PR 原始事实的增量观测归档。

“拉取到目标时刻 T”表示在 T 到达后完成一次最终观测，不承诺 T 的历史快照，也不
承诺发现 GitHub 未通过父对象或评论变化信号暴露的更新；实际完成时刻 C 单独记录。
每个 T 首次成功时原子发布一个 SQLite run，重试返回原 run；可检测的不完整响应不
推进水位。事实库无损保留拉取器实际取得的原始响应与 tombstone，可由
``iter_versions`` 完全离线重建已观测历史，并由 ``iter_heads`` 直接读取当前状态。

拉取算法、数据布局、恢复语义和可执行入口见 ``docs/github-puller.md``。
"""

from .client import GitHubAPI, GitHubAPIError
from .progress import ConsoleProgress, ProgressObserver, PullProgress, RateQuota
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
    "RateQuota",
    "incremental_pull",
    "iter_heads",
    "iter_runs",
    "iter_versions",
]
