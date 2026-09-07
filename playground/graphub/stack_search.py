"""Compare lexical retrieval with CBM-backed history for one archived traceback.

This standalone experiment uses a disposable, single-snapshot CBM SQLite projection.
Code search and traversal execute in CBM; Git traces the selected source intervals.
The projection contains graph rows, not indexing coverage, source files or vectors.
"""  # noqa: INP001 - The experiment is a standalone script, not a package.

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import tempfile
import time
import zlib
from collections import Counter
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from gh_puller.codebase import Archive, resolve_cbm_binary
from gh_puller.codebase.cbm_transport import PersistentMCPTransport
from gh_puller.codebase.store import GraphRows, load_rows
from gh_puller.github import git_store_path
from gh_puller.github.commit_references import commit_reference_provenance

TEXT_FAMILIES = ("issue", "issue-comments", "pull-reviews", "pull-review-comments")


def git(store, *arguments):
    return subprocess.check_output(
        ["git", f"--git-dir={store}", *arguments], text=True, encoding="utf-8", errors="replace", timeout=120,
    )


def fact(db, family, number):
    row = db.execute(
        "SELECT o.id,o.payload_digest,p.payload FROM selected o "
        "JOIN payload_blobs p ON p.digest=o.payload_digest "
        "WHERE o.family=? AND o.resource_number=? AND o.coverage='complete'",
        (family, number),
    ).fetchone()
    return row[0], row[1], payload(row[1], row[2])["value"]


def payload(digest, compressed):
    raw = zlib.decompress(compressed)
    if sha256(raw).hexdigest() != digest:
        raise ValueError(f"Source payload failed digest verification: {digest}")
    return json.loads(raw)


def freeze(db, cutoff):
    db.execute("""
        CREATE TEMP TABLE selected AS
        SELECT id,family,subject_key,resource_number,coverage,payload_digest FROM (
            SELECT id,family,subject_key,resource_number,coverage,payload_digest,
                   ROW_NUMBER() OVER (
                       PARTITION BY family,subject_key ORDER BY observed_until DESC,observed_from DESC,id DESC
                   ) AS position
            FROM fact_observations WHERE id<=? AND family IN (
                'issue','issue-comments','pull-reviews','pull-review-comments','pull-git','pull-commits','pull'
            )
        ) WHERE position=1
    """, (cutoff,))
    db.execute("CREATE INDEX selected_family_number ON selected(family,resource_number)")
    db.execute("CREATE UNIQUE INDEX selected_id ON selected(id)")
    ids = [row[0] for row in db.execute("SELECT id FROM selected ORDER BY id")]
    return sha256(json.dumps(ids).encode()).hexdigest()


