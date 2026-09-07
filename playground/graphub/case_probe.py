"""Probe real error-log coordinates and linked PR evidence without judging retrieval quality.

Inputs are verbatim error-log spans from frozen observations. CBM resolves code
symbols; Git verifies version and change evidence. Linked PRs are evaluation-only
references and never become retrieval query inputs.
"""  # noqa: INP001 - Standalone, source-grounded research probe.

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from gh_puller.codebase import Archive, resolve_cbm_binary
from gh_puller.codebase.cbm_transport import PersistentMCPTransport
from gh_puller.github import git_store_path

from .gates import pointer
from .stack_search import payload, restore, symbols

FRAME = re.compile(r'File "([^\"]+)", line (\d+), in ([\w.<>]+)')


def observation(db, family, number, cutoff):
    row = db.execute(
        "SELECT o.id,o.coverage,o.payload_digest,p.payload FROM fact_observations o "
        "JOIN payload_blobs p ON p.digest=o.payload_digest WHERE o.family=? AND o.resource_number=? AND o.id<=? "
        "ORDER BY o.observed_until DESC,o.observed_from DESC,o.id DESC LIMIT 1", (family, number, cutoff),
    ).fetchone()
    if row is None:
        return {"coverage": "not_observed"}
    return {"observation": row[0], "coverage": row[1], "digest": row[2], "payload": payload(row[2], row[3])}


def error_log(body):
    """Return a verbatim first-traceback region with exact character offsets.

    Closed fences retain their complete contents. An unfenced traceback starts
    at its physical line and ends at the next Markdown section, fence, details
    closure, or end of input. Its boundary marker exposes this weaker isolation;
    preceding diagnostics are not recovered and intervening prose can remain.
    An unterminated containing fence is rejected rather than guessed closed.

    Args:
        body: Complete issue body containing the traceback to isolate.
    """
    traceback = body.index("Traceback (most recent call last)")
    opening = None
    for match in re.finditer(r"^[ \t]*(`{3,}|~{3,})([^\r\n]*)(?:\r?\n|$)", body, re.MULTILINE):
        if opening is None:
            if match.start() > traceback:
                break
            opening = match
        elif match[1][0] == opening[1][0] and len(match[1]) >= len(opening[1]) and not match[2].strip():
            if opening.end() <= traceback < match.start():
                return {"start": opening.end(), "end": match.start(), "text": body[opening.end():match.start()]}
            opening = None
    if opening is not None:
        raise ValueError("The first traceback is not in a closed fenced log")
    start = body.rfind("\n", 0, traceback) + 1
    boundary = re.search(r"^ {0,3}(?:#{1,6}[ \t]|`{3,}|~{3,}|</details>)", body[traceback:], re.MULTILINE)
    end = traceback + boundary.start() if boundary else len(body)
    return {"start": start, "end": end, "text": body[start:end], "boundary": "unfenced_traceback_section"}


def frames(text, paths):
    output = []
    for match in FRAME.finditer(text):
        filename = match[1].replace("\\", "/")
        matches = [path for path in paths if filename == path or filename.endswith(f"/{path}")]
        qualified = [path for path in matches if "/" in path or filename == path]
        output.append({"reported_file": match[1], "line": int(match[2]), "name": match[3],
                       "file": max(qualified, key=len) if qualified else None,
                       "path_candidates": sorted(matches), "offset": match.start()})
    return output


def git(store, *args, check=True):
    return subprocess.run(
        ["git", f"--git-dir={store}", *args], check=check, text=True, capture_output=True,
        timeout=120, env=os.environ | {"GIT_NO_LAZY_FETCH": "1"},
    )


def ancestry(store, older, newer):
    result = git(store, "merge-base", "--is-ancestor", older, newer, check=False)
    return {0: True, 1: False}.get(result.returncode)


def references(db, store, case, commit, cutoff):
    repository = db.execute("SELECT value FROM archive_meta WHERE key='repository'").fetchone()[0]
    records = []
    for link in case["closing_pulls"]:
        row = db.execute(
            "SELECT o.family,o.resource_number,o.coverage,o.payload_digest,p.payload FROM fact_observations o "
            "JOIN payload_blobs p ON p.digest=o.payload_digest WHERE o.id=? AND o.id<=?",
            (link["observation"], cutoff),
        ).fetchone()
        if row is None or row[:4] != ("pull-closing-issues", link["pull"], "complete", link["digest"]):
            raise ValueError("Closing evidence does not match its source identity")
        record_payload = payload(link["digest"], row[4])
        relation = pointer(record_payload, link["pointer"])
        # Imported envelopes need not repeat the archive's repository binding.
        if (relation["number"] != case["number"]
                or relation["repository"]["nameWithOwner"].casefold() != repository.casefold()
                or record_payload.get("repository", repository).casefold() != repository.casefold()):
            raise ValueError("Closing evidence does not point to the selected issue")
        detail = observation(db, "pull", link["pull"], cutoff)
        record = {"number": link["pull"], "closing_evidence": link,
                  "pull_observation": {key: value for key, value in detail.items() if key != "payload"}}
        if detail["coverage"] == "complete":
            value = detail["payload"]["value"]
            record.update(merged=value.get("merged"), merged_at=value.get("merged_at"),
                          merge_commit_sha=value.get("merge_commit_sha"), head_sha=value["head"]["sha"])
            if value.get("merged") is True and (landing := value.get("merge_commit_sha")):
                record["in_reported_version"] = ancestry(store, landing, commit)
                record["descends_from_reported_version"] = ancestry(store, commit, landing)
                changed = git(store, "diff-tree", "--no-commit-id", "--name-only", "-r", f"{landing}^1", landing,
                              check=False)
                record["changed_files"] = changed.stdout.splitlines() if changed.returncode == 0 else None
        records.append(record)
    return records


