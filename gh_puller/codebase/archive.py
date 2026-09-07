"""Append-only archive with incrementally updated Merkle radix trees."""

from __future__ import annotations

import json
import os
import struct
import zlib
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from .graph import SnapshotGraph, snapshot_to_networkx
from .store import GraphRows, rows_to_snapshot

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    import networkx as nx

# These bytes and the numeric index value are an on-disk compatibility
# contract. They remain unchanged so existing large archives resume in place;
# they are not the Python package or product version.
FORMAT_VERSION = 5
MAGIC = b"KGA5\r\n\x1a\n"
FOOTER_MAGIC = b"KGA5IDX!"
CHECKPOINT_MAGIC = b"KGA5CP!!"
FRAME = struct.Struct(">BQQI32s")
FOOTER = struct.Struct(">Q8s")
CHECKPOINT = struct.Struct(">QQ8s")

PAGE = 1
COMMIT = 2
FINAL_INDEX = 3
# Nodes and outgoing edges are grouped by their normalized module prefix. Code
# changes are module-local, unlike hash sharding which scatters one file's rows
# across nearly every leaf and destroys archive compression.
TRIE_DEPTH = 1


class ArchiveError(Exception):
    pass


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _key_json(key):
    return list(key) if isinstance(key, tuple) else key


def _key_value(value):
    return tuple(value) if isinstance(value, list) else value


def _key_bytes(key) -> bytes:
    return _json_bytes(_key_json(key))


def _key_path(key) -> tuple[str]:
    identity = key[0] if isinstance(key, tuple) else key
    components = str(identity).split(".")
    return (".".join(components[:3]),)


@dataclass(frozen=True)
class FrameInfo:
    offset: int
    kind: int
    raw_len: int
    compressed_len: int
    digest: bytes
    end: int


@dataclass(frozen=True)
class TreeRef:
    offset: int
    logical_hash: str
    count: int

    def to_json(self) -> dict:
        return {"offset": self.offset, "logical_hash": self.logical_hash, "count": self.count}

    @classmethod
    def from_json(cls, value: dict | None) -> TreeRef | None:
        return None if value is None else cls(value["offset"], value["logical_hash"], value["count"])


