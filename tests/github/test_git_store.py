"""Verify complete PR Git diffs, pinned references, and repository binding."""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

import gh_puller.github.git_store as git_store_module
from gh_puller.github.git_store import (
    CommitFetchSource,
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


def _pull_source(repository: Path, number: int) -> CommitFetchSource:
    return CommitFetchSource(
        "pull-ref",
        str(repository),
        f"refs/pull/{number}/head",
        "acme/widgets",
        number,
    )


def _repository_source(
    repository: Path,
    number: int,
    branch: str,
) -> CommitFetchSource:
    return CommitFetchSource(
        "repository-ref",
        str(repository),
        f"refs/heads/{branch}",
        "contributor/widgets",
        number,
    )


def _pull_fetch_sizes(
    commands: Sequence[Sequence[str]],
    repository: Path | None = None,
) -> list[int]:
    return [
        sum("refs/pull/" in argument for argument in command)
        for command in commands
        if _is_fetch(command)
        and (repository is None or str(repository) in command)
        and any("refs/pull/" in argument for argument in command)
    ]


def _remove_loose_object(path: Path, oid: str) -> None:
    object_path = path / "objects" / oid[:2] / oid[2:]
    assert object_path.is_file()
    object_path.unlink()


def test_git_token_is_scoped_to_the_managed_origin(tmp_path: Path) -> None:
    remote = "https://github.example/acme/widgets.git"
    token = f"{tmp_path.name}-token"
    store = GitObjectStore(
        tmp_path / "facts.sqlite3.git",
        "acme/widgets",
        remote,
        token=token,
    )

    environment = store._environment()

    assert environment["GIT_CONFIG_KEY_1"] == f"http.{remote}.extraHeader"
    assert environment["GIT_CONFIG_VALUE_1"].startswith("Authorization: Basic ")
    assert token not in environment["GIT_CONFIG_VALUE_1"]


def test_git_maintenance_plan_tracks_loose_refs_and_pack_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(git_store_module, "_LOOSE_REF_MAINTENANCE_THRESHOLD", 2)
    monkeypatch.setattr(git_store_module, "_PACK_MAINTENANCE_THRESHOLD", 2)
    refs = tmp_path / "refs" / "heads"
    packs = tmp_path / "objects" / "pack"
    refs.mkdir(parents=True)
    packs.mkdir(parents=True)
    (refs / "one").write_text("one\n")
    (packs / "one.pack").touch()

    assert git_store_module._maintenance_tasks(tmp_path) == ()

    (refs / "two").write_text("two\n")
    assert git_store_module._maintenance_tasks(tmp_path) == (
        "pack-refs",
        "commit-graph",
    )

    (packs / "two.pack").touch()
    assert git_store_module._maintenance_tasks(tmp_path) == (
        "pack-refs",
        "incremental-repack",
        "commit-graph",
    )


@pytest.mark.asyncio
async def test_git_maintenance_packs_refs_without_changing_tips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    base, _ = _source_repository(source, 1)
    path = tmp_path / "facts.sqlite3.git"
    store = GitObjectStore(path, "acme/widgets", str(source))
    await store.sync_upstream()
    _stored_git(path, "update-ref", "refs/github-archive/commits/" + base, base)
    before = _stored_git(path, "for-each-ref", "--format=%(refname) %(objectname)")
    real_command = git_store_module._command
    commands: list[Sequence[str]] = []

    async def record(command: Sequence[str], **kwargs: Any) -> str:
        commands.append(command)
        return await real_command(command, **kwargs)

    monkeypatch.setattr(git_store_module, "_command", record)
    monkeypatch.setattr(git_store_module, "_LOOSE_REF_MAINTENANCE_THRESHOLD", 1)
    await store._maintain(None)

    assert _stored_git(path, "for-each-ref", "--format=%(refname) %(objectname)") == before
    assert not any(item.is_file() for item in (path / "refs").rglob("*"))
    assert (path / "packed-refs").is_file()
    assert any(
        command[3:7]
        == (
            "maintenance",
            "run",
            "--quiet",
            "--task=pack-refs",
        )
        for command in commands
    )


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
    _git(source, "tag", "-a", "annotated", base, "-m", "annotated")
    _git(source, "tag", "same-object", base)
    path = git_store_path(tmp_path / "facts.sqlite3")

    first = await GitObjectStore(path, "acme/widgets", str(source)).sync_upstream()

    assert _stored_git(path, "rev-parse", "refs/heads/main") == base
    assert _stored_git(path, "rev-parse", "refs/tags/v1") == base
    assert _stored_git(path, "rev-parse", f"refs/github-archive/upstream/heads/{base}") == base
    refs = {item["name"]: item for item in first["refs"]}
    assert first["symbolic_head"] == "refs/heads/main"
    assert first["default_branch"] == "main"
    assert refs["refs/tags/v1"] == {
        "name": "refs/tags/v1",
        "oid": base,
        "peeled_oid": base,
    }
    assert refs["refs/tags/annotated"]["oid"] != base
    assert refs["refs/tags/annotated"]["peeled_oid"] == base
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

    second = await GitObjectStore(path, "acme/widgets", str(source)).sync_upstream()
    _stored_git(path, "gc", "--prune=now")

    assert _stored_git(path, "rev-parse", "refs/heads/main") == replacement
    assert _stored_git(path, "for-each-ref", "--format=%(refname)", "refs/tags/v1") == ""
    assert _stored_git(path, "cat-file", "-t", base) == "commit"
    assert _stored_git(path, "rev-parse", f"refs/github-archive/upstream/heads/{base}") == base
    assert _stored_git(path, "rev-parse", f"refs/github-archive/upstream/heads/{replacement}") == replacement
    assert second["symbolic_head"] is None
    assert "refs/tags/v1" not in {item["name"] for item in second["refs"]}


@pytest.mark.asyncio
async def test_structured_commit_retention_pins_acquired_objects_and_reports_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    base, head = _source_repository(source, 1)
    path = git_store_path(tmp_path / "facts.sqlite3")
    real_command = git_store_module._command
    commands: list[Sequence[str]] = []

    async def record(command: Sequence[str], **kwargs: Any) -> str:
        commands.append(command)
        return await real_command(command, **kwargs)

    monkeypatch.setattr(git_store_module, "_command", record)
    store = GitObjectStore(path, "acme/widgets", str(source))
    await store.prefetch({7: {"head": {"sha": head}}})
    commands.clear()

    retained = await store.retain_commits((base, head, "f" * 40))

    assert retained[base]["status"] == "available"
    assert retained[base]["obtained"] == "existing"
    assert retained[head]["status"] == "available"
    assert retained[head]["obtained"] == "existing"
    assert retained["f" * 40]["status"] == "unavailable"
    checks = [
        command
        for command in commands
        if "cat-file" in command and any(argument.startswith("--batch-check=") for argument in command)
    ]
    assert len(checks) == 3
    assert not any("f" * 40 in argument for command in commands for argument in command)
    _stored_git(path, "update-ref", "-d", "refs/github-archive/staging/pulls/7/head")
    _git(source, "update-ref", "-d", "refs/pull/7/head")
    for ref in ("refs/heads/main", "refs/tags/v1"):
        _stored_git(path, "update-ref", "-d", ref)
    _stored_git(path, "reflog", "expire", "--expire=now", "--all")
    _stored_git(path, "gc", "--prune=now")

    assert _stored_git(path, "cat-file", "-t", retained[head]["ref"]) == "commit"
    assert _stored_git(path, "show", f"{retained[head]['ref']}:changes/0000.txt") == "0"
    assert _stored_git(path, "rev-list", retained[head]["ref"]).splitlines() == [
        head,
        base,
    ]
    assert _stored_git(path, "diff", "--name-only", base, retained[head]["ref"]) == ("changes/0000.txt")
    assert retained[head]["verification"]["snapshot"]["status"] == "complete"
    assert retained[head]["verification"]["history"]["status"] == "complete"


@pytest.mark.asyncio
async def test_structured_commit_is_fetched_from_its_known_pull_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    path = git_store_path(tmp_path / "facts.sqlite3")
    real_command = git_store_module._command
    commands: list[Sequence[str]] = []

    async def record(command: Sequence[str], **kwargs: Any) -> str:
        commands.append(command)
        return await real_command(command, **kwargs)

    monkeypatch.setattr(git_store_module, "_command", record)
    store = GitObjectStore(path, "acme/widgets", str(source))
    source_ref = CommitFetchSource(
        "pull-ref",
        str(source),
        "refs/pull/7/head",
        "acme/widgets",
        7,
    )

    retained = await store.retain_commits((head,), sources={head: (source_ref,)})

    assert retained[head]["status"] == "available"
    assert retained[head]["obtained"] == "pull-ref"
    assert retained[head]["attempts"][-1]["ref"] == "refs/pull/7/head"
    assert retained[head]["attempts"][-1]["outcome"] == "available"
    assert not any("+refs/heads/*:refs/heads/*" in command for command in commands)


@pytest.mark.asyncio
async def test_batched_source_fetch_matches_serial_source_attribution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    base, _ = _source_repository(source, 1)
    _git(source, "checkout", "--quiet", "--detach", base)
    (source / "middle.txt").write_text("middle\n")
    _git(source, "add", "middle.txt")
    _git(source, "commit", "--quiet", "-m", "middle")
    middle = _git(source, "rev-parse", "HEAD")
    (source / "second.txt").write_text("second\n")
    _git(source, "add", "second.txt")
    _git(source, "commit", "--quiet", "-m", "second pull")
    second_head = _git(source, "rev-parse", "HEAD")
    _git(source, "update-ref", "refs/pull/8/head", second_head)
    first_source = _pull_source(source, 7)
    second_source = _pull_source(source, 8)
    missing = "f" * 40
    routes = {
        middle: (first_source,),
        second_head: (second_source,),
        missing: (first_source, second_source),
    }
    real_command = git_store_module._command
    commands: list[Sequence[str]] = []

    async def record(command: Sequence[str], **kwargs: Any) -> str:
        commands.append(command)
        return await real_command(command, **kwargs)

    monkeypatch.setattr(git_store_module, "_command", record)
    def fixed_now() -> datetime:
        return datetime(2026, 9, 7, tzinfo=UTC)

    serial_path = tmp_path / "serial.git"
    batched_path = tmp_path / "batched.git"
    serial = await GitObjectStore(
        serial_path,
        "acme/widgets",
        str(source),
        ref_batch_size=1,
        now=fixed_now,
    ).retain_commits((middle, second_head, missing), sources=routes)
    batched = await GitObjectStore(
        batched_path,
        "acme/widgets",
        str(source),
        ref_batch_size=8,
        now=fixed_now,
    ).retain_commits((middle, second_head, missing), sources=routes)

    assert batched == serial
    assert _pull_fetch_sizes(commands, serial_path) == [1, 1]
    assert _pull_fetch_sizes(commands, batched_path) == [2]
    assert batched[missing]["status"] == "unavailable"
    assert {middle, second_head} <= set(
        _stored_git(batched_path, "rev-list", "--all").splitlines(),
    )


@pytest.mark.asyncio
async def test_equal_repository_tip_reuses_pull_ref_without_fetching_pack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    _git(source, "branch", "feature", head)
    missing = "f" * 40
    routes = {
        missing: (
            _pull_source(source, 7),
            _repository_source(source, 7, "feature"),
        ),
    }
    real_command = git_store_module._command
    commands: list[Sequence[str]] = []

    async def record(command: Sequence[str], **kwargs: Any) -> str:
        commands.append(command)
        return await real_command(command, **kwargs)

    monkeypatch.setattr(git_store_module, "_command", record)
    retained = await GitObjectStore(
        tmp_path / "facts.sqlite3.git",
        "acme/widgets",
        str(source),
    ).retain_commits((missing,), sources=routes)

    attempt = next(
        item
        for item in retained[missing]["attempts"]
        if item["kind"] == "repository-ref"
    )
    assert retained[missing]["status"] == "unavailable"
    assert attempt["outcome"] == "unavailable"
    assert attempt["preflight"]["advertised_sha"] == head
    assert attempt["preflight"]["equivalent_source"] == {
        "kind": "pull-ref",
        "ref": "refs/pull/7/head",
        "resource_number": 7,
    }
    assert any(len(command) > 3 and command[3] == "ls-remote" for command in commands)
    assert not any(
        _is_fetch(command)
        and any("+refs/heads/feature:" in argument for argument in command)
        for command in commands
    )

    baseline_commands: list[Sequence[str]] = []

    async def force_full_fetch(command: Sequence[str], **kwargs: Any) -> str:
        baseline_commands.append(command)
        if (
            len(command) > 3
            and command[3] == "ls-remote"
            and "refs/heads/feature" in command
        ):
            raise GitStoreError("git ls-remote failed: forced inconclusive preflight")
        return await real_command(command, **kwargs)

    monkeypatch.setattr(git_store_module, "_command", force_full_fetch)
    baseline = await GitObjectStore(
        tmp_path / "baseline.git",
        "acme/widgets",
        str(source),
    ).retain_commits((missing,), sources=routes)

    for key in ("sha", "status", "reason", "verification"):
        assert retained[missing][key] == baseline[missing][key]
    assert any(
        _is_fetch(command)
        and any("+refs/heads/feature:" in argument for argument in command)
        for command in baseline_commands
    )


@pytest.mark.asyncio
async def test_changed_repository_tip_is_fetched_and_can_supply_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    _git(source, "checkout", "--quiet", "--detach", head)
    (source / "later.txt").write_text("later\n")
    _git(source, "add", "later.txt")
    _git(source, "commit", "--quiet", "-m", "later fork commit")
    later = _git(source, "rev-parse", "HEAD")
    _git(source, "branch", "feature", later)
    routes = {
        later: (
            _pull_source(source, 7),
            _repository_source(source, 7, "feature"),
        ),
    }
    real_command = git_store_module._command
    commands: list[Sequence[str]] = []

    async def record(command: Sequence[str], **kwargs: Any) -> str:
        commands.append(command)
        return await real_command(command, **kwargs)

    monkeypatch.setattr(git_store_module, "_command", record)
    retained = await GitObjectStore(
        tmp_path / "facts.sqlite3.git",
        "acme/widgets",
        str(source),
    ).retain_commits((later,), sources=routes)

    attempt = retained[later]["attempts"][-1]
    assert retained[later]["status"] == "available"
    assert retained[later]["obtained"] == "repository-ref"
    assert attempt["kind"] == "repository-ref"
    assert attempt["preflight"]["advertised_sha"] == later
    assert "equivalent_source" not in attempt["preflight"]
    assert any(
        _is_fetch(command)
        and any("+refs/heads/feature:" in argument for argument in command)
        for command in commands
    )


@pytest.mark.asyncio
async def test_missing_repository_ref_is_recorded_without_fetching_pack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _source_repository(source, 1)
    missing = "f" * 40
    repository_source = _repository_source(source, 7, "deleted")
    real_command = git_store_module._command
    commands: list[Sequence[str]] = []

    async def record(command: Sequence[str], **kwargs: Any) -> str:
        commands.append(command)
        return await real_command(command, **kwargs)

    monkeypatch.setattr(git_store_module, "_command", record)
    retained = await GitObjectStore(
        tmp_path / "facts.sqlite3.git",
        "acme/widgets",
        str(source),
    ).retain_commits((missing,), sources={missing: (repository_source,)})

    attempt = next(
        item
        for item in retained[missing]["attempts"]
        if item["kind"] == "repository-ref"
    )
    assert retained[missing]["status"] == "unavailable"
    assert attempt["preflight"]["outcome"] == "absent"
    assert "found no advertised ref" in attempt["error"]
    assert not any(
        _is_fetch(command)
        and any("+refs/heads/deleted:" in argument for argument in command)
        for command in commands
    )


@pytest.mark.asyncio
async def test_missing_fork_is_recorded_without_fetching_pack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _source_repository(source, 1)
    missing = "f" * 40
    remote = "https://github.com/deleted/widgets.git"
    repository_source = CommitFetchSource(
        "repository-ref",
        remote,
        "refs/heads/feature",
        "deleted/widgets",
        7,
    )
    real_command = git_store_module._command
    commands: list[Sequence[str]] = []

    async def missing_repository(command: Sequence[str], **kwargs: Any) -> str:
        commands.append(command)
        if len(command) > 4 and command[3] == "ls-remote" and command[4] == remote:
            raise GitStoreError(
                "git ls-remote failed: remote: Repository not found.\n"
                "fatal: repository 'https://github.com/deleted/widgets.git/' not found",
            )
        return await real_command(command, **kwargs)

    monkeypatch.setattr(git_store_module, "_command", missing_repository)
    retained = await GitObjectStore(
        tmp_path / "facts.sqlite3.git",
        "acme/widgets",
        str(source),
    ).retain_commits((missing,), sources={missing: (repository_source,)})

    attempt = next(
        item
        for item in retained[missing]["attempts"]
        if item["kind"] == "repository-ref"
    )
    assert retained[missing]["status"] == "unavailable"
    assert attempt["outcome"] == "unavailable"
    assert attempt["preflight"]["outcome"] == "absent"
    assert "Repository not found" in attempt["error"]
    assert not any(_is_fetch(command) and remote in command for command in commands)


@pytest.mark.asyncio
async def test_fork_deleted_after_preflight_is_a_source_absence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _source_repository(source, 1)
    remote = "https://github.com/deleted/widgets.git"
    repository_source = CommitFetchSource(
        "repository-ref",
        remote,
        "refs/heads/feature",
        "deleted/widgets",
        7,
    )
    real_command = git_store_module._command

    async def deleted_during_fetch(command: Sequence[str], **kwargs: Any) -> str:
        if _is_fetch(command) and remote in command:
            raise GitStoreError(
                "git fetch failed: remote: Repository not found.\n"
                "fatal: repository 'https://github.com/deleted/widgets.git/' not found",
            )
        return await real_command(command, **kwargs)

    monkeypatch.setattr(git_store_module, "_command", deleted_during_fetch)
    store = GitObjectStore(
        tmp_path / "facts.sqlite3.git",
        "acme/widgets",
        str(source),
    )
    await store._prepare()

    errors = await store._fetch_source_batch(
        (repository_source,),
        refetch=False,
        heartbeat=None,
        retry=None,
    )

    assert "Repository not found" in (errors[repository_source] or "")


@pytest.mark.asyncio
async def test_repository_ref_preflight_is_bounded_and_parallel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(git_store_module, "_REMOTE_REF_CONCURRENCY", 2)
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    for branch in ("one", "two", "three"):
        _git(source, "branch", branch, head)
    store = GitObjectStore(
        tmp_path / "facts.sqlite3.git",
        "acme/widgets",
        str(source),
        ref_batch_size=2,
    )
    await store._prepare()
    sources = tuple(
        _repository_source(source, number, branch)
        for number, branch in enumerate(("one", "two", "three"), start=1)
    )
    real_command = git_store_module._command
    gate = asyncio.Event()
    active = 0
    peak = 0

    async def measure(command: Sequence[str], **kwargs: Any) -> str:
        nonlocal active, peak
        if len(command) <= 3 or command[3] != "ls-remote":
            return await real_command(command, **kwargs)
        active += 1
        peak = max(peak, active)
        if active == 2:
            gate.set()
        await asyncio.wait_for(gate.wait(), timeout=1)
        try:
            return await real_command(command, **kwargs)
        finally:
            active -= 1

    monkeypatch.setattr(git_store_module, "_command", measure)
    observed = await store._observe_remote_refs(sources, heartbeat=None)

    assert peak == 2
    assert {item.sha for item in observed.values()} == {head}


@pytest.mark.asyncio
async def test_source_batch_isolates_a_missing_remote_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    missing = "f" * 40
    sources = {
        head: (_pull_source(source, 7),),
        missing: (_pull_source(source, 8),),
    }
    real_command = git_store_module._command
    commands: list[Sequence[str]] = []

    async def record(command: Sequence[str], **kwargs: Any) -> str:
        commands.append(command)
        return await real_command(command, **kwargs)

    monkeypatch.setattr(git_store_module, "_command", record)
    retained = await GitObjectStore(
        tmp_path / "facts.sqlite3.git",
        "acme/widgets",
        str(source),
        ref_batch_size=8,
    ).retain_commits((head, missing), sources=sources)

    assert _pull_fetch_sizes(commands) == [2, 1, 1]
    assert retained[head]["status"] == "available"
    assert retained[missing]["status"] == "unavailable"
    assert "couldn't find remote ref refs/pull/8/head" in retained[missing]["attempts"][1]["error"]


@pytest.mark.asyncio
async def test_transient_source_batch_failure_splits_before_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    base, first_head = _source_repository(source, 1)
    _git(source, "checkout", "--quiet", "--detach", base)
    (source / "second.txt").write_text("second\n")
    _git(source, "add", "second.txt")
    _git(source, "commit", "--quiet", "-m", "second pull")
    second_head = _git(source, "rev-parse", "HEAD")
    _git(source, "update-ref", "refs/pull/8/head", second_head)
    sources = {
        first_head: (_pull_source(source, 7),),
        second_head: (_pull_source(source, 8),),
    }
    real_command = git_store_module._command
    fetch_sizes: list[int] = []
    failed = False

    async def disconnect_batch(command: Sequence[str], **kwargs: Any) -> str:
        nonlocal failed
        if _is_fetch(command) and any("refs/pull/" in argument for argument in command):
            size = sum("refs/pull/" in argument for argument in command)
            fetch_sizes.append(size)
            if size > 1 and not failed:
                failed = True
                raise GitStoreError("git fetch failed: RPC failed; curl 56 connection reset")
        return await real_command(command, **kwargs)

    monkeypatch.setattr(git_store_module, "_command", disconnect_batch)
    retained = await GitObjectStore(
        tmp_path / "facts.sqlite3.git",
        "acme/widgets",
        str(source),
        ref_batch_size=8,
    ).retain_commits((first_head, second_head), sources=sources)

    assert fetch_sizes == [2, 1, 1]
    assert {result["status"] for result in retained.values()} == {"available"}


@pytest.mark.asyncio
async def test_unavailable_known_pull_ref_can_be_retried_after_it_appears(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    _git(source, "update-ref", "-d", "refs/pull/7/head")
    path = git_store_path(tmp_path / "facts.sqlite3")
    store = GitObjectStore(path, "acme/widgets", str(source))
    source_ref = CommitFetchSource(
        "pull-ref",
        str(source),
        "refs/pull/7/head",
        "acme/widgets",
        7,
    )

    missing = await store.retain_commits((head,), sources={head: (source_ref,)})
    _git(source, "update-ref", "refs/pull/7/head", head)
    retained = await store.retain_commits((head,), sources={head: (source_ref,)})

    assert missing[head]["status"] == "unavailable"
    assert missing[head]["attempts"][-1]["outcome"] == "unavailable"
    pull_attempt = next(attempt for attempt in missing[head]["attempts"] if attempt["kind"] == "pull-ref")
    assert "couldn't find remote ref" in pull_attempt["error"]
    assert retained[head]["status"] == "available"


@pytest.mark.asyncio
async def test_known_source_authentication_failure_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    path = git_store_path(tmp_path / "facts.sqlite3")
    source_ref = CommitFetchSource(
        "pull-ref",
        str(source),
        "refs/pull/7/head",
        "acme/widgets",
        7,
    )
    real_command = git_store_module._command

    async def rejected(command: Sequence[str], **kwargs: Any) -> str:
        if _is_fetch(command) and any("refs/pull/7/head" in value for value in command):
            raise GitStoreError("git fetch failed: fatal: Authentication failed")
        return await real_command(command, **kwargs)

    monkeypatch.setattr(git_store_module, "_command", rejected)
    store = GitObjectStore(path, "acme/widgets", str(source))

    with pytest.raises(GitStoreError, match="Authentication failed") as caught:
        await store.retain_commits((head,), sources={head: (source_ref,)})
    assert "pull-ref acme/widgets refs/pull/7/head" in str(caught.value)


@pytest.mark.asyncio
async def test_verification_distinguishes_snapshot_from_missing_parent_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    base, head = _source_repository(source, 1)
    path = git_store_path(tmp_path / "facts.sqlite3")
    store = GitObjectStore(path, "acme/widgets", str(source))
    await store.prefetch({7: {"head": {"sha": head}}})
    _remove_loose_object(path, base)

    async def unchanged(**_: Any) -> None:
        return None

    monkeypatch.setattr(store, "_sync_upstream", unchanged)

    retained = await store.retain_commits((head,))

    assert retained[head]["status"] == "partial"
    assert retained[head]["verification"]["endpoint"]["status"] == "complete"
    assert retained[head]["verification"]["snapshot"]["status"] == "complete"
    assert retained[head]["verification"]["history"]["status"] == "partial"


@pytest.mark.asyncio
async def test_incomplete_closure_tries_a_known_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    base, head = _source_repository(source, 1)
    path = git_store_path(tmp_path / "facts.sqlite3")
    store = GitObjectStore(path, "acme/widgets", str(source))
    await store.prefetch({7: {"head": {"sha": head}}})
    _remove_loose_object(path, base)
    pull_source = CommitFetchSource(
        "pull-ref",
        str(source),
        "refs/pull/7/head",
        "acme/widgets",
        7,
    )

    retained = await store.retain_commits(
        (head,),
        sources={head: (pull_source,)},
    )

    assert [attempt["kind"] for attempt in retained[head]["attempts"]] == [
        "managed-store",
        "pull-ref",
    ]
    assert retained[head]["attempts"][0]["outcome"] == "partial"
    assert retained[head]["attempts"][1]["outcome"] == "available"
    assert retained[head]["status"] == "available"


@pytest.mark.asyncio
async def test_verification_rejects_a_snapshot_with_a_missing_blob(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    blob = _git(source, "rev-parse", f"{head}:changes/0000.txt")
    path = git_store_path(tmp_path / "facts.sqlite3")
    store = GitObjectStore(path, "acme/widgets", str(source))
    await store.prefetch({7: {"head": {"sha": head}}})
    _remove_loose_object(path, blob)

    retained = await store.retain_commits((head,))

    assert retained[head]["status"] == "partial"
    assert retained[head]["verification"]["snapshot"]["status"] == "partial"
    assert retained[head]["verification"]["history"]["status"] == "partial"


@pytest.mark.asyncio
async def test_verification_does_not_publish_operational_git_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    store = GitObjectStore(
        git_store_path(tmp_path / "facts.sqlite3"),
        "acme/widgets",
        str(source),
    )
    await store.prefetch({7: {"head": {"sha": head}}})
    real_command = git_store_module._command

    async def denied(command: Sequence[str], **kwargs: Any) -> str:
        if len(command) > 3 and command[3] == "rev-list":
            raise GitStoreError("git rev-list failed: Permission denied")
        return await real_command(command, **kwargs)

    monkeypatch.setattr(git_store_module, "_command", denied)

    with pytest.raises(GitStoreError, match="Permission denied"):
        await store.retain_commits((head,))


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
    pulls = {number: {"base": {"sha": base}, "head": {"sha": head}, "merged": False} for number in (7, 8)}

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
async def test_resumed_cycle_skips_only_the_implicit_upstream_fetch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _, head = _source_repository(source, 1)
    path = tmp_path / "facts.sqlite3.git"
    await GitObjectStore(path, "acme/widgets", str(source)).sync_upstream()
    real_command = git_store_module._command
    commands: list[Sequence[str]] = []

    async def record(command: Sequence[str], **kwargs: Any) -> str:
        commands.append(command)
        return await real_command(command, **kwargs)

    monkeypatch.setattr(git_store_module, "_command", record)
    resumed = GitObjectStore(
        path,
        "acme/widgets",
        str(source),
        upstream_synced=True,
    )

    await resumed.prefetch({7: {"head": {"sha": head}}})

    assert not any("+refs/heads/*:refs/heads/*" in command for command in commands)
    await resumed.sync_upstream()
    assert any("+refs/heads/*:refs/heads/*" in command for command in commands)


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
                    "git fetch failed: gnutls_handshake() failed: Error decoding the received TLS packet.",
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
