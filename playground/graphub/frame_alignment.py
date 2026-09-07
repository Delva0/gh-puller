"""Qualify captured traceback coordinates with literal Git and native-symbol evidence.

Statement spans count characters in the original log. Only framing and surrounding
ASCII whitespace are removed; escapes are not decoded. Matches describe a selected
snapshot, not runtime identity. File-wide evidence and native symbol associations
remain separate, with observed counts and explicit truncation. See case_probe for
frame grammar and captured CBM queries; this module never constructs code symbols.
"""  # noqa: INP001 - Standalone source-alignment experiment.

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import time
from collections import Counter
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from gh_puller.codebase import Archive
from gh_puller.github import git_store_path

from .case_probe import FRAME, frames, locate
from .change_git import exact_ids, run
from .entity_search import verify_inputs
from .patch_evidence import blob, lines
from .patch_probe import byte_value
from .stack_search import symbols


def printed_statement(log, frame):
    """Extract a literal next-line candidate without decoding the source log.

    Args:
        log: Verbatim input containing the frame header.
        frame: Parsed case_probe frame with original offset, path, line and name.

    Returns:
        Original header, prefix and statement spans, or an explicit framing gap.
        Escaped separators require a uniform backslash depth; statement escapes
        remain literal rather than being promoted to decoded source evidence.
    """
    offset = frame["offset"]
    header = FRAME.match(log, offset)
    if header is None or (header[1], int(header[2]), header[3]) != (
        frame["reported_file"], frame["line"], frame["name"],
    ):
        raise ValueError("Frame identity differs from its original header span")
    result = {"header_start": offset, "header_end": header.end()}
    end = header.end()
    while end < len(log) and log[end] in " \t":
        end += 1
    physical_start = log.rfind("\n", 0, offset) + 1
    if log.startswith(("\n", "\r\n"), end):
        separator = "\r\n" if log.startswith("\r\n", end) else "\n"
        prefix_start, start = physical_start, end + len(separator)
        finish = log.find("\n", start)
        finish = len(log) if finish < 0 else finish
        result.update(encoding="physical", separator=separator)
    else:
        escaped = re.match(r"(\\+)(?:n|r(\\+)n)", log[end:])
        if escaped is None:
            return {**result, "status": "no_line_boundary"}
        if escaped[2] is not None and escaped[1] != escaped[2]:
            return {**result, "status": "mixed_escape_depth"}
        separator = escaped[0]
        pattern = re.compile(r"(?<!\\)" + re.escape(separator))
        previous = list(pattern.finditer(log, physical_start, offset))
        prefix_start = previous[-1].end() if previous else physical_start
        start = end + len(separator)
        following = pattern.search(log, start)
        physical_end = log.find("\n", start)
        ends = [value for value in (following.start() if following else -1, physical_end) if value >= 0]
        finish = min(ends, default=len(log))
        result.update(encoding="escaped", separator=separator, backslash_depth=len(escaped[1]))
    prefix = log[prefix_start:offset]
    result.update(prefix_start=prefix_start, prefix_end=offset, raw_start=start, raw_end=finish)
    if not log[start:finish].strip(" \t\r"):
        return {**result, "status": "no_printed_statement"}
    if not log[start:finish].startswith(prefix):
        return {**result, "status": "prefix_mismatch"}
    start += len(prefix)
    while start < finish and log[start] in " \t\r":
        start += 1
    while finish > start and log[finish - 1] in " \t\r":
        finish -= 1
    text = log[start:finish]
    boundary = text.startswith(("Traceback (most recent call last)", "[Previous line repeated"))
    if (not text or FRAME.match(text) or boundary
            or re.match(r"[\w.]*(?:Error|Exception):", text)):
        return {**result, "status": "no_printed_statement"}
    return {**result, "status": "statement", "start": start, "end": finish, "text": text}


