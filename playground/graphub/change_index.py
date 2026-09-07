"""Index source-backed PR file changes independently of code-snapshot coverage.

Change boundaries follow change_git. Equivalent endpoint observations share a
record while retaining their individual sources; conflicting pairs stay separate.
Paths are Git bytes, stored as SQLite BLOBs. Complete means a native file-difference
query succeeded, not that every blob, external submodule or runtime fix is verified.
Unavailable states describe this build's object readability, not historical absence.
"""  # noqa: INP001 - Standalone derived-index experiment.

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import subprocess
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from gh_puller.github import git_store_path

from .audit import facts, select
from .change_git import boundary, compare, object_types, run
from .gates import pointer
from .stack_search import payload


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def source_specs(family, oid, number, digest, value):
    source = {"family": family, "observation": oid, "digest": digest}
    if family == "pull-git":
        endpoints = (value["base_sha"], value["head_sha"])
        proposal = {"/value/base_sha": endpoints[0], "/value/head_sha": endpoints[1],
                    "/value/comparison_kind": value["comparison_kind"],
                    "/value/comparison_sha": value["comparison_sha"]}
        landing = {"/value/landing_sha": value["landing_sha"]} if value.get("landing_sha") else None
    else:
        endpoints = (value["base"]["sha"], value["head"]["sha"])
        proposal = {"/value/base/sha": endpoints[0], "/value/head/sha": endpoints[1]}
        landing = ({"/value/merge_commit_sha": value["merge_commit_sha"], "/value/merged": True}
                   if value.get("merged") is True and value.get("merge_commit_sha") else None)
    yield (number, "proposal", endpoints), {**source, "locations": proposal}
    if landing:
        sha = value["landing_sha"] if family == "pull-git" else value["merge_commit_sha"]
        yield (number, "landing", (sha,)), {**source, "locations": landing}


def records(db):
    grouped = defaultdict(list)
    for family in ("pull-git", "pull"):
        for oid, number, digest, value in facts(db, family):
            for key, source in source_specs(family, oid, number, digest, value):
                grouped[key].append(source)
    return [{"id": i, "number": key[0], "kind": key[1], "inputs": key[2], "sources": sources}
            for i, (key, sources) in enumerate(sorted(grouped.items()), 1)]


def rows_digest(index):
    digest, counts = sha256(), {}
    for table, query in (
        ("changes", "SELECT id,number,kind,inputs,sources,detail,status,file_count FROM changes ORDER BY id"),
        ("files", "SELECT change_id,hex(file),change FROM files ORDER BY change_id,file"),
    ):
        digest.update(table.encode() + b"\n")
        counts[table] = 0
        for row in index.execute(query):
            digest.update(encode(row).encode() + b"\n")
            counts[table] += 1
    return {"rows": counts, "rows_digest": digest.hexdigest()}


def verify_index(index, scope):
    """Attest a sealed index before querying it.

    Args:
        index: Read-only derived database, unchanged for the reader's lifetime.
        scope: Frozen repository, cutoff and selected_digest, without graph identity.
    """
    meta = {key: json.loads(value) for key, value in index.execute("SELECT key,value FROM meta")}
    if meta["schema"] != 1 or meta["scope"] != scope:
        raise ValueError("Change index differs from its source boundary")
    if rows_digest(index) != {key: meta[key] for key in ("rows", "rows_digest")}:
        raise ValueError("Change index differs from its build attestation")
    if index.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("Change index integrity check failed")
    return meta


def search(index, paths, *, kind, method, excluded=(), limit=20):
    """Rank exact changed-file matches, collapsing comparisons to distinct PRs.

    Args:
        index: Sealed database already checked by verify_index.
        paths: Repository-relative Git path bytes; no basename or prefix guessing.
        kind: Proposal or landing evidence, not the current merge state of a PR.
        method: Exact overlap count or changed-file BM25 with k1=1.2 and b=0.75.
        excluded: PR/thread numbers removed before corpus statistics and ranking.
        limit: Maximum number of distinct PRs; the best comparison represents each PR.

    Returns:
        Scores, comparison IDs and matched path bytes. Scores concern changed-file
        overlap, not proof that a PR fixes a reported failure. verify_hits checks
        canonical evidence before a result is used as a verified relation.
    """
    if kind not in ("proposal", "landing") or method not in ("overlap", "bm25") or limit < 1:
        raise ValueError("Unknown change query policy or invalid candidate budget")
    paths, excluded = sorted(set(paths)), sorted(set(excluded))
    if not paths:
        return []
    filters = "c.kind=? AND c.status='complete'"
    if excluded:
        filters += " AND c.number NOT IN (" + ",".join("?" for _ in excluded) + ")"
    args = [kind, *excluded]
    count, average = index.execute(
        f"SELECT COUNT(*),AVG(file_count) FROM changes c WHERE {filters}", args,  # noqa: S608 - Bound filters.
    ).fetchone()
    query = ("SELECT f.file,c.id,c.number,c.file_count FROM files f JOIN changes c ON c.id=f.change_id "  # noqa: S608 - Bound paths.
             f"WHERE {filters} AND f.file IN ({','.join('?' for _ in paths)}) ORDER BY c.id,f.file")
    matches = index.execute(query, [*args, *paths]).fetchall()
    frequencies = defaultdict(int)
    for file, *_ in matches:
        frequencies[file] += 1
    documents = {}
    for file, change, number, length in matches:
        if change not in documents:
            documents[change] = {"change": change, "number": number, "score": 0.0, "matched": []}
        item = documents[change]
        item["matched"].append(file)
        item["score"] += (1.0 if method == "overlap" else
                          math.log1p((count - frequencies[file] + 0.5) / (frequencies[file] + 0.5))
                          * 2.2 / (1 + 1.2 * (0.25 + 0.75 * length / average)))
    output, seen = [], set()
    for item in sorted(documents.values(), key=lambda item: (-item["score"], item["number"], item["change"])):
        if item["number"] not in seen:
            output.append(item)
            seen.add(item["number"])
            if len(output) == limit:
                break
    return output


