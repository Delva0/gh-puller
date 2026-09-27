"""Exercise snapshot updates against real local Git remotes and a fake CBM."""

import asyncio
import fcntl
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version

from vllm_kb_adapter import update
from vllm_kb_adapter.snapshots import SnapshotRegistry


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=path, capture_output=True, text=True, check=True,
    ).stdout.strip()


def release(remote: Path, tag: str, text: str, *, annotated: bool = False) -> str:
    (remote / "example.py").write_text(text)
    git(remote, "add", ".")
    git(remote, "commit", "--quiet", "-m", tag)
    git(remote, "tag", "--force", *(["--annotate", "--message", tag] if annotated else []), tag)
    return git(remote, "rev-parse", "HEAD")


class FakeCBM:
    def __init__(self, cache: Path) -> None:
        self.cache = cache
        self.projects: dict[str, Path] = {}
        self.built: list[str] = []
        self.deleted: list[str] = []
        self.fail = False
        self.fail_delete = False

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "list_projects":
            data = {
                "projects": [{"name": name, "root_path": str(path)} for name, path in self.projects.items()],
                "has_more": False,
            }
        elif name == "delete_project":
            if self.fail_delete:
                return {"isError": True, "content": [{"type": "text", "text": "delete failed"}]}
            project = arguments["project"]
            self.deleted.append(project)
            self.projects.pop(project, None)
            (self.cache / f"{project}.db").unlink(missing_ok=True)
            data = {"deleted": project}
        else:
            self.built.append(arguments["name"])
            if self.fail:
                return {"isError": True, "content": [{"type": "text", "text": "build failed"}]}
            self.projects[arguments["name"]] = Path(arguments["repo_path"])
            (self.cache / f"{arguments['name']}.db").write_text("fake published index\n")
            data = {"project": arguments["name"]}
        return {"isError": False, "structuredContent": data}


@pytest.fixture
def setup_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    remotes = {}
    for repo in ("vllm", "vllm-ascend"):
        remote = tmp_path / f"remote-{repo}"
        remote.mkdir()
        git(remote, "init", "--quiet")
        release(remote, "v1.0.0", "def first():\n    return 1\n", annotated=True)
        remotes[repo] = remote
    upstream = FakeCBM(tmp_path / "cache")
    monkeypatch.setattr(update, "LocalCBM", lambda *args: upstream)
    monkeypatch.setattr(update, "_available_memory", lambda: 20 * update.GIB)
    args = [
        "--vllm-root", str(tmp_path / "snapshots-vllm"),
        "--vllm-ascend-root", str(tmp_path / "snapshots-vllm-ascend"),
        "--vllm-remote", str(remotes["vllm"]),
        "--vllm-ascend-remote", str(remotes["vllm-ascend"]),
        "--cache-dir", str(tmp_path / "cache"),
        "--cbm-binary", "git",
        "--min-free-disk-gib", "0.001",
    ]
    return args, remotes, upstream


def snapshots(tmp_path: Path):
    return SnapshotRegistry.discover(tmp_path / "snapshots-vllm", tmp_path / "snapshots-vllm-ascend")


def test_annotated_tag_peeling_is_order_independent_and_filters_nonversions() -> None:
    tags = update._parse_tags(
        "commit refs/tags/v0.30.0^{}\nobject refs/tags/v0.30.0\n"
        "old refs/tags/v0.9.0\nrc refs/tags/v0.31.0-rc2\n"
        "dev refs/tags/v99.0.0.dev1\nlocal refs/tags/v99.0.0+local\nother refs/tags/latest\n",
    )
    assert tags["v0.30.0"].commit == "commit"
    assert tags["v0.31.0-rc2"].version == Version("0.31.0rc2")
    assert tags["v99.0.0.dev1"].version.is_devrelease
    assert tags["v99.0.0+local"].version.local == "local"
    assert len(tags) == 5


def test_updates_new_and_moved_older_tag_then_skips_unchanged(setup_update, tmp_path: Path) -> None:
    args, remotes, upstream = setup_update
    assert update.main(args) == 0
    assert upstream.built == ["vllm-kb-vllm-1.0.0", "vllm-kb-vllm-ascend-1.0.0"]
    assert (tmp_path / "snapshots-vllm/1.0.0/vllm-1.0.0/.git").is_dir()
    assert (tmp_path / "snapshots-vllm-ascend/v1.0.0/vllm-ascend-1.0.0/.git").is_dir()

    assert update.main(args) == 0
    assert len(upstream.built) == 2
    moved = release(remotes["vllm"], "v1.0.0", "def first():\n    return 2\n")
    release(remotes["vllm"], "v1.1.0rc1", "def second():\n    return 3\n")
    assert update.main(args) == 0
    assert upstream.built[2:] == ["vllm-kb-vllm-1.0.0", "vllm-kb-vllm-1.1.0rc1"]
    old = snapshots(tmp_path).resolve("vllm-project/vllm", "1.0.0")
    assert git(old.path, "rev-parse", "HEAD") == moved
    assert json.loads((old.path.parent / update.RECEIPT).read_text())["commit"] == moved
    assert git(old.path, "rev-parse", "--is-shallow-repository") == "true"


