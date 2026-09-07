"""Compare traceback-segment text and matched native symbol/file histories.

Segments are literal traceback-header intervals, not proven causal episodes or
process identities. The first complete segment has a parsed frame and a Python
exception diagnostic; its original text offsets remain visible. Native CBM
counterparts and Git line histories retain the correspondence limits described
in future_search. Evaluation references do not select text, frames or candidates.
"""  # noqa: INP001 - Standalone research comparison.

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from hashlib import sha256
from pathlib import Path

from gh_puller.github import git_store_path

from .case_probe import git
from .checkpoint import interleave
from .entity_search import verify_inputs
from .future_search import history, rank, read_landings
from .lexical_probe import diagnostics, fuse, queries
from .text_index import search, verify_matches

CODE_POLICIES = ("first_raw", "first_symbol", "all_raw", "all_symbol", "first_upper_file", "all_upper_file")


def segment_input(log, frames):
    """Select the first structurally complete segment without interpreting causality.

    Args:
        log: Verbatim input, including any compiler preamble before its first header.
        frames: Parsed frames with character offsets relative to the complete input.

    Returns:
        Segment evidence and an exact selected region. If no segment is complete,
        the whole input is retained with an explicit fallback marker.
    """
    headers = [match.start() for match in re.finditer(r"Traceback \(most recent call last\)", log)]
    starts = [0, *headers[1:]]
    segments = []
    for start, end in zip(starts, [*starts[1:], len(log)], strict=True):
        indices = [i for i, frame in enumerate(frames) if start <= frame["offset"] < end]
        errors = diagnostics(log[start:end])[0]
        segments.append({"start": start, "end": end, "frame_indices": indices,
                         "complete": bool(headers and indices and errors)})
    selected = next((i for i, segment in enumerate(segments) if segment["complete"]), None)
    region = segments[selected] if selected is not None else {"start": 0, "end": len(log),
                                                             "frame_indices": list(range(len(frames)))}
    text = log[region["start"]:region["end"]]
    return {"segments": segments, "selected": selected, "fallback": selected is None,
            "region": {key: region[key] for key in ("start", "end", "frame_indices")},
            "digest": sha256(text.encode()).hexdigest()}


def code_scopes(store, case, trace, upper, segmentation):
    """Prepare raw scopes and exact counterpart-matched file/range ablations.

    Args:
        store: Canonical local Git store, queried without fetching.
        case: Original parsed frames and reported commit from the case probe.
        trace: Complete native Git histories and native CBM counterpart evidence.
        upper: Inclusive frozen descendant endpoint used by every history query.
        segmentation: Selection from segment_input with original frame indices.
    """
    frames = [case["frames"][i] for i in segmentation["region"]["frame_indices"]]
    first_raw = {(frame["file"], frame["line"], frame["name"]) for frame in frames if frame["file"]}
    first_symbols = {frame["symbols"][0]["qn"] for frame in frames if frame.get("status") == "resolved"}
    output = []
    for item in trace["generated"]["histories"]:
        node = item["lower_anchor"]
        if item["kind"] == "trace_file":
            scopes = ["all_raw"]
            if (node["file"], node["line"], node["name"]) in first_raw:
                scopes.append("first_raw")
            output.extend({**item, "kind": kind, "roles": ["evidence"]} for kind in scopes)
        elif item["kind"] == "upper_symbol":
            groups = ["all"] + (["first"] if node["qn"] in first_symbols else [])
            full = history(store, case["commit"], upper, item["upper_anchor"]["file"])
            for group in groups:
                matched = {**item, "file": item["upper_anchor"]["file"], "roles": ["evidence"]}
                output.append({**matched, "kind": f"{group}_symbol"})
                output.append({**matched, "kind": f"{group}_upper_file", "commits": full,
                               "outside_descendant_interval": 0})
    return output