def verify_hits(db, store, index, hits, scope):
    """Verify source pointers and complete native file sets for retrieved comparisons.

    Args:
        db: Read-only canonical source, including later publications if present.
        store: Canonical Git store associated with this source database.
        index: Sealed derived index checked by verify_index.
        hits: Retrieved change IDs, PR numbers and matched byte paths from search.
        scope: Expected frozen GitHub boundary, as in verify_index.
    """
    if db.execute("SELECT value FROM archive_meta WHERE key='repository'").fetchone() != (scope["repository"],):
        raise ValueError("Change evidence uses a different source repository")
    observed, verified = {}, {}
    files_checked = 0
    for hit in hits:
        change = hit["change"]
        if change not in verified:
            row = index.execute("SELECT number,kind,inputs,sources,detail,status,file_count FROM changes WHERE id=?",
                                (change,)).fetchone()
            number, kind, inputs, sources, detail, status, file_count = row
            inputs, sources, detail = json.loads(inputs), json.loads(sources), json.loads(detail)
            if status != "complete" or not sources:
                raise ValueError("Change hit has no complete reconstruction")
            for source in sources:
                oid = source["observation"]
                if oid not in observed:
                    record = db.execute(
                        "SELECT o.family,o.subject_key,o.resource_number,o.coverage,o.payload_digest,p.payload "
                        "FROM fact_observations o JOIN payload_blobs p ON p.digest=o.payload_digest "
                        "WHERE o.id=? AND o.id<=?",
                        (oid, scope["cutoff"]),
                    ).fetchone()
                    if record is None or record[2:5] != (number, "complete", source["digest"]):
                        raise ValueError("Change hit differs from its source identity")
                    latest = db.execute(
                        "SELECT id FROM fact_observations WHERE family=? AND subject_key=? AND id<=? "
                        "ORDER BY observed_until DESC,observed_from DESC,id DESC LIMIT 1",
                        (*record[:2], scope["cutoff"]),
                    ).fetchone()
                    if latest != (oid,):
                        raise ValueError("Change hit is not the frozen latest observation")
                    document = payload(record[4], record[5])
                    if any(pointer(document, location) != value for location, value in source["locations"].items()):
                        raise ValueError("Change hit lacks an exact source pointer")
                    observed[oid] = list(source_specs(record[0], oid, number, record[4], document["value"]))
                if ((number, kind, tuple(inputs)), source) not in observed[oid]:
                    raise ValueError("Change relation differs from its canonical source semantics")
            native = boundary(store, kind, tuple(inputs), object_types(store, inputs))
            if native != {"status": "ready", **detail}:
                raise ValueError("Change boundary differs from native Git")
            result = run(store, "diff", "--name-status", "--no-renames", "--no-ext-diff", "--no-textconv",
                         "--ignore-submodules=none", "-z", detail["before"], detail["after"])
            result.check_returncode()
            fields = result.stdout.split(b"\0")[:-1]
            native_files = [(file, status.decode()) for status, file in zip(fields[::2], fields[1::2], strict=True)]
            indexed_files = index.execute("SELECT file,change FROM files WHERE change_id=? ORDER BY file",
                                          (change,)).fetchall()
            if indexed_files != native_files or len(indexed_files) != file_count:
                raise ValueError("Indexed changes differ from the complete native file diff")
            files_checked += len(native_files)
            verified[change] = number, {file for file, _ in native_files}
        number, files = verified[change]
        if number != hit["number"] or not set(hit["matched"]) <= files:
            raise ValueError("Retrieved file match lacks native change evidence")
    return {"source_observations": len(observed), "changes": len(verified), "files": files_checked}


