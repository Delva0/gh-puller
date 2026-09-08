"""Store durable graph snapshots in append-only Merkle radix trees.

One writer holds an advisory lock while independent readers capture immutable file
prefixes. Commit checkpoints publish roots only after their referenced frames exist;
readers therefore ignore an incomplete append tail without blocking the writer.
"""

from __future__ import annotations

import fcntl
import json
import os
import struct
import zlib
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import msgspec

from .graph import SnapshotGraph, snapshot_to_networkx
from .store import ExtractionError, GraphRows, rows_to_snapshot, validate_rows

if TYPE_CHECKING:
    from collections.abc import Iterable

    import networkx as nx

# These bytes and the numeric index value are an on-disk compatibility
# contract. They remain unchanged so existing large archives resume in place;
# they are not the Python package or product version.
FORMAT_VERSION = 5
GRAPH_FIDELITY_VERSION = 2
MAGIC = b"KGA5\r\n\x1a\n"
FOOTER_MAGIC = b"KGA5IDX!"
CHECKPOINT_MAGIC = b"KGA5CP!!"
FRAME = struct.Struct(">BQQI32s")
FOOTER = struct.Struct(">Q8s")
CHECKPOINT = struct.Struct(">QQ8s")

PAGE = 1
COMMIT = 2
FINAL_INDEX = 3
# Nodes and outgoing edges are grouped by their qualified module prefix. Code
# changes are module-local, unlike hash sharding which scatters one file's rows
# across nearly every leaf and destroys archive compression.
TRIE_DEPTH = 1


class ArchiveError(Exception):
    pass


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _json_value(raw: bytes):
    try:
        return msgspec.json.decode(raw)
    except msgspec.DecodeError:
        return json.loads(raw)


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


def _record_map(records: Iterable[tuple], tree: str) -> dict:
    result = {}
    for raw_key, value in records:
        key = _key_value(raw_key)
        if key in result:
            raise ArchiveError(f"duplicate {tree} identity: {key!r}")
        result[key] = value
    return result


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


class _PageRef(msgspec.Struct, frozen=True):
    offset: int
    logical_hash: str
    count: int


class _BranchPage(msgspec.Struct, tag="branch", tag_field="kind"):
    tree: str
    depth: int
    children: list[tuple[str, _PageRef]]
    logical_hash: str
    count: int


class _NodeLeafPage(msgspec.Struct, tag="leaf", tag_field="kind"):
    tree: str
    depth: int
    entries: list[tuple[str, dict[str, Any]]]
    logical_hash: str
    count: int


class _EdgeLeafPage(msgspec.Struct, tag="leaf", tag_field="kind"):
    tree: str
    depth: int
    entries: list[tuple[tuple[str, str, str | None, str | None], dict[str, Any]]]
    logical_hash: str
    count: int


_NODE_PAGE_DECODER = msgspec.json.Decoder(_BranchPage | _NodeLeafPage)
_EDGE_PAGE_DECODER = msgspec.json.Decoder(_BranchPage | _EdgeLeafPage)


class _ReadFile:
    def __init__(self, path: Path):
        self.path = path
        self.fd = -1
        self.fd = os.open(path, os.O_RDONLY)
        self.size = os.fstat(self.fd).st_size

    def read(self, offset: int, size: int) -> bytes:
        return os.pread(self.fd, size, offset)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


def _footer_offset(reader: _ReadFile) -> int | None:
    if reader.size < len(MAGIC) + FOOTER.size:
        return None
    offset = reader.size - FOOTER.size
    payload = reader.read(offset, FOOTER.size)
    if len(payload) != FOOTER.size:
        return None
    _, magic = FOOTER.unpack(payload)
    return offset if magic == FOOTER_MAGIC else None