def test_initial_sync_builds_every_remote_version(setup_update, tmp_path: Path) -> None:
    args, remotes, upstream = setup_update
    release(remotes["vllm"], "v0.8.0", "older version\n")
    release(remotes["vllm"], "v1.1.0rc1", "release candidate\n")
    release(remotes["vllm"], "v1.1.0", "stable version\n")
    release(remotes["vllm-ascend"], "v0.9.0rc1", "older ascend version\n")
    release(remotes["vllm-ascend"], "v1.2.0rc1", "new ascend version\n")
    assert update.main(args) == 0
    assert upstream.built == [
        "vllm-kb-vllm-0.8.0", "vllm-kb-vllm-1.0.0", "vllm-kb-vllm-1.1.0rc1", "vllm-kb-vllm-1.1.0",
        "vllm-kb-vllm-ascend-0.9.0rc1", "vllm-kb-vllm-ascend-1.0.0", "vllm-kb-vllm-ascend-1.2.0rc1",
    ]
    assert len(snapshots(tmp_path).snapshots) == 7
    assert update.main(args) == 0
    assert len(upstream.built) == 7


def test_sync_adds_missing_older_and_multiple_newer_versions(setup_update, tmp_path: Path) -> None:
    args, remotes, upstream = setup_update
    assert update.main(args) == 0
    release(remotes["vllm"], "v0.9.0", "newly discovered older tag\n")
    release(remotes["vllm"], "v1.1.0", "intermediate new version\n")
    release(remotes["vllm"], "v1.2.0", "highest new version\n")
    release(remotes["vllm-ascend"], "v0.8.0rc1", "newly discovered older ascend tag\n")
    assert update.main(args) == 0
    assert upstream.built[2:] == [
        "vllm-kb-vllm-0.9.0", "vllm-kb-vllm-1.1.0", "vllm-kb-vllm-1.2.0", "vllm-kb-vllm-ascend-0.8.0rc1",
    ]
    assert len(snapshots(tmp_path).snapshots) == 6


def test_annotation_only_change_does_not_rebuild(setup_update) -> None:
    args, remotes, upstream = setup_update
    assert update.main(args) == 0
    git(remotes["vllm"], "tag", "--force", "--annotate", "--message", "new annotation", "v1.0.0")
    assert update.main(args) == 0
    assert len(upstream.built) == 2


def test_failed_rebuild_keeps_old_receipt_and_retries(setup_update, tmp_path: Path) -> None:
    args, remotes, upstream = setup_update
    assert update.main(args) == 0
    snapshot = snapshots(tmp_path).resolve("vllm-project/vllm")
    receipt_path = snapshot.path.parent / update.RECEIPT
    previous = receipt_path.read_text()
    moved = release(remotes["vllm"], "v1.0.0", "def changed():\n    return 2\n")
    upstream.fail = True
    assert update.main(args) == 1
    assert receipt_path.read_text() == previous
    assert git(snapshot.path, "rev-parse", "HEAD") == moved
    upstream.fail = False
    assert update.main(args) == 0
    assert upstream.built.count(snapshot.index_name) == 3
    assert json.loads(receipt_path.read_text())["commit"] == moved


def test_missing_index_rebuilt_and_wrong_binding_rejected(setup_update, tmp_path: Path) -> None:
    args, _, upstream = setup_update
    assert update.main(args) == 0
    snapshot = snapshots(tmp_path).snapshots[0]
    del upstream.projects[snapshot.index_name]
    assert update.main(args) == 0
    assert upstream.built.count(snapshot.index_name) == 2
    upstream.projects[snapshot.index_name] = tmp_path / "wrong"
    assert update.main(args) == 1
    assert upstream.built.count(snapshot.index_name) == 2


def test_missing_cache_db_rebuilds_despite_receipt_and_cached_binding(setup_update, tmp_path: Path) -> None:
    args, _, upstream = setup_update
    assert update.main(args) == 0
    snapshot = snapshots(tmp_path).snapshots[0]
    (upstream.cache / f"{snapshot.index_name}.db").unlink()
    assert snapshot.index_name in upstream.projects
    assert (snapshot.path.parent / update.RECEIPT).is_file()
    assert update.main(args) == 0
    assert upstream.built.count(snapshot.index_name) == 2
    assert (upstream.cache / f"{snapshot.index_name}.db").is_file()


