"""Inventory frozen GitHub evidence and code-graph coverage for traceback experiments.

The catalog is a sampling aid, not a relevance benchmark. Closing links describe
GitHub relationships rather than verified fixes; missing observations stay unknown.
"""  # noqa: INP001 - Standalone research code lives outside the production package.

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import time
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path

from gh_puller.codebase import Archive
from gh_puller.codebase.archive import TreeRef, graph_digest
from gh_puller.github import git_store_path

from .stack_search import payload


def select(db, cutoff):
    db.execute("""
        CREATE TEMP TABLE selected AS
        SELECT id,family,subject_key,resource_number,coverage,payload_digest,origin FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY family,subject_key ORDER BY observed_until DESC,observed_from DESC,id DESC
            ) AS position FROM fact_observations WHERE id<=?
        ) WHERE position=1
    """, (cutoff,))
    db.execute("CREATE INDEX selected_family ON selected(family,resource_number)")
    db.execute("CREATE UNIQUE INDEX selected_id ON selected(id)")
    records = db.execute("SELECT id,payload_digest,coverage FROM selected ORDER BY id").fetchall()
    return sha256(json.dumps(records, separators=(",", ":")).encode()).hexdigest()


def facts(db, family):
    for oid, number, digest, compressed in db.execute(
        "SELECT o.id,o.resource_number,o.payload_digest,p.payload FROM selected o "
        "JOIN payload_blobs p ON p.digest=o.payload_digest WHERE o.family=? AND o.coverage='complete' "
        "ORDER BY o.id", (family,),
    ):
        yield oid, number, digest, payload(digest, compressed)["value"]


def resolve_objects(store, objects):
    """Peel immutable observed object IDs without fetching absent Git objects."""
    result = subprocess.run(
        ["git", f"--git-dir={store}", "cat-file", "--batch-check"],
        input="".join(f"{oid}^{{commit}}\n" for oid in objects), text=True, capture_output=True,
        check=True, timeout=120, env=os.environ | {"GIT_NO_LAZY_FETCH": "1"},
    )
    return {oid: line.split()[0] if line.split()[1] == "commit" else None
            for oid, line in zip(objects, result.stdout.splitlines(), strict=True)}


def version_fields(body, package):
    """Read version metadata without stripping development or post-release suffixes."""
    pattern = re.compile(
        rf"^(?:[ \t]*[-*]?[ \t]*\*{{0,2}}{re.escape(package)}[ _-]*version\*{{0,2}}[ \t]*:"
        rf"|Name:[ \t]*{re.escape(package)}[ \t]*\r?\nVersion:)[ \t]*([^\r\n]+)",
        re.IGNORECASE | re.MULTILINE,
    )
    fields = []
    for match in pattern.finditer(body):
        raw = match[1].strip().strip("`")
        token = re.match(r"v?(\d+(?:\.\d+){2}[\w.+-]*)(?:@([^\s`]*))?", raw)
        if token:
            fields.append({"line": match[0], "version": token[1], "revision": token[2]})
    return fields


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--graph-count", type=int, required=True)
    parser.add_argument("--cutoff", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    archive = Archive(args.archive, allow_incomplete=True)
    if not 0 < args.graph_count <= len(archive):
        raise ValueError("The requested graph prefix is not durably available")
    entries = archive._commits[:args.graph_count]
    graph_entries = {item["sha"]: item for item in entries}
    for item in entries:
        if graph_digest(TreeRef.from_json(item.get("node_root")),
                        TreeRef.from_json(item.get("edge_root"))) != item["graph_digest"]:
            raise ValueError(f"Graph manifest roots do not match: {item['sha']}")
    graph_identity = [(sha, item["graph_digest"], item["parents"]) for sha, item in sorted(graph_entries.items())]
    with sqlite3.connect(f"file:{args.github.resolve()}?mode=ro", uri=True) as db:
        metadata = dict(db.execute("SELECT key,value FROM archive_meta"))
        selected_digest = select(db, args.cutoff)
        coverage = db.execute(
            "SELECT family,coverage,origin,COUNT(*) FROM selected GROUP BY family,coverage,origin",
        ).fetchall()
        print(json.dumps({"coverage": coverage}), flush=True)
        refs_oid, _, refs_digest, refs_value = list(facts(db, "git-refs"))[-1]
        refs = [ref for ref in refs_value["refs"] if ref["name"].startswith("refs/tags/")]
        resolved = resolve_objects(git_store_path(args.github), [ref["oid"] for ref in refs])
        tags = {ref["name"].removeprefix("refs/tags/"): {
            "object": ref["oid"], "commit": resolved[ref["oid"]],
            "graph": resolved[ref["oid"]] in graph_entries,
        } for ref in refs}
        closing = defaultdict(list)
        for oid, number, digest, value in facts(db, "pull-closing-issues"):
            for index, issue in enumerate(value):
                if issue["repository"]["nameWithOwner"].casefold() != metadata["repository"].casefold():
                    continue
                closing[issue["number"]].append({
                    "pull": number, "observation": oid, "digest": digest, "pointer": f"/value/{index}",
                })
        counts, candidates = Counter(), []
        repo_name = metadata["repository"].split("/")[-1]
        for oid, number, digest, value in facts(db, "issue"):
            if "pull_request" in value:
                continue
            counts["issues"] += 1
            body = value.get("body") or ""
            if "Traceback (most recent call last)" not in body:
                continue
            counts["traceback_issues"] += 1
            versions = version_fields(body, repo_name)
            reported = {"v" + field["version"] for field in versions if field["revision"] is None}
            known = {ref: tags[ref] for ref in sorted(reported) if ref in tags}
            has_graph = any(tag["graph"] for tag in known.values())
            counts["traceback_with_version_line"] += bool(versions)
            counts["traceback_with_mentioned_graph_tag"] += has_graph
            counts["traceback_with_closing_link"] += bool(closing[number])
            counts["traceback_with_mentioned_graph_and_closing_link"] += has_graph and bool(closing[number])
            candidates.append({
                "number": number, "observation": oid, "digest": digest, "title": value["title"],
                "created_at": value["created_at"], "state": value["state"],
                "versions": versions[:6], "tags": known,
                "frames": re.findall(r'File "([^\"]+)", line (\d+), in ([^\n]+)', body)[-6:],
                "exceptions": re.findall(r"\b[\w.]*(?:Error|Exception):[^\n]+", body)[-3:],
                "closing_pulls": closing[number],
            })
        result = {
            "repository": metadata["repository"], "schema": metadata["schema_version"],
            "cutoff": args.cutoff, "selected_digest": selected_digest, "coverage": coverage,
            "graph": {"commits": len(entries), "manifest_roots_verified": True,
                      "all_graph_pages_verified": False,
                      "identity_digest": sha256(json.dumps(graph_identity).encode()).hexdigest(),
                      "last_archived_commit": entries[-1]["sha"]},
            "observed_source": {"archived_commits": len(archive), "complete": archive.complete},
            "refs": {"observation": refs_oid, "digest": refs_digest}, "tags": tags,
            "counts": dict(counts), "candidates": candidates,
        }
    result["seconds"] = time.monotonic() - started
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"counts": counts, "output": str(args.out), "seconds": result["seconds"]}), flush=True)


if __name__ == "__main__":
    main()
