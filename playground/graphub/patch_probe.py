"""Compare log and CBM-anchored source terms on identical native patch candidates.

This development probe reuses captured R008/R009 retrieval, not a language-model
or natural-language entry point. Patch hunk identity follows patch_evidence.
Scores are lexical support, not judgments of applicability or demonstrated fixes.
References are consumed only after all controlled rankings have been constructed.
"""  # noqa: INP001 - Standalone patch-context experiment.

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from gh_puller.github import git_store_path

from .case_probe import frames
from .change_git import exact_ids, run
from .change_index import search, verify_hits, verify_index
from .change_probe import serializable
from .checkpoint import interleave
from .entity_search import verify_inputs
from .episode_search import segment_input
from .lexical_probe import diagnostics
from .patch_evidence import blob, lines, read_comparison
from .stack_search import symbols

LANES = ("first_raw_landing_bm25", "typed_entities_landing_bm25",
         "first_raw_proposal_bm25", "typed_entities_proposal_bm25")


def tokens(raw):
    return [word.decode().lower() for word in re.findall(rb"[A-Za-z_][A-Za-z_0-9]*", raw) if len(word) >= 3]


def byte_value(raw):
    return {"hex": raw.hex(), "display": raw.decode(errors="replace")}


def source_input(store, case, episode):
    """Read a literal source window after checking captured native frame decoding.

    Args:
        store: Canonical local Git store, without lazy fetching.
        case: Source-verified input with captured native CBM responses and frame bindings.
        episode: R008 segmentation at the same reported commit; no references are used.
    """
    selected = segment_input(case["log"]["text"], case["frames"])
    if selected != episode["segmentation"] or case["commit"] != episode["commit"]:
        raise ValueError("Patch terms use a different traceback selection")
    region = selected["region"]
    message = diagnostics(case["log"]["text"][region["start"]:region["end"]], primary_messages=True)[1]
    result = {"log_terms": sorted(set(tokens(message.encode()))), "source_terms": [],
              "region": region, "commit": case["commit"], "status": "no_resolved_frame"}
    indices = [i for i in region["frame_indices"] if case["frames"][i].get("status") == "resolved"]
    if not indices:
        return result
    exact_ids((case["commit"],))
    native = run(store, "ls-tree", "-r", "-z", case["commit"])
    native.check_returncode()
    entries = {}
    for record in native.stdout.split(b"\0")[:-1]:
        header, path = record.split(b"\t", 1)
        entries[path.decode(errors="surrogateescape")] = header.split()
    parsed = frames(case["log"]["text"], entries)
    captured = [{key: value for key, value in frame.items() if key not in ("symbols", "status")}
                for frame in case["frames"]]
    if parsed != captured:
        raise ValueError("Captured traceback coordinates differ from the source input and native tree")
    index = indices[-1]
    frame = case["frames"][index]
    call = next(call for call in case["calls"] if call["arguments"]["file_pattern"] == frame["file"])
    names = sorted({item["name"] for item in case["frames"] if item["file"] == frame["file"]})
    arguments = {"project": "graphub-probe", "file_pattern": frame["file"],
                 "name_pattern": "^(" + "|".join(map(re.escape, names)) + ")$", "format": "json", "limit": 5000}
    matches = [node for node in symbols(call["response"])
               if node["file"] == frame["file"] and node["qn"].rsplit(".", 1)[-1] == frame["name"]
               and node["start"] <= frame["line"] <= node["end"]]
    if (call["tool"] != "search_graph" or call["arguments"] != arguments or call["response"]["has_more"]
            or len(matches) != 1 or matches != frame["symbols"]):
        raise ValueError("Source window lacks its captured unique native CBM binding")
    node = matches[0]
    _, kind, oid = entries[frame["file"]]
    if kind != b"blob":
        raise ValueError("Native source frame is not a blob")
    size = run(store, "cat-file", "-s", oid.decode())
    size.check_returncode()
    if int(size.stdout) > 4194304:
        return {**result, "status": "source_blob_limit", "blob": oid.decode()}
    content = blob(store, oid.decode())
    source = lines(content)
    if not 1 <= node["start"] <= frame["line"] <= node["end"] <= len(source):
        raise ValueError("Native source symbol extends beyond its Git blob")
    start, end = max(node["start"], frame["line"] - 5), min(node["end"], frame["line"] + 5)
    window = b"".join(source[start - 1:end])
    return {**result, "status": "resolved_window", "source_terms": sorted(set(tokens(window))),
            "frame_index": index, "symbol": node, "blob": oid.decode(), "blob_sha256": sha256(content).hexdigest(),
            "start": start, "end": end, "window": window}