def align(source, reported_line, statement, candidates, *, truncated=False, limit=20):
    """Compare independent coordinate, file and native-symbol literal constraints.

    Args:
        source: Exact Git blob bytes at the selected snapshot.
        reported_line: Original one-based frame coordinate, never silently shifted.
        statement: Literal statement extracted by printed_statement.
        candidates: Native same-file, exact-case frame-name nodes in captured order.
        truncated: The native query may omit candidates; observed matches cannot prove uniqueness.
        limit: Returned positions per scope; complete counts are computed before truncation.
    """
    if limit < 1:
        raise ValueError("Alignment position budget must be positive")
    content = lines(source)
    expected = statement.encode()
    positions = [number for number, line in enumerate(content, 1) if line.strip(b" \t\r\n") == expected]
    valid = [i for i, node in enumerate(candidates) if 1 <= node["start"] <= node["end"] <= len(content)]
    associations = [{"candidate": i, "line": number} for number in positions for i in valid
                    if candidates[i]["start"] <= number <= candidates[i]["end"]]
    coordinate_nodes = [i for i in valid if candidates[i]["start"] <= reported_line <= candidates[i]["end"]]
    at_coordinate = content[reported_line - 1] if 1 <= reported_line <= len(content) else None
    equal = at_coordinate.strip(b" \t\r\n") == expected if at_coordinate is not None else None
    if truncated:
        status = "native_candidates_truncated"
    elif equal:
        status = ("exact_symbol_coordinate" if len(coordinate_nodes) == 1 else
                  "ambiguous_symbol_at_coordinate" if coordinate_nodes else "exact_file_coordinate")
    elif associations:
        status = "unique_symbol_statement_candidate" if len(associations) == 1 else "ambiguous_symbol_statement"
    elif positions:
        status = "unique_file_statement_candidate" if len(positions) == 1 else "ambiguous_file_statement"
    else:
        status = "statement_not_found"
    return {"status": status, "reported_line": reported_line, "at_reported_line": at_coordinate,
            "coordinate_equal": equal, "coordinate_candidates": coordinate_nodes,
            "file_matches": {"count": len(positions), "positions": positions[:limit],
                             "truncated": len(positions) > limit},
            "symbol_matches": {"count": len(associations), "associations": associations[:limit],
                               "truncated": len(associations) > limit, "native_complete": not truncated},
            "unusable_native_ranges": [i for i in range(len(candidates)) if i not in valid]}


class Captured:
    """Validate native query requests while replaying their recorded responses."""

    def __init__(self, calls):
        self.calls = iter(calls)

    def call_tool(self, name, arguments):
        """Supply only the next expected captured native response.

        Args:
            name: Native tool name issued by case_probe.locate.
            arguments: Exact request dictionary issued by the frozen frame decoder.
        """
        call = next(self.calls)
        if (name, arguments) != (call["tool"], call["arguments"]):
            raise ValueError("Captured native query differs from frame reconstruction")
        return {"structuredContent": call["response"]}


def read_source(store, entry):
    _, kind, oid = entry
    identity = {"blob": oid.decode()}
    if kind != b"blob":
        return {**identity, "status": "non_blob", "object_type": kind.decode()}
    try:
        size = run(store, "cat-file", "-s", oid.decode())
        size.check_returncode()
        if int(size.stdout) > 4194304:
            return {**identity, "status": "source_limit", "bytes": int(size.stdout)}
        raw = blob(store, oid.decode())
    except subprocess.CalledProcessError as error:
        # Unreadable canonical content is an evidence gap, not a literal mismatch.
        return {**identity, "status": "source_unavailable", "returncode": error.returncode,
                "error": error.stderr.decode(errors="replace")}
    except subprocess.TimeoutExpired:
        # subprocess.run has already terminated this child.
        return {**identity, "status": "source_timeout", "timeout_seconds": 120}
    return {**identity, "status": "complete", "sha256": sha256(raw).hexdigest(), "bytes": len(raw), "content": raw}


