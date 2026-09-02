"""Shared generator failure type."""

from typing import Any


class RequestFailedError(Exception):
    """Report a provider failure with a caller-readable detail string."""

    def __init__(self, detail: Any):
        super().__init__(detail)
        self.detail = str(detail)
