"""Upgrade a version-seven archive into the version-eight public format.

Migration is local and network-free. Git objects and permanent refs are published
before one SQLite transaction redirects payload identities and exposes the new
relational indexes. Pending observation work remains resumable.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..archive_format import PullGitSnapshot, pull_git_snapshot, pull_ref, upstream_ref
from ..git_store import git_store_path
from ..locking import archive_lock
from .schema import GIT_LAYOUT_VERSION, SCHEMA, VERSION

if TYPE_CHECKING:
    from collections.abc import Sequence

_BUNDLE_VERSION = 7
_CODEC = "zlib-json-v1"
_LEGACY_PREFIX = "refs/gh-puller/"
_LEGACY_HEADS = f"{_LEGACY_PREFIX}remotes/heads/"
_SHA = re.compile(r"[0-9a-f]{40,64}\Z")


class ArchiveMigrationError(RuntimeError):
    """The archive cannot be migrated without losing or inventing facts."""


@dataclass(frozen=True, slots=True)
class MigrationResult:
    database: Path  # Canonical SQLite path.
    repository: str  # Bound GitHub owner/repo.
    bundles: int  # Referenced bundles rewritten or validated.
    refs: int  # Permanent Git refs published or validated.
    changed: bool  # False when the archive was already current.


@dataclass(frozen=True, slots=True)
class _BundleRewrite:
    old_digest: str
    new_digest: str
    raw: bytes
    value: dict[str, Any]


async def migrate_archive(database: Path) -> MigrationResult:
    """Migrate one stopped archive pair in place without network access.

    Args:
        database: SQLite archive whose companion Git store uses the ``.git`` suffix.

    Returns:
        Canonical archive identity and migration counts.

    Raises:
        ArchiveMigrationError: The source format, Git objects, or payloads are invalid.
        ArchiveLockedError: A puller or another migration owns the archive writer lock.
    """
    destination = await asyncio.to_thread(Path(database).resolve)
    async with archive_lock(destination, wait=False):
        return await asyncio.to_thread(_migrate_archive, destination)


def _migrate_archive(database: Path) -> MigrationResult:
    if not database.is_file():
        raise ArchiveMigrationError(f"archive database does not exist: {database}")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        metadata = dict(connection.execute("SELECT key, value FROM archive_meta"))
        repository = metadata.get("repository")
        if not isinstance(repository, str):
            raise ArchiveMigrationError("archive has no repository identity")
        source_version = metadata.get("schema_version")
        layout = metadata.get("git_layout_version")
        git = _GitRepository(git_store_path(database), repository)
        if source_version == VERSION:
            if layout != GIT_LAYOUT_VERSION:
                raise ArchiveMigrationError(f"unsupported Git layout {layout!r}")
            refs = _validate_index(connection, git)
            git.finish()
            bundles = _referenced_bundle_count(connection)
            return MigrationResult(database, repository, bundles, refs, False)
        if source_version != "7":
            raise ArchiveMigrationError(f"unsupported source schema {source_version!r}")

        bundles = _load_bundles(connection)
        if any(_is_pull(rewrite.value) for rewrite in bundles.values()) and not git.exists:
            raise ArchiveMigrationError(f"companion Git store does not exist: {git.path}")
        refs = git.upstream_refs()
        rewrites = [_rewrite_bundle(rewrite, git, refs) for rewrite in bundles.values()]
        git.publish(refs)
        _publish_sqlite(connection, rewrites)
        _validate_index(connection, git)
        git.finish()
        return MigrationResult(database, repository, len(rewrites), len(refs), True)
    except (
        json.JSONDecodeError,
        sqlite3.Error,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
        zlib.error,
    ) as exc:
        raise ArchiveMigrationError(str(exc)) from exc
    finally:
        connection.close()


class _GitRepository:
    def __init__(self, path: Path, repository: str) -> None:
        self.path = path
        self.repository = repository
        self.exists = path.is_dir()
        if not self.exists:
            return
        if self.run("rev-parse", "--is-bare-repository").strip() != "true":
            raise ArchiveMigrationError(f"Git store is not bare: {path}")
        bound = self.run("config", "--get", "github-archive.repository", ok=(0, 1)).strip()
        legacy = self.run("config", "--get", "gh-puller.repository", ok=(0, 1)).strip()
        layout = self.run("config", "--get", "github-archive.layoutVersion", ok=(0, 1)).strip()
        identity = bound or legacy
        if identity and identity != repository:
            raise ArchiveMigrationError(f"Git store belongs to {identity}, not {repository}")
        if layout and layout != GIT_LAYOUT_VERSION:
            raise ArchiveMigrationError(f"unsupported Git layout {layout!r}")

    def run(
        self,
        *arguments: str,
        input_text: str | None = None,
        ok: tuple[int, ...] = (0,),
    ) -> str:
        result = subprocess.run(
            ("git", "--git-dir", str(self.path), *arguments),
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
            env=os.environ | {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
        )
        if result.returncode not in ok:
            detail = result.stderr.strip() or f"exit status {result.returncode}"
            raise ArchiveMigrationError(f"git {arguments[0]} failed: {detail}")
        return result.stdout

    def upstream_refs(self) -> dict[str, str]:
        if not self.exists:
            return {}
        refs: dict[str, str] = {}
        for ref, sha in self.list_refs("refs/heads", "refs/tags"):
            kind = "heads" if ref.startswith("refs/heads/") else "tags"
            refs[upstream_ref(kind, sha)] = sha
        for ref, sha in self.list_refs(_LEGACY_HEADS):
            name = ref.removeprefix(_LEGACY_HEADS)
            refs[f"refs/heads/{name}"] = sha
            refs[upstream_ref("heads", sha)] = sha
        return refs

    def list_refs(self, *prefixes: str) -> list[tuple[str, str]]:
        if not self.exists:
            return []
        output = self.run("for-each-ref", "--format=%(refname) %(objectname)", *prefixes)
        return [tuple(line.rsplit(" ", 1)) for line in output.splitlines()]

    def has_commit(self, sha: str) -> bool:
        return self.exists and self.run("cat-file", "-t", f"{sha}^{{commit}}", ok=(0, 1, 128)).strip() == "commit"

    def comparison(self, base: str, head: str) -> tuple[str, str | None]:
        if not self.has_commit(base) or not self.has_commit(head):
            return "unavailable", None
        merge_bases = self.run("merge-base", "--all", base, head, ok=(0, 1)).splitlines()
        if len(merge_bases) > 1:
            return "unavailable", None
        if merge_bases:
            return "merge_base", merge_bases[0]
        empty = self.run("hash-object", "-w", "-t", "tree", "--stdin", input_text="").strip()
        return "empty_tree", empty

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.run("merge-base", ancestor, descendant, ok=(0, 1)).strip() == ancestor

    def publish(self, refs: dict[str, str]) -> None:
        if not self.exists:
            return
        if refs:
            self.run(
                "update-ref",
                "--stdin",
                input_text="".join(f"update {ref} {sha}\n" for ref, sha in sorted(refs.items())),
            )
            for ref, sha in refs.items():
                if self.run("rev-parse", ref).strip() != sha:
                    raise ArchiveMigrationError(f"Git ref does not resolve to its object: {ref}")
        self.run("config", "github-archive.repository", self.repository)
        self.run("config", "github-archive.layoutVersion", GIT_LAYOUT_VERSION)
        self._set_head()

    def finish(self) -> None:
        if not self.exists:
            return
        legacy = self.list_refs(_LEGACY_PREFIX)
        if legacy:
            self.run(
                "update-ref",
                "--stdin",
                input_text="".join(f"delete {ref}\n" for ref, _ in legacy),
            )
        self.run("config", "--unset-all", "gh-puller.repository", ok=(0, 5))

    def _set_head(self) -> None:
        branches = {ref for ref, _ in self.list_refs("refs/heads")}
        for candidate in ("refs/heads/main", "refs/heads/master"):
            if candidate in branches:
                self.run("symbolic-ref", "HEAD", candidate)
                return
        if len(branches) == 1:
            self.run("symbolic-ref", "HEAD", branches.pop())


def _load_bundles(connection: sqlite3.Connection) -> dict[str, _BundleRewrite]:
    rows = connection.execute(
        """
        SELECT DISTINCT b.digest, b.codec, b.raw_size, b.payload
        FROM payload_blobs AS b
        JOIN (
            SELECT bundle_digest FROM resource_heads WHERE bundle_digest IS NOT NULL
            UNION
            SELECT bundle_digest FROM resource_versions WHERE bundle_digest IS NOT NULL
        ) AS used ON used.bundle_digest = b.digest
        """,
    )
    bundles = {}
    for row in rows:
        digest = str(row["digest"])
        raw = _decode(digest, str(row["codec"]), int(row["raw_size"]), bytes(row["payload"]))
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ArchiveMigrationError(f"bundle {digest} is not an object")
        bundles[digest] = _BundleRewrite(digest, digest, raw, value)
    return bundles


def _rewrite_bundle(
    source: _BundleRewrite,
    git: _GitRepository,
    refs: dict[str, str],
) -> _BundleRewrite:
    value = copy.deepcopy(source.value)
    value["schema_version"] = _BUNDLE_VERSION
    if _is_pull(value):
        pull = _mapping(value.get("pull_request"), "pull_request")
        pull["git"] = _rewrite_manifest(value, pull, git, refs)
    raw = _json_bytes(value)
    digest = hashlib.sha256(raw).hexdigest()
    return _BundleRewrite(source.old_digest, digest, raw, value)


def _rewrite_manifest(
    bundle: dict[str, Any],
    pull: dict[str, Any],
    git: _GitRepository,
    refs: dict[str, str],
) -> dict[str, Any]:
    number = bundle.get("number")
    if not isinstance(number, int) or number < 1:
        raise ArchiveMigrationError("pull bundle has no valid number")
    old = _mapping(pull.get("git"), f"pull #{number} Git manifest")
    base = _sha(old.get("base_sha"), f"pull #{number} base")
    head = _sha(old.get("head_sha"), f"pull #{number} head")
    comparison_kind, comparison = git.comparison(base, head)
    result: dict[str, Any] = {
        "base_sha": base,
        "comparison_kind": comparison_kind,
        "head_sha": head,
        "history_preserved": None,
    }
    missing = [sha for sha in (base, head) if not git.has_commit(sha)]
    if git.has_commit(base):
        result["base_ref"] = _add_ref(refs, pull_ref(number, "bases", base), base)
    if git.has_commit(head):
        result["head_ref"] = _add_ref(refs, pull_ref(number, "heads", head), head)
    if comparison is not None:
        result["comparison_sha"] = comparison
        result["comparison_ref"] = _add_ref(
            refs,
            pull_ref(number, "comparisons", comparison),
            comparison,
        )
    if missing:
        result["unavailable_commits"] = sorted(missing)
    detail = _mapping(pull.get("detail"), f"pull #{number} detail")
    landing = _optional_sha(detail.get("merge_commit_sha")) if detail.get("merged") is True else None
    if landing is not None and git.has_commit(landing):
        result["landing_sha"] = landing
        result["landing_ref"] = _add_ref(refs, pull_ref(number, "landings", landing), landing)
        if git.has_commit(head):
            result["history_preserved"] = git.is_ancestor(head, landing)
    return result


def _publish_sqlite(connection: sqlite3.Connection, rewrites: Sequence[_BundleRewrite]) -> None:
    connection.executescript(SCHEMA)
    connection.execute("BEGIN IMMEDIATE")
    try:
        for rewrite in rewrites:
            connection.execute(
                """
                INSERT OR IGNORE INTO payload_blobs(digest, codec, raw_size, payload)
                VALUES (?, ?, ?, ?)
                """,
                (rewrite.new_digest, _CODEC, len(rewrite.raw), zlib.compress(rewrite.raw)),
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO bundle_http_cache(
                    bundle_digest, cache_digest, codec, raw_size, payload
                )
                SELECT ?, cache_digest, codec, raw_size, payload
                FROM bundle_http_cache
                WHERE bundle_digest = ?
                """,
                (rewrite.new_digest, rewrite.old_digest),
            )
            connection.execute(
                "UPDATE resource_heads SET bundle_digest = ? WHERE bundle_digest = ?",
                (rewrite.new_digest, rewrite.old_digest),
            )
            connection.execute(
                "UPDATE resource_versions SET bundle_digest = ? WHERE bundle_digest = ?",
                (rewrite.new_digest, rewrite.old_digest),
            )
            snapshot = pull_git_snapshot(rewrite.new_digest, rewrite.value)
            if snapshot is not None:
                _put_index(connection, snapshot)
        for rewrite in rewrites:
            if rewrite.old_digest == rewrite.new_digest:
                continue
            connection.execute(
                "DELETE FROM bundle_http_cache WHERE bundle_digest = ?",
                (rewrite.old_digest,),
            )
            connection.execute(
                "DELETE FROM git_pull_commits WHERE bundle_digest = ?",
                (rewrite.old_digest,),
            )
            connection.execute(
                "DELETE FROM git_pull_snapshots WHERE bundle_digest = ?",
                (rewrite.old_digest,),
            )
            connection.execute(
                """
                DELETE FROM payload_blobs
                WHERE digest = ?
                    AND NOT EXISTS (
                        SELECT 1 FROM resource_heads
                        WHERE summary_digest = ? OR bundle_digest = ?
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM resource_versions
                        WHERE summary_digest = ? OR bundle_digest = ?
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM pull_tasks WHERE summary_digest = ?
                    )
                """,
                (rewrite.old_digest,) * 6,
            )
        connection.execute(
            "INSERT OR REPLACE INTO archive_meta(key, value) VALUES ('git_layout_version', ?)",
            (GIT_LAYOUT_VERSION,),
        )
        connection.execute(
            "UPDATE archive_meta SET value = ? WHERE key = 'schema_version'",
            (VERSION,),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _put_index(connection: sqlite3.Connection, snapshot: PullGitSnapshot) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO git_pull_snapshots(
            bundle_digest, number, merged, base_sha, head_sha,
            comparison_kind, comparison_sha, base_ref, head_ref,
            comparison_ref, landing_sha, landing_ref, history_preserved
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.bundle_digest,
            snapshot.number,
            int(snapshot.merged),
            snapshot.base_sha,
            snapshot.head_sha,
            snapshot.comparison_kind,
            snapshot.comparison_sha,
            snapshot.base_ref,
            snapshot.head_ref,
            snapshot.comparison_ref,
            snapshot.landing_sha,
            snapshot.landing_ref,
            None if snapshot.history_preserved is None else int(snapshot.history_preserved),
        ),
    )
    connection.executemany(
        """
        INSERT OR REPLACE INTO git_pull_commits(bundle_digest, ordinal, sha)
        VALUES (?, ?, ?)
        """,
        ((snapshot.bundle_digest, ordinal, sha) for ordinal, sha in enumerate(snapshot.commits)),
    )


