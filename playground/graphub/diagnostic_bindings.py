"""Ablate diagnostic source roles and native node kinds without changing code queries.

The candidate vocabulary and compact CBM responses come from the R003 control.
Source roles describe textual evidence, not runtime identity. Path-like strings,
command arguments and serialized traceback tails remain inspectable but do not
seed the source-role arm. Container nodes do not seed the definition-kind arm.
Git and the frozen landing index supply the same historical evidence for every
arm. Closing references are read only after candidate ranking.
"""  # noqa: INP001 - Standalone research ablation.

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

from gh_puller.github import git_store_path

from .case_probe import git
from .checkpoint import interleave
from .entity_search import candidate_scopes, verify_inputs
from .future_search import rank, read_landings
from .lexical_probe import diagnostics, identifier_terms
from .stack_search import symbols

CONTAINERS = {"File", "Folder", "Project", "Module", "Package", "Section"}
POLICIES = {"baseline": (False, False), "source_roles": (True, False),
            "node_kinds": (False, True), "source_and_kind": (True, True)}


def occurrences(log):
    """Locate the control's identifiers in verbatim primary-message spans.

    Args:
        log: Unmodified diagnostic input; offsets count characters in this string.

    Returns:
        Occurrences with explicit source roles and the containing message span.
        Every baseline term remains represented, including rejected occurrences.
    """
    output = []
    for pattern in (r"\b[\w.]*(?:Error|Exception):[^\r\n]*", r"\berror:[^\r\n]*"):
        for diagnostic in re.finditer(pattern, log):
            start = diagnostic.start() + diagnostic[0].index(":") + 1
            raw = log[start:diagnostic.end()]
            start += len(raw) - len(raw.lstrip())
            message = raw.strip().split(". ", 1)[0]
            end = start + len(message)
            regions = [("command_argument", match.start(1), match.end(1)) for match in re.finditer(
                r"\bCommand (.+?) returned non-zero exit status\b", message,
            )]
            # A token boundary avoids retrying a long slash-free word at every character.
            paths = [(match.start(), match.end()) for match in re.finditer(
                r"(?<![\w./\\-])(?:[A-Za-z]:[\\/]|(?:\.{0,2}|[\w.-]+)/)[^\s'\"<>()\[\]{},;]+", message,
            )]
            regions.extend(("path_like", left, right) for left, right in paths)
            last_frame = max((match.start() for match in re.finditer(
                r"\bFile [\"'].*?[, ]line \d+", message,
            )), default=-1)
            for newline in re.finditer(r"(?<!\\)\\n", message):
                if newline.end() > last_frame:
                    break
                if any(left <= newline.start() < right for left, right in paths):
                    continue
                regions.append(("serialized_traceback_context", newline.start(), len(message)))
                break
            for match in re.finditer(r"\b[A-Za-z_]\w*\b", message):
                if not identifier_terms(match[0]):
                    continue
                role = next((role for role, left, right in regions
                             if left <= match.start() and match.end() <= right), "diagnostic_message")
                output.append({"name": match[0], "start": start + match.start(), "end": start + match.end(),
                               "role": role, "message_start": start, "message_end": end})
    if sorted({item["name"] for item in output}) != identifier_terms(diagnostics(log, primary_messages=True)[1]):
        raise ValueError("Source-span extraction differs from the control vocabulary")
    return output


def bind(log, response):
    """Retain exact-case candidate links, native labels and source-role ambiguity.

    Args:
        log: Verbatim input used by the baseline name query.
        response: Native compact search_graph JSON with name, label and lines columns.

    Returns:
        Candidate mentions, not proven runtime entity identities. Missing and
        truncated name lookups remain explicit regardless of policy filtering.
    """
    if response["cols"][:3] != ["name", "label", "lines"]:
        raise ValueError("Native compact columns differ from the pinned decoder")
    rows = [row for group in response["groups"] for row in group["rows"]]
    nodes = [{**node, "label": row[1]} for node, row in zip(symbols(response), rows, strict=True)]
    grouped = defaultdict(list)
    for item in occurrences(log):
        if log[item["start"]:item["end"]] != item["name"]:
            raise ValueError("A diagnostic name differs from its source span")
        grouped[item["name"]].append(item)
    output = []
    for name, spans in sorted(grouped.items()):
        matches = [node for node in nodes if node["qn"].rsplit(".", 1)[-1] == name]
        output.append({"name": name, "occurrences": spans, "symbols": matches,
                       "status": "query_truncated" if response["has_more"] else
                       "unique_name_match" if len(matches) == 1 else "ambiguous" if matches else "not_found"})
    return output


def select_mentions(mentions, *, source_roles, node_kinds):
    output = []
    for mention in mentions:
        supported = any(item["role"] == "diagnostic_message" for item in mention["occurrences"])
        if source_roles and not supported:
            continue
        nodes = [node for node in mention["symbols"] if not node_kinds or node["label"] not in CONTAINERS]
        output.append({**mention, "symbols": nodes})
    return output


