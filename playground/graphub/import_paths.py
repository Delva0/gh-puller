"""Measure bounded file-import paths rooted in native compiler error coordinates.

CBM evaluates each directed path at the reported version. Static imports do not
establish active build conditions. File histories and landing provenance follow
future_search; cases without compiler coordinates remain explicit empty controls.
"""  # noqa: INP001 - Standalone research comparison.

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from gh_puller.codebase import Archive, resolve_cbm_binary
from gh_puller.codebase.cbm_transport import PersistentMCPTransport
from gh_puller.github import git_store_path

from .case_probe import git
from .entity_search import native_locations, verify_inputs
from .future_search import generate, rank, read_landings
from .stack_search import restore


def query_paths(transport, files, depth, *, limit=500):
    if depth not in (1, 2) or limit < 1:
        raise ValueError("Use one or two hops and a positive path limit")
    if not files:
        return {"paths": [], "truncated": False, "call": None}
    pattern = "(n0:File)" + "".join(f"-[r{i}:IMPORTS]->(n{i + 1}:File)" for i in range(depth))
    columns = [f"n{i}.{field} AS n{i}_{alias}" for i in range(depth + 1)
               for field, alias in (("qualified_name", "qn"), ("file_path", "file"))]
    columns.extend(f"{expression} AS r{i}_{alias}" for i in range(depth)
                   for expression, alias in ((f"type(r{i})", "type"), (f"r{i}.local_name", "local_name"),
                                             (f"r{i}.confidence", "confidence"), (f"r{i}.strategy", "strategy")))
    predicate = " OR ".join(f"n0.file_path = {json.dumps(file)}" for file in sorted(set(files)))
    query = f"MATCH {pattern} WHERE {predicate} RETURN {', '.join(columns)} LIMIT {limit + 1}"
    arguments = {"project": "graphub-probe", "query": query, "format": "json", "max_rows": limit + 1}
    response = transport.call_tool("query_graph", arguments)["structuredContent"]
    paths = []
    for values in response["rows"][:limit]:
        row = dict(zip(response["columns"], values, strict=True))
        nodes = [{"qn": row[f"n{i}_qn"], "file": row[f"n{i}_file"]} for i in range(depth + 1)]
        edges = [{key: row[f"r{i}_{key}"] for key in ("type", "local_name", "confidence", "strategy")}
                 for i in range(depth)]
        if nodes[0]["file"] not in files or any(edge["type"] != "IMPORTS" for edge in edges):
            raise ValueError("The native path does not satisfy its source predicate")
        paths.append({"nodes": nodes, "edges": edges})
    return {"paths": paths, "truncated": response["total"] > limit,
            "call": {"arguments": arguments, "response": response}}


def bind(locations, queries, paths):
    records = {}

    def add(node, depth):
        if node["file"] not in paths:
            return
        key = node["file"], depth
        if key in records:
            records[key]["additional_anchors"].append(node)
        else:
            records[key] = {"node": node, "roles": [f"depth_{value}" for value in range(depth, 3)],
                            "additional_anchors": []}

    for location in locations:
        add({**location, "hop": 0}, 0)
    for depth, query in queries.items():
        for path in query["paths"]:
            if all(node["file"] in paths for node in path["nodes"]):
                add({**path["nodes"][-1], "hop": depth, "path": path, "query_truncated": query["truncated"]}, depth)
    return list(records.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--round", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--landing-index", type=Path)
    args = parser.parse_args()
    data = json.loads(args.cases.read_text())
    registration = json.loads(args.round.read_text())
    scope = data["scope"]
    if registration["scope"] != scope:
        raise ValueError("Cases differ from the registered evidence boundary")
    started = time.monotonic()
    with sqlite3.connect(f"file:{Path(scope['github']).resolve()}?mode=ro", uri=True) as db:
        verify_inputs(db, data)
    archive = Archive(scope["archive"], allow_incomplete=True)
    entries = archive._commits[:scope["graph_count"]]
    identity = [(item["sha"], item["graph_digest"], item["parents"])
                for item in sorted(entries, key=lambda item: item["sha"])]
    if sha256(json.dumps(identity).encode()).hexdigest() != scope["graph_identity_digest"]:
        raise ValueError("Code graphs differ from the registered prefix")
    binary = resolve_cbm_binary()
    if binary.sha256 != scope["cbm_sha256"]:
        raise ValueError("CBM executable differs from the registered experiment")
    upper = entries[-1]["sha"]
    store = git_store_path(scope["github"])
    results = []
    for case in data["cases"]:
        begin = time.monotonic()
        paths = set(git(store, "ls-tree", "-r", "--name-only", case["commit"]).stdout.splitlines())
        locations = native_locations(case["log"]["text"], paths)
        files = sorted({item["file"] for item in locations if item["file"]})
        queries, projection = {}, None
        if files:
            with tempfile.TemporaryDirectory(prefix="graphub-imports-") as scratch:
                root = Path(scratch)
                projection = restore(archive.load_rows(case["commit"]), root, "graphub-probe")
                monitor = SimpleNamespace(child_pid=None, exceeded=False, sample=lambda: None)
                transport = PersistentMCPTransport(
                    binary.path, root, 60, monitor, extra_environment={"CBM_RUNTIME_DIR": str(root)},
                )
                try:
                    queries = {depth: query_paths(transport, files, depth) for depth in (1, 2)}
                finally:
                    transport.close()
        bindings = bind(locations, queries, paths)
        generated = generate(store, case["commit"], upper, [], {}, bindings)
        result = {"number": case["number"], "commit": case["commit"], "source": case["source"],
                  "log_digest": case["log"]["digest"], "locations": locations, "queries": queries,
                  "projection": projection, "bindings": bindings, "generated": generated,
                  "status": "queried" if files else "no_native_repository_coordinates",
                  "seconds": time.monotonic() - begin}
        results.append(result)
        print(json.dumps({"case": case["number"], "status": result["status"], "files": files,
                          "paths": {depth: len(query["paths"]) for depth, query in queries.items()},
                          "seconds": result["seconds"]}), flush=True)
    commits = {commit["sha"] for case in results
               for item in case["generated"]["histories"] for commit in item["commits"]}
    links, verification = read_landings(scope, commits, args.landing_index)
    excluded = {number for split in ("development", "held_out", "known_regression")
                for number in registration["sampling"][split]}
    labels = {case["number"]: case["references"] for case in data["cases"]}
    for case in results:
        case["arms"] = rank(case["generated"]["histories"], links, case["generated"].pop("positions", {}), excluded,
                            kinds=("trace_file",), roles=("depth_0", "depth_1", "depth_2"))
        case["reference_ranks"] = {str(reference["number"]): {
            name: arm["ranking"].index(reference["number"]) + 1 if reference["number"] in arm["ranking"] else None
            for name, arm in case["arms"].items()
        } for reference in labels[case["number"]]}
        print(json.dumps({"case": case["number"], "reference_ranks": case["reference_ranks"]}), flush=True)
    args.out.write_text(json.dumps({"scope": scope, "upper": upper, "cases": results, "verification": verification,
                                    "excluded_threads": sorted(excluded), "seconds": time.monotonic() - started},
                                   ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
