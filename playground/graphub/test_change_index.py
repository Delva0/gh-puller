"""Exercise source identity, relation semantics and exact path ranking for PR changes."""
# ruff: noqa: INP001, S101 - Standalone research tests use pytest assertions.

import json
import sqlite3
import zlib
from hashlib import sha256

import pytest

from . import change_gates, test_change_git, test_landings
from .audit import select
from .change_index import build, records, search, verify_hits, verify_index
from .test_change_git import commit

canonical = test_landings.canonical
repository = test_change_git.repository


def add(db, oid, number, family, value, *, coverage="complete"):
    raw = json.dumps({"value": value}).encode()
    digest = sha256(raw).hexdigest()
    db.execute("INSERT OR IGNORE INTO payload_blobs VALUES (?,?)", (digest, zlib.compress(raw)))
    db.execute("INSERT INTO fact_observations VALUES (?,?,?,?,?,?,?,?,?)",
               (oid, family, f"pull:{number}", number, coverage, digest, "git", str(oid).zfill(4), str(oid).zfill(4)))


def pull(base, head, *, merged=False, landing=None):
    return {"base": {"sha": base}, "head": {"sha": head}, "merged": merged, "merge_commit_sha": landing}


def snapshot(base, head, comparison, landing=None):
    return {"base_sha": base, "head_sha": head, "comparison_kind": "merge_base", "comparison_sha": comparison,
            "landing_sha": landing}


def prepare(db, store, path, cutoff=10):
    scope = {"repository": "example/widgets", "cutoff": cutoff, "selected_digest": select(db, cutoff)}
    db.execute("DROP TABLE selected")
    build(db, store, path, scope)
    return scope


def test_native_index_keeps_aliases_relations_and_missing_proposals(canonical, repository, tmp_path):
    db, _ = canonical
    a = commit(repository, {b"shared": (b"100644", b"shared")})
    b = commit(repository, {b"shared": (b"100644", b"shared"), b"proposal": (b"100644", b"proposed")}, (a,))
    add(db, 1, 1, "pull", pull(a, b, merged=True, landing=b))
    add(db, 2, 1, "pull-git", snapshot(a, b, a, b))
    add(db, 3, 2, "pull", pull(a, b, landing=b))
    add(db, 4, 3, "pull", pull(a, "1" * 40))
    path = tmp_path / "changes.sqlite3"
    scope = prepare(db, repository, path)
    with sqlite3.connect(path) as index:
        meta = verify_index(index, scope)
        assert meta["rows"] == {"changes": 4, "files": 3}
        assert index.execute("SELECT COUNT(*) FROM changes WHERE status='endpoint_unavailable'").fetchone() == (1,)
        for kind, expected in [("proposal", [1, 2]), ("landing", [1])]:
            hits = search(index, [b"proposal"], kind=kind, method="overlap")
            assert [hit["number"] for hit in hits] == expected
            assert verify_hits(db, repository, index, hits, scope)["changes"] == len(expected)
        aliases = index.execute("SELECT sources FROM changes WHERE number=1 AND kind='proposal'").fetchone()[0]
        assert len(json.loads(aliases)) == 2
        assert search(index, [b"shared"], kind="proposal", method="bm25") == []
        assert [hit["number"] for hit in search(index, [b"proposal"], kind="proposal", method="bm25",
                                               excluded=[1])] == [2]
        hits = search(index, [b"proposal"], kind="proposal", method="overlap", limit=1)
        hits[0]["matched"] = [b"invented"]
        with pytest.raises(ValueError, match="native change evidence"):
            verify_hits(db, repository, index, hits, scope)


def test_conflicting_observed_pairs_remain_separate_but_pr_budget_is_unique(canonical, repository, tmp_path):
    db, _ = canonical
    a = commit(repository, {b"a": (b"100644", b"a")})
    b = commit(repository, {b"x": (b"100644", b"b")}, (a,))
    c = commit(repository, {b"x": (b"100644", b"c")}, (a,))
    add(db, 1, 1, "pull", pull(a, b))
    add(db, 2, 1, "pull-git", snapshot(a, c, a))
    path = tmp_path / "changes.sqlite3"
    prepare(db, repository, path)
    with sqlite3.connect(path) as index:
        assert index.execute("SELECT COUNT(*) FROM changes").fetchone() == (2,)
        assert len(search(index, [b"x"], kind="proposal", method="overlap", limit=20)) == 1


def test_bad_archived_comparison_is_explicit_not_searchable(canonical, repository, tmp_path):
    db, _ = canonical
    a = commit(repository, {b"a": (b"100644", b"a")})
    b = commit(repository, {b"b": (b"100644", b"b")}, (a,))
    add(db, 1, 1, "pull-git", snapshot(a, b, b))
    path = tmp_path / "changes.sqlite3"
    prepare(db, repository, path)
    with sqlite3.connect(path) as index:
        assert index.execute("SELECT status,file_count FROM changes").fetchone() == ("comparison_conflict", 0)
        assert search(index, [b"b"], kind="proposal", method="overlap") == []