def _has_footer(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < len(MAGIC) + FOOTER.size:
        return False
    with path.open("rb") as file:
        file.seek(-FOOTER.size, os.SEEK_END)
        _, magic = FOOTER.unpack(file.read(FOOTER.size))
    return magic == FOOTER_MAGIC


def _read_frame_at(path: Path, offset: int, expected_kind: int | None = None) -> tuple[FrameInfo, bytes]:
    with path.open("rb") as file:
        file.seek(offset)
        header = file.read(FRAME.size)
        if len(header) != FRAME.size:
            raise ArchiveError(f"truncated frame header at {offset}")
        kind, raw_len, compressed_len, crc, digest = FRAME.unpack(header)
        if expected_kind is not None and kind != expected_kind:
            raise ArchiveError(f"frame at {offset} has kind {kind}, expected {expected_kind}")
        compressed = file.read(compressed_len)
    if len(compressed) != compressed_len or zlib.crc32(compressed) != crc:
        raise ArchiveError(f"frame at {offset} is truncated or corrupt")
    try:
        raw = zlib.decompress(compressed)
    except zlib.error as exc:
        raise ArchiveError(f"frame at {offset} cannot be decompressed") from exc
    if len(raw) != raw_len or sha256(raw).digest() != digest:
        raise ArchiveError(f"frame at {offset} failed content verification")
    info = FrameInfo(offset, kind, raw_len, compressed_len, digest, offset + FRAME.size + compressed_len)
    return info, raw


def _scan_frames(path: Path, start: int, limit: int, *, verify: bool) -> tuple[list[FrameInfo], int]:
    frames = []
    with path.open("rb") as file:
        offset = start
        while offset < limit:
            file.seek(offset)
            prefix = file.read(min(FRAME.size, limit - offset))
            # Checkpoint trailers are deliberately retained between frames.  A
            # checkpoint is tiny, belongs to the final archive, and lets an
            # interrupted writer recover from the tail instead of rescanning
            # every historical page.  A footer can likewise be embedded when a
            # previously complete archive is extended.
            if len(prefix) >= CHECKPOINT.size and prefix[16:24] == CHECKPOINT_MAGIC:
                offset += CHECKPOINT.size
                continue
            if len(prefix) >= FOOTER.size and prefix[8:16] == FOOTER_MAGIC:
                offset += FOOTER.size
                continue
            if len(prefix) < FRAME.size:
                break
            header = prefix
            kind, raw_len, compressed_len, crc, digest = FRAME.unpack(header)
            end = offset + FRAME.size + compressed_len
            if compressed_len <= 0 or end > limit:
                break
            info = FrameInfo(offset, kind, raw_len, compressed_len, digest, end)
            if verify:
                compressed = file.read(compressed_len)
                try:
                    raw = zlib.decompress(compressed)
                except zlib.error:
                    break
                if zlib.crc32(compressed) != crc or len(raw) != raw_len or sha256(raw).digest() != digest:
                    break
            frames.append(info)
            offset = end
    return frames, offset


def _scan(path: Path, *, verify: bool) -> tuple[list[FrameInfo], int]:
    size = path.stat().st_size
    limit = size - FOOTER.size if _has_footer(path) else size
    with path.open("rb") as file:
        if file.read(len(MAGIC)) != MAGIC:
            raise ArchiveError(f"{path} is not a supported archive")
    return _scan_frames(path, len(MAGIC), limit, verify=verify)


def _read_final_index(path: Path, footer_offset: int | None = None) -> tuple[FrameInfo, dict]:
    if footer_offset is None:
        footer_offset = path.stat().st_size - FOOTER.size
    with path.open("rb") as file:
        file.seek(footer_offset)
        payload = file.read(FOOTER.size)
    if len(payload) != FOOTER.size:
        raise ArchiveError(f"truncated footer at {footer_offset}")
    index_offset, magic = FOOTER.unpack(payload)
    if magic != FOOTER_MAGIC:
        raise ArchiveError(f"invalid footer at {footer_offset}")
    frame, raw = _read_frame_at(path, index_offset, FINAL_INDEX)
    index = json.loads(raw)
    if index.get("version") != FORMAT_VERSION or not isinstance(index.get("commits"), list):
        raise ArchiveError("invalid archive index")
    return frame, index


def _read_checkpoint(path: Path, offset: int) -> tuple[int, int, dict]:
    with path.open("rb") as file:
        file.seek(offset)
        payload = file.read(CHECKPOINT.size)
    if len(payload) != CHECKPOINT.size:
        raise ArchiveError(f"truncated checkpoint at {offset}")
    commit_offset, previous_offset, magic = CHECKPOINT.unpack(payload)
    if magic != CHECKPOINT_MAGIC:
        raise ArchiveError(f"invalid checkpoint at {offset}")
    frame, raw = _read_frame_at(path, commit_offset, COMMIT)
    if frame.end != offset:
        raise ArchiveError(f"checkpoint at {offset} is not adjacent to its commit")
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("sha"), str):
        raise ArchiveError(f"invalid commit manifest at {commit_offset}")
    return previous_offset, offset + CHECKPOINT.size, manifest


def _find_last_checkpoint(path: Path, *, chunk_size: int = 1 << 20) -> int | None:
    """Find the newest valid checkpoint, normally by reading only the tail.

    A crash may leave partial page frames after the last committed checkpoint,
    so the trailer is not required to be exactly at EOF.  False magic matches
    inside compressed payloads are rejected by checking the referenced commit.
    """
    size = path.stat().st_size
    overlap = len(CHECKPOINT_MAGIC) - 1
    end = size
    with path.open("rb") as file:
        while end > len(MAGIC):
            start = max(len(MAGIC), end - chunk_size)
            file.seek(start)
            block = file.read(end - start)
            position = len(block)
            while True:
                found = block.rfind(CHECKPOINT_MAGIC, 0, position)
                if found < 0:
                    break
                checkpoint_offset = start + found - (CHECKPOINT.size - len(CHECKPOINT_MAGIC))
                if checkpoint_offset >= len(MAGIC):
                    try:
                        _read_checkpoint(path, checkpoint_offset)
                    except (ArchiveError, json.JSONDecodeError, OSError):
                        pass
                    else:
                        return checkpoint_offset
                position = found
            if start == len(MAGIC):
                break
            end = start + overlap
    return None