def evaluate(store, case):
    """Audit every captured frame against its original log and exact source snapshot.

    Args:
        store: Canonical read-only Git object store, without lazy fetching.
        case: Source-verified case_probe artifact at a verified graph snapshot identity.
    """
    exact_ids((case["commit"],))
    tree = run(store, "ls-tree", "-r", "-z", case["commit"])
    tree.check_returncode()
    entries = {}
    for record in tree.stdout.split(b"\0")[:-1]:
        header, path = record.split(b"\t", 1)
        entries[path.decode(errors="surrogateescape")] = header.split()
    parsed = frames(case["log"]["text"], entries)
    captured = Captured(case["calls"])
    replay = locate(captured, parsed, "graphub-probe")
    if parsed != case["frames"] or replay != case["calls"] or next(captured.calls, None) is not None:
        raise ValueError("Captured frames differ from native-query decoding and source paths")
    native = {call["arguments"]["file_pattern"]: call["response"] for call in case["calls"]}
    sources, output = {}, []
    for i, frame in enumerate(parsed):
        statement = printed_statement(case["log"]["text"], frame)
        result = {"frame": i, "file": frame["file"], "name": frame["name"], "reported_line": frame["line"],
                  "baseline_status": frame.get("status", "external"), "statement": statement}
        output.append(result)
        if frame["file"] is None:
            result["status"] = "external_path"
            continue
        response = native[frame["file"]]
        nodes = [node for node in symbols(response) if node["file"] == frame["file"]
                 and node["qn"].rsplit(".", 1)[-1] == frame["name"]]
        result.update(candidates=nodes, native_truncated=response["has_more"])
        if statement["status"] != "statement":
            result["status"] = "statement_unavailable"
            continue
        file = frame["file"]
        if file not in sources:
            sources[file] = read_source(store, entries[file])
        source = sources[file]
        result["source"] = {key: value for key, value in source.items() if key != "content"}
        if source["status"] != "complete":
            result["status"] = source["status"]
            continue
        result.update(align(source["content"], frame["line"], statement["text"], nodes, truncated=response["has_more"]))
    return {"number": case["number"], "commit": case["commit"], "graph_digest": case["graph_digest"],
            "frames": output, "native_queries_replayed": len(replay), "source_files": len(sources)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", nargs="+", type=Path, required=True)
    for name in ("round", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    registration = json.loads(args.round.read_text())
    datasets = [json.loads(path.read_text()) for path in args.cases]
    scope = registration["scope"]
    if any(data["scope"] != scope for data in datasets):
        raise ValueError("Alignment cases differ from the registered source boundary")
    cases = [case for data in datasets for case in data["cases"]]
    numbers = [case["number"] for case in cases]
    allowed = {number for split in ("development", "held_out", "known_regression")
               for number in registration["sampling"][split]}
    if len(numbers) != len(set(numbers)) or not set(numbers) <= allowed:
        raise ValueError("Alignment cases are duplicate or unregistered")
    for name, digest in registration.get("frozen_policy", {}).get("method_sha256", {}).items():
        if sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() != digest:
            raise ValueError("Alignment code differs from the frozen validation policy")
    archive = Archive(scope["archive"], allow_incomplete=True)
    entries = archive._commits[:scope["graph_count"]]
    identity = [(item["sha"], item["graph_digest"], item["parents"])
                for item in sorted(entries, key=lambda item: item["sha"])]
    if sha256(json.dumps(identity).encode()).hexdigest() != scope["graph_identity_digest"]:
        raise ValueError("Alignment graphs differ from the frozen prefix")
    graphs = {item["sha"]: item["graph_digest"] for item in entries}
    store = git_store_path(scope["github"])
    output = []
    with closing(sqlite3.connect(f"file:{Path(scope['github']).resolve()}?mode=ro", uri=True)) as db:
        if db.execute("SELECT value FROM archive_meta WHERE key='repository'").fetchone() != (scope["repository"],):
            raise ValueError("Alignment source archive has a different owner")
        for data in datasets:
            verify_inputs(db, data)
        for case in cases:
            if graphs.get(case["commit"]) != case["graph_digest"]:
                raise ValueError("Captured native queries have a different graph snapshot")
            result = evaluate(store, case)
            output.append(result)
            print(json.dumps({"case": case["number"], "frames": len(result["frames"]),
                              "statuses": Counter(frame["status"] for frame in result["frames"])}), flush=True)
    result = {"scope": scope, "cases": output,
              "inputs_sha256": {str(path): sha256(path.read_bytes()).hexdigest() for path in args.cases},
              "seconds": time.monotonic() - started}
    args.out.write_text(json.dumps(result, default=byte_value, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
