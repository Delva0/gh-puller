"""Ensure evidence gates reject fabricated, leaked, and mislabeled references."""
# ruff: noqa: INP001, S101 - These standalone research checks use pytest assertions.

import copy
import json
import sqlite3
import zlib
from hashlib import sha256

import pytest

from .gates import logical_result, pointer, verify


@pytest.fixture
def evidence():
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE fact_observations (id INTEGER, family TEXT, resource_number INTEGER, "
                   "coverage TEXT, payload_digest TEXT)")
        db.execute("CREATE TABLE payload_blobs (digest TEXT, payload BLOB)")
        values = [
            (1, "issue", 1, {"body": "Traceback fixture"}),
            (2, "pull", 2, {"merged": True, "merge_commit_sha": "a" * 40}),
            (3, "issue", 3, {"body": "lexical evidence"}),
        ]
        digests = {}
        for oid, family, number, value in values:
            raw = json.dumps({"value": value}).encode()
            digest = sha256(raw).hexdigest()
            digests[oid] = digest
            db.execute("INSERT INTO fact_observations VALUES (?,?,?,'complete',?)", (oid, family, number, digest))
            db.execute("INSERT INTO payload_blobs VALUES (?,?)", (digest, zlib.compress(raw)))
        result = {
            "seconds": 1, "method_sha256": "method", "cbm": {"sha256": "binary", "validation_commit": "attestation"},
            "scope": {"observation_cutoff": 3, "excluded_thread": 1, "archived_commits": 10},
            "input": {"observation_id": 1, "issue": 1, "payload_digest": digests[1], "traceback": "Traceback fixture"},
            "membership_only_links": [],
            "candidates": {"symbol": {"results": [{"number": 2, "evidence": [{
                "commit": "a" * 40, "github": {"observation_id": 2, "number": 2,
                                              "relation": "merged_pull_commit", "location": "/value/merge_commit_sha"},
            }]}]}},
            "lexical": {"seconds": 1, "queries": {"text": {"top": [{
                "number": 3, "observation_id": 3, "location": "/value",
            }]}}},
        }
        yield result, db


def test_exact_references_pass(evidence):
    result, db = evidence
    report = verify(result, db)
    assert report["observations_verified"] == 3
    assert report["landing_references_verified"] == 1


def test_changed_commit_fails(evidence):
    result, db = evidence
    result["candidates"]["symbol"]["results"][0]["evidence"][0]["commit"] = "b" * 40
    with pytest.raises(ValueError, match="exact landing"):
        verify(result, db)


def test_membership_is_not_landing(evidence):
    result, db = evidence
    result["candidates"]["symbol"]["results"][0]["evidence"][0]["github"]["relation"] = "pull_commit"
    with pytest.raises(ValueError, match="exact landing"):
        verify(result, db)


def test_partial_evidence_is_not_complete(evidence):
    result, db = evidence
    db.execute("UPDATE fact_observations SET coverage='partial' WHERE id=2")
    with pytest.raises(ValueError, match="complete frozen"):
        verify(result, db)


def test_source_thread_leak_fails(evidence):
    result, db = evidence
    result["lexical"]["queries"]["text"]["top"][0]["number"] = 1
    with pytest.raises(ValueError, match="excluded issue leaked"):
        verify(result, db)


def test_fabricated_traceback_fails(evidence):
    result, db = evidence
    result["input"]["traceback"] = "invented"
    with pytest.raises(ValueError, match="traceback or exclusion"):
        verify(result, db)


def test_operational_metadata_is_not_query_identity(evidence):
    result, _ = evidence
    replay = copy.deepcopy(result)
    replay["seconds"] = 2
    replay["method_sha256"] = "different-method"
    replay["scope"]["archived_commits"] = 20
    replay["cbm"]["validation_commit"] = "different-attestation"
    assert logical_result(result) == logical_result(replay)
    replay["cbm"]["sha256"] = "different-binary"
    assert logical_result(result) != logical_result(replay)


def test_pointer_array_and_escaped_keys():
    assert pointer({"a/b": [{"~": 1}]}, "/a~1b/0/~0") == 1
