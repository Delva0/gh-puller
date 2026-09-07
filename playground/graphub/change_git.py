"""Reconstruct observed change boundaries and byte-exact file differences with Git.

All commands are local and read-only. Git owns ancestry, merge-base selection and
tree comparison; no code graph is constructed here. Proposal comparisons and
first-parent landings are different evidence relations. Unreadable objects and
non-unique merge bases remain explicit reconstruction outcomes.
"""  # noqa: INP001 - Standalone native-evidence experiment.

from __future__ import annotations

import os
import re
import subprocess
from functools import cache


def run(store, *args, data=b""):
    return subprocess.run(
        ["git", f"--git-dir={store}", *args], input=data, capture_output=True, check=False, timeout=120,
        env=os.environ | {"GIT_NO_LAZY_FETCH": "1", "LC_ALL": "C"},
    )


def exact_ids(objects):
    ordered = sorted(set(objects))
    if any(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha) is None for sha in ordered):
        raise ValueError("Change endpoints must be exact Git object IDs")
    return ordered


def object_types(store, objects):
    """Check exact object IDs without resolving mutable refs or fetching.

    Args:
        store: Canonical local Git object store.
        objects: Full hexadecimal object IDs from source fields or native Git.
    """
    ordered = exact_ids(objects)
    result = run(store, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                 data="".join(f"{sha}\n" for sha in ordered).encode())
    result.check_returncode()
    output = {}
    for sha, line in zip(ordered, result.stdout.decode().splitlines(), strict=True):
        oid, kind = line.split()
        if oid != sha:
            raise ValueError("Git object response differs from its requested identity")
        output[sha] = kind
    return output


@cache
def empty_tree(store):
    result = run(store, "hash-object", "-t", "tree", "--stdin")
    result.check_returncode()
    return result.stdout.decode().strip()


def boundary(store, kind, inputs, types):
    """Resolve one source-backed comparison without silently choosing a merge base.

    Args:
        store: Canonical local Git object store.
        kind: Proposal or landing, as defined in the module contract.
        inputs: Proposal base/head IDs, or a singleton observed landing ID.
        types: Native object_types results covering every input, shared across a build.

    Returns:
        Native boundary evidence. Only a ready boundary may be compared; its tree
        closure still has to pass an actual diff. Object readability is measured
        now and does not prove availability at the source's observation time.
    """
    selected = {sha: types[sha] for sha in inputs}
    if any(value != "commit" for value in selected.values()):
        return {"status": "endpoint_unavailable", "object_types": selected}
    if kind == "proposal":
        base, head = inputs
        result = run(store, "merge-base", "--all", base, head)
        if result.returncode not in (0, 1):
            return {"status": "ancestry_unavailable", "error": result.stderr.decode(errors="replace")}
        bases = result.stdout.decode().splitlines()
        if len(bases) > 1:
            return {"status": "ambiguous_merge_base", "merge_bases": sorted(bases)}
        return {"status": "ready", "before": bases[0] if bases else empty_tree(store), "after": head,
                "comparison_kind": "merge_base" if bases else "empty_tree"}
    if kind != "landing":
        raise ValueError("Unknown change relation")
    result = run(store, "cat-file", "commit", inputs[0])
    result.check_returncode()
    header = result.stdout.split(b"\n\n", 1)[0]
    parents = [line.split()[1].decode() for line in header.splitlines() if line.startswith(b"parent ")]
    return {"status": "ready", "before": parents[0] if parents else empty_tree(store), "after": inputs[0],
            "comparison_kind": "first_parent" if parents else "empty_tree"}


def parse_batch(raw, pairs):
    """Decode Git's NUL-delimited path stream without interpreting path bytes.

    Args:
        raw: Successful diff-tree output with the exact markers emitted by compare.
        pairs: Ordered native tree IDs, including comparisons with no changes.
    """
    output, offset = [], 0
    for i, (before, after) in enumerate(pairs):
        marker = f"graphub-change:{i}\n{before} {after}\n".encode()
        if raw[offset:offset + len(marker)] != marker:
            raise ValueError("Native diff batch lost its input boundary")
        offset += len(marker)
        next_marker = f"graphub-change:{i + 1}\n".encode()
        files = []
        while raw[offset:offset + len(next_marker)] != next_marker:
            end = raw.index(b"\0", offset)
            status = raw[offset:end]
            if status not in (b"A", b"D", b"M", b"T", b"U", b"X", b"B"):
                raise ValueError("Unexpected native file-change status")
            offset = end + 1
            end = raw.index(b"\0", offset)
            files.append((raw[offset:end], status.decode()))
            offset = end + 1
        output.append(files)
    if raw[offset:] != f"graphub-change:{len(pairs)}\n".encode():
        raise ValueError("Native diff batch has trailing or missing records")
    return output


def compare_trees(store, pairs):
    data = "".join(f"graphub-change:{i}\n{before} {after}\n" for i, (before, after) in enumerate(pairs))
    data += f"graphub-change:{len(pairs)}\n"
    try:
        result = run(store, "diff-tree", "--stdin", "-r", "--name-status", "--no-commit-id", "--no-renames",
                     "--no-ext-diff", "--no-textconv", "--ignore-submodules=none", "-z", data=data.encode())
        if result.returncode == 0 and not result.stderr:
            return [{"status": "complete", "files": files} for files in parse_batch(result.stdout, pairs)]
        error = {"returncode": result.returncode, "stderr": result.stderr.decode(errors="replace")}
    except subprocess.TimeoutExpired:
        # A completed timeout has killed this child; subdivision isolates costly or unreadable pairs.
        error = {"timeout_seconds": 120}
    if len(pairs) > 1:
        middle = len(pairs) // 2
        return compare_trees(store, pairs[:middle]) + compare_trees(store, pairs[middle:])
    return [{"status": "diff_unavailable", "error": error}]


def compare(store, pairs):
    """Compare a bounded batch while isolating failures to their exact tree pairs.

    Args:
        store: Canonical local Git object store.
        pairs: Ordered before/after object IDs; callers bound batch size.

    Returns:
        One reconstruction outcome per pair. Complete outcomes contain byte-path
        and status tuples. Renames are represented as deletion/addition, including
        mode-only changes and submodule pointer changes without entering submodules.
    """
    objects = exact_ids(sha for pair in pairs for sha in pair)
    result = run(store, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                 data="".join(f"{sha}^{{tree}}\n" for sha in objects).encode())
    result.check_returncode()
    trees = {}
    for sha, line in zip(objects, result.stdout.decode().splitlines(), strict=True):
        oid, kind = line.split()
        trees[sha] = oid if kind == "tree" else None
    output, ready = [], []
    for i, pair in enumerate(pairs):
        missing = [sha for sha in pair if trees[sha] is None]
        output.append({"status": "diff_unavailable", "unreadable_trees": missing})
        if not missing:
            ready.append(i)
    # Commit-pair stdin means a synthetic parent override; explicit trees preserve A-to-B direction.
    compared = compare_trees(store, [(trees[pairs[i][0]], trees[pairs[i][1]]) for i in ready]) if ready else []
    for i, value in zip(ready, compared, strict=True):
        output[i] = value
    return output
