"""Build a reusable frozen text corpus with benchmark threads excluded before indexing.

SQLite FTS5 owns lexical matching and BM25 ranking. The index is rebuildable from
GitHub observations; benchmark labels and closing relationships are not indexed.
"""  # noqa: INP001 - Experimental retrieval, not a production package.

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

from .audit import facts, select
from .gates import pointer
from .stack_search import TEXT_FAMILIES, payload


def build(db, path, excluded, scope):
    """Create a fresh index, preserving source pointers and complete-family counts.

    Args:
        db: Read-only source connection with the frozen TEMP selected table.
        path: New derived SQLite path; existing files are rejected.
        excluded: Source thread numbers excluded from text and corpus statistics.
        scope: Frozen input identity to store verbatim alongside the corpus.
    """
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    with sqlite3.connect(path) as index:
        index.executescript("""
            CREATE VIRTUAL TABLE docs USING fts5(
                number UNINDEXED, observation UNINDEXED, digest UNINDEXED, location UNINDEXED,
                text, tokenize='unicode61'
            );
            CREATE TABLE threads (number INTEGER PRIMARY KEY, title TEXT, url TEXT, kind TEXT);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        for family in TEXT_FAMILIES:
            for oid, number, digest, value in facts(db, family):
                if number in excluded:
                    continue
                if family == "issue":
                    index.execute("INSERT INTO threads VALUES (?,?,?,?)", (
                        number, value.get("title"), value.get("html_url"),
                        "pull" if "pull_request" in value else "issue",
                    ))
                items = [value] if family == "issue" else value
                for position, item in enumerate(items):
                    text = (item.get("title") or "") + "\n" + (item.get("body") or "")
                    if text.strip():
                        location = "/value" if family == "issue" else f"/value/{position}/body"
                        index.execute("INSERT INTO docs VALUES (?,?,?,?,?)", (number, oid, digest, location, text))
                        counts[family] += 1
            print(json.dumps({"family": family, "documents": counts[family]}), flush=True)
        index.execute("CREATE VIRTUAL TABLE vocabulary USING fts5vocab(docs,'row')")
        metadata = {"scope": scope, "excluded": sorted(excluded), "documents": dict(counts), "schema": 1}
        index.executemany(
            "INSERT INTO meta VALUES (?,?)", [(key, json.dumps(value)) for key, value in metadata.items()],
        )
    return metadata


def search(index, query, limit=20):
    """Return threads ordered by their best matching document's BM25 score.

    Args:
        index: Open frozen text index.
        query: Explicit FTS5 query expression, not natural language interpretation.
        limit: Positive maximum distinct threads to return.
    """
    if limit < 1:
        raise ValueError("The thread limit must be positive")
    found = {}
    for number, oid, digest, location, score, snippet in index.execute(
        "SELECT number,observation,digest,location,rank,snippet(docs,4,'[',']','...',32) "
        "FROM docs WHERE docs MATCH ? ORDER BY rank,rowid", (query,),
    ):
        if number in found:
            continue
        thread = index.execute("SELECT title,url,kind FROM threads WHERE number=?", (number,)).fetchone()
        found[number] = {"number": number, "observation": oid, "digest": digest, "pointer": location,
                         "score": score, "snippet": snippet,
                         **dict(zip(("title", "url", "kind"), thread or (None, None, None), strict=True))}
        if len(found) >= limit:
            break
    return list(found.values())


def verify_matches(db, index, matches, cutoff):
    """Verify retrieved documents against source bytes in one index scan.

    Args:
        db: Read-only canonical GitHub connection.
        index: Open derived text index.
        matches: Retrieved document records, possibly repeated across queries.
        cutoff: Inclusive publication boundary of the experiment.
    """
    found = {}
    for match in matches:
        key = (match["observation"], match["pointer"])
        old = found.get(key, match)
        if (old["number"], old["digest"]) != (match["number"], match["digest"]):
            raise ValueError("Repeated text evidence has conflicting source identities")
        found[key] = match
    # FTS UNINDEXED columns have no lookup index; per-document point scans are quadratic.
    saved = {(oid, location): text for oid, location, text in index.execute(
        "SELECT observation,location,text FROM docs",
    ) if (oid, location) in found}
    for (oid, location), match in found.items():
        row = db.execute(
            "SELECT o.payload_digest,o.resource_number,o.coverage,p.payload FROM fact_observations o "
            "JOIN payload_blobs p ON p.digest=o.payload_digest WHERE o.id=? AND o.id<=?", (oid, cutoff),
        ).fetchone()
        if row is None or row[:3] != (match["digest"], match["number"], "complete"):
            raise ValueError("Retrieved text does not match its source identity")
        value = pointer(payload(row[0], row[3]), location)
        text = ((value.get("title") or "") + "\n" + (value.get("body") or "")
                if isinstance(value, dict) else "\n" + value)
        if saved.get((oid, location)) != text:
            raise ValueError("Indexed text differs from the source observation")
    excluded = json.loads(index.execute("SELECT value FROM meta WHERE key='excluded'").fetchone()[0])
    if set(excluded) & {row[0] for row in index.execute("SELECT DISTINCT number FROM docs")}:
        raise ValueError("The index contains an excluded source thread")
    if index.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("Text index integrity check failed")
    return {"source_documents_verified": len(found), "excluded_threads": len(excluded), "index_quick_check": "ok"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github", type=Path, required=True)
    parser.add_argument("--round", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    run = json.loads(args.round.read_text())
    scope = run["scope"]
    excluded = set().union(*(run["sampling"][key] for key in ("development", "held_out", "known_regression")))
    started = time.monotonic()
    with sqlite3.connect(f"file:{args.github.resolve()}?mode=ro", uri=True) as db:
        repository = dict(db.execute("SELECT key,value FROM archive_meta"))["repository"]
        digest = select(db, scope["cutoff"])
        if repository != scope["repository"] or digest != scope["selected_digest"]:
            raise ValueError("Text corpus input does not match the frozen experiment")
        result = build(db, args.out, excluded, {
            "repository": repository, "cutoff": scope["cutoff"], "selected_digest": digest,
        })
    print(json.dumps({**result, "bytes": args.out.stat().st_size, "seconds": time.monotonic() - started}))


if __name__ == "__main__":
    main()