def locate(transport, log_frames, project):
    groups = defaultdict(list)
    for frame in log_frames:
        if frame["file"] is not None:
            groups[frame["file"]].append(frame)
    calls = []
    for file, grouped in sorted(groups.items()):
        names = sorted({frame["name"] for frame in grouped})
        args = {"project": project, "file_pattern": file, "name_pattern": "^(" + "|".join(map(re.escape, names)) + ")$",
                "format": "json", "limit": 5000}
        response = transport.call_tool("search_graph", args)["structuredContent"]
        calls.append({"tool": "search_graph", "arguments": args, "response": response})
        nodes = symbols(response)
        for frame in grouped:
            matches = [node for node in nodes if node["file"] == file and node["qn"].rsplit(".", 1)[-1] == frame["name"]
                       and node["start"] <= frame["line"] <= node["end"]]
            frame["symbols"] = matches
            frame["status"] = ("query_truncated" if response["has_more"] else
                               "resolved" if len(matches) == 1 else "unresolved" if not matches else "ambiguous")
    return calls


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--split", choices=("development", "held_out"), default="development")
    args = parser.parse_args()
    run = json.loads(args.round.read_text())
    audit = json.loads(args.audit.read_text())
    scope = run["scope"]
    if (scope["selected_digest"] != audit["selected_digest"]
            or scope["graph_identity_digest"] != audit["graph"]["identity_digest"]):
        raise ValueError("Case catalog differs from the registered experiment")
    cases = {case["number"]: case for case in audit["candidates"]}
    archive = Archive(scope["archive"], allow_incomplete=True)
    entries = archive._commits[:scope["graph_count"]]
    identity = [(item["sha"], item["graph_digest"], item["parents"])
                for item in sorted(entries, key=lambda item: item["sha"])]
    if sha256(json.dumps(identity).encode()).hexdigest() != scope["graph_identity_digest"]:
        raise ValueError("Code graph input differs from the registered experiment")
    binary = resolve_cbm_binary()
    if binary.sha256 != scope["cbm_sha256"]:
        raise ValueError("CBM executable differs from the registered experiment")
    store = git_store_path(scope["github"])
    grouped = defaultdict(list)
    for number in run["sampling"][args.split]:
        case = cases[number]
        ref, tag = next(iter(case["tags"].items()))
        grouped[(ref, tag["commit"])].append(case)
    records = []
    with sqlite3.connect(f"file:{Path(scope['github']).resolve()}?mode=ro", uri=True) as db:
        if db.execute("SELECT value FROM archive_meta WHERE key='repository'").fetchone() != (scope["repository"],):
            raise ValueError("Case source repository differs from the registered archive")
        for (ref, commit), selected in grouped.items():
            paths = set(git(store, "ls-tree", "-r", "--name-only", commit).stdout.splitlines())
            with tempfile.TemporaryDirectory(prefix="graphub-probe-") as scratch:
                cache = Path(scratch)
                project = "graphub-probe"
                projection = restore(archive.load_rows(commit), cache, project)
                monitor = SimpleNamespace(child_pid=None, exceeded=False, sample=lambda: None)
                transport = PersistentMCPTransport(
                    binary.path, cache, 60, monitor, extra_environment={"CBM_RUNTIME_DIR": str(cache)},
                )
                try:
                    for case in selected:
                        root = observation(db, "issue", case["number"], scope["cutoff"])
                        if (root["coverage"] != "complete" or root["digest"] != case["digest"]
                                or root["observation"] != case["observation"]):
                            raise ValueError("Case input differs from the catalog")
                        log = error_log(root["payload"]["value"]["body"])
                        log_frames = frames(log["text"], paths)
                        calls = locate(transport, log_frames, project)
                        record = {
                            "number": case["number"], "ref": ref, "commit": commit,
                            "graph_digest": archive._entries[commit]["graph_digest"],
                            "source": {"observation": root["observation"], "digest": root["digest"],
                                       "pointer": "/value/body"},
                            "log": {**log, "digest": sha256(log["text"].encode()).hexdigest()},
                            "frames": log_frames, "calls": calls, "projection": projection,
                            "references": references(db, store, case, commit, scope["cutoff"]),
                        }
                        records.append(record)
                        print(json.dumps({"case": case["number"], "frames": len(log_frames),
                                          "resolved": sum(frame.get("status") == "resolved" for frame in log_frames),
                                          "references": [{"number": ref["number"], "merged": ref.get("merged")}
                                                         for ref in record["references"]]}), flush=True)
                finally:
                    transport.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"scope": scope, "cases": records}, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
