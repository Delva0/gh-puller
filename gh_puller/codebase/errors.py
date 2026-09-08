"""Define build failures shared by the commit and repository pipelines."""


class BuildError(Exception):
    """A requested graph build cannot produce a trustworthy archive generation."""

