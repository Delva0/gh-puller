"""Verify complete PR Git diffs, pinned references, and repository binding."""

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

import gh_puller.github.git_store as git_store_module
from gh_puller.github.git_store import (
    GitObjectStore,
    GitStoreError,
    TransientGitStoreError,
    git_store_path,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def _git(repository: Path, *arguments: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        input=input_text,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _source_repository(path: Path, files: int) -> tuple[str, str]:
    _git(path.parent, "init", "--initial-branch=main", str(path))
    _git(path, "config", "user.name", "Archive Test")
    _git(path, "config", "user.email", "archive@example.test")
    (path / "README.md").write_text("base\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "--quiet", "-m", "base")
    base = _git(path, "rev-parse", "HEAD")
    changes = path / "changes"
    changes.mkdir()
    for index in range(files):
        (changes / f"{index:04}.txt").write_text(f"{index}\n")
    _git(path, "add", "changes")
    _git(path, "commit", "--quiet", "-m", "large pull")
    head = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/pull/7/head", head)
    _git(path, "reset", "--hard", base)
    return base, head


def _unrelated_pull(path: Path, files: int) -> tuple[str, str]:
    base, _ = _source_repository(path, 1)
    _git(path, "checkout", "--quiet", "--orphan", "unrelated")
    _git(path, "rm", "--quiet", "-rf", ".")
    for index in range(files):
        (path / f"unrelated-{index:02}.txt").write_text(f"{index}\n")
    _git(path, "add", ".")
    _git(path, "commit", "--quiet", "-m", "unrelated root")
    head = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/pull/8/head", head)
    return base, head


def _stored_git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "--git-dir", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _is_fetch(command: Sequence[str]) -> bool:
    return len(command) > 3 and command[1] == "--git-dir" and command[3] == "fetch"


@pytest.mark.asyncio
async def test_git_store_reconstructs_more_than_three_thousand_changed_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    base, head = _source_repository(source, 3_001)
    database = tmp_path / "facts.sqlite3"
    path = git_store_path(database)
    store = GitObjectStore(path, "acme/widgets", str(source))

    await store.prefetch({7: {"head": {"sha": head}}})
    snapshot = await store.capture(
        7,
        {"base": {"sha": base}, "head": {"sha": head}, "merged": False},
    )

    changed = _stored_git(
        path,
        "diff",
        "--name-only",
        snapshot["comparison_ref"],
        snapshot["head_ref"],
    ).splitlines()
    assert snapshot["comparison_kind"] == "merge_base"
    assert snapshot["comparison_sha"] == base
    assert len(changed) == 3_001
    assert changed[0] == "changes/0000.txt"
    assert changed[-1] == "changes/3000.txt"


@pytest.mark.asyncio
async def test_git_store_compares_an_unrelated_root_pull_from_the_empty_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    base, head = _unrelated_pull(source, 17)
    path = git_store_path(tmp_path / "facts.sqlite3")
    store = GitObjectStore(path, "acme/widgets", str(source))

    await store.prefetch({8: {"head": {"sha": head}}})
    snapshot = await store.capture(
        8,
        {"base": {"sha": base}, "head": {"sha": head}, "merged": False},
    )

    changed = _stored_git(
        path,
        "diff",
        "--name-only",
        snapshot["comparison_ref"],
        snapshot["head_ref"],
    ).splitlines()
    assert snapshot["comparison_kind"] == "empty_tree"
    assert _stored_git(path, "cat-file", "-t", snapshot["comparison_ref"]) == "tree"
    assert changed == [f"unrelated-{index:02}.txt" for index in range(17)]


@pytest.mark.asyncio
async def test_git_store_keeps_old_head_after_remote_force_push(tmp_path: Path) -> None:
    source = tmp_path / "source"
    base, first_head = _source_repository(source, 1)
    path = git_store_path(tmp_path / "facts.sqlite3")
    store = GitObjectStore(path, "acme/widgets", str(source))
    await store.prefetch({7: {"head": {"sha": first_head}}})
    first = await store.capture(
        7,
        {"base": {"sha": base}, "head": {"sha": first_head}, "merged": False},
    )
    _git(source, "checkout", "--quiet", "--detach", base)
    (source / "replacement.txt").write_text("replacement\n")
    _git(source, "add", "replacement.txt")
    _git(source, "commit", "--quiet", "-m", "replacement")
    second_head = _git(source, "rev-parse", "HEAD")
    _git(source, "update-ref", "refs/pull/7/head", second_head)

    second = await store.capture(
        7,
        {"base": {"sha": base}, "head": {"sha": second_head}, "merged": False},
    )

    assert _stored_git(path, "rev-parse", first["head_ref"]) == first_head
    assert _stored_git(path, "rev-parse", second["head_ref"]) == second_head
    assert first["head_ref"] != second["head_ref"]


@pytest.mark.asyncio
async def test_git_store_pins_reachable_merge_commit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    base, head = _source_repository(source, 1)
    _git(source, "branch", "feature", head)
    _git(source, "reset", "--hard", base)
    _git(source, "merge", "--quiet", "--no-ff", "feature", "-m", "merge pull")
    merge = _git(source, "rev-parse", "HEAD")
    path = git_store_path(tmp_path / "facts.sqlite3")
    store = GitObjectStore(path, "acme/widgets", str(source))
    await store.prefetch({7: {"head": {"sha": head}}})

    snapshot = await store.capture(
        7,
        {
            "base": {"sha": base},
            "head": {"sha": head},
            "merge_commit_sha": merge,
            "merged": True,
        },
    )

    assert snapshot["landing_sha"] == merge
    assert snapshot["history_preserved"] is True
    assert _stored_git(path, "rev-parse", snapshot["landing_ref"]) == merge


@pytest.mark.asyncio
async def test_git_store_keeps_snapshot_when_merge_commit_is_unreachable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    base, head = _source_repository(source, 1)
    path = git_store_path(tmp_path / "facts.sqlite3")
    store = GitObjectStore(path, "acme/widgets", str(source))
    await store.prefetch({7: {"head": {"sha": head}}})

    snapshot = await store.capture(
        7,
        {
            "base": {"sha": base},
            "head": {"sha": head},
            "merge_commit_sha": "f" * 40,
            "merged": True,
        },
    )

    assert snapshot["base_sha"] == base
    assert snapshot["head_sha"] == head
    assert "landing_ref" not in snapshot
    assert "landing_sha" not in snapshot


@pytest.mark.asyncio
async def test_git_store_marks_comparison_unavailable_when_base_is_unreachable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    missing_base = "f" * 40
    path = git_store_path(tmp_path / "facts.sqlite3")
    store = GitObjectStore(path, "acme/widgets", str(source))
    await store.prefetch({7: {"head": {"sha": head}}})

    snapshot = await store.capture(
        7,
        {
            "base": {"sha": missing_base},
            "head": {"sha": head},
            "merge_commit_sha": head,
            "merged": True,
        },
    )

    assert snapshot == {
        "base_sha": missing_base,
        "comparison_kind": "unavailable",
        "head_ref": f"refs/github-archive/pulls/7/heads/{head}",
        "head_sha": head,
        "history_preserved": True,
        "landing_ref": f"refs/github-archive/pulls/7/landings/{head}",
        "landing_sha": head,
        "unavailable_commits": [missing_base],
    }
    assert _stored_git(path, "rev-parse", snapshot["head_ref"]) == head


@pytest.mark.asyncio
async def test_git_store_marks_comparison_unavailable_when_api_head_is_unreachable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    base, _ = _source_repository(source, 1)
    missing_head = "e" * 40
    path = git_store_path(tmp_path / "facts.sqlite3")
    store = GitObjectStore(path, "acme/widgets", str(source))
    await store.prefetch({7: {"head": {"sha": missing_head}}})

    snapshot = await store.capture(
        7,
        {
            "base": {"sha": base},
            "head": {"sha": missing_head},
            "merged": False,
        },
    )

    assert snapshot == {
        "base_ref": f"refs/github-archive/pulls/7/bases/{base}",
        "base_sha": base,
        "comparison_kind": "unavailable",
        "head_sha": missing_head,
        "history_preserved": None,
        "unavailable_commits": [missing_head],
    }
    assert _stored_git(path, "rev-parse", snapshot["base_ref"]) == base


@pytest.mark.asyncio
async def test_upstream_sync_publishes_native_refs_and_pins_removed_tips(tmp_path: Path) -> None:
    source = tmp_path / "source"
    base, _ = _source_repository(source, 1)
    _git(source, "branch", "same-tip", base)
    _git(source, "tag", "v1", base)
    _git(source, "tag", "same-object", base)
    path = git_store_path(tmp_path / "facts.sqlite3")

    await GitObjectStore(path, "acme/widgets", str(source)).sync_upstream()

    assert _stored_git(path, "rev-parse", "refs/heads/main") == base
    assert _stored_git(path, "rev-parse", "refs/tags/v1") == base
    assert _stored_git(path, "rev-parse", f"refs/github-archive/upstream/heads/{base}") == base
    _git(source, "checkout", "--quiet", "--orphan", "replacement")
    _git(source, "rm", "--quiet", "-rf", ".")
    (source / "replacement.txt").write_text("replacement\n")
    _git(source, "add", "replacement.txt")
    _git(source, "commit", "--quiet", "-m", "replacement root")
    replacement = _git(source, "rev-parse", "HEAD")
    _git(source, "update-ref", "refs/heads/main", replacement)
    _git(source, "checkout", "--quiet", "--detach", replacement)
    _git(source, "update-ref", "-d", "refs/heads/replacement")
    _git(source, "tag", "-d", "v1")

    await GitObjectStore(path, "acme/widgets", str(source)).sync_upstream()
    _stored_git(path, "gc", "--prune=now")

    assert _stored_git(path, "rev-parse", "refs/heads/main") == replacement
    assert _stored_git(path, "for-each-ref", "--format=%(refname)", "refs/tags/v1") == ""
    assert _stored_git(path, "cat-file", "-t", base) == "commit"
    assert _stored_git(path, "rev-parse", f"refs/github-archive/upstream/heads/{base}") == base
    assert _stored_git(path, "rev-parse", f"refs/github-archive/upstream/heads/{replacement}") == replacement


@pytest.mark.asyncio
async def test_merged_head_reachable_from_upstream_skips_pull_ref_fetch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    base, head = _source_repository(source, 1)
    _git(source, "branch", "feature", head)
    _git(source, "merge", "--quiet", "--no-ff", "feature", "-m", "merge pull")
    landing = _git(source, "rev-parse", "HEAD")
    _git(source, "branch", "-D", "feature")
    real_command = git_store_module._command
    commands: list[Sequence[str]] = []

    async def record(command: Sequence[str], **kwargs: Any) -> str:
        commands.append(command)
        return await real_command(command, **kwargs)

    monkeypatch.setattr(git_store_module, "_command", record)
    path = git_store_path(tmp_path / "facts.sqlite3")
    store = GitObjectStore(path, "acme/widgets", str(source))
    pull = {
        "base": {"sha": base},
        "head": {"sha": head},
        "merge_commit_sha": landing,
        "merged": True,
    }

    await store.prefetch({7: pull})
    snapshot = await store.capture(7, pull)

    pull_fetches = [command for command in commands if any("refs/pull/7" in part for part in command)]
    assert pull_fetches == []
    assert snapshot["history_preserved"] is True


@pytest.mark.asyncio
async def test_squash_merge_retains_original_pull_history_after_remote_deletion(tmp_path: Path) -> None:
    source = tmp_path / "source"
    base, head = _source_repository(source, 1)
    (source / "squashed.txt").write_text("squashed\n")
    _git(source, "add", "squashed.txt")
    _git(source, "commit", "--quiet", "-m", "squash landing")
    landing = _git(source, "rev-parse", "HEAD")
    path = git_store_path(tmp_path / "facts.sqlite3")
    store = GitObjectStore(path, "acme/widgets", str(source))
    pull = {
        "base": {"sha": base},
        "head": {"sha": head},
        "merge_commit_sha": landing,
        "merged": True,
    }

    await store.prefetch({7: pull})
    snapshot = await store.capture(7, pull)
    _git(source, "update-ref", "-d", "refs/pull/7/head")
    _stored_git(path, "gc", "--prune=now")

    assert snapshot["history_preserved"] is False
    assert _stored_git(path, "cat-file", "-t", snapshot["head_ref"]) == "commit"
    assert _stored_git(path, "rev-parse", snapshot["landing_ref"]) == landing
    assert _stored_git(path, "merge-base", head, landing) == base


@pytest.mark.asyncio
async def test_same_commit_can_belong_to_multiple_pull_requests(tmp_path: Path) -> None:
    source = tmp_path / "source"
    base, head = _source_repository(source, 1)
    _git(source, "update-ref", "refs/pull/8/head", head)
    path = git_store_path(tmp_path / "facts.sqlite3")
    store = GitObjectStore(path, "acme/widgets", str(source))
    pulls = {
        number: {"base": {"sha": base}, "head": {"sha": head}, "merged": False}
        for number in (7, 8)
    }

    await store.prefetch(pulls)
    snapshots = {number: await store.capture(number, pull) for number, pull in pulls.items()}

    assert snapshots[7]["head_ref"] != snapshots[8]["head_ref"]
    assert {_stored_git(path, "rev-parse", snapshot["head_ref"]) for snapshot in snapshots.values()} == {head}


@pytest.mark.asyncio
async def test_unavailable_remote_pull_ref_publishes_an_explicit_partial_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source"
    base, head = _source_repository(source, 1)
    _git(source, "update-ref", "-d", "refs/pull/7/head")
    path = git_store_path(tmp_path / "facts.sqlite3")
    store = GitObjectStore(path, "acme/widgets", str(source))
    pull = {"base": {"sha": base}, "head": {"sha": head}, "merged": False}

    await store.prefetch({7: pull})
    snapshot = await store.capture(7, pull)

    assert snapshot == {
        "base_ref": f"refs/github-archive/pulls/7/bases/{base}",
        "base_sha": base,
        "comparison_kind": "unavailable",
        "head_sha": head,
        "history_preserved": None,
        "unavailable_commits": [head],
    }


@pytest.mark.asyncio
async def test_git_store_rejects_rebinding_to_another_repository(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    path = git_store_path(tmp_path / "facts.sqlite3")
    await GitObjectStore(path, "acme/widgets", str(source)).prefetch({7: {"head": {"sha": head}}})

    with pytest.raises(GitStoreError, match="belongs to acme/widgets"):
        await GitObjectStore(path, "acme/other", str(source)).prefetch({7: {"head": {"sha": head}}})


@pytest.mark.asyncio
async def test_git_store_removes_an_interrupted_fetch_pack_on_open(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    path = tmp_path / "facts.sqlite3.git"
    await GitObjectStore(path, "acme/widgets", str(source)).prefetch({7: {"head": {"sha": head}}})
    temporary = path / "objects" / "pack" / "tmp_pack_interrupted"
    temporary.write_bytes(b"incomplete")

    await GitObjectStore(path, "acme/widgets", str(source)).prefetch({7: {"head": {"sha": head}}})

    assert not temporary.exists()


@pytest.mark.asyncio
async def test_git_fetch_retries_transient_transport_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    real_command = git_store_module._command
    attempts = 0
    failed_pack = tmp_path / "facts.sqlite3.git" / "objects" / "pack" / "tmp_pack_failed"

    async def flaky(command: Sequence[str], **kwargs: Any) -> str:
        nonlocal attempts
        if _is_fetch(command):
            attempts += 1
            if attempts <= 7:
                failed_pack.write_bytes(b"incomplete")
                raise GitStoreError(
                    "git fetch failed: gnutls_handshake() failed: "
                    "Error decoding the received TLS packet.",
                )
        return await real_command(command, **kwargs)

    waits: list[float] = []
    reported: list[float] = []
    heartbeats = 0

    async def sleep(wait: float) -> None:
        waits.append(wait)

    def heartbeat() -> None:
        nonlocal heartbeats
        heartbeats += 1

    monkeypatch.setattr(git_store_module, "_command", flaky)
    store = GitObjectStore(
        tmp_path / "facts.sqlite3.git",
        "acme/widgets",
        str(source),
        sleep=sleep,
    )

    await store.prefetch({7: {"head": {"sha": head}}}, heartbeat=heartbeat, retry=reported.append)

    assert attempts == 9
    assert waits == [1, 2, 4, 8, 16, 30, 30]
    assert reported == waits
    assert heartbeats >= 7
    assert not failed_pack.exists()


@pytest.mark.asyncio
async def test_git_prefetch_can_delegate_a_transient_ref_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    real_command = git_store_module._command
    attempts = 0
    environments: list[dict[str, str]] = []

    async def disconnected(command: Sequence[str], **kwargs: Any) -> str:
        nonlocal attempts
        if _is_fetch(command):
            environments.append(kwargs["environment"])
        if _is_fetch(command) and any("refs/pull/7" in value for value in command):
            attempts += 1
            raise GitStoreError("git fetch failed: fatal: early EOF")
        return await real_command(command, **kwargs)

    waits: list[float] = []
    monkeypatch.setattr(git_store_module, "_command", disconnected)
    store = GitObjectStore(
        tmp_path / "facts.sqlite3.git",
        "acme/widgets",
        str(source),
    )

    with pytest.raises(TransientGitStoreError, match="early EOF"):
        await store.prefetch(
            {7: {"head": {"sha": head}}},
            retry=waits.append,
            retry_transient=False,
        )

    assert attempts == 1
    assert waits == []
    assert environments
    assert all(environment["GIT_CONFIG_KEY_0"] == "http.version" for environment in environments)
    assert all(environment["GIT_CONFIG_VALUE_0"] == "HTTP/1.1" for environment in environments)


@pytest.mark.asyncio
async def test_git_fetch_does_not_retry_permanent_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    real_command = git_store_module._command
    attempts = 0

    async def rejected(command: Sequence[str], **kwargs: Any) -> str:
        nonlocal attempts
        if _is_fetch(command):
            attempts += 1
            raise GitStoreError("git fetch failed: fatal: Authentication failed")
        return await real_command(command, **kwargs)

    waits: list[float] = []
    monkeypatch.setattr(git_store_module, "_command", rejected)
    store = GitObjectStore(
        tmp_path / "facts.sqlite3.git",
        "acme/widgets",
        str(source),
        sleep=asyncio.sleep,
    )

    with pytest.raises(GitStoreError, match="Authentication failed"):
        await store.prefetch({7: {"head": {"sha": head}}}, retry=waits.append)

    assert attempts == 1
    assert waits == []


@pytest.mark.asyncio
async def test_git_fetch_retry_remains_cancellable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    real_command = git_store_module._command
    attempts = 0

    async def disconnected(command: Sequence[str], **kwargs: Any) -> str:
        nonlocal attempts
        if _is_fetch(command):
            attempts += 1
            raise GitStoreError("git fetch failed: fatal: Failed to connect")
        return await real_command(command, **kwargs)

    async def cancel(_: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(git_store_module, "_command", disconnected)
    store = GitObjectStore(
        tmp_path / "facts.sqlite3.git",
        "acme/widgets",
        str(source),
        sleep=cancel,
    )

    with pytest.raises(asyncio.CancelledError):
        await store.prefetch({7: {"head": {"sha": head}}})

    assert attempts == 1