def _read_frame(reader: _ReadFile, offset: int, expected_kind: int | None = None) -> tuple[FrameInfo, bytes]:
    header = reader.read(offset, FRAME.size)
    if len(header) != FRAME.size:
        raise ArchiveError(f"truncated frame header at {offset}")
    kind, raw_len, compressed_len, crc, digest = FRAME.unpack(header)
    if expected_kind is not None and kind != expected_kind:
        raise ArchiveError(f"frame at {offset} has kind {kind}, expected {expected_kind}")
    compressed = reader.read(offset + FRAME.size, compressed_len)
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


def _read_frame_at(path: Path, offset: int, expected_kind: int | None = None) -> tuple[FrameInfo, bytes]:
    with _ReadFile(path) as reader:
        return _read_frame(reader, offset, expected_kind)


def _scan_frames_reader(reader: _ReadFile, start: int, limit: int, *, verify: bool) -> tuple[list[FrameInfo], int]:
    frames = []
    with reader.path.open("rb") as file:
        offset = start
        while offset < limit:
            file.seek(offset)
            prefix = file.read(min(FRAME.size, limit - offset))
            # Checkpoint trailers are deliberately retained between frames. A
            # checkpoint is tiny, belongs to the final archive, and lets an
            # interrupted writer recover from the tail instead of rescanning
            # every historical page. A footer can likewise be embedded when a
            # previously complete archive is extended.
            if len(prefix) >= CHECKPOINT.size and prefix[16:24] == CHECKPOINT_MAGIC:
                offset += CHECKPOINT.size
                continue
            if len(prefix) >= FOOTER.size and prefix[8:16] == FOOTER_MAGIC:
                offset += FOOTER.size
                continue
            if len(prefix) < FRAME.size:
                break
            kind, raw_len, compressed_len, crc, digest = FRAME.unpack(prefix)
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


def _scan_frames(path: Path, start: int, limit: int, *, verify: bool) -> tuple[list[FrameInfo], int]:
    with _ReadFile(path) as reader:
        return _scan_frames_reader(reader, start, limit, verify=verify)


def _scan_reader(reader: _ReadFile, *, verify: bool) -> tuple[list[FrameInfo], int]:
    footer_offset = _footer_offset(reader)
    limit = footer_offset if footer_offset is not None else reader.size
    if reader.read(0, len(MAGIC)) != MAGIC:
        raise ArchiveError(f"{reader.path} is not a supported archive")
    return _scan_frames_reader(reader, len(MAGIC), limit, verify=verify)


def _read_final_index_reader(reader: _ReadFile, footer_offset: int | None = None) -> tuple[FrameInfo, dict]:
    if footer_offset is None:
        footer_offset = reader.size - FOOTER.size
    payload = reader.read(footer_offset, FOOTER.size)
    if len(payload) != FOOTER.size:
        raise ArchiveError(f"truncated footer at {footer_offset}")
    index_offset, magic = FOOTER.unpack(payload)
    if magic != FOOTER_MAGIC:
        raise ArchiveError(f"invalid footer at {footer_offset}")
    frame, raw = _read_frame(reader, index_offset, FINAL_INDEX)
    index = _json_value(raw)
    if index.get("version") != FORMAT_VERSION or not isinstance(index.get("commits"), list):
        raise ArchiveError("invalid archive index")
    return frame, index


def _read_final_index(path: Path, footer_offset: int | None = None) -> tuple[FrameInfo, dict]:
    with _ReadFile(path) as reader:
        return _read_final_index_reader(reader, footer_offset)


def _read_checkpoint_reader(reader: _ReadFile, offset: int) -> tuple[int, int, dict]:
    payload = reader.read(offset, CHECKPOINT.size)
    if len(payload) != CHECKPOINT.size:
        raise ArchiveError(f"truncated checkpoint at {offset}")
    commit_offset, previous_offset, magic = CHECKPOINT.unpack(payload)
    if magic != CHECKPOINT_MAGIC:
        raise ArchiveError(f"invalid checkpoint at {offset}")
    frame, raw = _read_frame(reader, commit_offset, COMMIT)
    if frame.end != offset:
        raise ArchiveError(f"checkpoint at {offset} is not adjacent to its commit")
    manifest = _json_value(raw)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("sha"), str):
        raise ArchiveError(f"invalid commit manifest at {commit_offset}")
    return previous_offset, offset + CHECKPOINT.size, manifest


