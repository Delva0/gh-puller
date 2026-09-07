"""Materialize frozen PR landing pointers for source-verified indexed lookup.

Landing semantics follow future_search. The index owns no source facts or code
relationships. A sealed index is checked with verify_index before use; lookup
checks each hit against its canonical observation and frozen latest-subject state.
Full-index attestation and per-hit provenance verification are separate costs.
"""  # noqa: INP001 - Standalone downstream-index experiment.

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from .audit import facts, select
from .gates import pointer
from .stack_search import payload

SOURCES = {
    "pull_landing": ("pull-git", "/value/landing_sha"),
    "merged_pull_commit": ("pull", "/value/merge_commit_sha"),
}


def rows_digest(index):
    rows = index.execute("SELECT sha,number,observation,digest,location,relation FROM landings ORDER BY sha,number")
    values = rows.fetchall()
    return len(values), sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def build(db, path, scope):
    """Create a new sealed index from a fresh canonical-source connection.

    Args:
        db: Read-only GitHub connection without an existing TEMP selected table.
        path: New derived SQLite path; existing files are rejected.
        scope: Expected repository, cutoff and selected_digest, without code-graph identity.
    """
    if path.exists():
        raise FileExistsError(path)
    repository = db.execute("SELECT value FROM archive_meta WHERE key='repository'").fetchone()
    if repository != (scope["repository"],) or select(db, scope["cutoff"]) != scope["selected_digest"]:
        raise ValueError("Landing source differs from the frozen boundary")
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as index, index:
        index.executescript("""
            CREATE TABLE landings (
                sha TEXT, number INTEGER, observation INTEGER, digest TEXT, location TEXT, relation TEXT,
                PRIMARY KEY (sha,number)
            ) WITHOUT ROWID;
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        for relation, (family, location) in SOURCES.items():
            for oid, number, digest, value in facts(db, family):
                commit = value.get(location.rsplit("/", 1)[-1])
                if commit and (family != "pull" or value.get("merged") is True):
                    # Earlier source families retain the registered per-SHA/PR preference.
                    index.execute("INSERT OR IGNORE INTO landings VALUES (?,?,?,?,?,?)",
                                  (commit, number, oid, digest, location, relation))
        count, digest = rows_digest(index)
        metadata = {"schema": 1, "scope": scope, "rows": count, "rows_digest": digest,
                    "coverage": db.execute("SELECT family,coverage,COUNT(*) FROM selected "
                                           "WHERE family IN ('pull','pull-git') GROUP BY family,coverage").fetchall()}
        index.executemany("INSERT INTO meta VALUES (?,?)",
                          [(key, json.dumps(value)) for key, value in metadata.items()])
    return metadata


def metadata(index, scope):
    value = {key: json.loads(value) for key, value in index.execute("SELECT key,value FROM meta")}
    if value.get("schema") != 1 or value.get("scope") != scope:
        raise ValueError("Landing index differs from the requested evidence boundary")
    return value


def verify_index(index, scope):
    """Attest the complete sealed row set before serving queries.

    Args:
        index: Read-only derived index; it must remain unchanged while in use.
        scope: Expected GitHub evidence boundary, as in build.
    """
    value = metadata(index, scope)
    if rows_digest(index) != (value["rows"], value["rows_digest"]):
        raise ValueError("Landing index rows differ from their build attestation")
    if index.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("Landing index integrity check failed")
    return value


def lookup(db, index, commits, scope):
    """Return the scanning lane's link shape, with canonical verification for every hit.

    Args:
        db: Read-only canonical GitHub source, which may have later publications.
        index: Sealed read-only index checked by verify_index before use.
        commits: Exact Git object IDs; missing targets retain empty result lists.
        scope: Expected GitHub evidence boundary, as in build.
    """
    metadata(index, scope)
    if db.execute("SELECT value FROM archive_meta WHERE key='repository'").fetchone() != (scope["repository"],):
        raise ValueError("Landing lookup uses a different repository")
    ordered = sorted(set(commits))
    output = {commit: [] for commit in ordered}
    observed = {}
    count = 0
    for start in range(0, len(ordered), 400):
        group = ordered[start:start + 400]
        slots = ",".join("?" for _ in group)
        rows = index.execute(
            "SELECT sha,number,observation,digest,location,relation FROM landings "  # noqa: S608 - Bound SHA placeholders.
            f"WHERE sha IN ({slots}) ORDER BY sha,relation='merged_pull_commit',number", group,
        )
        for commit, number, oid, digest, location, relation in rows:
            if oid not in observed:
                source = db.execute(
                    "SELECT o.family,o.subject_key,o.resource_number,o.coverage,o.payload_digest,p.payload "
                    "FROM fact_observations o JOIN payload_blobs p ON p.digest=o.payload_digest "
                    "WHERE o.id=? AND o.id<=?", (oid, scope["cutoff"]),
                ).fetchone()
                if source is None or source[2:5] != (number, "complete", digest):
                    raise ValueError("Landing hit differs from its source identity")
                latest = db.execute(
                    "SELECT id FROM fact_observations WHERE family=? AND subject_key=? AND id<=? "
                    "ORDER BY observed_until DESC,observed_from DESC,id DESC LIMIT 1",
                    (*source[:2], scope["cutoff"]),
                ).fetchone()
                if latest != (oid,):
                    raise ValueError("Landing hit is not the frozen latest observation")
                observed[oid] = (source[0], source[2], source[4], payload(source[4], source[5]))
            family, source_number, source_digest, document = observed[oid]
            if ((family, location) != SOURCES.get(relation) or (number, digest) != (source_number, source_digest)
                    or pointer(document, location) != commit
                    or (family == "pull" and document["value"].get("merged") is not True)):
                raise ValueError("Landing hit lacks an exact source pointer")
            output[commit].append({"number": number, "observation_id": oid, "location": location,
                                   "relation": relation, "digest": digest})
            count += 1
    return output, {"source_observations_verified": len(observed), "landing_pointers_verified": count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    registration = json.loads(args.round.read_text())["scope"]
    scope = {key: registration[key] for key in ("repository", "cutoff", "selected_digest")}
    started = time.monotonic()
    with closing(sqlite3.connect(f"file:{Path(registration['github']).resolve()}?mode=ro", uri=True)) as db:
        report = build(db, args.out, scope)
    with closing(sqlite3.connect(f"file:{args.out.resolve()}?mode=ro", uri=True)) as index:
        verify_index(index, scope)
    print(json.dumps({**report, "bytes": args.out.stat().st_size, "seconds": time.monotonic() - started}), flush=True)


if __name__ == "__main__":
    main()