def _find_last_footer(path: Path, *, chunk_size: int = 1 << 20) -> int | None:
    """Find a valid embedded final-index footer before an interrupted tail."""
    size = path.stat().st_size
    overlap = len(FOOTER_MAGIC) - 1
    end = size
    with path.open("rb") as file:
        while end > len(MAGIC):
            start = max(len(MAGIC), end - chunk_size)
            file.seek(start)
            block = file.read(end - start)
            position = len(block)
            while True:
                found = block.rfind(FOOTER_MAGIC, 0, position)
                if found < 0:
                    break
                footer_offset = start + found - (FOOTER.size - len(FOOTER_MAGIC))
                if footer_offset >= len(MAGIC):
                    try:
                        frame, _ = _read_final_index(path, footer_offset)
                    except (ArchiveError, json.JSONDecodeError, OSError):
                        pass
                    else:
                        if frame.end == footer_offset:
                            return footer_offset
                position = found
            if start == len(MAGIC):
                break
            end = start + overlap
    return None


def _load_checkpoint_chain(path: Path) -> tuple[list[dict], int, int] | None:
    """Return manifests, durable end, and latest checkpoint trailer offset."""
    latest = _find_last_checkpoint(path)
    if latest is None:
        return None
    manifests = []
    offset = latest
    durable = 0
    seen = set()
    while offset:
        if offset in seen:
            raise ArchiveError("checkpoint cycle detected")
        seen.add(offset)
        with path.open("rb") as file:
            file.seek(offset + 8)
            marker = file.read(8)
        if marker == FOOTER_MAGIC:
            _, index = _read_final_index(path, offset)
            manifests = list(index["commits"]) + list(reversed(manifests))
            break
        previous, checkpoint_end, manifest = _read_checkpoint(path, offset)
        if not durable:
            durable = checkpoint_end
        manifests.append(manifest)
        offset = previous
    else:
        manifests.reverse()
    return manifests, durable, latest


class PageStore:
    def __init__(self, path: Path, *, cache_bytes: int = 64 << 20):
        self.path = path
        self.cache_bytes = cache_bytes
        self._cache: OrderedDict[int, tuple[dict, int]] = OrderedDict()
        self._cache_size = 0

    def _remember(self, offset: int, page: dict, raw_size: int) -> None:
        if raw_size > self.cache_bytes:
            return
        while self._cache and self._cache_size + raw_size > self.cache_bytes:
            _, (_, evicted_size) = self._cache.popitem(last=False)
            self._cache_size -= evicted_size
        self._cache[offset] = (page, raw_size)
        self._cache_size += raw_size

    def read_page(self, ref: TreeRef) -> dict:
        cached = self._cache.get(ref.offset)
        if cached is None:
            _, raw = _read_frame_at(self.path, ref.offset, PAGE)
            page = json.loads(raw)
            if page.get("logical_hash") != ref.logical_hash or page.get("count") != ref.count:
                raise ArchiveError(f"page reference mismatch at {ref.offset}")
            self._remember(ref.offset, page, len(raw))
        else:
            page, _ = cached
            self._cache.move_to_end(ref.offset)
        return page