def combine(text_all, text_first, trace, entities, code, budgets=(10, 20, 30)):
    """Compare policies with the same distinct-thread budget and per-lane cap.

    Args:
        text_all: Full-input lexical ranking.
        text_first: Selected-segment lexical ranking.
        trace: Terminal raw-file ranking used by the baseline.
        entities: R007 source-and-kind-filtered entity-file ranking.
        code: Independent code policy rankings from matched scopes.
        budgets: Explicit candidate limits shared by every method.
    """
    lanes = {"baseline": [text_all, trace, entities],
             **{name: [text_first, values, entities] for name, values in code.items()}}
    return {str(budget): {"text_all": text_all[:budget], "text_first": text_first[:budget], **{
        name: interleave([lane[:100] for lane in values], budget) for name, values in lanes.items()
    }} for budget in budgets}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cases", "lexical", "temporal", "bindings", "index", "landing-index", "round", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    data, lexical, temporal, bindings, registration = [json.loads(path.read_text()) for path in (
        args.cases, args.lexical, args.temporal, args.bindings, args.round,
    )]
    scope = data["scope"]
    if any(value["scope"] != scope for value in (lexical, temporal, bindings, registration)):
        raise ValueError("Episode controls have different source boundaries")
    excluded = sorted({number for split in ("development", "held_out", "known_regression")
                       for number in registration["sampling"][split]})
    groups = [{case["number"]: case for case in value["cases"]} for value in (data, lexical, temporal, bindings)]
    if any(group.keys() != groups[0].keys() for group in groups[1:]) or not groups[0].keys() <= set(excluded):
        raise ValueError("Episode controls have different or unregistered cases")
    upper = temporal["upper"]
    if bindings["upper"] != upper or not lexical["primary_messages"]:
        raise ValueError("Episode control method or upper boundary differs")
    store = git_store_path(scope["github"])
    outputs, positions = [], {}
    with sqlite3.connect(f"file:{Path(scope['github']).resolve()}?mode=ro", uri=True) as db:
        verify_inputs(db, data)
        with sqlite3.connect(f"file:{args.index.resolve()}?mode=ro", uri=True) as index:
            corpus = {key: json.loads(value) for key, value in index.execute("SELECT key,value FROM meta")}
            if corpus != lexical["corpus"] or corpus != bindings["corpus"] or corpus["excluded"] != excluded:
                raise ValueError("Episode controls use different text corpora")
            if corpus["scope"] != {key: scope[key] for key in ("repository", "cutoff", "selected_digest")}:
                raise ValueError("Episode text corpus differs from its source identity")
            for number, case in groups[0].items():
                trace = groups[2][number]
                if trace["lower"] != case["commit"] or groups[3][number]["commit"] != case["commit"]:
                    raise ValueError("Episode controls have different reported commits")
                selected = segment_input(case["log"]["text"], case["frames"])
                region = selected["region"]
                text = case["log"]["text"][region["start"]:region["end"]]
                frames = [case["frames"][i] for i in region["frame_indices"]]
                arms = {}
                for name, query in queries(index, text, frames, primary_messages=True).items():
                    matches = search(index, query, limit=100)
                    arms[name] = {"query": query, "results": matches, "ranking": [row["number"] for row in matches]}
                output = {"number": number, "commit": case["commit"], "segmentation": selected,
                          "lexical": {"arms": arms, "fusion": fuse(arm["ranking"] for arm in arms.values())},
                          "scopes": code_scopes(store, case, trace, upper, selected),
                          "missing_upper": trace["generated"]["unresolved_at_upper"]}
                outputs.append(output)
                print(json.dumps({"case": number, "segments": len(selected["segments"]),
                                  "selected": selected["selected"], "scopes": len(output["scopes"])}), flush=True)
            matches = (row for case in outputs for arm in case["lexical"]["arms"].values() for row in arm["results"])
            text_verification = verify_matches(db, index, matches, scope["cutoff"])
    commits = {commit["sha"] for case in outputs for item in case["scopes"] for commit in item["commits"]}
    links, landing_verification = read_landings(scope, commits, args.landing_index)
    for case in outputs:
        number, commit = case["number"], case["commit"]
        if commit not in positions:
            ordered = git(store, "rev-list", "--reverse", "--topo-order", "--ancestry-path",
                          f"{commit}..{upper}").stdout.splitlines()
            positions[commit] = {sha: i for i, sha in enumerate(ordered)}
        case["code"] = rank(case["scopes"], links, positions[commit], set(excluded),
                            kinds=CODE_POLICIES, roles=("evidence",))
        lanes = [groups[1][number]["fusion"], case["lexical"]["fusion"],
                 groups[2][number]["arms"]["terminal_trace_file"]["ranking"],
                 groups[3][number]["arms"]["evidence_source_and_kind"]["ranking"]]
        clean = [[value for value in lane if value not in excluded] for lane in lanes]
        case["rankings"] = combine(
            *clean, {name: case["code"][f"evidence_{name}"]["ranking"] for name in CODE_POLICIES},
        )
        case["references"] = {str(ref["number"]): {"merged": ref.get("merged"), "ranks": {
            budget: {name: ranked.index(ref["number"]) + 1 if ref["number"] in ranked else None
                     for name, ranked in methods.items()} for budget, methods in case["rankings"].items()
        }} for ref in groups[0][number]["references"]}
        print(json.dumps({"case": number, "references": case["references"]}), flush=True)
    result = {"scope": scope, "upper": upper, "corpus": corpus, "cases": outputs,
              "verification": {"text": text_verification, "landings": landing_verification},
              "seconds": time.monotonic() - started}
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