def _validate_index(connection: sqlite3.Connection, git: _GitRepository) -> int:
    rows = connection.execute(
        """
        SELECT base_ref, base_sha, head_ref, head_sha,
            comparison_ref, comparison_sha, landing_ref, landing_sha
        FROM git_pull_snapshots
        """,
    )
    checked: set[str] = set()
    for row in rows:
        for role in ("base", "head", "comparison", "landing"):
            ref = row[f"{role}_ref"]
            sha = row[f"{role}_sha"]
            if ref is None:
                continue
            if not git.exists or git.run("rev-parse", str(ref)).strip() != sha:
                raise ArchiveMigrationError(f"SQLite Git ref is unavailable: {ref}")
            checked.add(str(ref))
    return len(checked)


def _referenced_bundle_count(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        """
        SELECT count(*)
        FROM (
            SELECT bundle_digest FROM resource_heads WHERE bundle_digest IS NOT NULL
            UNION
            SELECT bundle_digest FROM resource_versions WHERE bundle_digest IS NOT NULL
        )
        """,
    ).fetchone()
    return int(row[0])


def _decode(digest: str, codec: str, raw_size: int, payload: bytes) -> bytes:
    if codec != _CODEC:
        raise ArchiveMigrationError(f"unsupported payload codec {codec}")
    raw = zlib.decompress(payload)
    if len(raw) != raw_size or hashlib.sha256(raw).hexdigest() != digest:
        raise ArchiveMigrationError(f"corrupt payload blob {digest}")
    return raw


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _is_pull(bundle: dict[str, Any]) -> bool:
    return bundle.get("kind") == "pull"


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ArchiveMigrationError(f"{field} is not an object")
    return value


def _sha(value: Any, field: str) -> str:
    sha = _optional_sha(value)
    if sha is None:
        raise ArchiveMigrationError(f"{field} has no valid SHA")
    return sha


def _optional_sha(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


def _add_ref(refs: dict[str, str], ref: str, sha: str) -> str:
    existing = refs.setdefault(ref, sha)
    if existing != sha:
        raise ArchiveMigrationError(f"conflicting Git ref target: {ref}")
    return ref