def lexical(db, cache, excluded, exception, seed):
    started = time.monotonic()
    search = sqlite3.connect(cache / "text.sqlite3")
    search.execute("PRAGMA journal_mode=OFF")
    search.execute("PRAGMA synchronous=OFF")
    search.execute("""
        CREATE VIRTUAL TABLE docs USING fts5(
            number UNINDEXED,observation UNINDEXED,location UNINDEXED,text,tokenize='unicode61'
        )
    """)
    titles, counts = {}, Counter()
    exact = {"exception": set(), "frame": set()}
    for family in TEXT_FAMILIES:
        cursor = db.execute(
            "SELECT o.id,o.resource_number,o.payload_digest,p.payload FROM selected o "
            "JOIN payload_blobs p ON p.digest=o.payload_digest "
            "WHERE o.family=? AND o.coverage='complete' ORDER BY o.id", (family,),
        )
        for observation, number, digest, compressed in cursor:
            value = payload(digest, compressed)["value"]
            if family == "issue":
                titles[number] = {"title": value.get("title"), "url": value.get("html_url"),
                                  "kind": "pull" if "pull_request" in value else "issue"}
            if number == excluded:
                continue
            items = [value] if family == "issue" else value
            for index, item in enumerate(items):
                text = (item.get("title") or "") + "\n" + (item.get("body") or "")
                if not text.strip():
                    continue
                location = "/value" if family == "issue" else f"/value/{index}/body"
                search.execute("INSERT INTO docs VALUES(?,?,?,?)", (number, observation, location, text))
                counts[family] += 1
                if exception in text:
                    exact["exception"].add(number)
                if seed in text:
                    exact["frame"].add(number)
        print(json.dumps({"indexed_family": family, "documents": counts[family]}), flush=True)
    search.commit()
    words = sorted({word.lower() for word in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", exception) if len(word) > 2})
    error_query = " OR ".join(f'"{word}"' for word in words)
    queries = {"exception_bm25": error_query, "frame_bm25": f'"{seed}"',
               "combined_bm25": f'{error_query} OR "{seed}"'}
    phrase = exception.partition(":")[2].strip().split(". ", 1)[0].rstrip(".")
    queries["exception_phrase"] = '"' + phrase.replace('"', '""') + '"'
    search.execute("CREATE VIRTUAL TABLE vocabulary USING fts5vocab(docs,'row')")
    frequencies = []
    for word in sorted(set(re.findall(r"[a-z]+", exception.lower()))):
        row = search.execute("SELECT doc FROM vocabulary WHERE term=?", (word,)).fetchone()
        if row and len(word) > 2:
            frequencies.append((row[0], word))
    rare = [word for _, word in sorted(frequencies)[:5]]
    queries["rare_error_terms"] = " OR ".join(f'"{word}"' for word in rare)
    rankings = {}
    for label, query in queries.items():
        matches = {}
        for number, observation, location, score, excerpt in search.execute(
            "SELECT number,observation,location,rank,snippet(docs,3,'[',']','...',32) "
            "FROM docs WHERE docs MATCH ? ORDER BY rank,rowid", (query,),
        ):
            if number not in matches:
                matches[number] = {"number": number, "observation_id": observation, "location": location,
                                   "score": score, "excerpt": excerpt, **titles[number]}
        rankings[label] = {"query": query, "threads": len(matches), "ranking": list(matches),
                           "top": list(matches.values())[:20]}
    search.close()
    return titles, {
        "documents": dict(counts), "seconds": time.monotonic() - started,
        "literal_matches": {key: sorted(value) for key, value in exact.items()}, "queries": rankings,
    }


def pull_links(db, commits, cutoff):
    links = {commit: {} for commit in commits}
    for oid, number, digest, compressed in db.execute(
        "SELECT o.id,o.resource_number,o.payload_digest,p.payload FROM selected o "
        "JOIN payload_blobs p ON p.digest=o.payload_digest WHERE o.family='pull-git' AND o.coverage='complete'",
    ):
        landing = payload(digest, compressed)["value"].get("landing_sha")
        if landing in links:
            links[landing][number] = {"number": number, "observation_id": oid, "location": "/value/landing_sha",
                                      "relation": "pull_landing"}
    for oid, number, digest, compressed in db.execute(
        "SELECT o.id,o.resource_number,o.payload_digest,p.payload FROM selected o "
        "JOIN payload_blobs p ON p.digest=o.payload_digest WHERE o.family='pull' AND o.coverage='complete'",
    ):
        value = payload(digest, compressed)["value"]
        commit = value.get("merge_commit_sha")
        if value.get("merged") and commit in links and number not in links[commit]:
            links[commit][number] = {"number": number, "observation_id": oid, "location": "/value/merge_commit_sha",
                                     "relation": "merged_pull_commit"}
    source_ids = {row[0] for row in db.execute("SELECT id FROM selected WHERE family='pull-commits'")}
    commits = sorted(commits)
    for start in range(0, len(commits), 400):
        group = commits[start:start + 400]
        slots = ",".join("?" for _ in group)
        cursor = db.execute(
            "SELECT DISTINCT o.id,o.resource_number,o.payload_digest,p.payload FROM commit_reference_index i "  # noqa: S608 - Bound SHA placeholders.
            "JOIN fact_observations o ON o.id=i.observation_id JOIN payload_blobs p ON p.digest=o.payload_digest "
            f"WHERE i.sha IN ({slots}) AND o.id<=? AND o.coverage='complete'",
            (*group, cutoff),
        )
        for oid, number, digest, compressed in cursor:
            value = payload(digest, compressed)
            if value.get("source_family") != "pull-commits" or value.get("source_observation_id") not in source_ids:
                continue
            for reference in commit_reference_provenance(oid, number, value):
                commit = reference["sha"]
                if commit in links and number not in links[commit]:
                    links[commit][number] = {"number": number, "observation_id": reference["source_observation_id"],
                                             "location": reference["field_path"], "relation": "pull_commit",
                                             "reference_observation_id": oid}
    return {commit: list(values.values()) for commit, values in links.items()}


def symbols(response):
    """Decode compact CBM rows; an empty line display denotes native zero coordinates."""
    return [
        {"qn": f"{group['qn_prefix']}.{row[0]}", "file": group["file"],
         "start": int(row[2].split("-")[0] or 0), "end": int(row[2].split("-")[-1] or 0)}
        for group in response["groups"] for row in group["rows"]
    ]


def histories(store, sha, seed, neighbors):
    scopes = [{**seed, "kind": "file", "hop": 0}, {**seed, "kind": "symbol", "hop": 0}]
    scopes.extend({**node, "kind": "caller", "hop": 1} for node in neighbors)
    for scope in scopes:
        command = ["log", "--format=%H%x09%ct", "--no-patch"]
        if scope["kind"] == "file":
            command += ["--follow", sha, "--", scope["file"]]
        else:
            command += ["-L", f"{scope['start']},{scope['end']}:{scope['file']}", sha]
        scope["commits"] = [
            {"sha": line.split("\t")[0], "time": int(line.split("\t")[1])}
            for line in git(store, *command).splitlines()
        ]
        print(json.dumps({"history": scope["qn"], "kind": scope["kind"], "commits": len(scope["commits"])}), flush=True)
    return scopes


def candidates(scopes, links, titles, lexical_results):
    output = {}
    for arm, kinds in (("file", {"file"}), ("symbol", {"symbol"}), ("callers", {"caller"})):
        found = {}
        for scope in scopes:
            if scope["kind"] not in kinds:
                continue
            for commit in scope["commits"]:
                for link in links[commit["sha"]]:
                    # Membership in a large rebase PR does not identify a change's landing PR.
                    if link["relation"] == "pull_commit":
                        continue
                    number = link["number"]
                    if number not in titles:
                        continue
                    value = found.setdefault(number, {"number": number, **titles[number], "time": 0, "evidence": []})
                    value["time"] = max(value["time"], commit["time"])
                    evidence = {"kind": scope["kind"], "file": scope["file"], "commit": commit["sha"], "github": link}
                    if scope["kind"] != "file":
                        evidence.update(symbol=scope["qn"], start=scope["start"], end=scope["end"])
                    value["evidence"].append(evidence)
        ranking = sorted(found.values(), key=lambda value: (-value["time"], value["number"]))
        for value in ranking:
            value["lexical_ranks"] = {
                key: query["ranking"].index(value["number"]) + 1 if value["number"] in query["ranking"] else None
                for key, query in lexical_results["queries"].items()
            }
        output[arm] = {"threads": len(ranking), "results": ranking}
    return output


def restore(rows, cache, project):
    """Expose endpoint-closed rows to CBM, accounting for dangling source edges."""
    dangling = [edge for edge in rows.edges if edge[0] not in rows.nodes or edge[1] not in rows.nodes]
    retained = {key: value for key, value in rows.edges.items() if key[0] in rows.nodes and key[1] in rows.nodes}
    names = {name: project if name == "__project__" else f"{project}.{name}" for name in rows.nodes}

    def qualify(value):
        if isinstance(value, str):
            return names.get(value, value)
        if isinstance(value, list):
            return [qualify(item) for item in value]
        if isinstance(value, dict):
            # Import aliases participate in native edge identity, not the project namespace.
            return {key: item if key == "local_name" else qualify(item) for key, item in value.items()}
        return value

    path = cache / f"{project}.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE projects(name TEXT PRIMARY KEY, indexed_at TEXT NOT NULL, root_path TEXT NOT NULL);
            CREATE TABLE nodes(
                id INTEGER PRIMARY KEY, project TEXT NOT NULL, label TEXT NOT NULL, name TEXT NOT NULL,
                qualified_name TEXT NOT NULL, file_path TEXT, start_line INTEGER, end_line INTEGER,
                properties TEXT, UNIQUE(project,qualified_name)
            );
            CREATE TABLE edges(
                id INTEGER PRIMARY KEY, project TEXT NOT NULL, source_id INTEGER, target_id INTEGER,
                type TEXT, properties TEXT,
                local_name_gen TEXT GENERATED ALWAYS AS (
                    CASE WHEN type='IMPORTS' THEN coalesce(json_extract(properties,'$.local_name'),'') ELSE '' END
                ),
                UNIQUE(source_id,target_id,type,local_name_gen)
            );
            CREATE INDEX edge_in ON edges(project,target_id,type);
            CREATE INDEX edge_out ON edges(project,source_id,type);
            CREATE INDEX node_file ON nodes(project,file_path);
        """)
        # The source path is deliberately nonexistent: this probe serves graph-only tools.
        db.execute("INSERT INTO projects VALUES(?,?,?)", (project, "", str(cache / "no-source-tree")))
        ids = {name: index for index, name in enumerate(rows.nodes, 1)}
        db.executemany(
            "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?)",
            (
                (ids[name], project, node["label"], qualify(node["name"]), names[name], node["file_path"],
                 node["start_line"], node["end_line"], json.dumps(qualify(node["properties"])))
                for name, node in rows.nodes.items()
            ),
        )
        db.executemany(
            "INSERT INTO edges(project,source_id,target_id,type,properties) VALUES(?,?,?,?,?)",
            (
                (project, ids[source], ids[target], kind, json.dumps(qualify(edge["properties"])))
                for (source, target, kind, _), edge in retained.items()
            ),
        )
    actual = load_rows(path, project)
    if actual != GraphRows(rows.nodes, retained):
        changed = [name for name, value in rows.nodes.items() if actual.nodes.get(name) != value]
        edge_changes = [key for key, value in retained.items() if actual.edges.get(key) != value]
        raise ValueError(f"CBM projection changed normalized graph rows: nodes={changed[:3]}, edges={edge_changes[:3]}")
    return {
        "bytes": path.stat().st_size, "nodes": len(rows.nodes), "source_edges": len(rows.edges),
        "retained_edges": len(retained), "retained_round_trip": True, "snapshots": 1,
        "source_dangling_edges": len(dangling), "dangling_sample": dangling[:10],
        "dangling_types": dict(Counter(edge[2] for edge in dangling)),
        "affected_existing_endpoints": dict(
            Counter(name for edge in dangling for name in edge[:2] if name in rows.nodes),
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--cutoff", type=int)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    store = git_store_path(args.github)
    sha = git(store, "rev-parse", f"{args.ref}^{{commit}}").strip()
    with sqlite3.connect(f"file:{args.github.resolve()}?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        cutoff = args.cutoff or db.execute("SELECT MAX(id) FROM fact_observations").fetchone()[0]
        selection_digest = freeze(db, cutoff)
        observation_id, digest, issue = fact(db, "issue", args.issue)
    body = issue["body"]
    trace = body[body.index("Traceback (most recent call last)"):].split("```", 1)[0]
    exceptions = re.findall(r"\b[\w.]*(?:Error|Exception):[^\n]+", trace)
    frames = re.findall(r'File "([^\"]+)", line (\d+), in ([^\n]+)', trace)
    paths = set(git(store, "ls-tree", "-r", "--name-only", sha).splitlines())
    local = []
    for filename, line, function in frames:
        matches = [path for path in paths if filename == path or filename.endswith(f"/{path}")]
        if len(matches) == 1:
            local.append({"file": matches[0], "line": int(line), "name": function.strip()})
    seed = local[-1]
    archive = Archive(args.archive, allow_incomplete=True)
    rows = archive.load_rows(sha)
    binary = resolve_cbm_binary()
    result = {
        "input": {"issue": args.issue, "observation_id": observation_id, "payload_digest": digest,
                  "traceback": trace, "frames": local, "seed": seed},
        "scope": {"ref": args.ref, "commit": sha, "observation_cutoff": cutoff,
                  "excluded_thread": args.issue, "archived_commits": len(archive),
                  "selected_observations_digest": selection_digest,
                  "history_boundary": "ancestors of selected code commit; not future fixes",
                  "fact_boundary": "retrospective offline knowledge, not knowledge at issue creation",
                  "graph_digest": archive._entries[sha]["graph_digest"]},
        "cbm": binary.provenance(),
        "calls": [],
    }
    project = "graphub-probe"
    with tempfile.TemporaryDirectory(prefix="graphub-stack-") as scratch:
        cache = Path(scratch)
        result["projection"] = restore(rows, cache, project)
        del rows
        monitor = SimpleNamespace(child_pid=None, exceeded=False, sample=lambda: None)
        # CBM's account-scoped daemon rendezvous is independent of its cache directory.
        transport = PersistentMCPTransport(
            binary.path, cache, 60, monitor, extra_environment={"CBM_RUNTIME_DIR": str(cache)},
        )
        try:
            arguments = {
                "project": project, "file_pattern": seed["file"],
                "name_pattern": f"^{re.escape(seed['name'])}$", "format": "json", "limit": 20,
            }
            response = transport.call_tool("search_graph", arguments)
            result["calls"].append({"tool": "search_graph", "arguments": arguments,
                                     "response": response["structuredContent"]})
            matched = [node for node in symbols(response["structuredContent"])
                       if node["file"] == seed["file"] and node["start"] <= seed["line"] <= node["end"]]
            if len(matched) != 1 or response["structuredContent"]["has_more"]:
                raise ValueError("CBM could not uniquely resolve the traceback coordinate within the search limit")
            result["seed_symbol"] = matched[0]
            qualified = matched[0]["qn"]
            arguments = {
                "project": project, "function_name": qualified, "direction": "both", "depth": 1,
                "include_tests": True, "include_evidence": True, "format": "json", "limit": 5000,
            }
            response = transport.call_tool("trace_path", arguments)
            trace_result = response["structuredContent"]
            result["calls"].append({"tool": "trace_path", "arguments": arguments, "response": trace_result})
            names = [
                f"{group['qn_prefix']}.{row[0]}"
                for group in trace_result["callers"]["groups"] for row in group["rows"]
            ]
            arguments = {"project": project, "qn_pattern": "^(" + "|".join(map(re.escape, names)) + ")$",
                         "format": "json", "limit": 5000}
            response = transport.call_tool("search_graph", arguments)["structuredContent"]
            result["calls"].append({"tool": "search_graph", "arguments": arguments, "response": response})
            neighbors = symbols(response)
            if len(neighbors) != len(names):
                raise ValueError("CBM caller lookup was incomplete")
        finally:
            transport.close()
        result["histories"] = histories(store, sha, result["seed_symbol"], neighbors)
        commits = {commit["sha"] for scope in result["histories"] for commit in scope["commits"]}
        links = pull_links(db, commits, cutoff)
        result["membership_only_links"] = [
            {"commit": commit, **link} for commit, values in links.items() for link in values
            if link["relation"] == "pull_commit"
        ]
        result["unlinked_commits"] = sorted(
            commit for commit, values in links.items() if not any(link["relation"] != "pull_commit" for link in values)
        )
        titles, result["lexical"] = lexical(db, cache, args.issue, exceptions[-1], seed["name"])
        result["candidates"] = candidates(result["histories"], links, titles, result["lexical"])
        for query in result["lexical"]["queries"].values():
            del query["ranking"]
    db.close()
    result["seconds"] = time.monotonic() - started
    result["method_sha256"] = sha256(Path(__file__).read_bytes()).hexdigest()
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.out), "seconds": result["seconds"]}))


if __name__ == "__main__":
    main()
