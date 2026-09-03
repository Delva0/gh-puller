"""Parse unified diffs and construct bounded graph-impact queries.

The supplied diff is data only: this module never applies it to a snapshot or
invokes Git. Old-side hunk coordinates select definitions in the prebuilt base
version before CALLS traversal.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath

_HUNK = re.compile(r"^@@ -(?P<start>\d+)(?:,(?P<count>\d+))? \+\d+(?:,\d+)? @@")
_CONTAINER_LABELS = ("File", "Folder", "Project", "Module", "Package", "Section")


@dataclass(frozen=True, slots=True, order=True)
class LineRange:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    ranges: tuple[LineRange, ...]


@dataclass(slots=True)
class _Section:
    old_path: str | None = None
    new_path: str | None = None
    ranges: list[LineRange] = field(default_factory=list)


def parse_unified_diff(diff: str) -> tuple[ChangedFile, ...]:
    """Extract repository-relative base paths and old-side hunk ranges.

    Args:
        diff: Unified Git diff text supplied by vllm-kb.
    """
    sections: list[_Section] = []
    current: _Section | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                sections.append(current)
            current = _section_from_header(line)
            continue
        if line.startswith("--- "):
            if current is None:
                current = _Section()
            current.old_path = _marker_path(line[4:])
            continue
        if line.startswith("+++ "):
            if current is None:
                current = _Section()
            current.new_path = _marker_path(line[4:])
            continue
        match = _HUNK.match(line)
        if match is not None and current is not None:
            start = max(1, int(match.group("start")))
            count = int(match.group("count") or "1")
            current.ranges.append(LineRange(start=start, end=start + max(1, count) - 1))
    if current is not None:
        sections.append(current)

    by_path: dict[str, list[LineRange]] = {}
    for section in sections:
        path = section.old_path or section.new_path
        if path is None:
            continue
        by_path.setdefault(path, []).extend(section.ranges)
    return tuple(ChangedFile(path=path, ranges=_merge_ranges(ranges)) for path, ranges in sorted(by_path.items()))


def build_impact_query(
    changes: tuple[ChangedFile, ...],
    *,
    direction: str,
    hop: int,
) -> str:
    """Build one fixed-hop query over all changed-file seed definitions.

    Args:
        changes: Parsed changed files included in this bounded traversal.
        direction: ``inbound``, ``outbound``, or ``both``.
        hop: Exact CALLS distance represented by this query.
    """
    seed_filter = " OR ".join(_file_predicate(change) for change in changes)
    branches = []
    if direction in {"inbound", "both"}:
        branches.append(_impact_branch(seed_filter, hop, inbound=True))
    if direction in {"outbound", "both"}:
        branches.append(_impact_branch(seed_filter, hop, inbound=False))
    return " UNION ".join(branches)


def build_seed_query(changes: tuple[ChangedFile, ...]) -> str:
    """Build the symbol-selection query shared by all impact hops.

    Args:
        changes: Parsed changed files included in the traversal.
    """
    return (
        f"MATCH (seed) WHERE {_seed_filter(changes)} "
        "RETURN DISTINCT seed.qualified_name AS seed_qn, seed.file_path AS file"
    )


def _section_from_header(line: str) -> _Section:
    try:
        fields = shlex.split(line)
    except ValueError:
        fields = line.split()
    if len(fields) < 4:
        return _Section()
    return _Section(old_path=_git_path(fields[2], "a/"), new_path=_git_path(fields[3], "b/"))


def _marker_path(value: str) -> str | None:
    try:
        marker = shlex.split(value)[0]
    except (IndexError, ValueError):
        marker = value.split("\t", maxsplit=1)[0].strip()
    if marker == "/dev/null":
        return None
    prefix = "a/" if marker.startswith("a/") else "b/"
    return _git_path(marker, prefix)


def _git_path(value: str, prefix: str) -> str | None:
    if value == "/dev/null":
        return None
    path = value.removeprefix(prefix)
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts or not path:
        return None
    return candidate.as_posix()


def _merge_ranges(ranges: list[LineRange]) -> tuple[LineRange, ...]:
    merged: list[LineRange] = []
    for item in sorted(ranges):
        if merged and item.start <= merged[-1].end + 1:
            merged[-1] = LineRange(merged[-1].start, max(merged[-1].end, item.end))
        else:
            merged.append(item)
    return tuple(merged)


def _file_predicate(change: ChangedFile) -> str:
    path = json.dumps(change.path, ensure_ascii=False)
    file_match = f"seed.file_path = {path}"
    if not change.ranges:
        return f"({file_match})"
    ranges = " OR ".join(f"(seed.start_line <= {item.end} AND seed.end_line >= {item.start})" for item in change.ranges)
    return f"({file_match} AND ({ranges}))"


def _impact_branch(seed_filter: str, hop: int, *, inbound: bool) -> str:
    relationship = f"(impact)-[:CALLS*{hop}..{hop}]->(seed)" if inbound else f"(seed)-[:CALLS*{hop}..{hop}]->(impact)"
    return (
        f"MATCH (seed) WHERE {_guard_seed_filter(seed_filter)} MATCH {relationship} "
        "RETURN DISTINCT seed.qualified_name AS seed_qn, "
        "impact.qualified_name AS qn, labels(impact) AS labels, "
        "impact.file_path AS file"
    )


def _seed_filter(changes: tuple[ChangedFile, ...]) -> str:
    return _guard_seed_filter(" OR ".join(_file_predicate(change) for change in changes))


def _guard_seed_filter(seed_filter: str) -> str:
    containers = " AND ".join(f"NOT seed:{label}" for label in _CONTAINER_LABELS)
    return f"({seed_filter}) AND {containers}"
