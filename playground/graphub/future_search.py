"""Compare bounded post-version file and CBM-symbol histories with lexical retrieval.

Same-qualified-name endpoints are candidate correspondences, not proven symbol
lineages. Symbol histories belong to the upper snapshot's source interval. Git owns
history traversal and CBM owns symbol lookup; closing references are evaluation-only.
"""  # noqa: INP001 - Standalone research comparison.

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import tempfile
import time
from contextlib import closing
from functools import cache
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from gh_puller.codebase import Archive, resolve_cbm_binary
from gh_puller.codebase.cbm_transport import PersistentMCPTransport
from gh_puller.github import git_store_path

from .audit import select
from .case_probe import ancestry, git
from .gates import pointer
from .landing_index import lookup, verify_index
from .lexical_probe import fuse
from .stack_search import payload, pull_links, restore, symbols


def anchors(log, frames, *, symbol=True):
    """Label stack positions; the path-only control does not require CBM resolution."""
    boundaries = [match.start() for match in re.finditer("Traceback \\(most recent call last\\)", log)]
    resolved = [frame for frame in frames
                if (frame.get("status") == "resolved" if symbol else frame["file"] is not None)]
    by_name = {}
    ordered = []
    for frame in resolved:
        node = frame["symbols"][0] if symbol else {
            key: frame[key] for key in ("file", "line", "name", "reported_file")
        }
        key = node["qn"] if symbol else (node["file"], node["line"], node["name"])
        by_name.setdefault(key, {"node": node, "roles": {"stack"}})
        ordered.append((frame["offset"], key))
    if resolved:
        by_name[ordered[-1][1]]["roles"].add("terminal")
    for index, start in enumerate(boundaries):
        end = boundaries[index + 1] if index + 1 < len(boundaries) else len(log)
        group = [key for offset, key in ordered if start <= offset < end]
        if group:
            by_name[group[-1]]["roles"].add("leaf")
    return [{"node": item["node"], "roles": sorted(item["roles"])} for item in by_name.values()]


@cache
def history(store, lower, upper, file, start=None, end=None):
    """Read immutable lower..upper history; file scope requires first-parent diffs and does not follow renames."""
    command = ["log", "--reverse", "--topo-order"]
    if start is None:
        command += ["--format=%x00%H%x09%ct", "--name-only", "-z", "--full-history", "--no-renames",
                    "--diff-merges=first-parent", "--ancestry-path", f"{lower}..{upper}", "--", file]
        result = git(store, *command)
        records = []
        for block in result.stdout.split("\x00\x00"):
            header, _, files = block.removeprefix("\x00").partition("\x00")
            # History membership alone can include merges that do not change this path.
            if file in files.removeprefix("\n").split("\x00"):
                sha, stamp = header.split("\t")
                records.append({"sha": sha, "time": int(stamp)})
        return records
    command += ["--format=%H%x09%ct", "--no-patch", "-L", f"{start},{end}:{file}", f"{lower}..{upper}"]
    result = git(store, *command)
    return [{"sha": line.split("\t")[0], "time": int(line.split("\t")[1])} for line in result.stdout.splitlines()]


def generate(store, lower, upper, selected, upper_symbols, paths=()):
    """Generate code histories exclusively from resolved query anchors and version scope."""
    if ancestry(store, lower, upper) is not True:
        return {"status": "unverified_version_interval", "histories": [], "unresolved_at_upper": []}
    ordered = git(store, "rev-list", "--reverse", "--topo-order", "--ancestry-path",
                  f"{lower}..{upper}").stdout.splitlines()
    positions = {sha: position for position, sha in enumerate(ordered)}
    missing = []
    scopes = [{"kind": "trace_file", "roles": anchor["roles"], "lower_anchor": anchor["node"],
               "file": anchor["node"]["file"], "commits": history(store, lower, upper, anchor["node"]["file"])}
              for anchor in paths]
    for anchor in selected:
        original = anchor["node"]
        scopes.append({"kind": "file", "roles": anchor["roles"], "lower_anchor": original,
                       "file": original["file"], "commits": history(store, lower, upper, original["file"])})
        counterpart = upper_symbols.get(original["qn"])
        if counterpart is None:
            missing.append(original)
            continue
        commits = history(store, lower, upper, counterpart["file"], counterpart["start"], counterpart["end"])
        scopes.append({"kind": "upper_symbol", "roles": anchor["roles"], "lower_anchor": original,
                       "upper_anchor": counterpart, "correspondence": "same_qualified_name",
                       "outside_descendant_interval": sum(commit["sha"] not in positions for commit in commits),
                       "commits": [commit for commit in commits if commit["sha"] in positions]})
    return {"status": "bounded", "histories": scopes, "unresolved_at_upper": missing,
            "positions": positions, "interval_commits": len(ordered)}


