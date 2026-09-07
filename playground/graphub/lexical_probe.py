"""Measure lexical baselines on frozen real logs using evaluation-only PR references.

Known-reference ranks measure retrieval of archived linked PRs, not complete recall,
precision, or demonstrated fixes. Query construction consumes only logs and frames.
"""  # noqa: INP001 - Standalone research comparison.

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

from .text_index import search, verify_matches


def phrase(text):
    return '"' + text.replace('"', '""') + '"'


def diagnostics(text, *, primary_messages=False):
    errors = re.findall(r"\b[\w.]*(?:Error|Exception):[^\r\n]*", text)
    native = re.findall(r"\berror:[^\r\n]*", text)
    diagnostic_text = "\n".join(errors + native)
    if primary_messages:
        diagnostic_text = "\n".join(error.partition(":")[2].strip().split(". ", 1)[0] for error in errors + native)
    return errors, diagnostic_text


def identifier_terms(text):
    return sorted({name for name in re.findall(r"\b[A-Za-z_]\w*\b", text)
                   if ("_" in name or any(letter.isupper() for letter in name[1:]))
                   and not name.endswith(("Error", "Exception"))})


def queries(index, text, frames, *, primary_messages=False):
    """Construct fixed lexical arms without reading issue titles or reference labels."""
    errors, diagnostic_text = diagnostics(text, primary_messages=primary_messages)
    words = sorted(set(re.findall(r"[a-z]+", diagnostic_text.lower())))
    frequencies = []
    for word in words:
        row = index.execute("SELECT doc FROM vocabulary WHERE term=?", (word,)).fetchone()
        if row and len(word) > 2:
            frequencies.append((row[0], word))
    rare = [word for _, word in sorted(frequencies)[:5]]
    names = sorted({frame["name"] for frame in frames if frame["file"] and not frame["name"].startswith("<")})
    identifiers = identifier_terms(diagnostic_text)
    output = {}
    if errors:
        output.update(first_error_phrase=phrase(errors[0].partition(":")[2].strip().split(". ", 1)[0]),
                      last_error_phrase=phrase(errors[-1].partition(":")[2].strip().split(". ", 1)[0]))
    for label, terms in (("rare_error_terms", rare), ("frame_names", names), ("diagnostic_identifiers", identifiers)):
        if terms:
            output[label] = " OR ".join(map(phrase, terms))
    return output


def fuse(rankings):
    scores = defaultdict(float)
    for ranking in rankings:
        for rank, number in enumerate(ranking, 1):
            scores[number] += 1 / (60 + rank)
    return sorted(scores, key=lambda number: (-scores[number], number))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--primary-messages", action="store_true")
    parser.add_argument("--verify-sources", action="store_true")
    args = parser.parse_args()
    data = json.loads(args.cases.read_text())
    outputs = []
    verification = None
    with sqlite3.connect(f"file:{args.index.resolve()}?mode=ro", uri=True) as index:
        metadata = {key: json.loads(value) for key, value in index.execute("SELECT key,value FROM meta")}
        if metadata["scope"] != {key: data["scope"][key] for key in ("repository", "cutoff", "selected_digest")}:
            raise ValueError("The corpus and cases have different evidence boundaries")
        for case in data["cases"]:
            if case["number"] not in metadata["excluded"]:
                raise ValueError("The input thread is not excluded from the text index")
            started = time.monotonic()
            arms = {}
            inputs = queries(index, case["log"]["text"], case["frames"], primary_messages=args.primary_messages)
            for name, query in inputs.items():
                matches = search(index, query, limit=100)
                arms[name] = {"query": query, "results": matches, "ranking": [match["number"] for match in matches]}
            ranking = fuse(arm["ranking"] for arm in arms.values())
            reference_ranks = {
                str(reference["number"]): {
                    **{name: arm["ranking"].index(reference["number"]) + 1
                       if reference["number"] in arm["ranking"] else None for name, arm in arms.items()},
                    "fusion": ranking.index(reference["number"]) + 1 if reference["number"] in ranking else None,
                    "merged": reference.get("merged"),
                } for reference in case["references"]
            }
            result = {"number": case["number"], "arms": arms, "fusion": ranking, "references": reference_ranks,
                      "seconds": time.monotonic() - started}
            outputs.append(result)
            print(json.dumps({key: result[key] for key in ("number", "references", "seconds")}), flush=True)
        if args.verify_sources:
            matches = (row for case in outputs for arm in case["arms"].values() for row in arm["results"])
            with sqlite3.connect(f"file:{Path(data['scope']['github']).resolve()}?mode=ro", uri=True) as db:
                verification = verify_matches(db, index, matches, data["scope"]["cutoff"])
            print(json.dumps({"verification": verification}), flush=True)
    document = {"scope": data["scope"], "corpus": metadata, "primary_messages": args.primary_messages,
                "verification": verification, "cases": outputs}
    args.out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
