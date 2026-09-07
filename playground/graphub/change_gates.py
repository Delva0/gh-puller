"""Audit every indexed source pointer and native file-difference pair independently.

The source pass reconstructs the frozen observation selection; the Git pass uses
individual diff commands rather than the builder's batched tree protocol. Current
object readability is compared with the build's recorded outcomes, not conflated
with publication cutoff or silently substituted into sealed query results.
"""  # noqa: INP001 - Standalone full-index audit.

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from gh_puller.github import git_store_path

from .audit import select
from .change_git import object_types, run
from .change_index import encode, records, verify_index
from .gates import pointer
from .stack_search import payload


def audit(db, store, index, scope):
    """Verify the complete source and file index, preserving readability changes.

    Args:
        db: Fresh read-only canonical source connection.
        store: Canonical Git object store, accessed without fetching.
        index: Read-only sealed change index.
        scope: Frozen GitHub identity without code-graph coverage constraints.
    """
    meta = verify_index(index, scope)
    if (db.execute("SELECT value FROM archive_meta WHERE key='repository'").fetchone() != (scope["repository"],)
            or select(db, scope["cutoff"]) != scope["selected_digest"]):
        raise ValueError("Full change audit uses a different frozen source")
    expected = records(db)
    rows = index.execute("SELECT id,number,kind,inputs,sources FROM changes ORDER BY id").fetchall()
    actual = [{"id": i, "number": n, "kind": k, "inputs": tuple(json.loads(inputs)), "sources": json.loads(sources)}
              for i, n, k, inputs, sources in rows]
    if actual != expected:
        raise ValueError("Change records differ from the complete frozen source reconstruction")
    sources = defaultdict(list)
    for item in actual:
        for source in item["sources"]:
            sources[source["observation"]].append((item["number"], source))
    checked = 0
    for oid, usages in sources.items():
        family, number, digest, compressed = db.execute(
            "SELECT o.family,o.resource_number,o.payload_digest,p.payload FROM selected o "
            "JOIN payload_blobs p ON p.digest=o.payload_digest WHERE o.id=? AND o.coverage='complete'", (oid,),
        ).fetchone()
        document = payload(digest, compressed)
        for n, source in usages:
            if (n, source["family"], source["digest"]) != (number, family, digest):
                raise ValueError("Indexed source identity differs from its canonical observation")
            for location, value in source["locations"].items():
                if pointer(document, location) != value:
                    raise ValueError("Indexed field differs from its exact canonical pointer")
                checked += 1
    print(encode({"stage": "source_pointers", "observations": len(sources), "pointers": checked}), flush=True)
    pairs, initial_types = defaultdict(list), {}
    for change, inputs, encoded, status in index.execute("SELECT id,inputs,detail,status FROM changes ORDER BY id"):
        detail = json.loads(encoded)
        for sha in json.loads(inputs):
            initial_types.setdefault(sha, "commit")
        if status == "endpoint_unavailable":
            initial_types.update(detail["object_types"])
        elif status == "complete":
            pairs[detail["before"], detail["after"]].append(change)
        if status != "complete" and index.execute("SELECT 1 FROM files WHERE change_id=?", (change,)).fetchone():
            raise ValueError("Incomplete reconstruction has searchable file records")
    if sha256(encode(initial_types).encode()).hexdigest() != meta["object_readability_digest"]:
        raise ValueError("Stored reconstruction states differ from their readability attestation")

    def individual(pair):
        result = run(store, "diff", "--name-status", "--no-renames", "--no-ext-diff", "--no-textconv",
                     "--ignore-submodules=none", "-z", *pair)
        result.check_returncode()
        fields = result.stdout.split(b"\0")[:-1]
        return [(file, status.decode()) for status, file in zip(fields[::2], fields[1::2], strict=True)]

    file_count = 0
    ordered = sorted(pairs)
    with ThreadPoolExecutor(max_workers=4) as workers:
        for i, (pair, files) in enumerate(zip(ordered, workers.map(individual, ordered), strict=True), 1):
            for change in pairs[pair]:
                saved = index.execute("SELECT file,change FROM files WHERE change_id=? ORDER BY file",
                                      (change,)).fetchall()
                count = index.execute("SELECT file_count FROM changes WHERE id=?", (change,)).fetchone()[0]
                if saved != files or count != len(files):
                    raise ValueError(f"Independent Git diff differs for change {change}")
                file_count += len(files)
            if i % 5000 == 0:
                print(encode({"stage": "individual_diffs", "done": i, "total": len(ordered)}), flush=True)
    current = object_types(store, initial_types)
    changed = {sha: {"build": kind, "now": current[sha]} for sha, kind in initial_types.items() if kind != current[sha]}
    return {"source_selection_equal": True, "source_records": len(actual), "source_observations": len(sources),
            "source_pointers": checked, "individual_native_pairs": len(ordered), "file_relations": file_count,
            "readability_changes_since_build": changed, "index": meta}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("round", "index", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    scope = json.loads(args.round.read_text())["scope"]
    started = time.monotonic()
    with closing(sqlite3.connect(f"file:{Path(scope['github']).resolve()}?mode=ro", uri=True)) as db, closing(
        sqlite3.connect(f"file:{args.index.resolve()}?mode=ro", uri=True),
    ) as index:
        result = audit(db, git_store_path(scope["github"]), index,
                       {key: scope[key] for key in ("repository", "cutoff", "selected_digest")})
    result["seconds"] = time.monotonic() - started
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(encode({key: value for key, value in result.items() if key != "index"}), flush=True)


if __name__ == "__main__":
    main()