def _find_last_checkpoint_reader(reader: _ReadFile, *, chunk_size: int = 1 << 20) -> int | None:
    """Find the newest valid checkpoint, normally by reading only the tail.

    A crash may leave partial page frames after the last committed checkpoint,
    so the trailer is not required to be exactly at EOF.  False magic matches
    inside compressed payloads are rejected by checking the referenced commit.
    """
    overlap = len(CHECKPOINT_MAGIC) - 1
    end = reader.size
    while end > len(MAGIC):
        start = max(len(MAGIC), end - chunk_size)
        block = reader.read(start, end - start)
        position = len(block)
        while True:
            found = block.rfind(CHECKPOINT_MAGIC, 0, position)
            if found < 0:
                break
            checkpoint_offset = start + found - (CHECKPOINT.size - len(CHECKPOINT_MAGIC))
            if checkpoint_offset >= len(MAGIC):
                try:
                    _read_checkpoint_reader(reader, checkpoint_offset)
                except (ArchiveError, json.JSONDecodeError, OSError):
                    pass
                else:
                    return checkpoint_offset
            position = found
        if start == len(MAGIC):
            break
        end = start + overlap
    return None


def _find_last_footer_reader(reader: _ReadFile, *, chunk_size: int = 1 << 20) -> int | None:
    """Find a valid embedded final-index footer before an interrupted tail."""
    overlap = len(FOOTER_MAGIC) - 1
    end = reader.size
    while end > len(MAGIC):
        start = max(len(MAGIC), end - chunk_size)
        block = reader.read(start, end - start)
        position = len(block)
        while True:
            found = block.rfind(FOOTER_MAGIC, 0, position)
            if found < 0:
                break
            footer_offset = start + found - (FOOTER.size - len(FOOTER_MAGIC))
            if footer_offset >= len(MAGIC):
                try:
                    frame, _ = _read_final_index_reader(reader, footer_offset)
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


def _load_checkpoint_chain_reader(reader: _ReadFile) -> tuple[list[dict], int, int] | None:
    """Return manifests, durable end, and latest checkpoint trailer offset."""
    latest = _find_last_checkpoint_reader(reader)
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
        marker = reader.read(offset + 8, 8)
        if marker == FOOTER_MAGIC:
            _, index = _read_final_index_reader(reader, offset)
            manifests = list(index["commits"]) + list(reversed(manifests))
            break
        previous, checkpoint_end, manifest = _read_checkpoint_reader(reader, offset)
        if not durable:
            durable = checkpoint_end
        manifests.append(manifest)
        offset = previous
    else:
        manifests.reverse()
    return manifests, durable, latest


class PageStore:
    def __init__(self, path: Path, *, cache_bytes: int = 64 << 20, reader: _ReadFile | None = None):
        self.path = path
        self.cache_bytes = cache_bytes
        self._reader = reader
        self._cache: OrderedDict[tuple[int, str], tuple[Any, int]] = OrderedDict()
        self._cache_size = 0

    def _remember(self, key: tuple[int, str], page: Any, raw_size: int) -> None:
        if raw_size > self.cache_bytes:
            return
        while self._cache and self._cache_size + raw_size > self.cache_bytes:
            _, (_, evicted_size) = self._cache.popitem(last=False)
            self._cache_size -= evicted_size
        self._cache[key] = (page, raw_size)
        self._cache_size += raw_size

    def _read_page_frame(self, ref: TreeRef | _PageRef) -> tuple[FrameInfo, bytes]:
        if self._reader is None:
            return _read_frame_at(self.path, ref.offset, PAGE)
        return _read_frame(self._reader, ref.offset, PAGE)

    def read_page(self, ref: TreeRef) -> dict:
        key = (ref.offset, "json")
        cached = self._cache.get(key)
        if cached is None:
            _, raw = self._read_page_frame(ref)
            page = _json_value(raw)
            if page.get("logical_hash") != ref.logical_hash or page.get("count") != ref.count:
                raise ArchiveError(f"page reference mismatch at {ref.offset}")
            self._remember(key, page, len(raw))
        else:
            page, _ = cached
            self._cache.move_to_end(key)
        return page


