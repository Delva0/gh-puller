"""Test repository acquisition used by the DeepWiki engine."""

import pytest

from gh_puller.utils import Repo, _clone_url_with_token, _path_is_url


@pytest.mark.integration
def test_download_failure_hides_token():
    """Raise ValueError without exposing the token when an unreachable clone fails."""
    secret = "ghp_SECRET_TOKEN_123"  # noqa: S105 - Deliberately fake test credential.
    repo = Repo("https://127.0.0.1:1/foo/bar.git", "github", access_token=secret)
    with pytest.raises(ValueError, match="unable to access") as ei:
        repo.download()
    assert secret not in str(ei.value)


def test_clone_url_with_token():
    assert _path_is_url("https://github.com/a/b") is True
    assert _path_is_url("/local/dir") is False
    github = _clone_url_with_token("https://github.com/a/b.git", "github", "tok")
    assert github.startswith("https://tok@github.com/a/b.git")
    gitlab = _clone_url_with_token("https://gitlab.com/a/b.git", "gitlab", "tok")
    assert gitlab.startswith("https://oauth2:tok@gitlab.com/a/b.git")
    bb = _clone_url_with_token("https://bitbucket.org/a/b.git", "bitbucket", "ATCTTabc")
    assert bb.startswith("https://x-bitbucket-api-token-auth:ATCTTabc@bitbucket.org/a/b.git")
