"""Exercise temporal retrieval boundaries, merge semantics, and independent path controls."""
# ruff: noqa: INP001, S101 - Standalone research tests use pytest assertions.

import json
import os
import sqlite3
import subprocess
import zlib
from hashlib import sha256

import pytest

from . import future_search
from .case_probe import frames
from .checkpoint import verify_changes
from .future_search import anchors, generate, history, landings, rank


def test_path_control_retains_unresolved_frames_and_separate_traceback_leaves():
    log = ('Traceback (most recent call last):\nFile "/repo/pkg/a.py", line 3, in run\n'
           'RuntimeError: failed\nTraceback (most recent call last):\n'
           'File "/repo/pkg/b.py", line 5, in wrapper\nRuntimeError: wrapped\n')
    parsed = frames(log, {"pkg/a.py", "pkg/b.py"})
    parsed[0].update(status="resolved", symbols=[{"qn": "project.pkg.a.run", "file": "pkg/a.py", "start": 1, "end": 3}])
    parsed[1].update(status="unresolved", symbols=[])
    assert [item["roles"] for item in anchors(log, parsed)] == [["leaf", "stack", "terminal"]]
    paths = anchors(log, parsed, symbol=False)
    assert [item["roles"] for item in paths] == [["leaf", "stack"], ["leaf", "stack", "terminal"]]
    assert all("qn" not in item["node"] for item in paths)
    assert anchors("", []) == []


@pytest.fixture
def repository(tmp_path):
    env = os.environ | {"GIT_AUTHOR_NAME": "Graphub fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                        "GIT_COMMITTER_NAME": "Graphub fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
                        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_NO_LAZY_FETCH": "1"}

    def command(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], env=env, check=True, capture_output=True,
                              text=True, timeout=10).stdout.strip()

    command("init", "-b", "main")
    (tmp_path / "target.txt").write_text("old\n")
    command("add", "target.txt")
    command("commit", "-m", "base")
    return tmp_path, command


@pytest.mark.parametrize("change_on_side", [True, False])
def test_file_history_requires_a_first_parent_change_at_a_merge(repository, *, change_on_side):
    root, command = repository
    lower = command("rev-parse", "HEAD")
    command("branch", "side")
    command("switch", "side" if change_on_side else "main")
    (root / "target.txt").write_text("new\n")
    command("commit", "-am", "target change")
    change = command("rev-parse", "HEAD")
    command("switch", "main" if change_on_side else "side")
    (root / "unrelated.txt").write_text("unrelated\n")
    command("add", "unrelated.txt")
    command("commit", "-m", "unrelated change")
    command("switch", "main")
    command("merge", "--no-ff", "side", "-m", "PR landing")
    upper = command("rev-parse", "HEAD")
    commits = [item["sha"] for item in history(root / ".git", lower, upper, "target.txt")]
    assert change in commits
    assert (upper in commits) is change_on_side
    assert lower not in commits
    assert history(root / ".git", lower, upper, "absent.txt") == []


def test_line_history_uses_the_upper_interval_and_excludes_the_lower_version(repository):
    root, command = repository
    lower = command("rev-parse", "HEAD")
    (root / "target.txt").write_text("new\n")
    command("commit", "-am", "target change")
    upper = command("rev-parse", "HEAD")
    assert [item["sha"] for item in history(root / ".git", lower, upper, "target.txt", 1, 1)] == [upper]


def test_independent_diff_gate_deduplicates_pairs_and_rejects_unmodified_files(repository):
    root, command = repository
    (root / "target.txt").write_text("changed\n")
    command("commit", "-am", "change target")
    commit = command("rev-parse", "HEAD")
    assert verify_changes(root / ".git", [(commit, "target.txt")] * 2) == {"commits": 1, "commit_file_pairs": 1}
    with pytest.raises(ValueError, match="first-parent diff"):
        verify_changes(root / ".git", [(commit, "unrelated.txt")])
    assert verify_changes(root / ".git", []) == {"commits": 0, "commit_file_pairs": 0}


@pytest.mark.parametrize("related", [None, False])
def test_unverified_or_unrelated_versions_do_not_produce_histories(monkeypatch, related):
    monkeypatch.setattr(future_search, "ancestry", lambda *_: related)
    result = generate(None, "lower", "upper", [], {})
    assert result["status"] == "unverified_version_interval"
    assert result["histories"] == []


def test_missing_endpoint_does_not_turn_a_file_history_into_a_symbol_lineage(repository):
    root, command = repository
    commit = command("rev-parse", "HEAD")
    anchor = {"node": {"qn": "project.example.run", "file": "target.txt", "start": 1, "end": 1},
              "roles": ["stack", "terminal"]}
    result = generate(root / ".git", commit, commit, [anchor], {})
    assert result["unresolved_at_upper"] == [anchor["node"]]
    assert [item["kind"] for item in result["histories"]] == ["file"]


def test_ranking_uses_topology_and_excludes_membership_and_source_threads():
    scope = {"kind": "file", "roles": ["terminal"], "lower_anchor": {"file": "pkg/a.py"},
             "commits": [{"sha": "later", "time": 1}, {"sha": "earlier", "time": 2}]}
    links = {"later": [{"number": 2, "relation": "pull_landing"}],
             "earlier": [{"number": 1, "relation": "merged_pull_commit"},
                         {"number": 3, "relation": "pull_commit"}, {"number": 4, "relation": "pull_landing"}]}
    result = rank([scope], links, {"earlier": 0, "later": 1}, {4})
    assert result["terminal_file"]["ranking"] == [1, 2]
    assert result["terminal_upper_symbol"]["ranking"] == []


@pytest.mark.parametrize("merged", [True, False])
def test_landing_validation_checks_the_payload_not_just_the_pointer(monkeypatch, *, merged):
    raw = json.dumps({"value": {"merged": merged, "merge_commit_sha": "commit"}}).encode()
    digest = sha256(raw).hexdigest()
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE selected (id INTEGER, family TEXT, resource_number INTEGER, "
                   "coverage TEXT, payload_digest TEXT)")
        db.execute("CREATE TABLE payload_blobs (digest TEXT, payload BLOB)")
        db.execute("INSERT INTO selected VALUES (1,'pull',42,'complete',?)", (digest,))
        db.execute("INSERT INTO payload_blobs VALUES (?,?)", (digest, zlib.compress(raw)))
        monkeypatch.setattr(future_search, "pull_links", lambda *_: {"commit": [
            {"number": 42, "observation_id": 1, "location": "/value/merge_commit_sha",
             "relation": "merged_pull_commit"},
        ]})
        if merged:
            links, verification = landings(db, {"commit"}, 1)
            assert links["commit"][0]["digest"] == digest
            assert verification["landing_pointers_verified"] == 1
        else:
            with pytest.raises(ValueError, match="exact landing"):
                landings(db, {"commit"}, 1)