class ArchiveWriter(PageStore):
    """Transactional single writer with constant-time complete-archive resume.

    Args:
        path: Archive file to create, resume, or extend.
        compression_level: Zlib level for newly appended frames.
        resume: Reuse the final durable transaction in an existing file.
        extend_complete: Permit appending to a file with a final index.
        cache_bytes: Raw JSON byte budget for parsed writer pages.
    """

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
        existed = self.path.exists()
        self._lock_file = self.path.open("a+b")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise ArchiveError(f"archive already has a writer: {self.path}") from exc
        self.compression_level = compression_level
        self.frames: list[FrameInfo] = []
        self.commits: list[dict] = []
        self._previous_checkpoint_offset = 0
        self._final_size: int | None = None
        try:
            needs_anchor = False
            if existed and resume:
                with _ReadFile(self.path) as reader:
                    footer_offset = _footer_offset(reader)
                    if footer_offset is not None and not extend_complete:
                        raise ArchiveError(f"archive is already complete: {self.path}")
                    if footer_offset is not None:
                        # Keep the prior final index and footer as a durable anchor.
                        # New checkpoints link back to it, so even a crash during the
                        # first extension commit never turns a fast-resumable archive
                        # into one that needs a historical scan.
                        _, index = _read_final_index_reader(reader, footer_offset)
                        self.commits = list(index["commits"])
                        durable = reader.size
                        self._previous_checkpoint_offset = footer_offset
                    elif checkpoint := _load_checkpoint_chain_reader(reader):
                        self.commits, durable, self._previous_checkpoint_offset = checkpoint
                    elif embedded_footer := _find_last_footer_reader(reader):
                        _, index = _read_final_index_reader(reader, embedded_footer)
                        self.commits = list(index["commits"])
                        durable = embedded_footer + FOOTER.size
                        self._previous_checkpoint_offset = embedded_footer
                    else:
                        # Compatibility path for archives created before checkpoint
                        # trailers existed. It is paid once because the next commit
                        # establishes a fast-resume chain.
                        self.frames, _ = _scan_reader(reader, verify=True)
                        self._load_commits(reader)
                        durable = self.commits[-1]["_frame_end"] if self.commits else len(MAGIC)
                        self.frames = [frame for frame in self.frames if frame.end <= durable]
                        needs_anchor = bool(self.commits)
                with self.path.open("r+b") as file:
                    file.truncate(durable)
                self._append_start = durable
                self._file = self.path.open("ab")
                if needs_anchor:
                    index = {
                        "version": FORMAT_VERSION,
                        "commits": [
                            {key: value for key, value in item.items() if not key.startswith("_")}
                            for item in self.commits
                        ],
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
        except BaseException:
            # Constructor failures must release both the partial writer and its
            # advisory lock, including cancellation and process-exit exceptions.
            if file := getattr(self, "_file", None):
                file.close()
            self._release_lock()
            raise

    def _release_lock(self) -> None:
        lock_file = self._lock_file
        if not lock_file.closed:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def _load_commits(self, reader: _ReadFile) -> None:
        for frame in self.frames:
            if frame.kind == COMMIT:
                _, raw = _read_frame(reader, frame.offset, COMMIT)
                item = _json_value(raw)
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
        self._remember((ref.offset, "json"), {**payload, "logical_hash": logical_hash, "count": count}, len(raw))
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
        self._final_size = self._file.tell()
        self._file.close()
        self._closed = True
        self._release_lock()

    def verify_appended(self) -> None:
        """Verify every byte appended by this writer, trusting its durable prefix."""
        if not self._closed or self._final_size is None:
            raise ArchiveError("archive must be finalized before appended-byte verification")
        limit = self._final_size - FOOTER.size
        frames, end = _scan_frames(self.path, self._append_start, limit, verify=True)
        if not frames or frames[-1].kind != FINAL_INDEX or end != limit:
            raise ArchiveError("appended archive frame verification failed")
        _read_final_index(self.path, limit)

    def close_incomplete(self) -> None:
        try:
            if not self._closed:
                self._file.flush()
                self._file.close()
                self._closed = True
        finally:
            self._release_lock()


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
        return self._update(root, 0, prepared, None)

    def build(self, records: Iterable[tuple]) -> TreeRef | None:
        return self.apply(None, _record_map(records, self.tree))

    def _page(self, ref: TreeRef) -> dict:
        self.pages_read += 1
        return self.store.read_page(ref)

    def _update(
        self,
        ref: TreeRef | None,
        depth: int,
        changes: list[tuple],
        shard: str | None,
    ) -> TreeRef | None:
        if depth == TRIE_DEPTH:
            if shard is None:
                raise ArchiveError("KGA leaf has no identity shard")
            entries = {}
            if ref is not None:
                page = self._page(ref)
                if page.get("kind") != "leaf":
                    raise ArchiveError("expected leaf page")
                entries = _record_map(page["entries"], self.tree)
                if any(_key_path(key) != (shard,) for key in entries):
                    raise ArchiveError(f"{self.tree} identity is stored in the wrong shard")
            for key, value, _ in changes:
                if value is None:
                    if key not in entries:
                        raise ArchiveError(f"cannot delete absent {self.tree} identity: {key!r}")
                    del entries[key]
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
            children = {
                slot: TreeRef.from_json(child)
                for slot, child in _record_map(page["children"], f"{self.tree} shard").items()
            }
        grouped: dict[str, list] = {}
        for change in changes:
            grouped.setdefault(change[2][depth], []).append(change)
        for slot, group in grouped.items():
            child = self._update(children.get(slot), depth + 1, group, slot)
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
    """Stable commit view backed by one positional-read file descriptor.

    Args:
        path: Archive file to open.
        allow_incomplete: Read the final durable checkpoint when no final footer
            exists. The captured view does not observe later commits.
        cache_bytes: Raw JSON byte budget for parsed pages. Parsed Python objects
            can occupy more memory than this accounting value.
    """

    def __init__(self, path: str | Path, *, allow_incomplete: bool = False, cache_bytes: int = 64 << 20):
        self.path = Path(path)
        reader = _ReadFile(self.path)
        try:
            super().__init__(self.path, cache_bytes=cache_bytes, reader=reader)
            self._footer_offset = _footer_offset(reader)
            self.complete = self._footer_offset is not None
            if not self.complete and not allow_incomplete:
                raise ArchiveError("archive is incomplete")
            self._commits = (
                self._load_index(self._footer_offset) if self._footer_offset is not None else self._load_incomplete()
            )
            self._entries = {item["sha"]: item for item in self._commits}
            self.latest_commit = self._commits[-1]["sha"] if self._commits else None
        except BaseException:
            # Initialization owns the descriptor even when archive validation or
            # cancellation aborts before the instance reaches its public surface.
            reader.close()
            raise

    def _load_index(self, footer_offset: int) -> list[dict]:
        _, index = _read_final_index_reader(self._reader, footer_offset)
        return index["commits"]

    def _load_incomplete(self) -> list[dict]:
        if checkpoint := _load_checkpoint_chain_reader(self._reader):
            return checkpoint[0]
        if footer_offset := _find_last_footer_reader(self._reader):
            return _read_final_index_reader(self._reader, footer_offset)[1]["commits"]
        frames, _ = _scan_reader(self._reader, verify=False)
        result = []
        for frame in frames:
            if frame.kind == COMMIT:
                _, raw = _read_frame(self._reader, frame.offset, COMMIT)
                result.append(_json_value(raw))
        return result

    def close(self) -> None:
        """Release the reader's persistent file descriptor."""
        self._reader.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

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

    def _typed_page(self, ref: TreeRef | _PageRef, tree: str):
        key = (ref.offset, tree)
        cached = self._cache.get(key)
        if cached is not None:
            page, _ = cached
            self._cache.move_to_end(key)
            return page
        _, raw = self._read_page_frame(ref)
        decoder = _NODE_PAGE_DECODER if tree == "nodes" else _EDGE_PAGE_DECODER
        try:
            page = decoder.decode(raw)
        except msgspec.DecodeError:
            # Standard json accepts non-finite floats written by historical KGA5
            # archives, while strict native decoders correctly reject them.
            page = _json_value(raw)
        logical_hash = page.get("logical_hash") if isinstance(page, dict) else page.logical_hash
        count = page.get("count") if isinstance(page, dict) else page.count
        page_tree = page.get("tree") if isinstance(page, dict) else page.tree
        if logical_hash != ref.logical_hash or count != ref.count or page_tree != tree:
            raise ArchiveError(f"page reference mismatch at {ref.offset}")
        self._remember(key, page, len(raw))
        return page

    def _records(self, ref: TreeRef | None, tree: str) -> dict:
        records = {}
        stack: list[tuple[TreeRef | _PageRef, str | None]] = [] if ref is None else [(ref, None)]
        while stack:
            reference, shard = stack.pop()
            page = self._typed_page(reference, tree)
            if isinstance(page, _BranchPage):
                if shard is not None or page.depth != 0:
                    raise ArchiveError(f"invalid {tree} branch depth")
                children = _record_map(page.children, f"{tree} shard")
                stack.extend((child, slot) for slot, child in reversed(tuple(children.items())))
            elif isinstance(page, dict) and page["kind"] != "leaf":
                if page["kind"] != "branch" or shard is not None or page.get("depth") != 0:
                    raise ArchiveError(f"invalid {tree} branch page")
                children = _record_map(page["children"], f"{tree} shard")
                stack.extend(
                    (TreeRef.from_json(child), slot) for slot, child in reversed(tuple(children.items()))
                )
            else:
                depth = page.get("depth") if isinstance(page, dict) else page.depth
                if shard is None or depth != TRIE_DEPTH:
                    raise ArchiveError(f"invalid {tree} leaf depth")
                entries = _record_map(page["entries"] if isinstance(page, dict) else page.entries, tree)
                if any(_key_path(key) != (shard,) for key in entries):
                    raise ArchiveError(f"{tree} identity is stored in the wrong shard")
                duplicate = records.keys() & entries.keys()
                if duplicate:
                    raise ArchiveError(f"duplicate {tree} identity: {next(iter(duplicate))!r}")
                records.update(entries)
        return records

    def load_rows(self, commit: str | None = None) -> GraphRows:
        """Materialize the exact node and parallel-edge rows for one commit.

        Args:
            commit: Exact archived commit ID. Omission selects the latest commit
                captured when this reader opened.

        Raises:
            KeyError: The requested commit is absent from this reader's view.
        """
        sha = self.latest_commit if commit is None else commit
        if sha not in self._entries:
            raise KeyError(sha)
        item = self._entries[sha]
        nodes = self._records(TreeRef.from_json(item.get("node_root")), "nodes")
        edges = self._records(TreeRef.from_json(item.get("edge_root")), "edges")
        return GraphRows(nodes, edges)

    def _restorable_manifest(self, commit: str | None = None) -> tuple[dict, str]:
        """Validate native restoration metadata without materializing graph rows."""
        sha = self.latest_commit if commit is None else commit
        if sha not in self._entries:
            raise KeyError(sha)
        item = self._entries[sha]
        if item.get("graph_fidelity_version") != GRAPH_FIDELITY_VERSION:
            raise ArchiveError(f"snapshot {sha} has no verified CBM fidelity boundary")
        project = item.get("cbm_project")
        if not isinstance(project, str) or not project:
            raise ArchiveError(f"snapshot {sha} has no CBM project identity")
        nodes, edges = item.get("nodes"), item.get("edges")
        if type(nodes) is not int or nodes < 1 or type(edges) is not int or edges < 0:
            raise ArchiveError(f"snapshot {sha} has invalid row counts")
        try:
            node_root = TreeRef.from_json(item.get("node_root"))
            edge_root = TreeRef.from_json(item.get("edge_root"))
        except (KeyError, TypeError) as exc:
            raise ArchiveError(f"snapshot {sha} has invalid graph roots") from exc
        for root in (node_root, edge_root):
            if root is None:
                continue
            valid_hash = (
                isinstance(root.logical_hash, str)
                and len(root.logical_hash) == 64
                and all(byte in "0123456789abcdef" for byte in root.logical_hash)
            )
            if (
                type(root.offset) is not int
                or root.offset < len(MAGIC)
                or type(root.count) is not int
                or root.count < 1
                or not valid_hash
            ):
                raise ArchiveError(f"snapshot {sha} has invalid graph roots")
        if node_root is None or node_root.count != nodes or (edge_root is None) != (edges == 0):
            raise ArchiveError(f"snapshot {sha} row counts do not match its roots")
        if edge_root is not None and edge_root.count != edges:
            raise ArchiveError(f"snapshot {sha} row counts do not match its roots")
        if item.get("graph_digest") != graph_digest(node_root, edge_root):
            raise ArchiveError(f"snapshot {sha} has an invalid graph digest")
        return item, project

    def verify_snapshot(self, commit: str | None = None) -> None:
        """Verify that one snapshot can be restored exactly into CBM.

        Args:
            commit: Exact archived commit ID. Omission selects the latest commit
                captured when this reader opened.

        Raises:
            ArchiveError: The snapshot predates the fidelity protocol or violates
                its row counts, project identity, JSON, or graph-closure contract.
            KeyError: The requested commit is absent from this reader's view.
        """
        item, project = self._restorable_manifest(commit)
        rows = self.load_rows(item["sha"])
        sha = item["sha"]
        if item.get("nodes") != len(rows.nodes) or item.get("edges") != len(rows.edges):
            raise ArchiveError(f"snapshot {sha} row counts do not match its manifest")
        try:
            validate_rows(rows, project)
        except ExtractionError as exc:
            raise ArchiveError(f"snapshot {sha} is not restorable: {exc}") from exc

    def load_raw(self, commit: str | None = None) -> SnapshotGraph:
        return rows_to_snapshot(self.load_rows(commit))

    def load(self, commit: str | None = None) -> nx.DiGraph:
        return snapshot_to_networkx(self.load_raw(commit))

    def parents(self, commit: str) -> tuple[str, ...]:
        return tuple(self._entries[commit]["parents"])

    def verify(self) -> None:
        frames, end = _scan_reader(self._reader, verify=True)
        expected = self._footer_offset if self._footer_offset is not None else self._reader.size
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
        _read_final_index_reader(self._reader, self._footer_offset)
        for item in self._commits:
            node_root = TreeRef.from_json(item.get("node_root"))
            edge_root = TreeRef.from_json(item.get("edge_root"))
            if graph_digest(node_root, edge_root) != item["graph_digest"]:
                raise ArchiveError(f"root digest mismatch at {item['sha']}")
