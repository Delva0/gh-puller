"""Exercise frozen landing-index semantics independently of repository vocabulary."""
# ruff: noqa: INP001, S101 - Standalone research tests use pytest assertions.

import json
import sqlite3
import zlib
from hashlib import sha256

import pytest

from . import landing_index
from .audit import select
from .future_search import landings, read_landings
from .landing_index import build, lookup, verify_index
from .landing_probe import reference_subset, scan_reference


@pytest.fixture
def canonical():
    with sqlite3.connect(":memory:") as db:
        db.executescript("""
            CREATE TABLE archive_meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO archive_meta VALUES ('repository','example/widgets');
            CREATE TABLE fact_observations (
                id INTEGER PRIMARY KEY, family TEXT, subject_key TEXT, resource_number INTEGER,
                coverage TEXT, payload_digest TEXT, origin TEXT, observed_from TEXT, observed_until TEXT
            );
            CREATE INDEX fact_observations_subject ON fact_observations
                (family,subject_key,observed_until DESC,observed_from DESC,id DESC);
            CREATE TABLE payload_blobs (digest TEXT PRIMARY KEY, payload BLOB);
            CREATE TABLE commit_reference_index (sha TEXT, observation_id INTEGER);
        """)

        def add(oid, family, number, commit, *, coverage="complete", merged=True, window=None):
            value = {"landing_sha": commit} if family == "pull-git" else {"merge_commit_sha": commit, "merged": merged}
            raw = json.dumps({"repository": "example/widgets", "value": value}).encode()
            digest = sha256(raw).hexdigest()
            db.execute("INSERT OR IGNORE INTO payload_blobs VALUES (?,?)", (digest, zlib.compress(raw)))
            window = window or f"{oid:04d}"
            db.execute("INSERT INTO fact_observations VALUES (?,?,?,?,?,?,?,?,?)",
                       (oid, family, f"pull:{number}", number, coverage, digest, "api", window, window))

        yield db, add


def prepare(db, path, cutoff=10):
    scope = {"repository": "example/widgets", "cutoff": cutoff, "selected_digest": select(db, cutoff)}
    db.execute("DROP TABLE selected")
    build(db, path, scope)
    return scope


def test_index_matches_scanning_preference_and_keeps_distinct_landing_sources(canonical, tmp_path):
    db, add = canonical
    add(1, "pull", 1, "a")
    add(2, "pull-git", 1, "a")
    add(3, "pull", 2, "a")
    add(4, "pull", 3, "b", merged=False)
    add(5, "pull-git", 4, "b")
    add(6, "pull", 4, "c")
    path = tmp_path / "landings.sqlite3"
    scope = prepare(db, path)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as index:
        assert verify_index(index, scope)["rows"] == 4
        result = lookup(db, index, {"a", "b", "c", "absent"}, scope)
        assert result == landings(db, {"a", "b", "c", "absent"}, 10)
        assert [link["observation_id"] for link in result[0]["a"]] == [2, 3]
        assert result[0]["absent"] == []


def test_latest_incomplete_does_not_restore_an_older_complete_source(canonical, tmp_path):
    db, add = canonical
    add(1, "pull-git", 1, "a")
    add(2, "pull-git", 1, "a", coverage="unavailable")
    add(3, "pull", 1, "a")
    add(4, "pull", 2, "b")
    add(5, "pull", 2, "b", coverage="partial")
    path = tmp_path / "landings.sqlite3"
    scope = prepare(db, path)
    with sqlite3.connect(path) as index:
        links, _ = lookup(db, index, {"a", "b"}, scope)
        assert links["a"][0]["relation"] == "merged_pull_commit"
        assert links["a"][0]["observation_id"] == 3 and links["b"] == []


def test_observation_window_and_publication_ties_match_the_source_selection(canonical, tmp_path):
    db, add = canonical
    add(1, "pull", 1, "a", window="02")
    add(2, "pull", 1, "b", window="02")
    add(3, "pull", 1, "c", window="01")
    path = tmp_path / "landings.sqlite3"
    scope = prepare(db, path)
    with sqlite3.connect(path) as index:
        links, _ = lookup(db, index, {"a", "b", "c"}, scope)
        assert links["a"] == links["c"] == []
        assert links["b"][0]["observation_id"] == 2


