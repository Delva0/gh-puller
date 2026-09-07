"""Check frozen selection and offline Git resolution against adversarial fixtures."""
# ruff: noqa: INP001, S101 - These standalone research checks use pytest assertions.

import json
import sqlite3
import subprocess
import zlib
from hashlib import sha256

import pytest

from .audit import resolve_objects, select, version_fields
from .stack_search import payload


@pytest.fixture
def observations():
    with sqlite3.connect(":memory:") as db:
        db.execute("""CREATE TABLE fact_observations (
            id INTEGER PRIMARY KEY, family TEXT, subject_key TEXT, resource_number INTEGER,
            coverage TEXT, payload_digest TEXT, origin TEXT, observed_from TEXT, observed_until TEXT
        )""")
        db.executemany("INSERT INTO fact_observations VALUES (?,?,?,?,?,?,?,?,?)", [
            (1, "issue", "issue:1", 1, "complete", "a", "api", "01", "02"),
            (2, "issue", "issue:1", 1, "unavailable", "b", "api", "02", "03"),
            (3, "issue", "issue:1", 1, "complete", "c", "import", "00", "01"),
            (4, "issue-comments", "issue:1", 1, "complete", "d", "api", "01", "02"),
            (5, "issue", "issue:2", 2, "complete", "e", "api", "01", "02"),
        ])
        yield db


def test_latest_incomplete_is_not_replaced_by_old_complete(observations):
    select(observations, 5)
    rows = observations.execute("SELECT id,coverage FROM selected ORDER BY id").fetchall()
    assert rows == [(2, "unavailable"), (4, "complete"), (5, "complete")]


def test_evidence_point_lookups_use_the_temporary_id_index(observations):
    select(observations, 5)
    plan = observations.execute("EXPLAIN QUERY PLAN SELECT payload_digest FROM selected WHERE id=?", (2,)).fetchall()
    assert any("USING INDEX selected_id" in row[-1] for row in plan)


def test_cutoff_is_independent_of_later_publications(observations):
    digest = select(observations, 4)
    observations.execute("DROP TABLE selected")
    observations.execute("INSERT INTO fact_observations VALUES (6,'issue','issue:1',1,"
                         "'complete','f','api','04','05')")
    assert select(observations, 4) == digest
    assert observations.execute("SELECT COUNT(*) FROM selected").fetchone()[0] == 2


def test_equal_windows_use_publication_order(observations):
    observations.execute("INSERT INTO fact_observations VALUES (6,'issue','issue:1',1,"
                         "'complete','f','api','02','03')")
    select(observations, 6)
    assert observations.execute("SELECT id FROM selected WHERE family='issue' AND resource_number=1").fetchone()[0] == 6


def test_payload_integrity():
    raw = json.dumps({"value": []}).encode()
    assert payload(sha256(raw).hexdigest(), zlib.compress(raw)) == {"value": []}
    with pytest.raises(ValueError, match="digest verification"):
        payload("0" * 64, zlib.compress(raw))


@pytest.mark.parametrize(("body", "version", "revision"), [
    ("vLLM Version : 0.11.1rc6.dev35+g29de3cdee", "0.11.1rc6.dev35+g29de3cdee", None),
    ("vLLM Version: 0.5.3.post1\r\n", "0.5.3.post1", None),
    ("vLLM Version: 0.6.0@32e7db25365415841ebc7c4215851743fbb1bad1",
     "0.6.0", "32e7db25365415841ebc7c4215851743fbb1bad1"),
    ("Name: vllm\nVersion: 0.5.3.post1+neuron214", "0.5.3.post1+neuron214", None),
    ("vLLM Version: 0.5.5@", "0.5.5", ""),
])
def test_version_metadata_preserves_revision(body, version, revision):
    fields = version_fields(body, "vllm")
    assert len(fields) == 1
    assert fields[0]["version"] == version
    assert fields[0]["revision"] == revision


def test_version_does_not_use_good_version_from_prose_or_other_package():
    body = "vLLM Version: 0.8.5\nTips: Everything works fine with vLLM version 0.8.4.\nTorch Version: 2.7.0"
    assert [field["version"] for field in version_fields(body, "vllm")] == ["0.8.5"]
    assert version_fields("project_version: 1.2.3", "project")[0]["version"] == "1.2.3"


def test_offline_resolution_peels_tags_and_reports_missing(tmp_path):
    store = tmp_path / "repo.git"
    subprocess.run(["git", "init", "--bare", str(store)], check=True, capture_output=True)

    def run(*args, content=""):
        return subprocess.run(
            ["git", f"--git-dir={store}", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            input=content, text=True, capture_output=True, check=True,
        ).stdout.strip()

    tree = run("mktree")
    commit = run("commit-tree", tree, content="fixture\n")
    run("tag", "-a", "v1", "-m", "fixture", commit)
    tag = run("rev-parse", "refs/tags/v1")
    absent = "f" * 40
    assert resolve_objects(store, [commit, tag, absent]) == {commit: commit, tag: commit, absent: None}