def candidate_pool(episode, changes, excluded):
    lanes = [episode["lexical"]["fusion"], *[changes["arms"][name]["ranking"] for name in LANES]]
    return interleave([[number for number in lane[:100] if number not in excluded] for lane in lanes], 100)


def rank(pool, documents, terms, *, context):
    """Score hunks under a fixed PR budget without assigning absence a negative score.

    Args:
        pool: Ordered distinct candidates, already source-thread-excluded.
        documents: Verified native hunks with PR/change identities and byte lines.
        terms: Distinct query tokens from the log, optionally unioned with source terms.
        context: Include unchanged hunk lines, or score only additions and deletions.

    Returns:
        Full pool permutation and winning hunk evidence. Candidates without any
        readable token-bearing hunk keep the same slots in every arm.
    """
    active = {doc["number"] for doc in documents if any(tokens(line["text"]) for line in doc["lines"])}
    counts = [Counter(word for line in doc["lines"] if context or line["role"] != "context"
                      for word in tokens(line["text"])) for doc in documents]
    frequency = Counter(word for count in counts for word in count)
    average = sum(sum(count.values()) for count in counts) / len(counts) if counts else 0
    best = {}
    for doc, count in zip(documents, counts, strict=True):
        score = 0.0
        matched = []
        for word in sorted(set(terms) & count.keys()):
            tf = count[word]
            idf = math.log1p((len(counts) - frequency[word] + 0.5) / (frequency[word] + 0.5))
            score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * sum(count.values()) / average))
            matched.append(word)
        number = doc["number"]
        if number not in best or score > best[number]["score"]:
            best[number] = {"number": number, "hunk": doc["id"], "score": score, "matched_terms": matched}
    positions = {number: i for i, number in enumerate(pool)}
    ordered = iter(sorted(active, key=lambda number: (-best[number]["score"], positions[number])))
    ranking = [next(ordered) if number in active else number for number in pool]
    return {"ranking": ranking, "fixed_slots": [i for i, number in enumerate(pool) if number not in active],
            "results": [best[number] for number in ranking[:30] if number in best]}


