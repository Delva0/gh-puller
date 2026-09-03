"""GitHub Issue/PR 原始事实的增量观测归档。

“拉取到目标时刻 T”表示在 T 到达后完成一次最终观测，不承诺 T 的历史快照，也不
承诺发现 GitHub 未通过父对象或评论变化信号暴露的更新；实际完成时刻 C 单独记录。
每个 T 首次成功时原子发布一个 SQLite run，重试返回原 run；限流、网络错误和 HTTP
5xx 在当前调用内等待恢复，取消或不可恢复错误留下可续跑的 pending run。可检测的不
完整响应不推进水位。归档由 SQLite 语义事实库和同名 ``.git`` 对象库组成：前者
无损保留 API 响应与直接观测到的 tombstone，后者固定 PR 的 base/head Git 对象。
静默删除保留为最后一次观测状态。``iter_versions`` 可完全离线重建已观测历史，
``iter_heads`` 可直接读取当前状态。

拉取算法、数据布局、恢复语义和可执行入口见 ``docs/github-puller.md``。
"""

from .client import GitHubAPI, GitHubAPIError
from .git_store import GitStoreError, git_store_path
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
    "GitStoreError",
    "IncompleteGitHubDataError",
    "ProgressObserver",
    "PullProgress",
    "PullResult",
    "RateQuota",
    "git_store_path",
    "incremental_pull",
    "iter_heads",
    "iter_runs",
    "iter_versions",
]