def evaluate(data, entities, lexical, temporal, registration, index_path):
    """Re-rank matched historical evidence and compare fixed-budget lane combinations.

    Args:
        data: Source-verified case-probe artifact.
        entities: Baseline native lookups and complete per-file histories.
        lexical: Primary-message text results including exact corpus exclusions.
        temporal: Native Git traceback-path control at the same version boundary.
        registration: Frozen source identity and evaluation thread exclusions.
        index_path: Sealed, source-attested landing index.
    """
    scope = data["scope"]
    if any(value["scope"] != scope for value in (entities, lexical, temporal, registration)):
        raise ValueError("Binding controls have different evidence boundaries")
    excluded = {number for split in ("development", "held_out", "known_regression")
                for number in registration["sampling"][split]}
    if lexical["corpus"]["excluded"] != sorted(excluded) or not lexical["primary_messages"]:
        raise ValueError("Text control differs from the registered corpus")
    if lexical["corpus"]["scope"] != {key: scope[key] for key in ("repository", "cutoff", "selected_digest")}:
        raise ValueError("Text corpus has a different source identity")
    groups = [{case["number"]: case for case in artifact["cases"]}
              for artifact in (data, entities, lexical, temporal)]
    if any(group.keys() != groups[0].keys() for group in groups[1:]) or entities["upper"] != temporal["upper"]:
        raise ValueError("Binding controls have different cases or upper commits")
    if not groups[0].keys() <= excluded:
        raise ValueError("A binding case is outside the exclusion set")
    with sqlite3.connect(f"file:{Path(scope['github']).resolve()}?mode=ro", uri=True) as db:
        verify_inputs(db, data)
    commits = {commit["sha"] for case in entities["cases"] for item in case["histories"]
               if item["kind"] == "entity_file" for commit in item["commits"]}
    links, verification = read_landings(scope, commits, index_path)
    store = git_store_path(scope["github"])
    positions, outputs = {}, []
    for number, case in groups[0].items():
        baseline, text, trace = (group[number] for group in groups[1:])
        if baseline["commit"] != case["commit"] or trace["lower"] != case["commit"]:
            raise ValueError("Binding controls use different reported versions")
        lookup = baseline["probe"]["symbol_lookup"]
        names = identifier_terms(diagnostics(case["log"]["text"], primary_messages=True)[1])
        pattern = "^(" + "|".join(map(re.escape, names)) + ")$" if names else "a^"
        if lookup["arguments"] != {"project": "graphub-probe", "name_pattern": pattern,
                                   "format": "json", "limit": 5000}:
            raise ValueError("Native name query differs from the control")
        mentions = bind(case["log"]["text"], lookup["response"])
        plain = [{"name": item["name"], "status": item["status"],
                  "symbols": [{key: value for key, value in node.items() if key != "label"}
                              for node in item["symbols"]]} for item in mentions]
        if plain != baseline["probe"]["mentions"]:
            raise ValueError("Typed decoding differs from the native baseline")
        histories = {item["file"]: item["commits"] for item in baseline["histories"]
                     if item["kind"] == "entity_file"}
        scopes, selected = [], {}
        for policy, (source_roles, node_kinds) in POLICIES.items():
            filtered = select_mentions(mentions, source_roles=source_roles, node_kinds=node_kinds)
            values = candidate_scopes({"mentions": filtered, "native_locations": [],
                                       "seeds": [], "relationships": {}}, set(histories))
            selected[policy] = sorted({item["file"] for item in values})
            scopes.extend({**item, "kind": policy, "commits": histories[item["file"]]} for item in values)
        commit = case["commit"]
        if commit not in positions:
            ordered = git(store, "rev-list", "--reverse", "--topo-order", "--ancestry-path",
                          f"{commit}..{entities['upper']}").stdout.splitlines()
            positions[commit] = {sha: offset for offset, sha in enumerate(ordered)}
        arms = rank(scopes, links, positions[commit], excluded, kinds=POLICIES, roles=("evidence",))
        if arms["evidence_baseline"]["ranking"] != [item for item in baseline["arms"]["evidence_entity_file"]["ranking"]
                                                   if item not in excluded]:
            raise ValueError("Re-ranked baseline differs from the recorded control")
        trace_lane = [item for item in trace["arms"]["terminal_trace_file"]["ranking"] if item not in excluded][:100]
        text_lane = text["fusion"][:100]
        rankings = {str(budget): {"text": text_lane[:budget], **{
            policy: interleave([text_lane, trace_lane, arms[f"evidence_{policy}"]["ranking"][:100]], budget)
            for policy in POLICIES
        }} for budget in (10, 20, 30)}
        references = {str(reference["number"]): {"merged": reference.get("merged"), "ranks": {
            budget: {policy: values.index(reference["number"]) + 1 if reference["number"] in values else None
                     for policy, values in methods.items()} for budget, methods in rankings.items()
        }} for reference in case["references"]}
        result = {"number": number, "commit": commit, "mentions": mentions, "selected_files": selected,
                  "arms": arms, "rankings": rankings, "references": references}
        outputs.append(result)
        print(json.dumps({"case": number, "files": selected, "references": references}), flush=True)
    return {"scope": scope, "upper": entities["upper"], "corpus": lexical["corpus"], "cases": outputs,
            "verification": verification, "excluded_threads": sorted(excluded),
            "policy": {"ablations": POLICIES, "budgets": [10, 20, 30], "primary_budget": 20,
                       "lane_order": ["text_fusion", "terminal_trace_file", "ablated_entity_file"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cases", "entities", "lexical", "temporal", "round", "landing-index", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    paths = (args.cases, args.entities, args.lexical, args.temporal, args.round)
    inputs = [json.loads(path.read_text()) for path in paths]
    result = evaluate(*inputs, args.landing_index)
    result["seconds"] = time.monotonic() - started
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