def landings(db, commits, cutoff):
    """Retain exact, source-verified landing pointers; membership is not landing evidence."""
    links = pull_links(db, commits, cutoff)
    observations = {}
    count = 0
    for commit, values in links.items():
        links[commit] = [value for value in values if value["relation"] != "pull_commit"]
        for value in links[commit]:
            oid = value["observation_id"]
            if oid not in observations:
                row = db.execute(
                    "SELECT o.family,o.resource_number,o.payload_digest,p.payload FROM selected o "
                    "JOIN payload_blobs p ON p.digest=o.payload_digest "
                    "WHERE o.id=? AND o.id<=? AND o.coverage='complete'",
                    (oid, cutoff),
                ).fetchone()
                if row is None:
                    raise ValueError("A landing observation is outside the frozen selection")
                observations[oid] = (*row[:3], payload(row[2], row[3]))
            family, number, digest, document = observations[oid]
            if value["relation"] == "pull_landing":
                valid = family == "pull-git" and value["location"] == "/value/landing_sha"
            else:
                valid = (value["relation"] == "merged_pull_commit" and family == "pull"
                         and value["location"] == "/value/merge_commit_sha" and document["value"].get("merged") is True)
            if not valid or number != value["number"] or pointer(document, value["location"]) != commit:
                raise ValueError("A PR candidate lacks an exact landing pointer")
            value["digest"] = digest
            count += 1
    return links, {"source_observations_verified": len(observations), "landing_pointers_verified": count}


def read_landings(scope, commits, index_path=None):
    """Read equivalent landing evidence through a scan or an attested derived index.

    Args:
        scope: Frozen GitHub path, repository, cutoff and selected_digest.
        commits: Exact landing targets required by the current query.
        index_path: Sealed landing index; None selects the scanning control.
    """
    with closing(sqlite3.connect(f"file:{Path(scope['github']).resolve()}?mode=ro", uri=True)) as db:
        if index_path is None:
            repository = db.execute("SELECT value FROM archive_meta WHERE key='repository'").fetchone()
            if repository != (scope["repository"],) or select(db, scope["cutoff"]) != scope["selected_digest"]:
                raise ValueError("GitHub observations differ from the registered boundary")
            return landings(db, commits, scope["cutoff"])
        identity = {key: scope[key] for key in ("repository", "cutoff", "selected_digest")}
        with closing(sqlite3.connect(f"file:{index_path.resolve()}?mode=ro", uri=True)) as index:
            verify_index(index, identity)
            return lookup(db, index, commits, identity)


