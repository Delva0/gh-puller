"""Define caller-visible failures at the GitHub collection boundary.

The client produces API failures, while the syncer interprets status codes for directly
observed missing parents. Recovery and archive transactions live elsewhere.
"""


class GitHubAPIError(RuntimeError):
    """Report an unrecoverable GitHub response or inconsistent data.

    Args:
        message: Operator-facing failure description.
        status_code: HTTP status or equivalent operation status. Local validation and
            unclassified GraphQL failures use ``None``.
        url: Final URL of the failed HTTP request, or ``None`` for local validation.
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