def test_db_without_success_receipt_rebuilds(setup_update, tmp_path: Path) -> None:
    args, _, upstream = setup_update
    assert update.main(args) == 0
    snapshot = snapshots(tmp_path).snapshots[0]
    (snapshot.path.parent / update.RECEIPT).unlink()
    assert update.main(args) == 0
    assert upstream.built.count(snapshot.index_name) == 2


def test_version_floors_are_independent_inclusive_and_prune_old_indexes(setup_update, tmp_path: Path) -> None:
    args, remotes, upstream = setup_update
    assert update.main(args) == 0
    for repo in remotes:
        release(remotes[repo], "v1.1.0rc1", "release candidate\n")
        release(remotes[repo], "v1.1.0", "stable release\n")
    vllm_old = snapshots(tmp_path).resolve("vllm-project/vllm")
    leftover = upstream.cache / f"{vllm_old.index_name}.db.stage.abandoned-wal"
    leftover.write_text("unfinished build\n")
    unrelated = upstream.cache / "unrelated.db"
    unrelated.write_text("keep\n")
    scoped = [*args, "--vllm-from-version", "v1.1.0", "--vllm-ascend-from-version", "1.1.0rc1"]
    assert update.main(scoped) == 0
    assert [snapshot.index_name for snapshot in snapshots(tmp_path).snapshots] == [
        "vllm-kb-vllm-1.1.0", "vllm-kb-vllm-ascend-1.1.0rc1", "vllm-kb-vllm-ascend-1.1.0",
    ]
    assert set(upstream.deleted) == {"vllm-kb-vllm-1.0.0", "vllm-kb-vllm-ascend-1.0.0"}
    assert not leftover.exists()
    assert not vllm_old.path.parent.exists()
    assert unrelated.read_text() == "keep\n"
    assert update.main(scoped) == 0
    assert len(upstream.built) == 5


def test_floor_for_one_repository_preserves_the_other(setup_update, tmp_path: Path) -> None:
    args, remotes, upstream = setup_update
    assert update.main(args) == 0
    release(remotes["vllm"], "v2.0.0", "new vllm\n")
    assert update.main([*args, "--vllm-from-version", "2.0.0"]) == 0
    assert upstream.deleted == ["vllm-kb-vllm-1.0.0"]
    assert snapshots(tmp_path).resolve("vllm-project/vllm-ascend", "1.0.0").path.is_dir()


def test_prune_removes_orphan_db_and_stage_without_sources(setup_update, tmp_path: Path) -> None:
    args, _, upstream = setup_update
    assert update.main(args) == 0
    old_name = "vllm-kb-vllm-0.8.0"
    upstream.projects[old_name] = tmp_path / "snapshots-vllm/0.8.0/vllm-0.8.0"
    old_db = upstream.cache / f"{old_name}.db"
    old_db.write_text("orphan index\n")
    stage = upstream.cache / "vllm-kb-vllm-0.9.0.db.stage.unpublished"
    stage.write_text("unfinished index\n")
    assert update.main([*args, "--vllm-from-version", "1.0.0"]) == 0
    assert not old_db.exists()
    assert not stage.exists()
    assert upstream.deleted == [old_name]
    assert len(upstream.built) == 2