def build(db, store, path, scope):
    """Build a new index from canonical observations and bounded native Git batches.

    Args:
        db: Read-only canonical connection without a TEMP selected table.
        store: Canonical Git store bound to this source database.
        path: New derived SQLite path; an existing destination is rejected.
        scope: Frozen repository, cutoff and selected_digest, as in verify_index.
    """
    if path.exists():
        raise FileExistsError(path)
    if (db.execute("SELECT value FROM archive_meta WHERE key='repository'").fetchone() != (scope["repository"],)
            or select(db, scope["cutoff"]) != scope["selected_digest"]):
        raise ValueError("Change source differs from the registered archive")
    started = time.monotonic()
    items = records(db)
    print(encode({"stage": "sources", "records": len(items), "seconds": time.monotonic() - started}), flush=True)
    types = object_types(store, [sha for item in items for sha in item["inputs"]])
    jobs = sorted({(item["kind"], item["inputs"]) for item in items})

    def resolve(job):
        try:
            return boundary(store, *job, types)
        except subprocess.TimeoutExpired:
            # Timed-out children are terminated; this outcome is not object absence.
            return {"status": "boundary_timeout", "timeout_seconds": 120}

    resolved = {}
    with ThreadPoolExecutor(max_workers=4) as workers:
        for i, (job, result) in enumerate(zip(jobs, workers.map(resolve, jobs), strict=True), 1):
            resolved[job] = result
            if i % 2000 == 0:
                print(encode({"stage": "boundaries", "done": i, "total": len(jobs)}), flush=True)
    pairs = defaultdict(list)
    for item in items:
        detail = dict(resolved[item["kind"], item["inputs"]])
        if detail["status"] == "ready":
            conflicts = [source["observation"] for source in item["sources"]
                         if "/value/comparison_sha" in source["locations"] and (
                             source["locations"]["/value/comparison_sha"] != detail["before"]
                             or source["locations"]["/value/comparison_kind"] != detail["comparison_kind"])]
            if conflicts:
                detail.update(status="comparison_conflict", conflicting_observations=conflicts)
            else:
                pairs[detail["before"], detail["after"]].append(item)
        item["detail"] = detail
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as index, index:
        index.executescript("""
            CREATE TABLE changes (
                id INTEGER PRIMARY KEY, number INTEGER, kind TEXT, inputs TEXT, sources TEXT,
                detail TEXT, status TEXT, file_count INTEGER
            );
            CREATE INDEX changes_number ON changes(number,kind);
            CREATE TABLE files (
                change_id INTEGER, file BLOB, change TEXT, PRIMARY KEY (change_id,file)
            ) WITHOUT ROWID;
            CREATE INDEX files_path ON files(file,change_id);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        ordered = sorted(pairs)
        for start in range(0, len(ordered), 64):
            batch = ordered[start:start + 64]
            for pair, result in zip(batch, compare(store, batch), strict=True):
                for item in pairs[pair]:
                    item["detail"].update({key: value for key, value in result.items() if key != "files"})
                    index.executemany("INSERT INTO files VALUES (?,?,?)",
                                      [(item["id"], file, status) for file, status in result.get("files", [])])
                    item["file_count"] = len(result.get("files", []))
            if start % 2048 == 0:
                print(encode({"stage": "diffs", "done": start + len(batch), "total": len(ordered)}), flush=True)
        index.executemany("INSERT INTO changes VALUES (?,?,?,?,?,?,?,?)", [
            (item["id"], item["number"], item["kind"], encode(item["inputs"]), encode(item["sources"]),
             encode({key: value for key, value in item["detail"].items() if key != "status"}),
             item["detail"]["status"], item.get("file_count", 0)) for item in items
        ])
        meta = {"schema": 1, "scope": scope, **rows_digest(index),
                "object_readability_digest": sha256(encode(types).encode()).hexdigest(),
                "coverage": index.execute("SELECT kind,status,COUNT(*) FROM changes GROUP BY kind,status").fetchall()}
        index.executemany("INSERT INTO meta VALUES (?,?)", [(key, encode(value)) for key, value in meta.items()])
    return meta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    scope = json.loads(args.round.read_text())["scope"]
    started = time.monotonic()
    with closing(sqlite3.connect(f"file:{Path(scope['github']).resolve()}?mode=ro", uri=True)) as db:
        result = build(db, git_store_path(scope["github"]), args.out,
                       {key: scope[key] for key in ("repository", "cutoff", "selected_digest")})
    print(encode({**result, "seconds": time.monotonic() - started, "bytes": args.out.stat().st_size}), flush=True)


if __name__ == "__main__":
    main()