def test_later_publications_do_not_move_the_frozen_boundary(canonical, tmp_path):
    db, add = canonical
    add(1, "pull", 1, "a")
    path = tmp_path / "landings.sqlite3"
    scope = prepare(db, path)
    add(11, "pull", 1, "b", coverage="unavailable")
    with sqlite3.connect(path) as index:
        assert lookup(db, index, {"a"}, scope)[0]["a"][0]["observation_id"] == 1


def test_per_hit_verification_rejects_a_stale_selected_observation(canonical, tmp_path):
    db, add = canonical
    add(1, "pull", 1, "a")
    path = tmp_path / "landings.sqlite3"
    scope = prepare(db, path)
    add(2, "pull", 1, "b", coverage="unavailable")
    with sqlite3.connect(path) as index, pytest.raises(ValueError, match="frozen latest"):
        lookup(db, index, {"a"}, scope)


@pytest.mark.parametrize("mutation", ["DELETE FROM landings", "UPDATE landings SET sha='corrupt'"])
def test_full_index_attestation_rejects_deleted_or_changed_rows(canonical, tmp_path, mutation):
    db, add = canonical
    add(1, "pull", 1, "a")
    path = tmp_path / "landings.sqlite3"
    scope = prepare(db, path)
    with sqlite3.connect(path) as index:
        index.execute(mutation)
        with pytest.raises(ValueError, match="build attestation"):
            verify_index(index, scope)


def test_source_payload_bytes_are_rehashed_for_retrieved_hits(canonical, tmp_path):
    db, add = canonical
    add(1, "pull", 1, "a")
    path = tmp_path / "landings.sqlite3"
    scope = prepare(db, path)
    db.execute("UPDATE payload_blobs SET payload=?", (zlib.compress(b'{"value": {}}'),))
    with sqlite3.connect(path) as index, pytest.raises(ValueError, match="digest verification"):
        lookup(db, index, {"a"}, scope)


def test_per_hit_verification_does_not_trust_an_index_pointer(canonical, tmp_path):
    db, add = canonical
    add(1, "pull", 1, "a")
    path = tmp_path / "landings.sqlite3"
    scope = prepare(db, path)
    with sqlite3.connect(path) as index:
        index.execute("UPDATE landings SET sha='wrong'")
        with pytest.raises(ValueError, match="exact source pointer"):
            lookup(db, index, {"wrong"}, scope)


def test_index_and_canonical_repository_boundaries_are_independent(canonical, tmp_path):
    db, add = canonical
    add(1, "pull", 1, "a")
    path = tmp_path / "landings.sqlite3"
    scope = prepare(db, path)
    with sqlite3.connect(path) as index:
        with pytest.raises(ValueError, match="evidence boundary"):
            verify_index(index, scope | {"cutoff": 9})
        db.execute("UPDATE archive_meta SET value='another/repository' WHERE key='repository'")
        with pytest.raises(ValueError, match="different repository"):
            lookup(db, index, {"a"}, scope)


def test_empty_queries_do_not_decode_any_source_payload(canonical, tmp_path, monkeypatch):
    db, add = canonical
    add(1, "pull", 1, "a")
    path = tmp_path / "landings.sqlite3"
    scope = prepare(db, path)

    def unexpected(*_args):
        pytest.fail("An empty query decoded a source payload")

    monkeypatch.setattr(landing_index, "payload", unexpected)
    with sqlite3.connect(path) as index:
        assert lookup(db, index, [], scope) == ({}, {"source_observations_verified": 0, "landing_pointers_verified": 0})


def test_existing_output_is_not_overwritten(canonical, tmp_path):
    db, _add = canonical
    path = tmp_path / "existing.sqlite3"
    path.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        build(db, path, {})
    assert path.read_bytes() == b"preserve"


