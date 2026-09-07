"""Read byte-exact native patch hunks and verify their lines against Git blobs.

The comparison identity comes from change_index; Git owns diff construction.
Hunks are literal evidence units, not symbols, dependency edges or proof of fixes.
Binary files and gitlinks retain metadata without searchable source lines. Limits
and unavailable objects are explicit comparison outcomes, never empty changes.
"""  # noqa: INP001 - Standalone native patch experiment.

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from hashlib import sha256

from .change_git import exact_ids, run

OPTIONS = ("--no-renames", "--no-ext-diff", "--no-textconv", "--no-color", "--no-relative",
           "--ignore-submodules=none", "--submodule=short", "--diff-algorithm=myers", "--no-indent-heuristic",
           "--unified=3", "--inter-hunk-context=0", "--src-prefix=a/", "--dst-prefix=b/", "-O", "/dev/null")


def lines(raw):
    parts = raw.split(b"\n")
    return [part + b"\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


@lru_cache(maxsize=64)
def blob(store, oid):
    result = run(store, "cat-file", "blob", oid)
    result.check_returncode()
    return result.stdout


def raw_files(raw):
    fields = raw.split(b"\0")[:-1]
    output = []
    for header, file in zip(fields[::2], fields[1::2], strict=True):
        old_mode, new_mode, old, new, status = header.split()
        if not old_mode.startswith(b":") or status not in (b"A", b"D", b"M", b"T"):
            raise ValueError("Unexpected native patch file metadata")
        output.append({"file": file, "change": status.decode(), "old_mode": old_mode[1:].decode(),
                       "new_mode": new_mode.decode(), "old": old.decode(), "new": new.decode()})
    return output


def hunks(raw):
    """Decode unified hunk coordinates without interpreting quoted path headers.

    Args:
        raw: Native patch for one literal byte path, potentially with type-change sections.

    Returns:
        Byte lines with added/deleted/context roles and exact side coordinates.
        File headers and function-heading hints are excluded from line evidence.
    """
    output, remaining = [], [0, 0]
    position = [0, 0]
    for line in lines(raw):
        if line.startswith(b"\\ No newline at end of file"):
            if not output or not output[-1]["lines"] or not output[-1]["lines"][-1]["text"].endswith(b"\n"):
                raise ValueError("Native missing-newline marker lacks a preceding line")
            output[-1]["lines"][-1]["text"] = output[-1]["lines"][-1]["text"][:-1]
            continue
        if not any(remaining):
            if not line.startswith(b"@@ "):
                continue
            match = re.match(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if match is None:
                raise ValueError("Malformed native hunk header")
            old, old_count, new, new_count = (int(value) if value is not None else 1 for value in match.groups())
            position, remaining = [old, new], [old_count, new_count]
            output.append({"old_start": old, "old_count": old_count, "new_start": new, "new_count": new_count,
                           "lines": []})
            continue
        kind = {b"-": "deleted", b"+": "added", b" ": "context"}.get(line[:1])
        if kind is None:
            raise ValueError("Native patch lost a hunk line")
        used = [kind != "added", kind != "deleted"]
        if any(take and left == 0 for take, left in zip(used, remaining, strict=True)):
            raise ValueError("Native hunk exceeds its declared line counts")
        output[-1]["lines"].append({"role": kind, "old_line": position[0] if used[0] else None,
                                    "new_line": position[1] if used[1] else None, "text": line[1:]})
        position = [value + take for value, take in zip(position, used, strict=True)]
        remaining = [value - take for value, take in zip(remaining, used, strict=True)]
    if any(remaining):
        raise ValueError("Native patch ended inside a hunk")
    return output


def verify_lines(patch, old, new):
    sources = {"old_line": lines(old), "new_line": lines(new)}
    checked = 0
    for hunk in patch:
        for line in hunk["lines"]:
            for side, source in sources.items():
                number = line[side]
                if number is not None:
                    if not 1 <= number <= len(source) or source[number - 1] != line["text"]:
                        raise ValueError("Native hunk line differs from its Git blob coordinate")
                    checked += 1
    changed = {}
    for side, role in (("old_line", "deleted"), ("new_line", "added")):
        positions = [line[side] for hunk in patch for line in hunk["lines"] if line["role"] == role]
        changed[side] = set(positions)
        if len(positions) != len(changed[side]):
            raise ValueError("Native hunks repeat a changed blob coordinate")
    unchanged = [[line for number, line in enumerate(source, 1) if number not in changed[side]]
                 for side, source in sources.items()]
    if unchanged[0] != unchanged[1]:
        raise ValueError("Native hunks do not account for the complete blob difference")
    return checked


def read_comparison(store, before, after, expected, *, max_files=200, max_blob_bytes=4194304):
    """Reconstruct bounded native textual evidence for one verified comparison.

    Args:
        store: Read-only local Git store; lazy fetching stays disabled.
        before: Exact comparison origin from change_index, including an empty tree.
        after: Exact comparison destination from change_index.
        expected: Complete ordered byte-path/status pairs from the attested index.
        max_files: Comparisons exceeding this file count are not read as patches.
        max_blob_bytes: Maximum size of any compared blob, checked before content reads.
    """
    exact_ids((before, after))
    if len(expected) > max_files:
        return {"status": "file_limit", "files": len(expected), "limit": max_files}
    try:
        # Context-format flags imply patch output and would read blobs before the size gate.
        result = run(store, "diff", "--raw", "--no-abbrev", "-z", "--no-renames", "--no-ext-diff",
                     "--no-textconv", "--no-relative", "--ignore-submodules=none", "-O", "/dev/null",
                     before, after, "--")
        result.check_returncode()
        files = raw_files(result.stdout)
        if [(item["file"], item["change"]) for item in files] != list(expected):
            raise ValueError("Patch files differ from the complete indexed comparison")
        objects = sorted({item[side] for item in files for side in ("old", "new")
                          if item[f"{side}_mode"] not in ("000000", "160000")})
        result = run(store, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                     data="".join(f"{oid}\n" for oid in objects).encode())
        result.check_returncode()
        for oid, line in zip(objects, result.stdout.splitlines(), strict=True):
            fields = line.decode().split()
            if fields[0] != oid:
                raise ValueError("Native blob preflight differs from its requested identity")
            if len(fields) != 3 or fields[1] != "blob":
                return {"status": "blob_unavailable", "object": oid, "response": line.decode()}
            if int(fields[2]) > max_blob_bytes:
                return {"status": "blob_limit", "object": oid, "bytes": int(fields[2]), "limit": max_blob_bytes}
        checked, patch_bytes = 0, 0
        for item in files:
            if "160000" in (item["old_mode"], item["new_mode"]):
                item.update(status="gitlink", hunks=[])
                continue
            result = run(store, "--literal-pathspecs", "diff", "--patch", "--full-index", *OPTIONS,
                         before, after, "--", item["file"])
            result.check_returncode()
            patch_bytes += len(result.stdout)
            parsed = hunks(result.stdout)
            binary = any(line.startswith(b"Binary files ") for line in lines(result.stdout))
            if binary and parsed:
                raise ValueError("A native file patch mixes binary and textual evidence")
            if not binary:
                old, new = [blob(store, item[side]) if item[f"{side}_mode"] != "000000" else b""
                            for side in ("old", "new")]
                checked += verify_lines(parsed, old, new)
            item.update(status="binary" if binary else "text", hunks=parsed,
                        patch_sha256=sha256(result.stdout).hexdigest())
    except subprocess.CalledProcessError as error:
        # Missing native evidence cannot supply a zero-score document.
        return {"status": "native_unavailable", "returncode": error.returncode,
                "error": error.stderr.decode(errors="replace")}
    except subprocess.TimeoutExpired:
        # subprocess.run has terminated this child before this outcome is recorded.
        return {"status": "native_timeout", "timeout_seconds": 120}
    else:
        return {"status": "complete", "files": files, "verified_line_sides": checked, "patch_bytes": patch_bytes}