def test_latest_incomplete_family_does_not_revive_its_older_complete_record(canonical):
    db, _ = canonical
    add(db, 1, 1, "pull-git", snapshot("a", "b", "a"))
    add(db, 2, 1, "pull-git", {}, coverage="partial")
    add(db, 3, 1, "pull", pull("a", "b"))
    add(db, 4, 2, "pull", pull("c", "d"))
    add(db, 5, 2, "pull", {}, coverage="unavailable")
    select(db, 10)
    values = records(db)
    assert len(values) == 1 and values[0]["number"] == 1
    assert [source["observation"] for source in values[0]["sources"]] == [3]


def test_tampered_rows_and_canonical_pointers_fail_separate_gates(canonical, repository, tmp_path):
    db, _ = canonical
    a = commit(repository, {b"a": (b"100644", b"a")})
    b = commit(repository, {b"b": (b"100644", b"b")}, (a,))
    add(db, 1, 1, "pull", pull(a, b))
    path = tmp_path / "changes.sqlite3"
    scope = prepare(db, repository, path)
    with sqlite3.connect(path) as index:
        hits = search(index, [b"b"], kind="proposal", method="overlap")
        index.execute("DELETE FROM files WHERE file=?", (b"b",))
        with pytest.raises(ValueError, match="attestation"):
            verify_index(index, scope)
        with pytest.raises(ValueError, match="complete native file diff"):
            verify_hits(db, repository, index, hits, scope)
        index.rollback()
        add(db, 2, 1, "pull", {}, coverage="partial")
        with pytest.raises(ValueError, match="frozen latest"):
            verify_hits(db, repository, index, hits, scope)


def test_later_publications_do_not_move_the_frozen_query(canonical, repository, tmp_path):
    db, _ = canonical
    a = commit(repository, {b"a": (b"100644", b"a")})
    b = commit(repository, {b"b": (b"100644", b"b")}, (a,))
    add(db, 1, 1, "pull", pull(a, b))
    path = tmp_path / "changes.sqlite3"
    scope = prepare(db, repository, path)
    add(db, 11, 1, "pull", {}, coverage="partial")
    with sqlite3.connect(path) as index:
        hits = search(index, [b"b"], kind="proposal", method="overlap")
        assert verify_hits(db, repository, index, hits, scope)["changes"] == 1


def test_fixed_bm25_downweights_large_changes_without_path_guessing(canonical, repository, tmp_path):
    db, _ = canonical
    a = commit(repository, {})
    b = commit(repository, {b"x": (b"100644", b"x"), b"y": (b"100644", b"y")}, (a,))
    c = commit(repository, {b"x": (b"100644", b"x")}, (a,))
    add(db, 1, 1, "pull", pull(a, b))
    add(db, 2, 2, "pull", pull(a, c))
    path = tmp_path / "changes.sqlite3"
    prepare(db, repository, path)
    with sqlite3.connect(path) as index:
        assert [h["number"] for h in search(index, [b"x"], kind="proposal", method="overlap")] == [1, 2]
        assert [h["number"] for h in search(index, [b"x"], kind="proposal", method="bm25")] == [2, 1]
        assert search(index, [b"other/x"], kind="proposal", method="bm25") == []
        assert search(index, [], kind="proposal", method="bm25") == []


def test_stable_objects_rebuild_byte_identically_and_pass_full_independent_audit(canonical, repository, tmp_path):
    db, _ = canonical
    a = commit(repository, {b"a": (b"100644", b"a")})
    b = commit(repository, {b"b": (b"100644", b"b")}, (a,))
    add(db, 1, 1, "pull", pull(a, b, merged=True, landing=b))
    first, second = tmp_path / "first.sqlite3", tmp_path / "second.sqlite3"
    scope = prepare(db, repository, first)
    db.execute("DROP TABLE selected")
    build(db, repository, second, scope)
    assert first.read_bytes() == second.read_bytes()
    db.execute("DROP TABLE selected")
    with sqlite3.connect(first) as index:
        result = change_gates.audit(db, repository, index, scope)
        assert result["source_records"] == 2 and result["source_observations"] == 1
        assert result["source_pointers"] == 4 and result["individual_native_pairs"] == 1
        assert result["file_relations"] == 4 and result["readability_changes_since_build"] == {}


def test_hit_pointer_check_is_independent_of_row_attestation(canonical, repository, tmp_path):
    db, _ = canonical
    a = commit(repository, {b"a": (b"100644", b"a")})
    b = commit(repository, {b"b": (b"100644", b"b")}, (a,))
    add(db, 1, 1, "pull", pull(a, b))
    path = tmp_path / "changes.sqlite3"
    scope = prepare(db, repository, path)
    with sqlite3.connect(path) as index:
        hits = search(index, [b"b"], kind="proposal", method="overlap")
        sources = json.loads(index.execute("SELECT sources FROM changes").fetchone()[0])
        sources[0]["locations"]["/value/head/sha"] = a
        index.execute("UPDATE changes SET sources=?", (json.dumps(sources),))
        with pytest.raises(ValueError, match="exact source pointer"):
            verify_hits(db, repository, index, hits, scope)
