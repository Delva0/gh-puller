"""Compare diagnostic entity files and native CBM neighbors with traceback-only evidence.

Exact-name mentions are candidate entity links, not runtime identities. Native
compiler coordinates retain path ambiguity. CBM supplies typed incident and import
edges; Git supplies bounded file changes. Evaluation references do not guide either.
"""  # noqa: INP001 - Standalone research comparison.

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import tempfile
import time
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from gh_puller.codebase import Archive, resolve_cbm_binary
from gh_puller.codebase.cbm_transport import PersistentMCPTransport
from gh_puller.github import git_store_path

from .case_probe import ancestry, error_log, git, observation
from .future_search import anchors, history, rank, read_landings
from .lexical_probe import diagnostics, identifier_terms
from .stack_search import restore, symbols


def native_locations(log, paths):
    records = []
    pattern = r"^[ \t]*(.+?):(\d+):(\d+): (?:fatal )?error:"
    for match in re.finditer(pattern, log, re.MULTILINE):
        filename = match[1].replace("\\", "/")
        matches = sorted(path for path in paths if filename == path or filename.endswith(f"/{path}"))
        qualified = [path for path in matches if "/" in path or filename == path]
        records.append({"reported_file": match[1], "line": int(match[2]), "column": int(match[3]),
                        "file": max(qualified, key=len) if qualified else None,
                        "path_candidates": matches, "offset": match.start(), "end": match.end()})
    return records


def relationships(transport, predicate, *, imports=False, limit=500):
    edge = "r:IMPORTS" if imports else "r"
    query = (f"MATCH (a)-[{edge}]->(b) WHERE {predicate} RETURN "
             "a.qualified_name AS source_qn, a.file_path AS source_file, a.start_line AS source_start, "
             "a.end_line AS source_end, type(r) AS relation, r.line AS line, r.confidence AS confidence, "
             "r.strategy AS strategy, b.qualified_name AS target_qn, b.file_path AS target_file, "
             f"b.start_line AS target_start, b.end_line AS target_end LIMIT {limit + 1}")
    # The pinned CBM implements this encoding although its tools/list schema omits it.
    arguments = {"project": "graphub-probe", "query": query, "format": "json", "max_rows": limit + 1}
    response = transport.call_tool("query_graph", arguments)["structuredContent"]
    rows = [dict(zip(response["columns"], row, strict=True)) for row in response["rows"][:limit]]
    return {"arguments": arguments, "response": response, "rows": rows,
            "truncated": response["total"] > limit, "warning": response.get("warning")}


def probe(transport, log, frames, paths):
    names = identifier_terms(diagnostics(log, primary_messages=True)[1])
    pattern = "^(" + "|".join(map(re.escape, names)) + ")$" if names else "a^"
    arguments = {"project": "graphub-probe", "name_pattern": pattern, "format": "json", "limit": 5000}
    response = transport.call_tool("search_graph", arguments)["structuredContent"]
    nodes = symbols(response)
    mentions = []
    for name in names:
        matches = [node for node in nodes if node["qn"].rsplit(".", 1)[-1] == name]
        mentions.append({"name": name, "symbols": matches, "status": "query_truncated" if response["has_more"] else
                         "unique_name_match" if len(matches) == 1 else "ambiguous" if matches else "not_found"})
    locations = native_locations(log, paths)
    leaves = [anchor["node"] for anchor in anchors(log, frames) if "leaf" in anchor["roles"]]
    seeds = {node["qn"]: node for node in [*nodes, *leaves] if node["file"] in paths}
    files = sorted({node["file"] for node in seeds.values()} | {item["file"] for item in locations if item["file"]})
    queries = {}
    if seeds:
        terms = [f"{side}.qualified_name = {json.dumps(name)}" for name in sorted(seeds) for side in ("a", "b")]
        queries["symbol_incident"] = relationships(transport, " OR ".join(terms))
    if files:
        terms = [f"a.file_path = {json.dumps(file)}" for file in files]
        queries["file_imports"] = relationships(transport, " OR ".join(terms), imports=True)
    return {"mentions": mentions, "native_locations": locations, "seeds": list(seeds.values()),
            "symbol_lookup": {"arguments": arguments, "response": response}, "relationships": queries}


