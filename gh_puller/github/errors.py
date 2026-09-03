"""定义 GitHub 拉取边界中可由调用方识别的失败类型。

本模块不执行恢复或决定归档事务；client 产生 API 失败，puller 根据状态码判断
直接观测到的父对象缺失，其余恢复契约见 ``gh_puller.github``。
"""


class GitHubAPIError(RuntimeError):
    """GitHub 返回不可恢复响应或不一致数据。

    Args:
        message: 面向操作者的失败说明。
        status_code: HTTP 失败状态；本地验证或 GraphQL 失败时为 None。
        url: 失败 HTTP request 的最终 URL；本地验证或 GraphQL 结构失败时为 None。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
