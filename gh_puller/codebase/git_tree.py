"""Maintain a Git tree by applying only changed paths between commits."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path, PurePosixPath


class TreeError(Exception):
    pass


def _safe_path(tree: Path, raw: bytes) -> Path:
    try:
        text = raw.decode("utf-8", errors="surrogateescape")
    except UnicodeError as exc:
        raise TreeError("invalid Git path") from exc
    relative = PurePosixPath(text)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise TreeError(f"unsafe Git path: {text!r}")
    return tree.joinpath(*relative.parts)


def _extract(repo: Path, sha: str, tree: Path, paths: list[str] | None = None) -> None:
    command = ["git", "-C", str(repo), "archive", "--format=tar", sha]
    if paths:
        command.extend(["--", *paths])
    producer = subprocess.Popen(command, stdout=subprocess.PIPE)
    stream = producer.stdout
    if stream is None:
        producer.terminate()
        raise TreeError("Git archive stdout pipe could not be created")
    consumer = subprocess.Popen(["tar", "-xf", "-", "-C", str(tree), "--no-same-owner"], stdin=stream)
    stream.close()
    consumer_status = consumer.wait(timeout=600)
    producer_status = producer.wait(timeout=600)
    if producer_status or consumer_status:
        raise TreeError(f"Git tree extraction failed at {sha}: git={producer_status}, tar={consumer_status}")


def materialize_full(repo: Path, sha: str, tree: Path) -> None:
    if tree.exists():
        shutil.rmtree(tree)
    tree.mkdir(parents=True)
    _extract(repo, sha, tree)


def changed_paths(repo: Path, old_sha: str, new_sha: str) -> list[tuple[str, bytes]]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "diff-tree",
            "--no-commit-id",
            "--name-status",
            "-r",
            "-z",
            "--no-renames",
            old_sha,
            new_sha,
        ],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise TreeError(result.stderr.decode(errors="replace"))
    fields = result.stdout.split(b"\0")
    if fields and not fields[-1]:
        fields.pop()
    if len(fields) % 2:
        raise TreeError("invalid git diff-tree output")
    return [(fields[index].decode("ascii"), fields[index + 1]) for index in range(0, len(fields), 2)]


def materialize_incremental(repo: Path, old_sha: str, new_sha: str, tree: Path) -> int:
    changes = changed_paths(repo, old_sha, new_sha)
    extract = []
    for status, raw_path in changes:
        path = _safe_path(tree, raw_path)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        if status != "D":
            extract.append(raw_path.decode("utf-8", errors="surrogateescape"))
    for start in range(0, len(extract), 256):
        _extract(repo, new_sha, tree, extract[start : start + 256])
    return len(changes)