def test_prune_dry_run_changes_nothing(setup_update, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    args, remotes, upstream = setup_update
    assert update.main(args) == 0
    release(remotes["vllm"], "v2.0.0", "new vllm\n")
    assert update.main([*args, "--vllm-from-version", "2.0.0", "--dry-run"]) == 0
    assert "remove: vllm-kb-vllm-1.0.0" in capsys.readouterr().out
    assert snapshots(tmp_path).resolve("vllm-project/vllm", "1.0.0").path.is_dir()
    assert (upstream.cache / "vllm-kb-vllm-1.0.0.db").is_file()
    assert upstream.deleted == []
    assert len(upstream.built) == 2


@pytest.mark.parametrize("failure", ["dirty", "binding", "delete", "empty_selection"])
def test_failed_prune_preserves_sources(setup_update, tmp_path: Path, failure: str) -> None:
    args, remotes, upstream = setup_update
    assert update.main(args) == 0
    snapshot = snapshots(tmp_path).snapshots[0]
    release(remotes["vllm"], "v2.0.0", "new vllm\n")
    floor = "2.0.0"
    if failure == "dirty":
        (snapshot.path / "local.txt").write_text("local edits\n")
    elif failure == "binding":
        upstream.projects[snapshot.index_name] = tmp_path / "another-source"
    elif failure == "delete":
        upstream.fail_delete = True
    else:
        floor = "99.0.0"
    assert update.main([*args, "--vllm-from-version", floor]) == 1
    assert snapshot.path.is_dir()
    assert (upstream.cache / f"{snapshot.index_name}.db").is_file()
    assert upstream.deleted == []


def test_prune_handles_removed_old_remote_tag(setup_update, tmp_path: Path) -> None:
    args, remotes, upstream = setup_update
    assert update.main(args) == 0
    release(remotes["vllm"], "v2.0.0", "new vllm\n")
    git(remotes["vllm"], "tag", "--delete", "v1.0.0")
    assert update.main([*args, "--vllm-from-version", "2.0.0"]) == 0
    assert upstream.deleted == ["vllm-kb-vllm-1.0.0"]
    assert len(snapshots(tmp_path).snapshots) == 2


@pytest.mark.parametrize("untracked", [False, True])
def test_local_edits_are_preserved(setup_update, tmp_path: Path, untracked: bool) -> None:
    args, remotes, upstream = setup_update
    assert update.main(args) == 0
    snapshot = snapshots(tmp_path).snapshots[0]
    edited = snapshot.path / ("local.py" if untracked else "example.py")
    edited.write_text("local work\n")
    release(remotes["vllm"], "v1.0.0", "new remote source\n")
    assert update.main(args) == 1
    assert edited.read_text() == "local work\n"
    assert len(upstream.built) == 2


def test_dry_run_creates_no_roots_or_cache(setup_update, tmp_path: Path) -> None:
    args, _, upstream = setup_update
    assert update.main([*args, "--dry-run"]) == 0
    assert not (tmp_path / "snapshots-vllm").exists()
    assert not (tmp_path / "snapshots-vllm-ascend").exists()
    assert not (tmp_path / "cache").exists()
    assert upstream.built == []


def test_removed_tracked_tag_fails_without_deleting_snapshot(setup_update, tmp_path: Path) -> None:
    args, remotes, upstream = setup_update
    assert update.main(args) == 0
    snapshot = snapshots(tmp_path).snapshots[0]
    release(remotes["vllm"], "v2.0.0", "next release\n")
    git(remotes["vllm"], "tag", "--delete", "v1.0.0")
    assert update.main(args) == 1
    assert snapshot.path.is_dir()
    assert len(upstream.built) == 2


def test_insufficient_memory_stops_before_fetch(setup_update, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args, _, upstream = setup_update
    monkeypatch.setattr(update, "_available_memory", lambda: update.GIB)
    assert update.main(args) == 1
    assert not (tmp_path / "snapshots-vllm").exists()
    assert upstream.built == []


def test_disk_check_includes_cbm_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    disk_usage = update.shutil.disk_usage
    monkeypatch.setattr(update.shutil, "disk_usage", lambda path: disk_usage(path)._replace(
        free=update.GIB if path == cache else 20 * update.GIB,
    ))
    with pytest.raises(update.UpdateError, match=r"free at .*cache"):
        update._check_resources((tmp_path, cache), 10, 6)


def test_concurrent_updater_refused(setup_update, tmp_path: Path) -> None:
    args, _, upstream = setup_update
    root = tmp_path / "snapshots-vllm"
    root.mkdir()
    with (root / ".update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert update.main(args) == 1
    assert upstream.built == []


def test_fetch_race_does_not_publish_partial_snapshot(setup_update, tmp_path: Path) -> None:
    _, remotes, _ = setup_update
    root = tmp_path / "snapshots-vllm"
    root.mkdir()
    checkout = update._plan("vllm", root, str(remotes["vllm"]))[0]
    release(remotes["vllm"], "v1.0.0", "tag moved during fetch\n")
    with pytest.raises(update.UpdateError, match="tag moved during fetch"):
        update._sync(checkout, None)
    assert not checkout.snapshot.path.exists()
    assert list(root.iterdir()) == []


def test_publish_failure_leaves_registry_retryable(
    setup_update, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, remotes, _ = setup_update
    root = tmp_path / "snapshots-vllm"
    root.mkdir()
    checkout = update._plan("vllm", root, str(remotes["vllm"]))[0]

    def failed_rename(*args):
        raise OSError("publication failed")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", failed_rename)
        with pytest.raises(OSError, match="publication failed"):
            update._sync(checkout, None)
    assert list(root.iterdir()) == []
    update._sync(checkout, None)
    assert git(checkout.snapshot.path, "rev-parse", "HEAD") == checkout.tag.commit


@pytest.mark.asyncio
async def test_resource_pressure_terminates_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / "slow-cbm"
    executable.write_text("#!/bin/sh\nexec sleep 60\n")
    executable.chmod(0o700)
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def capture_process(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    def low_memory():
        raise update.UpdateError("low memory during build")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_process)
    upstream = update.LocalCBM(str(executable), tmp_path / "cache", 4096, 2, low_memory)
    with pytest.raises(update.UpdateError, match="low memory during build"):
        await upstream.call_tool("index_repository", {"repo_path": str(tmp_path)})
    assert len(processes) == 1
    assert processes[0].returncode is not None