class ArchiveWriter(PageStore):
    """Transactional writer with constant-time complete-archive resume."""

    def __init__(
        self,
        path: str | Path,
        *,
        compression_level: int = 1,
        resume: bool = True,
        extend_complete: bool = False,
        cache_bytes: int = 64 << 20,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.compression_level = compression_level
        self.frames: list[FrameInfo] = []
        self.commits: list[dict] = []
        self._previous_checkpoint_offset = 0
        needs_anchor = False
        complete = _has_footer(self.path)
        if self.path.exists() and resume:
            if complete and not extend_complete:
                raise ArchiveError(f"archive is already complete: {self.path}")
            if complete:
                # Keep the prior final index and footer as a durable anchor.
                # New checkpoints link back to it, so even a crash during the
                # first extension commit never turns a fast-resumable archive
                # into one that needs a 15 GB historical scan.
                _, index = _read_final_index(self.path)
                self.commits = list(index["commits"])
                durable = self.path.stat().st_size
                self._previous_checkpoint_offset = durable - FOOTER.size
            elif checkpoint := _load_checkpoint_chain(self.path):
                self.commits, durable, self._previous_checkpoint_offset = checkpoint
                with self.path.open("r+b") as file:
                    file.truncate(durable)
            elif footer_offset := _find_last_footer(self.path):
                _, index = _read_final_index(self.path, footer_offset)
                self.commits = list(index["commits"])
                durable = footer_offset + FOOTER.size
                self._previous_checkpoint_offset = footer_offset
                with self.path.open("r+b") as file:
                    file.truncate(durable)
            else:
                # Compatibility path for archives created before checkpoint
                # trailers existed.  It is paid at most once: the next commit
                # written by this version establishes a fast-resume chain.
                self.frames, _ = _scan(self.path, verify=True)
                self._load_commits()
                durable = self.commits[-1]["_frame_end"] if self.commits else len(MAGIC)
                with self.path.open("r+b") as file:
                    file.truncate(durable)
                self.frames = [frame for frame in self.frames if frame.end <= durable]
                needs_anchor = bool(self.commits)
            self._append_start = durable
            self._file = self.path.open("ab")
            if needs_anchor:
                index = {
                    "version": FORMAT_VERSION,
                    "commits": [{k: v for k, v in item.items() if not k.startswith("_")} for item in self.commits],
                    "metadata": {"checkpoint_anchor": True},
                }
                frame = self.append(FINAL_INDEX, _json_bytes(index))
                self._previous_checkpoint_offset = self._file.tell()
                self._file.write(FOOTER.pack(frame.offset, FOOTER_MAGIC))
                self._file.flush()
                os.fsync(self._file.fileno())
        else:
            self._file = self.path.open("wb")
            self._file.write(MAGIC)
            self._file.flush()
            os.fsync(self._file.fileno())
            self._append_start = len(MAGIC)
        self._closed = False
        super().__init__(self.path, cache_bytes=cache_bytes)

    def _load_commits(self) -> None:
        for frame in self.frames:
            if frame.kind == COMMIT:
                _, raw = _read_frame_at(self.path, frame.offset, COMMIT)
                item = json.loads(raw)
                item["_frame_end"] = frame.end
                self.commits.append(item)

    def append(self, kind: int, raw: bytes) -> FrameInfo:
        offset = self._file.tell()
        compressed = zlib.compress(raw, self.compression_level)
        digest = sha256(raw).digest()
        self._file.write(FRAME.pack(kind, len(raw), len(compressed), zlib.crc32(compressed), digest))
        self._file.write(compressed)
        info = FrameInfo(offset, kind, len(raw), len(compressed), digest, self._file.tell())
        self.frames.append(info)
        return info

    def store_page(self, logical: dict, payload: dict) -> TreeRef:
        logical_raw = _json_bytes(logical)
        logical_hash = sha256(logical_raw).hexdigest()
        count = logical["count"]
        raw = _json_bytes({**payload, "logical_hash": logical_hash, "count": count})
        frame = self.append(PAGE, raw)
        ref = TreeRef(frame.offset, logical_hash, count)
        self._remember(ref.offset, {**payload, "logical_hash": logical_hash, "count": count}, len(raw))
        return ref

    def commit(self, manifest: dict) -> None:
        frame = self.append(COMMIT, _json_bytes(manifest))
        checkpoint_offset = self._file.tell()
        self._file.write(CHECKPOINT.pack(frame.offset, self._previous_checkpoint_offset, CHECKPOINT_MAGIC))
        self._file.flush()
        os.fsync(self._file.fileno())
        self._previous_checkpoint_offset = checkpoint_offset
        item = dict(manifest, _frame_end=self._file.tell())
        self.commits.append(item)

    def finalize(self, metadata: dict | None = None) -> None:
        index = {
            "version": FORMAT_VERSION,
            "commits": [{k: v for k, v in item.items() if not k.startswith("_")} for item in self.commits],
            "metadata": metadata or {},
        }
        frame = self.append(FINAL_INDEX, _json_bytes(index))
        self._file.write(FOOTER.pack(frame.offset, FOOTER_MAGIC))
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        self._closed = True

    def verify_appended(self) -> None:
        """Verify every byte appended by this writer, trusting its durable prefix."""
        if not self._closed or not _has_footer(self.path):
            raise ArchiveError("archive must be finalized before appended-byte verification")
        limit = self.path.stat().st_size - FOOTER.size
        frames, end = _scan_frames(self.path, self._append_start, limit, verify=True)
        if not frames or frames[-1].kind != FINAL_INDEX or end != limit:
            raise ArchiveError("appended archive frame verification failed")
        _read_final_index(self.path)

    def close_incomplete(self) -> None:
        if not self._closed:
            self._file.flush()
            self._file.close()
            self._closed = True


class RadixTree:
    """Deterministic module-sharded tree; batch updates rewrite affected paths."""

    def __init__(self, store: ArchiveWriter, tree: str):
        self.store = store
        self.tree = tree
        self.pages_written = 0
        self.pages_read = 0

    def apply(self, root: TreeRef | None, changes: dict) -> TreeRef | None:
        if not changes:
            return root
        prepared = [(key, value, _key_path(key)) for key, value in changes.items()]
        return self._update(root, 0, prepared)

    def build(self, records: Iterable[tuple]) -> TreeRef | None:
        return self.apply(None, dict(records))

    def _page(self, ref: TreeRef) -> dict:
        self.pages_read += 1
        return self.store.read_page(ref)

    def _update(self, ref: TreeRef | None, depth: int, changes: list[tuple]) -> TreeRef | None:
        if depth == TRIE_DEPTH:
            entries = {}
            if ref is not None:
                page = self._page(ref)
                if page.get("kind") != "leaf":
                    raise ArchiveError("expected leaf page")
                entries = {_key_value(key): value for key, value in page["entries"]}
            for key, value, _ in changes:
                if value is None:
                    entries.pop(key, None)
                else:
                    entries[key] = value
            if not entries:
                return None
            ordered = [[_key_json(key), entries[key]] for key in sorted(entries, key=_key_bytes)]
            logical = {"tree": self.tree, "kind": "leaf", "depth": depth, "count": len(ordered), "entries": ordered}
            self.pages_written += 1
            return self.store.store_page(
                logical,
                {"tree": self.tree, "kind": "leaf", "depth": depth, "entries": ordered},
            )

        children = {}
        if ref is not None:
            page = self._page(ref)
            if page.get("kind") != "branch" or page.get("depth") != depth:
                raise ArchiveError("expected branch page")
            children = {slot: TreeRef.from_json(child) for slot, child in page["children"]}
        grouped: dict[str, list] = {}
        for change in changes:
            grouped.setdefault(change[2][depth], []).append(change)
        for slot, group in grouped.items():
            child = self._update(children.get(slot), depth + 1, group)
            if child is None:
                children.pop(slot, None)
            else:
                children[slot] = child
        if not children:
            return None
        ordered = [[slot, children[slot].to_json()] for slot in sorted(children)]
        count = sum(child.count for child in children.values())
        logical_children = [
            [slot, {"logical_hash": children[slot].logical_hash, "count": children[slot].count}]
            for slot in sorted(children)
        ]
        logical = {"tree": self.tree, "kind": "branch", "depth": depth, "count": count, "children": logical_children}
        self.pages_written += 1
        return self.store.store_page(
            logical,
            {"tree": self.tree, "kind": "branch", "depth": depth, "children": ordered},
        )


def graph_digest(node_root: TreeRef | None, edge_root: TreeRef | None) -> str:
    value = {
        "format": "merkle-module-shards-v1",
        "nodes": node_root.logical_hash if node_root else None,
        "edges": edge_root.logical_hash if edge_root else None,
    }
    return sha256(_json_bytes(value)).hexdigest()


class Archive(PageStore):
    def __init__(self, path: str | Path, *, allow_incomplete: bool = False):
        self.path = Path(path)
        self.complete = _has_footer(self.path)
        if not self.complete and not allow_incomplete:
            raise ArchiveError("archive is incomplete")
        self._commits = self._load_index() if self.complete else self._load_incomplete()
        self._entries = {item["sha"]: item for item in self._commits}
        self.latest_commit = self._commits[-1]["sha"] if self._commits else None
        super().__init__(self.path)

    def _load_index(self) -> list[dict]:
        _, index = _read_final_index(self.path)
        return index["commits"]

    def _load_incomplete(self) -> list[dict]:
        if checkpoint := _load_checkpoint_chain(self.path):
            return checkpoint[0]
        if footer_offset := _find_last_footer(self.path):
            return _read_final_index(self.path, footer_offset)[1]["commits"]
        frames, _ = _scan(self.path, verify=False)
        result = []
        for frame in frames:
            if frame.kind == COMMIT:
                _, raw = _read_frame_at(self.path, frame.offset, COMMIT)
                result.append(json.loads(raw))
        return result

    def __len__(self) -> int:
        return len(self._commits)

    def commit_ids(self) -> tuple[str, ...]:
        """Return published commit IDs in this reader's captured archive order."""
        return tuple(item["sha"] for item in self._commits)

    def manifest(self, commit: str | None = None) -> dict:
        """Return an independent copy of one snapshot's stored provenance and roots.

        Args:
            commit: Exact archived commit ID. Omission selects the latest commit
                captured when this reader opened, not a later append by a writer.

        Raises:
            KeyError: The requested commit is absent from this reader's view.
        """
        return deepcopy(self._entries[self.latest_commit if commit is None else commit])

    def _records(self, ref: TreeRef | None) -> Iterator[tuple]:
        if ref is None:
            return
        page = self.read_page(ref)
        if page["kind"] == "leaf":
            for key, value in page["entries"]:
                yield _key_value(key), value
            return
        for _, child in page["children"]:
            yield from self._records(TreeRef.from_json(child))

    def load_rows(self, commit: str | None = None) -> GraphRows:
        sha = self.latest_commit if commit is None else commit
        if sha not in self._entries:
            raise KeyError(sha)
        item = self._entries[sha]
        nodes = dict(self._records(TreeRef.from_json(item.get("node_root"))))
        edges = dict(self._records(TreeRef.from_json(item.get("edge_root"))))
        return GraphRows(nodes, edges)

    def load_raw(self, commit: str | None = None) -> SnapshotGraph:
        return rows_to_snapshot(self.load_rows(commit))

    def load(self, commit: str | None = None) -> nx.DiGraph:
        return snapshot_to_networkx(self.load_raw(commit))

    def parents(self, commit: str) -> tuple[str, ...]:
        return tuple(self._entries[commit]["parents"])

    def verify(self) -> None:
        frames, end = _scan(self.path, verify=True)
        expected = self.path.stat().st_size - (FOOTER.size if self.complete else 0)
        if not frames or end != expected:
            raise ArchiveError("archive frame verification failed")
        for item in self._commits:
            node_root = TreeRef.from_json(item.get("node_root"))
            edge_root = TreeRef.from_json(item.get("edge_root"))
            if graph_digest(node_root, edge_root) != item["graph_digest"]:
                raise ArchiveError(f"root digest mismatch at {item['sha']}")

    def verify_index(self) -> None:
        """Verify the final index frame and every manifest's logical roots."""
        if not self.complete:
            raise ArchiveError("archive is incomplete")
        _read_final_index(self.path)
        for item in self._commits:
            node_root = TreeRef.from_json(item.get("node_root"))
            edge_root = TreeRef.from_json(item.get("edge_root"))
            if graph_digest(node_root, edge_root) != item["graph_digest"]:
                raise ArchiveError(f"root digest mismatch at {item['sha']}")