def rank(scopes, links, positions, excluded, *, kinds=("trace_file", "file", "upper_symbol"),
         roles=("terminal", "leaf", "stack")):
    output = {}
    for kind in kinds:
        for role in roles:
            candidates = {}
            for scope in scopes:
                if scope["kind"] != kind or role not in scope["roles"]:
                    continue
                for commit in scope["commits"]:
                    for link in links[commit["sha"]]:
                        if link["relation"] == "pull_commit" or link["number"] in excluded:
                            continue
                        number = link["number"]
                        item = candidates.setdefault(number, {"number": number, "position": positions[commit["sha"]],
                                                              "evidence": []})
                        item["position"] = min(item["position"], positions[commit["sha"]])
                        evidence = {"commit": commit["sha"], "github": link,
                                    "lower_anchor": scope["lower_anchor"], "kind": kind}
                        if "upper_anchor" in scope:
                            evidence.update(upper_anchor=scope["upper_anchor"], correspondence=scope["correspondence"])
                        item["evidence"].append(evidence)
            values = sorted(candidates.values(), key=lambda value: (value["position"], value["number"]))
            output[f"{role}_{kind}"] = {"ranking": [value["number"] for value in values], "results": values[:100]}
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--lexical", type=Path, required=True)
    parser.add_argument("--round", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--landing-index", type=Path)
    args = parser.parse_args()
    data = json.loads(args.cases.read_text())
    lexical = json.loads(args.lexical.read_text())
    registration = json.loads(args.round.read_text())
    scope = data["scope"]
    if lexical["scope"] != scope or registration["scope"] != scope:
        raise ValueError("Lexical and code evidence boundaries differ")
    started = time.monotonic()
    store = git_store_path(scope["github"])
    archive = Archive(scope["archive"], allow_incomplete=True)
    entries = archive._commits[:scope["graph_count"]]
    identity = [(item["sha"], item["graph_digest"], item["parents"])
                for item in sorted(entries, key=lambda item: item["sha"])]
    if sha256(json.dumps(identity).encode()).hexdigest() != scope["graph_identity_digest"]:
        raise ValueError("Graph input differs from the registered prefix")
    upper = entries[-1]["sha"]
    binary = resolve_cbm_binary()
    if binary.sha256 != scope["cbm_sha256"]:
        raise ValueError("CBM binary differs from the registered experiment")
    selected = {case["number"]: anchors(case["log"]["text"], case["frames"]) for case in data["cases"]}
    names = sorted({anchor["node"]["qn"] for values in selected.values() for anchor in values})
    with tempfile.TemporaryDirectory(prefix="graphub-future-") as scratch:
        root = Path(scratch)
        projection = restore(archive.load_rows(upper), root, "graphub-probe")
        monitor = SimpleNamespace(child_pid=None, exceeded=False, sample=lambda: None)
        transport = PersistentMCPTransport(
            binary.path, root, 60, monitor, extra_environment={"CBM_RUNTIME_DIR": str(root)},
        )
        try:
            pattern = "^(" + "|".join(map(re.escape, names)) + ")$" if names else "a^"
            arguments = {"project": "graphub-probe", "qn_pattern": pattern,
                         "format": "json", "limit": 5000}
            response = transport.call_tool("search_graph", arguments)["structuredContent"]
            if response["has_more"]:
                raise ValueError("Upper symbol query was truncated")
            upper_symbols = {symbol["qn"]: symbol for symbol in symbols(response)}
        finally:
            transport.close()
    print(json.dumps({"upper": upper, "requested_symbols": len(names), "resolved": len(upper_symbols)}), flush=True)
    generated = {}
    for case in data["cases"]:
        begin = time.monotonic()
        paths = anchors(case["log"]["text"], case["frames"], symbol=False)
        result = generate(store, case["commit"], upper, selected[case["number"]], upper_symbols, paths)
        generated[case["number"]] = result
        result["seconds"] = time.monotonic() - begin
        print(json.dumps({"case": case["number"], "scopes": len(result["histories"]),
                          "missing_upper_symbols": len(result["unresolved_at_upper"]),
                          "seconds": result["seconds"]}), flush=True)
    commits = {commit["sha"] for result in generated.values()
               for item in result["histories"] for commit in item["commits"]}
    links, verification = read_landings(scope, commits, args.landing_index)
    lexical_by_case = {case["number"]: case for case in lexical["cases"]}
    excluded = {number for split in ("development", "held_out", "known_regression")
                for number in registration["sampling"][split]}
    if not set(lexical_by_case) <= excluded:
        raise ValueError("A source case is outside the exclusion set")
    records = []
    for case in data["cases"]:
        code = generated[case["number"]]
        arms = rank(code["histories"], links, code.pop("positions", {}), excluded)
        text = lexical_by_case[case["number"]]
        # Labels are read only after independent candidate generation has finished.
        rankings = {name: arm["ranking"] for name, arm in arms.items()}
        for name, arm in arms.items():
            rankings[f"text_plus_{name}"] = fuse([text["fusion"][:100], arm["ranking"][:100]])
        evaluation = {str(reference["number"]): {
            name: ranking.index(reference["number"]) + 1 if reference["number"] in ranking else None
            for name, ranking in rankings.items()
        } for reference in case["references"]}
        records.append({"number": case["number"], "lower": case["commit"], "generated": code,
                        "arms": arms, "rankings": rankings, "reference_ranks": evaluation})
        print(json.dumps({"case": case["number"], "reference_ranks": evaluation}), flush=True)
    result = {"scope": scope, "upper": upper, "projection": projection,
              "verification": verification, "excluded_threads": sorted(excluded),
              "upper_query": {"arguments": arguments, "response": response}, "cases": records,
              "seconds": time.monotonic() - started}
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
