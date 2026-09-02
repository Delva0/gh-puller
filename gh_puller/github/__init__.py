"""GitHub Issue/PR 原始数据的增量全量化归档。

“拉取到目标时刻”是覆盖水位而非历史快照；每次成功调用恰好创建一个以目标水位
为 title、以实际完成时刻为 Git 日期的提交。可检测的不完整响应必须失败，禁止推进
水位。算法、数据布局、恢复语义和可执行入口见 ``docs/github-puller.md``。

公共 Python API 是 ``incremental_pull`` 与 ``GitHubPuller.pull``。
"""

from .client import GitHubAPI, GitHubAPIError
from .puller import (
    GitHubPullConfig,
    GitHubPuller,
    IncompleteGitHubDataError,
    PullResult,
    incremental_pull,
)

__all__ = [
    "GitHubAPI",
    "GitHubAPIError",
    "GitHubPullConfig",
    "GitHubPuller",
    "IncompleteGitHubDataError",
    "PullResult",
    "incremental_pull",
]
