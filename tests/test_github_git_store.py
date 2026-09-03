"""验证 PR Git 对象归档的完整差异、固定引用与仓库绑定。"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from gh_puller.github.git_store import GitObjectStore, GitStoreError, git_store_path

if TYPE_CHECKING:
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


@pytest.mark.asyncio
async def test_git_store_reconstructs_more_than_three_thousand_changed_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    base, head = _source_repository(source, 3_001)
    database = tmp_path / "facts.sqlite3"
    path = git_store_path(database)
    store = GitObjectStore(path, "acme/widgets", str(source))

    await store.prefetch([7])
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

    await store.prefetch([8])
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
    await store.prefetch([7])
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

    await store.prefetch([7])
    second = await store.capture(
        7,
        {"base": {"sha": base}, "head": {"sha": second_head}, "merged": False},
    )

    assert _stored_git(path, "rev-parse", first["head_ref"]) == first_head
    assert _stored_git(path, "rev-parse", second["head_ref"]) == second_head
    assert first["head_ref"] != second["head_ref"]


@pytest.mark.asyncio
async def test_git_store_rejects_rebinding_to_another_repository(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _source_repository(source, 1)
    path = git_store_path(tmp_path / "facts.sqlite3")
    await GitObjectStore(path, "acme/widgets", str(source)).prefetch([7])

    with pytest.raises(GitStoreError, match="belongs to acme/widgets"):
        await GitObjectStore(path, "acme/other", str(source)).prefetch([7])