def test_lookup_plans_use_sha_and_subject_indexes(canonical, tmp_path):
    db, add = canonical
    add(1, "pull", 1, "a")
    path = tmp_path / "landings.sqlite3"
    prepare(db, path)
    with sqlite3.connect(path) as index:
        plan = index.execute("EXPLAIN QUERY PLAN SELECT observation FROM landings WHERE sha=?", ("a",)).fetchall()
        assert any("PRIMARY KEY" in row[-1] for row in plan)
    plan = db.execute("EXPLAIN QUERY PLAN SELECT id FROM fact_observations "
                      "WHERE family=? AND subject_key=? AND id<=? "
                      "ORDER BY observed_until DESC,observed_from DESC,id DESC LIMIT 1",
                      ("pull", "pull:1", 10)).fetchall()
    assert any("USING COVERING INDEX fact_observations_subject" in row[-1] for row in plan)


def test_shared_reader_uses_readonly_sources_and_identical_backends(canonical, tmp_path):
    db, add = canonical
    add(1, "pull", 1, "a")
    db.commit()
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as destination:
        db.backup(destination)
    destination.close()
    path = tmp_path / "landings.sqlite3"
    scope = prepare(db, path) | {"github": str(source)}
    assert read_landings(scope, {"a"}, path) == read_landings(scope, {"a"})
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as frozen:
        second = tmp_path / "readonly-rebuild.sqlite3"
        build(frozen, second, {key: scope[key] for key in ("repository", "cutoff", "selected_digest")})
    assert read_landings(scope, {"a"}, path) == read_landings(scope, {"a"}, second)
    for backend in (None, path):
        with pytest.raises(ValueError, match="boundary"):
            read_landings(scope | {"repository": "another/repository"}, {"a"}, backend)


def test_incomplete_build_cannot_be_served_as_a_sealed_index(canonical, tmp_path, monkeypatch):
    db, add = canonical
    add(1, "pull", 1, "a")
    scope = {"repository": "example/widgets", "cutoff": 10, "selected_digest": select(db, 10)}
    db.execute("DROP TABLE selected")

    def interrupted(_index):
        raise RuntimeError("Interrupted before attestation")

    monkeypatch.setattr(landing_index, "rows_digest", interrupted)
    path = tmp_path / "partial.sqlite3"
    with pytest.raises(RuntimeError, match="Interrupted"):
        build(db, path, scope)
    with sqlite3.connect(path) as index, pytest.raises(ValueError, match="evidence boundary"):
        verify_index(index, scope)


def test_queries_cross_chunk_boundaries_without_duplicate_results(canonical, tmp_path):
    db, add = canonical
    commits = [f"{number:040x}" for number in range(1, 402)]
    for number, commit in enumerate(commits, 1):
        add(number, "pull", number, commit)
    path = tmp_path / "landings.sqlite3"
    scope = prepare(db, path, 500)
    with sqlite3.connect(path) as index:
        found, verification = lookup(db, index, [*commits, *commits, "') OR 1=1 --"], scope)
    assert verification["source_observations_verified"] == verification["landing_pointers_verified"] == 401
    assert len(found) == 402 and found["') OR 1=1 --"] == []


def test_full_reference_slicing_counts_distinct_source_observations():
    links = {"a": [{"observation_id": 1}, {"observation_id": 2}], "b": [{"observation_id": 2}]}
    found, verification = reference_subset(links, ["a", "b", "missing"])
    assert found["missing"] == []
    assert verification == {"source_observations_verified": 2, "landing_pointers_verified": 3}


def test_reference_universe_includes_unmerged_targets_without_claiming_a_landing(canonical):
    db, add = canonical
    add(1, "pull", 1, "a")
    add(2, "pull-git", 1, "a")
    add(3, "pull", 2, "proposal", merged=False)
    scope = {"repository": "example/widgets", "cutoff": 10, "selected_digest": select(db, 10)}
    db.execute("DROP TABLE selected")
    reference = scan_reference(db, scope)
    assert set(reference["links"]) == {"a", "proposal", "0" * 40}
    assert reference["links"]["proposal"] == []
    assert reference["links"]["a"][0]["observation_id"] == 2
