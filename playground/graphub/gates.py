"""Verify baseline evidence against immutable observations and compare logical replays.

These are correctness gates, not relevance judgments. A valid landing reference does
not prove a fix or a causal connection between the change and a reported failure.
"""  # noqa: INP001 - Standalone experiment verification, not a production interface.

from __future__ import annotations

import argparse
import copy
import json
import sqlite3
from hashlib import sha256
from pathlib import Path

from .stack_search import payload


def pointer(value, path):
    for part in path.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def logical_result(result):
    """Exclude run telemetry while retaining binary identity and all query evidence."""
    value = copy.deepcopy(result)
    del value["seconds"], value["method_sha256"], value["lexical"]["seconds"]
    del value["scope"]["archived_commits"]
    # The deployment pointer's attestation can change without changing executable bytes.
    value["cbm"].pop("validation_commit", None)
    value["membership_only_links"].sort(key=lambda link: json.dumps(link, sort_keys=True))
    return value


def verify(result, db):
    cutoff = result["scope"]["observation_cutoff"]
    excluded = result["scope"]["excluded_thread"]
    observations = {}

    def read(oid):
        if oid not in observations:
            row = db.execute(
                "SELECT o.family,o.resource_number,o.coverage,o.payload_digest,p.payload FROM fact_observations o "
                "JOIN payload_blobs p ON p.digest=o.payload_digest WHERE o.id=? AND o.id<=?", (oid, cutoff),
            ).fetchone()
            if row is None or row[2] != "complete":
                raise ValueError(f"Evidence is not a complete frozen observation: {oid}")
            observations[oid] = (*row[:4], payload(row[3], row[4]))
        return observations[oid]

    root = read(result["input"]["observation_id"])
    if root[0] != "issue" or root[1] != result["input"]["issue"] or root[3] != result["input"]["payload_digest"]:
        raise ValueError("The input observation does not match its recorded identity")
    if excluded != result["input"]["issue"] or result["input"]["traceback"] not in root[4]["value"]["body"]:
        raise ValueError("The input traceback or exclusion does not match its source")
    links = set()
    for arm in result["candidates"].values():
        for candidate in arm["results"]:
            if candidate["number"] == excluded:
                raise ValueError("The excluded issue leaked into code candidates")
            for evidence in candidate["evidence"]:
                link = evidence["github"]
                family, number, _, _, value = read(link["observation_id"])
                if number != candidate["number"] or number != link["number"]:
                    raise ValueError("A landing reference points to a different PR")
                relation = link["relation"]
                if relation == "pull_landing":
                    valid = family == "pull-git" and link["location"] == "/value/landing_sha"
                elif relation == "merged_pull_commit":
                    valid = (family == "pull" and link["location"] == "/value/merge_commit_sha"
                             and value["value"].get("merged") is True)
                else:
                    valid = False
                if not valid or pointer(value, link["location"]) != evidence["commit"]:
                    raise ValueError("A candidate lacks an exact landing reference")
                links.add((link["observation_id"], link["location"], evidence["commit"]))
    lexical_documents = set()
    for query in result["lexical"]["queries"].values():
        for match in query["top"]:
            if match["number"] == excluded:
                raise ValueError("The excluded issue leaked into lexical candidates")
            _, number, _, _, value = read(match["observation_id"])
            if number != match["number"] or not pointer(value, match["location"]):
                raise ValueError("A lexical result lacks a valid source location")
            lexical_documents.add((match["observation_id"], match["location"]))
    encoded = json.dumps(logical_result(result), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return {"observations_verified": len(observations), "landing_references_verified": len(links),
            "lexical_documents_verified": len(lexical_documents), "logical_digest": sha256(encoded).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    result = json.loads(args.result.read_text())
    with sqlite3.connect(f"file:{args.github.resolve()}?mode=ro", uri=True) as db:
        report = verify(result, db)
    if args.reference:
        reference = json.loads(args.reference.read_text())
        if logical_result(reference) != logical_result(result):
            raise ValueError("Query results differ from the reference replay")
        report["replay_equal"] = True
    print(json.dumps(report))


if __name__ == "__main__":
    main()
