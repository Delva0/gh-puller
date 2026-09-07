"""Compare snapshot-independent change retrieval with captured stack and CBM inputs.

This is a development evaluation, not an agent or natural-language entry point.
Every query uses source-preserved traceback paths or R007 typed CBM bindings.
Reference labels explain coverage only after all candidate rankings are complete.
"""  # noqa: INP001 - Standalone retrieval experiment.

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from gh_puller.github import git_store_path

from .change_index import search, verify_hits, verify_index
from .checkpoint import interleave
from .entity_search import verify_inputs
from .episode_search import segment_input


def query_paths(case, episode, binding):
    selected = segment_input(case["log"]["text"], case["frames"])
    if selected != episode["segmentation"] or case["commit"] != binding["commit"]:
        raise ValueError("Change-query controls use different input selections")
    raw = {case["frames"][i]["file"] for i in selected["region"]["frame_indices"]} - {None}
    entities = set(binding["selected_files"]["source_and_kind"])
    return {name: sorted(path.encode() for path in paths) for name, paths in (
        ("first_raw", raw), ("typed_entities", entities), ("first_with_entities", raw | entities),
    )}


def evaluate(index, case, episode, binding, excluded):
    """Run fixed evidence-path controls without consulting evaluation references.

    Args:
        index: Sealed change index, attested before the evaluation batch.
        case: Source-verified verbatim input and captured native frame resolution.
        episode: R008 source-preserving segment selection and frozen text ranking.
        binding: R007 native source-and-kind-filtered entity-file selection.
        excluded: Source thread numbers excluded before corpus statistics and ranking.
    """
    paths = query_paths(case, episode, binding)
    arms = {}
    for source, files in paths.items():
        for kind in ("landing", "proposal"):
            for method in ("overlap", "bm25"):
                started = time.monotonic()
                hits = search(index, files, kind=kind, method=method, excluded=excluded, limit=100)
                arms[f"{source}_{kind}_{method}"] = {"results": hits, "ranking": [h["number"] for h in hits],
                                                     "seconds": time.monotonic() - started}
    text = [n for n in episode["lexical"]["fusion"] if n not in excluded]
    rankings = {str(budget): {"text_first": text[:budget],
                             "r008_first_raw": episode["rankings"][str(budget)]["first_raw"],
                             **{name: arm["ranking"][:budget] for name, arm in arms.items()},
                             **{f"text_plus_{name}": interleave([text[:100], arm["ranking"]], budget)
                                for name, arm in arms.items()}} for budget in (10, 20, 30)}
    return {"number": case["number"], "commit": case["commit"], "paths": paths,
            "arms": arms, "rankings": rankings}


def reference_coverage(index, case, paths):
    output = {}
    for ref in case["references"]:
        coverage = defaultdict(list)
        for change, kind, status, detail in index.execute(
            "SELECT id,kind,status,detail FROM changes WHERE number=? ORDER BY id", (ref["number"],),
        ):
            files = {row[0] for row in index.execute("SELECT file FROM files WHERE change_id=?", (change,))}
            coverage[kind].append({"change": change, "status": status, "boundary": json.loads(detail),
                                   "matched": {name: sorted(files & set(values)) for name, values in paths.items()}})
        output[str(ref["number"])] = dict(coverage)
    return output


def serializable(value):
    if isinstance(value, bytes):
        return {"git_path_hex": value.hex(), "display": value.decode(errors="replace")}
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serializable(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cases", "episodes", "bindings", "index", "round", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    data, episodes, bindings, registration = [json.loads(path.read_text()) for path in (
        args.cases, args.episodes, args.bindings, args.round,
    )]
    scope = data["scope"]
    if any(value["scope"] != scope for value in (episodes, bindings, registration)):
        raise ValueError("Change-query controls have different source boundaries")
    excluded = {n for split in ("development", "held_out", "known_regression")
                for n in registration["sampling"][split]}
    groups = [{case["number"]: case for case in value["cases"]} for value in (data, episodes, bindings)]
    if any(group.keys() != groups[0].keys() for group in groups[1:]) or not groups[0].keys() <= excluded:
        raise ValueError("Change-query controls have different or unregistered case sets")
    if episodes["corpus"] != bindings["corpus"] or episodes["corpus"]["excluded"] != sorted(excluded):
        raise ValueError("Change-query controls use different text corpora")
    source_scope = {key: scope[key] for key in ("repository", "cutoff", "selected_digest")}
    store = git_store_path(scope["github"])
    outputs = []
    with closing(sqlite3.connect(f"file:{Path(scope['github']).resolve()}?mode=ro", uri=True)) as db, closing(
        sqlite3.connect(f"file:{args.index.resolve()}?mode=ro", uri=True),
    ) as index:
        index_meta = verify_index(index, source_scope)
        verify_inputs(db, data)
        for number, case in groups[0].items():
            output = evaluate(index, case, groups[1][number], groups[2][number], excluded)
            hits = [hit for arm in output["arms"].values() for hit in arm["results"]]
            output["verification"] = verify_hits(db, store, index, hits, source_scope)
            output["reference_coverage"] = reference_coverage(index, case, output["paths"])
            output["references"] = {str(ref["number"]): {"merged": ref.get("merged"), "ranks": {
                budget: {name: values.index(ref["number"]) + 1 if ref["number"] in values else None
                         for name, values in methods.items()} for budget, methods in output["rankings"].items()
            }} for ref in case["references"]}
            outputs.append(output)
            print(json.dumps({"case": number, "verification": output["verification"],
                              "references_at_20": {key: value["ranks"]["20"]
                                                   for key, value in output["references"].items()}}), flush=True)
    result = {"scope": scope, "index": index_meta, "corpus": episodes["corpus"], "cases": outputs,
              "seconds": time.monotonic() - started}
    args.out.write_text(json.dumps(serializable(result), ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
