"""Evaluate frozen evidence lanes under equal, explicit candidate budgets.

This is an evaluation combiner, not a natural-language or agent entry point.
Reference labels are read after ranking. Best-text-arm scores are oracle controls,
not an implementable retrieval policy.
"""  # noqa: INP001 - Standalone checkpoint evaluation.

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from itertools import zip_longest
from pathlib import Path

from .case_probe import git


def verify_changes(store, pairs):
    """Independently check claimed commit/file pairs against first-parent Git diffs.

    Args:
        store: Canonical local Git object store, accessed without fetching.
        pairs: Commit SHA and repository-relative file pairs from candidate evidence.
    """
    grouped = defaultdict(set)
    for commit, file in pairs:
        grouped[commit].add(file)
    for commit, files in grouped.items():
        changed = set(git(store, "diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z",
                          f"{commit}^1", commit).stdout.split("\x00")) - {""}
        if not files <= changed:
            raise ValueError(f"Files are absent from the first-parent diff: {commit}, {sorted(files - changed)}")
    return {"commits": len(grouped), "commit_file_pairs": sum(map(len, grouped.values()))}


def interleave(rankings, budget):
    if budget < 1:
        raise ValueError("Candidate budget must be positive")
    output, seen = [], set()
    for group in zip_longest(*rankings):
        for number in group:
            if number is not None and number not in seen:
                output.append(number)
                seen.add(number)
                if len(output) == budget:
                    return output
    return output


def evaluate(lexical, temporal, entities, budgets):
    if lexical["scope"] != temporal["scope"] or lexical["scope"] != entities["scope"]:
        raise ValueError("Checkpoint lanes have different evidence boundaries")
    if not lexical["primary_messages"] or temporal["upper"] != entities["upper"]:
        raise ValueError("Checkpoint methods differ from the registered controls")
    source_cases = [{case["number"]: case for case in result["cases"]} for result in (lexical, temporal, entities)]
    if not source_cases[0].keys() == source_cases[1].keys() == source_cases[2].keys():
        raise ValueError("Checkpoint lanes have different case sets")
    outputs = []
    for number, text in source_cases[0].items():
        if source_cases[1][number]["lower"] != source_cases[2][number]["commit"]:
            raise ValueError("Checkpoint lanes use different reported versions")
        file_lane = source_cases[1][number]["arms"]["terminal_trace_file"]["ranking"][:100]
        entity_lane = source_cases[2][number]["arms"]["evidence_entity_file"]["ranking"][:100]
        lanes = [text["fusion"][:100], file_lane, entity_lane]
        rankings = {str(budget): {"text": lanes[0][:budget], "hybrid": interleave(lanes, budget)} for budget in budgets}
        references = {key: {"merged": reference["merged"], "ranks": {
            budget: {kind: ranked.index(int(key)) + 1 if int(key) in ranked else None
                     for kind, ranked in methods.items()}
            for budget, methods in rankings.items()
        }, "best_text_arm_rank": min((rank for name, rank in reference.items()
                                       if name not in ("merged", "fusion") and rank is not None), default=None)}
            for key, reference in text["references"].items()}
        outputs.append({"number": number, "rankings": rankings, "references": references,
                        "lane_sizes": list(map(len, lanes))})
    return {"scope": lexical["scope"], "upper": temporal["upper"],
            "policy": {"lane_order": ["text_fusion", "terminal_trace_file", "entity_file"],
                       "budgeting": "round-robin with exact thread deduplication", "budgets": budgets},
            "cases": outputs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lexical", type=Path, required=True)
    parser.add_argument("--temporal", type=Path, required=True)
    parser.add_argument("--entities", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    inputs = [json.loads(path.read_text()) for path in (args.lexical, args.temporal, args.entities)]
    result = evaluate(*inputs, [10, 20, 30])
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    for case in result["cases"]:
        print(json.dumps({"case": case["number"], "references": case["references"]}), flush=True)


if __name__ == "__main__":
    main()
