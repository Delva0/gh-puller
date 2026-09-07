"""Benchmark source-verified landing lookup against recorded real query sets.

The reference is the independently scanned mapping of every observed landing SHA,
not relevance labels. Results retain exact link equality; scan controls use the
smallest, median and largest nonempty recorded sets without choosing by timings.
"""  # noqa: INP001 - Standalone lookup-cost and equivalence experiment.

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from .audit import facts, select
from .future_search import landings, read_landings
from .landing_index import lookup, verify_index


def scan_reference(db, scope):
    """Enumerate possible targets from source fields, independently of index rows.

    Args:
        db: Fresh read-only GitHub connection without a TEMP selected table.
        scope: Expected repository, cutoff and selected_digest.
    """
    repository = db.execute("SELECT value FROM archive_meta WHERE key='repository'").fetchone()
    if repository != (scope["repository"],) or select(db, scope["cutoff"]) != scope["selected_digest"]:
        raise ValueError("The full-scan source has a different evidence boundary")
    targets = {"0" * 40}
    for family, key in (("pull-git", "landing_sha"), ("pull", "merge_commit_sha")):
        for _oid, _number, _digest, value in facts(db, family):
            if value.get(key):
                targets.add(value[key])
    links, verification = landings(db, targets, scope["cutoff"])
    return {"scope": scope, "links": links, "verification": verification}


def reference_subset(links, commits):
    selected = {commit: links.get(commit, []) for commit in commits}
    observations = {value["observation_id"] for values in selected.values() for value in values}
    return selected, {"source_observations_verified": len(observations),
                      "landing_pointers_verified": sum(map(len, selected.values()))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True,
                        help="Independent full-scan cache; build from the source when this path is absent")
    parser.add_argument("--cases", type=Path, nargs="+", required=True)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("The repetition count must be positive")
    scope = json.loads(args.round.read_text())["scope"]
    identity = {key: scope[key] for key in ("repository", "cutoff", "selected_digest")}
    if not args.reference.exists():
        with closing(sqlite3.connect(f"file:{Path(scope['github']).resolve()}?mode=ro", uri=True)) as db:
            reference = scan_reference(db, identity)
        args.reference.write_text(json.dumps(reference, indent=2) + "\n")
    reference = json.loads(args.reference.read_text())
    if reference["scope"] != identity:
        raise ValueError("The scanned reference has a different evidence boundary")
    queries, inputs = {}, {}
    for path in args.cases:
        raw = path.read_bytes()
        data = json.loads(raw)
        if data["scope"] != scope:
            raise ValueError("The recorded query has a different evidence boundary")
        inputs[str(path)] = sha256(raw).hexdigest()
        for case in data["cases"]:
            histories = case.get("generated", case)["histories"]
            queries[f"{path.stem}:{case['number']}"] = sorted({item["sha"] for h in histories for item in h["commits"]})
    reports = []
    with closing(sqlite3.connect(f"file:{Path(scope['github']).resolve()}?mode=ro", uri=True)) as db, closing(
        sqlite3.connect(f"file:{args.index.resolve()}?mode=ro", uri=True),
    ) as index:
        started = time.monotonic()
        attested = verify_index(index, identity)
        attestation_seconds = time.monotonic() - started
        for name, commits in queries.items():
            expected = reference_subset(reference["links"], commits)
            timings = []
            for _ in range(args.repeat):
                started = time.monotonic()
                actual = lookup(db, index, commits, identity)
                timings.append(time.monotonic() - started)
                if actual != expected:
                    raise ValueError(f"Indexed query differs from the full scan: {name}")
            report = {"query": name, "commits": len(commits), "verification": actual[1], "exact_equal": True,
                      "reused_connection_seconds": timings,
                      "logical_digest": sha256(json.dumps(actual[0], sort_keys=True).encode()).hexdigest()}
            reports.append(report)
            print(json.dumps(report), flush=True)
    ordered = sorted((len(commits), name) for name, commits in queries.items() if commits)
    positions = sorted({0, len(ordered) // 2, len(ordered) - 1}) if ordered else []
    controls = [ordered[position][1] for position in positions]
    comparisons = []
    for name in controls:
        expected = reference_subset(reference["links"], queries[name])
        timings = {"scan": [], "index": []}
        for repeat in range(args.repeat):
            order = ("scan", "index") if repeat % 2 == 0 else ("index", "scan")
            for backend in order:
                started = time.monotonic()
                actual = read_landings(scope, queries[name], args.index if backend == "index" else None)
                timings[backend].append(time.monotonic() - started)
                if actual != expected:
                    raise ValueError(f"Fresh reader differs from the full scan: {backend}, {name}")
        comparisons.append({"query": name, "commits": len(queries[name]), "fresh_reader_seconds": timings})
        print(json.dumps(comparisons[-1]), flush=True)
    report = {"scope": identity, "inputs": inputs, "reference_sha256": sha256(args.reference.read_bytes()).hexdigest(),
              "index": attested, "index_bytes": args.index.stat().st_size, "repetitions": args.repeat,
              "index_attestation_seconds": attestation_seconds, "queries": reports, "controls": comparisons,
              "cost_scope": "Fresh readers include boundary checks and index attestation or frozen selection. "
                            "OS page caches are not cleared; sources and machine load may be active concurrently."}
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