def evaluate(store, index, case, episode, changes, excluded):
    """Run reference-blind source/field ablations and retain native line evidence.

    Args:
        store: Canonical local Git store.
        index: Sealed change index already attested against its frozen source.
        case: Source-verified case with captured CBM responses.
        episode: Matching R008 text and segmentation artifact.
        changes: Matching R009 change-query artifact, including source path controls.
        excluded: Frozen source-thread exclusions used by both retrieval corpora.
    """
    terms = source_input(store, case, episode)
    for name in LANES:
        path_kind, kind, method = name.rsplit("_", 2)
        paths = [bytes.fromhex(item["git_path_hex"]) for item in changes["paths"][path_kind]]
        hits = search(index, paths, kind=kind, method=method, excluded=excluded, limit=100)
        if serializable(hits) != changes["arms"][name]["results"]:
            raise ValueError("Captured candidate lane differs from its sealed-index replay")
    pool = candidate_pool(episode, changes, excluded)
    selected, jobs = [], {}
    for number in pool:
        for change, kind, detail, status in index.execute(
            "SELECT id,kind,detail,status FROM changes WHERE number=? ORDER BY id", (number,),
        ):
            item = {"change": change, "number": number, "kind": kind, "status": status, "boundary": json.loads(detail)}
            selected.append(item)
            if status == "complete":
                pair = item["boundary"]["before"], item["boundary"]["after"]
                files = tuple(index.execute("SELECT file,change FROM files WHERE change_id=? ORDER BY file", (change,)))
                jobs[pair] = files
    ordered = sorted(jobs)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as workers:
        values = workers.map(lambda pair: read_comparison(store, *pair, jobs[pair]), ordered)
        patches = dict(zip(ordered, values, strict=True))
    read_seconds = time.monotonic() - started
    documents, evidence, proof_hits = [], [], []
    for item in selected:
        if item["status"] != "complete":
            evidence.append(item)
            continue
        patch = patches[item["boundary"]["before"], item["boundary"]["after"]]
        proof_hits.append({"change": item["change"], "number": item["number"], "matched": []})
        summary = {**item, "patch": {key: value for key, value in patch.items()
                                     if key != "files" or patch["status"] != "complete"}}
        if patch["status"] == "complete":
            summary["patch"]["files"] = [{key: value for key, value in file.items() if key != "hunks"}
                                           | {"hunk_count": len(file["hunks"])} for file in patch["files"]]
            for file_index, file in enumerate(patch["files"]):
                for hunk_index, hunk in enumerate(file["hunks"]):
                    documents.append({**hunk, "id": f"{item['change']}:{file_index}:{hunk_index}",
                                      "change": item["change"], "number": item["number"], "file": file["file"],
                                      "old_blob": file["old"], "new_blob": file["new"]})
        evidence.append(summary)
    arms = {}
    for source, query in (("log", terms["log_terms"]), ("source", terms["log_terms"] + terms["source_terms"])):
        for context in (False, True):
            arms[f"{source}_{'context' if context else 'delta'}"] = rank(pool, documents, query, context=context)
    rankings = {str(budget): {"pool_order": pool[:budget], **{name: arm["ranking"][:budget]
                                                          for name, arm in arms.items()}} for budget in (10, 20, 30)}
    retained = {hit["hunk"] for arm in arms.values() for hit in arm["results"]}
    output = {"number": case["number"], "commit": case["commit"], "query": terms, "pool": pool,
            "patches": evidence, "hunks": len(documents), "arms": arms, "rankings": rankings,
            "winning_hunks": [doc for doc in documents if doc["id"] in retained],
            "cost": {"unique_comparisons": len(patches), "seconds": read_seconds,
                     "patch_bytes": sum(patch.get("patch_bytes", 0) for patch in patches.values()),
                     "verified_line_sides": sum(patch.get("verified_line_sides", 0) for patch in patches.values())}}
    return output, proof_hits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cases", "episodes", "changes", "index", "round", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    paths = (args.cases, args.episodes, args.changes, args.round)
    data, episodes, changes, registration = [json.loads(path.read_text()) for path in paths]
    scope = data["scope"]
    if any(value["scope"] != scope for value in (episodes, changes, registration)):
        raise ValueError("Patch controls use different source boundaries")
    excluded = {number for split in ("development", "held_out", "known_regression")
                for number in registration["sampling"][split]}
    if episodes["corpus"] != changes["corpus"] or episodes["corpus"]["excluded"] != sorted(excluded):
        raise ValueError("Patch controls use different candidate corpora")
    groups = [{case["number"]: case for case in value["cases"]} for value in (data, episodes, changes)]
    if any(group.keys() != groups[0].keys() for group in groups[1:]) or not groups[0].keys() <= excluded:
        raise ValueError("Patch controls use different or unregistered cases")
    source_scope = {key: scope[key] for key in ("repository", "cutoff", "selected_digest")}
    store = git_store_path(scope["github"])
    outputs = []
    with closing(sqlite3.connect(f"file:{Path(scope['github']).resolve()}?mode=ro", uri=True)) as db, closing(
        sqlite3.connect(f"file:{args.index.resolve()}?mode=ro", uri=True),
    ) as index:
        meta = verify_index(index, source_scope)
        if meta != changes["index"]:
            raise ValueError("Patch controls differ from the captured change-index attestation")
        verify_inputs(db, data)
        for number, case in groups[0].items():
            output, hits = evaluate(store, index, case, groups[1][number], groups[2][number], excluded)
            output["verification"] = verify_hits(db, store, index, hits, source_scope)
            output["references"] = {str(ref["number"]): {"merged": ref.get("merged"),
                "in_pool": ref["number"] in output["pool"], "ranks": {
                    budget: {name: values.index(ref["number"]) + 1 if ref["number"] in values else None
                             for name, values in methods.items()} for budget, methods in output["rankings"].items()
                }} for ref in case["references"]}
            outputs.append(output)
            print(json.dumps({"case": number, "hunks": output["hunks"], "cost": output["cost"],
                              "references": output["references"]}), flush=True)
    result = {"scope": scope, "index": meta, "corpus": episodes["corpus"], "cases": outputs,
              "inputs_sha256": {str(path): sha256(path.read_bytes()).hexdigest() for path in paths[:-1]},
              "seconds": time.monotonic() - started}
    args.out.write_text(json.dumps(result, default=byte_value, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