def candidate_scopes(probed, paths):
    scopes = {}

    def add(kind, node):
        if node["file"] in paths:
            key = kind, node["file"]
            if key in scopes:
                scopes[key]["additional_anchors"].append(node)
            else:
                scopes[key] = {"kind": kind, "roles": ["evidence"], "lower_anchor": node,
                               "file": node["file"], "additional_anchors": []}

    for mention in probed["mentions"]:
        for node in mention["symbols"]:
            add("entity_file", {**node, "mention": mention["name"], "resolution": mention["status"]})
    for location in probed["native_locations"]:
        add("native_file", location)
    seeds = {node["qn"] for node in probed["seeds"]}
    for kind, result in probed["relationships"].items():
        for edge in result["rows"]:
            sides = ("target",) if kind == "file_imports" else (
                ("target",) if edge["source_qn"] in seeds else ("source",)
            )
            for side in sides:
                add(kind, {"qn": edge[f"{side}_qn"], "file": edge[f"{side}_file"],
                           "start": int(edge[f"{side}_start"] or 0), "end": int(edge[f"{side}_end"] or 0),
                           "edge": edge, "query_truncated": result["truncated"]})
    return list(scopes.values())


def verify_inputs(db, data):
    for case in data["cases"]:
        root = observation(db, "issue", case["number"], data["scope"]["cutoff"])
        if (root["coverage"] != "complete" or root["observation"] != case["source"]["observation"]
                or root["digest"] != case["source"]["digest"] or case["source"]["pointer"] != "/value/body"):
            raise ValueError("A case input differs from its frozen observation")
        log = error_log(root["payload"]["value"]["body"])
        if {**log, "digest": sha256(log["text"].encode()).hexdigest()} != case["log"]:
            raise ValueError("A query log differs from its verbatim source region")


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
        raise ValueError("Case evidence differs from the registered boundary")
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
    grouped = defaultdict(list)
    for case in data["cases"]:
        grouped[case["commit"]].append(case)
    results = []
    for commit, cases in grouped.items():
        paths = set(git(store, "ls-tree", "-r", "--name-only", commit).stdout.splitlines())
        with tempfile.TemporaryDirectory(prefix="graphub-entities-") as scratch:
            root = Path(scratch)
            projection = restore(archive.load_rows(commit), root, "graphub-probe")
            monitor = SimpleNamespace(child_pid=None, exceeded=False, sample=lambda: None)
            transport = PersistentMCPTransport(
                binary.path, root, 60, monitor, extra_environment={"CBM_RUNTIME_DIR": str(root)},
            )
            try:
                for case in cases:
                    begin = time.monotonic()
                    result = probe(transport, case["log"]["text"], case["frames"], paths)
                    scopes = candidate_scopes(result, paths)
                    results.append({"number": case["number"], "commit": commit,
                                    "probe": result, "histories": scopes, "projection": projection,
                                    "query_seconds": time.monotonic() - begin})
                    print(json.dumps({"case": case["number"], "mentions": result["mentions"],
                                      "native_files": sorted({item["file"] for item in result["native_locations"]
                                                              if item["file"]}),
                                      "edges": {name: len(value["rows"])
                                                for name, value in result["relationships"].items()}}),
                          flush=True)
            finally:
                transport.close()
    positions = {}
    for case in results:
        commit = case["commit"]
        if ancestry(store, commit, upper) is not True:
            raise ValueError("Unverified reported-to-upper version interval")
        if commit not in positions:
            ordered = git(store, "rev-list", "--reverse", "--topo-order", "--ancestry-path",
                          f"{commit}..{upper}").stdout.splitlines()
            positions[commit] = {sha: offset for offset, sha in enumerate(ordered)}
        for item in case["histories"]:
            item["commits"] = history(store, commit, upper, item["file"])
        print(json.dumps({"case": case["number"], "history_files": len({item["file"] for item in case["histories"]})}),
              flush=True)
    commits = {commit["sha"] for case in results for item in case["histories"] for commit in item["commits"]}
    links, verification = read_landings(scope, commits, args.landing_index)
    excluded = {number for split in ("development", "held_out", "known_regression")
                for number in registration["sampling"][split]}
    labels = {case["number"]: case["references"] for case in data["cases"]}
    for case in results:
        kinds = ("entity_file", "native_file", "symbol_incident", "file_imports")
        case["arms"] = rank(case["histories"], links, positions[case["commit"]], excluded,
                            kinds=kinds, roles=("evidence",))
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
