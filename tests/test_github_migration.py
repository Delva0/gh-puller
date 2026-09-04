"""Verify local archive migration, resumability, and public Git/SQL identities."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import zlib
from typing import TYPE_CHECKING, Any

import pytest

import gh_puller.github.v8.migrate as migration_module
from gh_puller.github.git_store import git_store_path
from gh_puller.github.locking import ArchiveLockedError, archive_lock
from gh_puller.github.v8.migrate import migrate_archive
from gh_puller.github.v8.schema import SCHEMA

if TYPE_CHECKING:
    from pathlib import Path

_REPOSITORY = "acme/widgets"
_TIME = "2026-09-03T12:00:00Z"


def _git(repository: Path, *arguments: str, bare: bool = False) -> str:
    prefix = ["git", "--git-dir", str(repository)] if bare else ["git", "-C", str(repository)]
    result = subprocess.run(
        [*prefix, *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _source_repository(path: Path) -> tuple[str, str, str]:
    _git(path.parent, "init", "--initial-branch=main", str(path))
    _git(path, "config", "user.name", "Archive Test")
    _git(path, "config", "user.email", "archive@example.test")
    (path / "README.md").write_text("base\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "--quiet", "-m", "base")
    base = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "--quiet", "-b", "feature")
    (path / "feature.py").write_text("feature = True\n")
    _git(path, "add", "feature.py")
    _git(path, "commit", "--quiet", "-m", "feature")
    head = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/pull/7/head", head)
    _git(path, "checkout", "--quiet", "main")
    _git(path, "merge", "--quiet", "--no-ff", "feature", "-m", "merge pull")
    landing = _git(path, "rev-parse", "HEAD")
    _git(path, "branch", "-D", "feature")
    return base, head, landing


def _legacy_git_store(database: Path, source: Path, base: str, head: str, landing: str) -> None:
    store = git_store_path(database)
    _git(store.parent, "init", "--bare", str(store))
    _git(store, "config", "gh-puller.repository", _REPOSITORY, bare=True)
    _git(store, "remote", "add", "origin", str(source), bare=True)
    _git(
        store,
        "fetch",
        "--quiet",
        "origin",
        "+refs/heads/*:refs/gh-puller/remotes/heads/*",
        "+refs/pull/7/head:refs/gh-puller/remotes/pulls/7/head",
        bare=True,
    )
    for role, sha in (("base", base), ("head", head), ("comparison", base), ("merge", landing)):
        _git(
            store,
            "update-ref",
            f"refs/gh-puller/snapshots/pulls/7/{role}/{sha}",
            sha,
            bare=True,
        )


def _json(value: Any) -> tuple[str, bytes]:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest(), raw


def _payload(connection: sqlite3.Connection, value: Any) -> str:
    digest, raw = _json(value)
    connection.execute(
        "INSERT INTO payload_blobs(digest, codec, raw_size, payload) VALUES (?, 'zlib-json-v1', ?, ?)",
        (digest, len(raw), zlib.compress(raw)),
    )
    return digest


def _legacy_database(database: Path, base: str, head: str, landing: str) -> str:
    bundle = {
        "schema_version": 6,
        "kind": "pull",
        "number": 7,
        "issue": {"id": 70, "number": 7, "title": "retain history"},
        "pull_request": {
            "detail": {
                "base": {"sha": base},
                "head": {"sha": head},
                "merge_commit_sha": landing,
                "merged": True,
            },
            "commits": [{"sha": head, "commit": {"message": "feature"}}],
            "git": {
                "base_ref": f"refs/gh-puller/snapshots/pulls/7/base/{base}",
                "base_sha": base,
                "comparison_kind": "merge_base",
                "comparison_ref": f"refs/gh-puller/snapshots/pulls/7/comparison/{base}",
                "comparison_sha": base,
                "head_ref": f"refs/gh-puller/snapshots/pulls/7/head/{head}",
                "head_sha": head,
                "merge_commit_ref": f"refs/gh-puller/snapshots/pulls/7/merge/{landing}",
                "merge_commit_sha": landing,
            },
        },
    }
    summary = {
        "created_at": _TIME,
        "id": 70,
        "number": 7,
        "pull_request": {"url": "https://api.github.test/pulls/7"},
        "updated_at": _TIME,
    }
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA)
        connection.execute("DROP VIEW current_pull_commits")
        connection.execute("DROP VIEW current_pull_git")
        connection.execute("DROP TABLE git_pull_commits")
        connection.execute("DROP TABLE git_pull_snapshots")
        connection.executemany(
            "INSERT INTO archive_meta(key, value) VALUES (?, ?)",
            (("schema_version", "7"), ("repository", _REPOSITORY)),
        )
        summary_digest = _payload(connection, summary)
        bundle_digest = _payload(connection, bundle)
        connection.execute(
            """
            INSERT INTO bundle_http_cache(
                bundle_digest, cache_digest, codec, raw_size, payload
            ) VALUES (?, ?, 'zlib-json-v1', 2, ?)
            """,
            (bundle_digest, "cache", zlib.compress(b"{}")),
        )
        connection.execute(
            """
            INSERT INTO pull_runs(
                id, target_at, started_at, observed_until, completed_at,
                status, request_count, changed_items, catalog_items
            ) VALUES (1, ?, ?, ?, ?, 'committed', 5, 1, 1)
            """,
            (_TIME, _TIME, _TIME, _TIME),
        )
        connection.execute(
            """
            INSERT INTO pull_runs(id, target_at, started_at, observed_until, status)
            VALUES (2, '2026-09-03T13:00:00Z', ?, ?, 'pending')
            """,
            (_TIME, _TIME),
        )
        head_values = (7, 70, "pull", _TIME, _TIME, summary_digest, bundle_digest, 1, None)
        connection.execute(
            """
            INSERT INTO resource_heads(
                number, github_id, kind, created_at, updated_at,
                summary_digest, bundle_digest, present, missing_since
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            head_values,
        )
        for row_id, run_id in ((1, 1), (2, 2)):
            connection.execute(
                """
                INSERT INTO resource_versions(
                    id, run_id, observed_at, number, github_id, kind,
                    created_at, updated_at, summary_digest, bundle_digest,
                    present, missing_since
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (row_id, run_id, _TIME, *head_values),
            )
        connection.execute(
            """
            INSERT INTO pull_passes(
                run_id, name, cutoff_at, mode, prepared, catalog_started,
                catalog_complete, next_url, catalog_pages, catalog_items,
                expected_count
            ) VALUES (
                2, 'closing', '2026-09-03T13:00:00Z', 'delta', 1, 1,
                0, 'https://api.github.test/page/2', 1, 100, 200
            )
            """,
        )
        connection.execute(
            """
            INSERT INTO pull_tasks(
                run_id, number, github_id, kind, created_at, updated_at,
                summary_digest, catalog_member, completed
            ) VALUES (2, 7, 70, 'pull', ?, ?, ?, 1, 1)
            """,
            (_TIME, _TIME, summary_digest),
        )
    return bundle_digest


def _fixture(tmp_path: Path) -> tuple[Path, str, str, str, str]:
    source = tmp_path / "source"
    base, head, landing = _source_repository(source)
    database = tmp_path / "archive.sqlite3"
    old_digest = _legacy_database(database, base, head, landing)
    _legacy_git_store(database, source, base, head, landing)
    return database, old_digest, base, head, landing


@pytest.mark.asyncio
async def test_migration_preserves_pending_work_and_publishes_native_archive(
    tmp_path: Path,
) -> None:
    database, old_digest, base, head, landing = _fixture(tmp_path)
    store = git_store_path(database)

    result = await migrate_archive(database)

    assert result.changed is True
    assert result.repository == _REPOSITORY
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        metadata = dict(connection.execute("SELECT key, value FROM archive_meta"))
        assert metadata["schema_version"] == "8"
        assert metadata["git_layout_version"] == "0"
        current = dict(connection.execute("SELECT * FROM current_pull_git").fetchone())
        assert current["head_ref"] == f"refs/github-archive/pulls/7/heads/{head}"
        assert current["landing_ref"] == f"refs/github-archive/pulls/7/landings/{landing}"
        assert current["comparison_sha"] == base
        assert current["history_preserved"] == 1
        assert [dict(row) for row in connection.execute("SELECT * FROM current_pull_commits")] == [
            {
                "number": 7,
                "present": 1,
                "bundle_digest": current["bundle_digest"],
                "ordinal": 0,
                "sha": head,
            },
        ]
        assert current["bundle_digest"] != old_digest
        assert (
            connection.execute(
                "SELECT count(*) FROM payload_blobs WHERE digest = ?",
                (old_digest,),
            ).fetchone()[0]
            == 0
        )
        payload = connection.execute(
            "SELECT raw_size, payload FROM payload_blobs WHERE digest = ?",
            (current["bundle_digest"],),
        ).fetchone()
        bundle = json.loads(zlib.decompress(payload["payload"]))
        assert bundle["schema_version"] == 7
        assert bundle["pull_request"]["git"]["landing_sha"] == landing
        assert "merge_commit_ref" not in bundle["pull_request"]["git"]
        assert (
            connection.execute(
                "SELECT count(*) FROM resource_versions WHERE bundle_digest = ?",
                (current["bundle_digest"],),
            ).fetchone()[0]
            == 2
        )
        assert (
            dict(connection.execute("SELECT * FROM pull_passes WHERE run_id = 2").fetchone())["next_url"]
            == "https://api.github.test/page/2"
        )
        assert (
            connection.execute(
                "SELECT completed FROM pull_tasks WHERE run_id = 2 AND number = 7",
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM bundle_http_cache WHERE bundle_digest = ?",
                (current["bundle_digest"],),
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    assert _git(store, "rev-parse", "refs/heads/main", bare=True) == landing
    assert _git(store, "rev-parse", f"refs/github-archive/upstream/heads/{landing}", bare=True) == landing
    assert _git(store, "rev-parse", current["head_ref"], bare=True) == head
    assert _git(store, "for-each-ref", "--format=%(refname)", "refs/gh-puller", bare=True) == ""
    assert _git(store, "config", "--get", "github-archive.layoutVersion", bare=True) == "0"
    _git(store, "gc", "--prune=now", bare=True)
    assert _git(store, "show", f"{current['head_ref']}:feature.py", bare=True) == "feature = True"

    repeated = await migrate_archive(database)
    assert repeated.changed is False
    assert repeated.bundles == 1


@pytest.mark.asyncio
async def test_migration_refuses_an_active_writer(tmp_path: Path) -> None:
    database, *_ = _fixture(tmp_path)

    async with archive_lock(database):
        with pytest.raises(ArchiveLockedError, match="writer is active"):
            await migrate_archive(database)


@pytest.mark.asyncio
async def test_migration_reuses_published_git_refs_after_sqlite_interruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database, _, _, head, _ = _fixture(tmp_path)
    store = git_store_path(database)
    publish_sqlite = migration_module._publish_sqlite

    def interrupt(*_: Any) -> None:
        raise RuntimeError("injected interruption")

    monkeypatch.setattr(migration_module, "_publish_sqlite", interrupt)
    with pytest.raises(RuntimeError, match="injected interruption"):
        await migrate_archive(database)

    with sqlite3.connect(database) as connection:
        assert dict(connection.execute("SELECT key, value FROM archive_meta"))["schema_version"] == "7"
    assert _git(store, "rev-parse", f"refs/github-archive/pulls/7/heads/{head}", bare=True) == head
    assert _git(store, "for-each-ref", "--format=%(refname)", "refs/gh-puller", bare=True)

    monkeypatch.setattr(migration_module, "_publish_sqlite", publish_sqlite)
    assert (await migrate_archive(database)).changed is True
    assert _git(store, "for-each-ref", "--format=%(refname)", "refs/gh-puller", bare=True) == ""


@pytest.mark.asyncio
async def test_empty_issue_only_archive_does_not_require_a_git_store(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA)
        connection.execute("DROP VIEW current_pull_commits")
        connection.execute("DROP VIEW current_pull_git")
        connection.execute("DROP TABLE git_pull_commits")
        connection.execute("DROP TABLE git_pull_snapshots")
        connection.executemany(
            "INSERT INTO archive_meta(key, value) VALUES (?, ?)",
            (("schema_version", "7"), ("repository", _REPOSITORY)),
        )

    result = await migrate_archive(database)

    assert result.changed is True
    assert not git_store_path(database).exists()
    with sqlite3.connect(database) as connection:
        assert dict(connection.execute("SELECT key, value FROM archive_meta"))["git_layout_version"] == "0"
