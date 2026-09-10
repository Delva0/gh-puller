"""Store graph and coverage snapshots in append-only Merkle trees.

One writer holds an advisory lock while independent readers capture immutable file
prefixes. Commit checkpoints publish roots only after their referenced frames exist;
readers therefore ignore an incomplete append tail without blocking the writer. The
recorder maps graph captures onto Git-parent-based physical transactions.
"""

from __future__ import annotations

import fcntl
import json
import os
import struct
import zlib
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import msgspec

from .graph import SnapshotGraph, snapshot_to_networkx
from .store import ExtractionError, GraphRows, rows_to_snapshot

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    import networkx as nx

    from .store import GraphCapture

# These bytes identify the KGA format implemented by this package.
MAGIC = b"KGA\r\n\x1a\n"
FOOTER_MAGIC = b"KGAIDX!!"
CHECKPOINT_MAGIC = b"KGACP!!!"
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
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _json_value(raw: bytes):
    try:
        return msgspec.json.decode(raw)
    except msgspec.DecodeError as exc:
        raise ArchiveError("invalid JSON payload in KGA") from exc


def _key_json(key):
    return list(key) if isinstance(key, tuple) else key


def _key_value(value):
    return tuple(value) if isinstance(value, list) else value


def _key_bytes(key) -> bytes:
    return _json_bytes(_key_json(key))


def _key_path(key, tree: str) -> tuple[str]:
    identity = key[0] if isinstance(key, tuple) else key
    if tree == "coverage":
        return (str(identity).split("/", 1)[0] or ".",)
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


@dataclass(frozen=True, slots=True)
class _EncodedPage:
    logical_hash: str
    count: int
    page: dict
    raw: bytes
    compressed: bytes


def _encode_page(logical: dict, payload: dict, compression_level: int) -> _EncodedPage:
    logical_hash = sha256(_json_bytes(logical)).hexdigest()
    count = logical["count"]
    page = {**payload, "logical_hash": logical_hash, "count": count}
    raw = _json_bytes(page)
    return _EncodedPage(logical_hash, count, page, raw, zlib.compress(raw, compression_level))


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


class _CoverageLeafPage(msgspec.Struct, tag="leaf", tag_field="kind"):
    tree: str
    depth: int
    entries: list[tuple[tuple[str, str], str]]
    logical_hash: str
    count: int


_NODE_PAGE_DECODER = msgspec.json.Decoder(_BranchPage | _NodeLeafPage)
_EDGE_PAGE_DECODER = msgspec.json.Decoder(_BranchPage | _EdgeLeafPage)
_COVERAGE_PAGE_DECODER = msgspec.json.Decoder(_BranchPage | _CoverageLeafPage)


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
    if not isinstance(index, dict) or not isinstance(index.get("commits"), list):
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
                except (ArchiveError, OSError):
                    pass
                else:
                    return checkpoint_offset
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
            if existed and resume:
                with _ReadFile(self.path) as reader:
                    if reader.read(0, len(MAGIC)) != MAGIC:
                        raise ArchiveError(f"{self.path} is not a supported archive")
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
                    else:
                        durable = len(MAGIC)
                with self.path.open("r+b") as file:
                    file.truncate(durable)
                self._append_start = durable
                self._file = self.path.open("ab")
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

    def _append_encoded(self, kind: int, raw: bytes, compressed: bytes) -> FrameInfo:
        offset = self._file.tell()
        digest = sha256(raw).digest()
        self._file.write(FRAME.pack(kind, len(raw), len(compressed), zlib.crc32(compressed), digest))
        self._file.write(compressed)
        info = FrameInfo(offset, kind, len(raw), len(compressed), digest, self._file.tell())
        self.frames.append(info)
        return info

    def append(self, kind: int, raw: bytes) -> FrameInfo:
        return self._append_encoded(kind, raw, zlib.compress(raw, self.compression_level))

    def store_page(self, logical: dict, payload: dict) -> TreeRef:
        return self.store_encoded_page(_encode_page(logical, payload, self.compression_level))

    def store_encoded_page(self, encoded: _EncodedPage) -> TreeRef:
        """Append a page encoded for this writer's configured compression level."""
        frame = self._append_encoded(PAGE, encoded.raw, encoded.compressed)
        ref = TreeRef(frame.offset, encoded.logical_hash, encoded.count)
        self._remember((ref.offset, "json"), encoded.page, len(encoded.raw))
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
        prepared = [(key, value, _key_path(key, self.tree)) for key, value in changes.items()]
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
                if any(_key_path(key, self.tree) != (shard,) for key in entries):
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
        "format": "merkle-module-shards",
        "nodes": node_root.logical_hash if node_root else None,
        "edges": edge_root.logical_hash if edge_root else None,
    }
    return sha256(_json_bytes(value)).hexdigest()


def coverage_digest(root: TreeRef | None, metadata: Mapping[str, object]) -> str:
    """Return the logical identity of coverage rows and metadata."""
    value = {
        "format": "coverage-snapshot",
        "root": root.logical_hash if root else None,
        "metadata": metadata,
    }
    return sha256(_json_bytes(value)).hexdigest()


def materialization_digest(graph: str, coverage: str) -> str:
    """Return the cache identity of a graph and its coverage attachment."""
    return sha256(
        _json_bytes(
            {
                "format": "graph-with-coverage",
                "graph": graph,
                "coverage": coverage,
            },
        ),
    ).hexdigest()


def _manifest_ref(item: dict, field: str, count: int) -> TreeRef | None:
    sha = item.get("sha", "unknown")
    try:
        root = TreeRef.from_json(item.get(field))
    except (KeyError, TypeError) as exc:
        raise ArchiveError(f"snapshot {sha} has an invalid {field}") from exc
    if (root is None) != (count == 0):
        raise ArchiveError(f"snapshot {sha} count does not match {field}")
    if root is None:
        return None
    valid_hash = (
        isinstance(root.logical_hash, str)
        and len(root.logical_hash) == 64
        and all(byte in "0123456789abcdef" for byte in root.logical_hash)
    )
    if (
        type(root.offset) is not int
        or root.offset < len(MAGIC)
        or type(root.count) is not int
        or root.count != count
        or not valid_hash
    ):
        raise ArchiveError(f"snapshot {sha} has an invalid {field}")
    return root


def _graph_manifest(item: dict) -> tuple[TreeRef | None, TreeRef | None]:
    sha = item.get("sha", "unknown")
    nodes, edges = item.get("nodes"), item.get("edges")
    if type(nodes) is not int or nodes < 0 or type(edges) is not int or edges < 0:
        raise ArchiveError(f"snapshot {sha} has invalid graph counts")
    node_root = _manifest_ref(item, "node_root", nodes)
    edge_root = _manifest_ref(item, "edge_root", edges)
    if item.get("graph_digest") != graph_digest(node_root, edge_root):
        raise ArchiveError(f"snapshot {sha} has an invalid graph digest")
    return node_root, edge_root


def _coverage_manifest(item: dict) -> tuple[TreeRef | None, dict[str, object]] | None:
    fields = {
        "coverage_root",
        "coverage_rows",
        "coverage_metadata",
        "coverage_digest",
        "materialization_digest",
    }
    sha = item.get("sha", "unknown")
    present = fields & item.keys()
    if not present:
        return None
    if present != fields:
        raise ArchiveError(f"snapshot {sha} has incomplete coverage metadata")
    count = item.get("coverage_rows")
    metadata = item.get("coverage_metadata")
    if type(count) is not int or count < 0 or not isinstance(metadata, dict):
        raise ArchiveError(f"snapshot {sha} has invalid coverage metadata")
    root = _manifest_ref(item, "coverage_root", count)
    digest = coverage_digest(root, metadata)
    if item.get("coverage_digest") != digest:
        raise ArchiveError(f"snapshot {sha} has an invalid coverage digest")
    if item.get("materialization_digest") != materialization_digest(item.get("graph_digest"), digest):
        raise ArchiveError(f"snapshot {sha} has an invalid materialization digest")
    return root, metadata


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
            if reader.read(0, len(MAGIC)) != MAGIC:
                raise ArchiveError(f"{self.path} is not a supported archive")
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
        return []

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
        decoder = {
            "nodes": _NODE_PAGE_DECODER,
            "edges": _EDGE_PAGE_DECODER,
            "coverage": _COVERAGE_PAGE_DECODER,
        }[tree]
        try:
            page = decoder.decode(raw)
        except msgspec.DecodeError as exc:
            raise ArchiveError(f"invalid {tree} page at {ref.offset}") from exc
        logical_hash = page.logical_hash
        count = page.count
        page_tree = page.tree
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
            else:
                if shard is None or page.depth != TRIE_DEPTH:
                    raise ArchiveError(f"invalid {tree} leaf depth")
                entries = _record_map(page.entries, tree)
                if any(_key_path(key, tree) != (shard,) for key in entries):
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

    def load_coverage(self, commit: str | None = None):
        """Materialize coverage rows and metadata when recorded.

        Args:
            commit: Exact archived commit ID. Omission selects the captured latest.

        Returns:
            The coverage snapshot, or ``None`` when none was recorded.
        """
        sha = self.latest_commit if commit is None else commit
        if sha not in self._entries:
            raise KeyError(sha)
        item = self._entries[sha]
        coverage = _coverage_manifest(item)
        if coverage is None:
            return None
        root, metadata = coverage
        from .store import CoverageSnapshot

        snapshot = CoverageSnapshot(self._records(root, "coverage"), deepcopy(metadata))
        if len(snapshot.rows) != item["coverage_rows"]:
            raise ArchiveError(f"snapshot {sha} coverage count does not match its manifest")
        return snapshot

    def _snapshot_manifest(self, commit: str | None = None) -> dict:
        """Return one manifest after validating only its KGA identities."""
        sha = self.latest_commit if commit is None else commit
        if sha not in self._entries:
            raise KeyError(sha)
        item = self._entries[sha]
        _graph_manifest(item)
        _coverage_manifest(item)
        return item

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
            _graph_manifest(item)
            _coverage_manifest(item)

    def verify_index(self) -> None:
        """Verify the final index frame and every manifest's logical roots."""
        if not self.complete:
            raise ArchiveError("archive is incomplete")
        _read_final_index_reader(self._reader, self._footer_offset)
        for item in self._commits:
            _graph_manifest(item)
            _coverage_manifest(item)


# --- Graph generation recorder ---


@dataclass(frozen=True, slots=True)
class KGACommit:
    """Git identity and archive position for one graph generation."""

    ordinal: int
    sha: str
    parents: tuple[str, ...]
    changed_files: int | None


@dataclass(slots=True)
class _TreeDraft:
    tree: str
    source_root: TreeRef | None
    children: dict[str, TreeRef | _EncodedPage] | None
    pages_read: int


@dataclass(frozen=True, slots=True)
class _TreePlan:
    root: TreeRef | None
    pages: tuple[tuple[TreeRef, _EncodedPage], ...]
    size: int
    parent_pages: int


@dataclass(frozen=True, slots=True)
class _CommitPlan:
    parent: str | None
    node: _TreePlan
    edge: _TreePlan
    coverage: _TreePlan

    @property
    def size(self) -> int:
        return self.node.size + self.edge.size + self.coverage.size

    @property
    def pages(self) -> int:
        return len(self.node.pages) + len(self.edge.pages) + len(self.coverage.pages)

    @property
    def parent_pages(self) -> int:
        return self.node.parent_pages + self.edge.parent_pages + self.coverage.parent_pages


def _same_tree(left: TreeRef | _PageRef | None, right: TreeRef | _EncodedPage | None) -> bool:
    return (
        left is not None
        and right is not None
        and left.logical_hash == right.logical_hash
        and left.count == right.count
    )


def _branch_values(tree: str, children: Mapping[str, TreeRef]) -> tuple[dict, dict]:
    ordered = [[slot, children[slot].to_json()] for slot in sorted(children)]
    logical_children = [
        [slot, {"logical_hash": children[slot].logical_hash, "count": children[slot].count}]
        for slot in sorted(children)
    ]
    logical = {
        "tree": tree,
        "kind": "branch",
        "depth": 0,
        "count": sum(child.count for child in children.values()),
        "children": logical_children,
    }
    return logical, {"tree": tree, "kind": "branch", "depth": 0, "children": ordered}


def _leaf_values(tree: str, entries: Mapping) -> tuple[dict, dict]:
    ordered = [[_key_json(key), entries[key]] for key in sorted(entries, key=_key_bytes)]
    logical = {
        "tree": tree,
        "kind": "leaf",
        "depth": TRIE_DEPTH,
        "count": len(ordered),
        "entries": ordered,
    }
    return logical, {
        "tree": tree,
        "kind": "leaf",
        "depth": TRIE_DEPTH,
        "entries": ordered,
    }


class KGARecorder:
    """Record complete roots while reusing the cheapest Git-parent pages."""

    def __init__(self, path: str | Path, *, compression_level: int = 1):
        """Open a new, incomplete, or finalized KGA for append.

        Args:
            path: Archive path that receives durable commit checkpoints.
            compression_level: Per-frame zlib level used for appended frames.
        """
        self.path = Path(path)
        complete = False
        if self.path.exists():
            with Archive(self.path, allow_incomplete=True) as archive:
                complete = archive.complete
        self.writer = ArchiveWriter(
            self.path,
            compression_level=compression_level,
            extend_complete=complete,
        )
        last = self.writer.commits[-1] if self.writer.commits else None
        self.node_root = self._root(last, "node_root")
        self.edge_root = self._root(last, "edge_root")
        coverage = _coverage_manifest(last) if last else None
        self.coverage_root = coverage[0] if coverage else None
        self.coverage_metadata = deepcopy(coverage[1]) if coverage else None
        self.coverage_recorded = coverage is not None
        self._entries = {item["sha"]: item for item in self.writer.commits}

    @staticmethod
    def _root(item: dict | None, name: str) -> TreeRef | None:
        return TreeRef.from_json(item.get(name)) if item else None

    @property
    def commits(self) -> Sequence[dict]:
        """Return the durable manifest prefix visible to this writer."""
        return self.writer.commits

    @property
    def latest_commit(self) -> str | None:
        """Return the latest archived commit identity, if any."""
        return self.writer.commits[-1]["sha"] if self.writer.commits else None

    @property
    def has_coverage_snapshot(self) -> bool:
        """Return whether the current KGA head has a coverage attachment."""
        return self.coverage_recorded

    def _children(self, root: TreeRef | None, tree: str) -> tuple[dict[str, TreeRef], int]:
        if root is None:
            return {}, 0
        page = self.writer.read_page(root)
        if page.get("tree") != tree or page.get("kind") != "branch" or page.get("depth") != 0:
            raise ArchiveError(f"invalid {tree} root page")
        raw_children = _record_map(page.get("children", ()), f"{tree} shard")
        children = {}
        try:
            for slot, value in raw_children.items():
                child = TreeRef.from_json(value)
                if not isinstance(slot, str) or child is None:
                    raise TypeError
                children[slot] = child
        except (KeyError, TypeError) as exc:
            raise ArchiveError(f"invalid {tree} root children") from exc
        logical, _ = _branch_values(tree, children)
        if sha256(_json_bytes(logical)).hexdigest() != root.logical_hash:
            raise ArchiveError(f"invalid {tree} root identity")
        return children, 1

    def _entries_for(self, ref: TreeRef, tree: str, shard: str) -> dict:
        page = self.writer.read_page(ref)
        if (
            page.get("tree") != tree
            or page.get("kind") != "leaf"
            or page.get("depth") != TRIE_DEPTH
        ):
            raise ArchiveError(f"invalid {tree} leaf page")
        entries = _record_map(page.get("entries", ()), tree)
        if any(_key_path(key, tree) != (shard,) for key in entries):
            raise ArchiveError(f"{tree} identity is stored in the wrong shard")
        logical, _ = _leaf_values(tree, entries)
        if len(entries) != ref.count or sha256(_json_bytes(logical)).hexdigest() != ref.logical_hash:
            raise ArchiveError(f"invalid {tree} leaf identity")
        return entries

    def _draft(
        self,
        tree: str,
        source_root: TreeRef | None,
        changes: Mapping,
        *,
        snapshot: bool,
    ) -> _TreeDraft:
        if not snapshot and not changes:
            return _TreeDraft(tree, source_root, None, 0)
        children, pages_read = self._children(None if snapshot else source_root, tree)
        grouped: dict[str, dict] = {}
        for key, value in changes.items():
            shard = _key_path(key, tree)[0]
            grouped.setdefault(shard, {})[key] = value
        for shard, mutations in grouped.items():
            old = children.get(shard)
            entries = {} if old is None else self._entries_for(old, tree, shard)
            pages_read += int(old is not None)
            for key, value in mutations.items():
                if value is None:
                    if key not in entries:
                        raise ArchiveError(f"cannot delete absent {tree} identity: {key!r}")
                    del entries[key]
                else:
                    entries[key] = value
            if not entries:
                children.pop(shard, None)
                continue
            logical, payload = _leaf_values(tree, entries)
            encoded = _encode_page(logical, payload, self.writer.compression_level)
            children[shard] = old if _same_tree(old, encoded) else encoded
        return _TreeDraft(tree, source_root, children, pages_read)

    def _plan_tree(
        self,
        draft: _TreeDraft,
        parent_root: TreeRef | None,
        offset: int,
        parent_cache: dict[tuple[str, int], dict[str, TreeRef]],
    ) -> tuple[_TreePlan, int]:
        if draft.children is None:
            matches = _same_tree(parent_root, draft.source_root)
            root = parent_root if matches else draft.source_root
            return _TreePlan(root, (), 0, int(matches)), 0
        if not draft.children:
            return _TreePlan(None, (), 0, int(parent_root is None)), 0

        identities = {
            slot: TreeRef(0, child.logical_hash, child.count)
            for slot, child in draft.children.items()
        }
        logical, _ = _branch_values(draft.tree, identities)
        logical_hash = sha256(_json_bytes(logical)).hexdigest()
        count = logical["count"]
        if parent_root is not None and parent_root.logical_hash == logical_hash and parent_root.count == count:
            return _TreePlan(parent_root, (), 0, 1), 0
        if (
            draft.source_root is not None
            and draft.source_root.logical_hash == logical_hash
            and draft.source_root.count == count
        ):
            return _TreePlan(draft.source_root, (), 0, 0), 0

        pages_read = 0
        parent_children = {}
        if parent_root is not None:
            cache_key = (draft.tree, parent_root.offset)
            parent_children = parent_cache.get(cache_key)
            if parent_children is None:
                parent_children, pages_read = self._children(parent_root, draft.tree)
                parent_cache[cache_key] = parent_children

        refs = {}
        pages = []
        size = 0
        parent_pages = 0
        for slot in sorted(draft.children):
            child = draft.children[slot]
            parent_child = parent_children.get(slot)
            if _same_tree(parent_child, child):
                refs[slot] = parent_child
                parent_pages += 1
            elif isinstance(child, TreeRef):
                refs[slot] = child
            else:
                ref = TreeRef(offset, child.logical_hash, child.count)
                refs[slot] = ref
                pages.append((ref, child))
                frame_size = FRAME.size + len(child.compressed)
                offset += frame_size
                size += frame_size

        logical, payload = _branch_values(draft.tree, refs)
        encoded = _encode_page(logical, payload, self.writer.compression_level)
        ref = TreeRef(offset, encoded.logical_hash, encoded.count)
        pages.append((ref, encoded))
        size += FRAME.size + len(encoded.compressed)
        return _TreePlan(ref, tuple(pages), size, parent_pages), pages_read

    def _plan(
        self,
        parent: str | None,
        drafts: tuple[_TreeDraft, _TreeDraft, _TreeDraft],
    ) -> tuple[_CommitPlan, int]:
        item = self._entries.get(parent) if parent is not None else None
        if parent is not None and item is None:
            raise ArchiveError(f"Git parent {parent} is absent from KGA")
        roots = (
            self._root(item, "node_root"),
            self._root(item, "edge_root"),
            self._root(item, "coverage_root"),
        )
        offset = self.writer._file.tell()
        cache = {}
        plans = []
        pages_read = 0
        for draft, root in zip(drafts, roots, strict=True):
            plan, reads = self._plan_tree(draft, root, offset, cache)
            plans.append(plan)
            pages_read += reads
            offset += plan.size
        return _CommitPlan(parent, *plans), pages_read

    def _write(self, plan: _CommitPlan) -> None:
        for tree in (plan.node, plan.edge, plan.coverage):
            for expected, encoded in tree.pages:
                actual = self.writer.store_encoded_page(encoded)
                if actual != expected:
                    raise ArchiveError("KGA page plan disagrees with the writer position")

    def append(
        self,
        commit: KGACommit,
        capture: GraphCapture,
        *,
        project: str,
        metadata: Mapping[str, object],
    ) -> dict:
        """Atomically publish one logical graph generation.

        Args:
            commit: Git identity, archive ordinal, and source-change count.
            capture: Full rows or exact mutations produced by :class:`GraphReader`.
            project: Project identity passed through to the native CBM importer.
            metadata: CBM execution and binary provenance for this generation.

        Returns:
            The manifest committed at the new durable archive boundary.

        Raises:
            ExtractionError: A full capture contains deletions.
        """
        if capture.snapshot and (
            any(value is None for value in capture.nodes.values())
            or any(value is None for value in capture.edges.values())
        ):
            raise ExtractionError("complete graph snapshot contains deletions")
        coverage = capture.coverage
        if coverage is not None and coverage.snapshot and any(value is None for value in coverage.rows.values()):
            raise ExtractionError("complete coverage snapshot contains deletions")

        node = self._draft("nodes", self.node_root, capture.nodes, snapshot=capture.snapshot)
        edge = self._draft("edges", self.edge_root, capture.edges, snapshot=capture.snapshot)
        coverage_draft = self._draft(
            "coverage",
            self.coverage_root,
            {} if coverage is None else coverage.rows,
            snapshot=coverage is None or coverage.snapshot,
        )
        drafts = (node, edge, coverage_draft)
        candidates = commit.parents or (None,)
        planned = []
        selection_reads = 0
        for position, parent in enumerate(candidates):
            plan, reads = self._plan(parent, drafts)
            planned.append((plan.size, -plan.parent_pages, position, plan))
            selection_reads += reads
        _, _, _, selected = min(planned, key=lambda item: item[:3])
        self._write(selected)
        self.node_root = selected.node.root
        self.edge_root = selected.edge.root
        self.coverage_root = selected.coverage.root
        self.coverage_metadata = None if coverage is None else dict(coverage.metadata)
        self.coverage_recorded = coverage is not None

        graph_identity = graph_digest(self.node_root, self.edge_root)
        coverage_fields = {}
        if self.coverage_recorded:
            coverage_identity = coverage_digest(self.coverage_root, self.coverage_metadata)
            coverage_fields = {
                "coverage_root": self.coverage_root.to_json() if self.coverage_root else None,
                "coverage_rows": self.coverage_root.count if self.coverage_root else 0,
                "coverage_metadata": self.coverage_metadata,
                "coverage_digest": coverage_identity,
                "materialization_digest": materialization_digest(graph_identity, coverage_identity),
            }
        changed_files = commit.changed_files
        parent_files = metadata.get("git_parent_changed_files")
        if selected.parent is not None and isinstance(parent_files, Mapping):
            changed_files = parent_files.get(selected.parent, changed_files)
        manifest = {
            **metadata,
            **coverage_fields,
            "ordinal": commit.ordinal,
            "sha": commit.sha,
            "parents": list(commit.parents),
            "node_root": self.node_root.to_json() if self.node_root else None,
            "edge_root": self.edge_root.to_json() if self.edge_root else None,
            "nodes": self.node_root.count if self.node_root else 0,
            "edges": self.edge_root.count if self.edge_root else 0,
            "graph_digest": graph_identity,
            "changed_files": changed_files,
            "changed_rows": capture.changed_rows,
            "changed_coverage_rows": capture.changed_coverage_rows,
            "pages_read": sum(draft.pages_read for draft in drafts) + selection_reads,
            "pages_written": selected.pages,
            "generation_diff_source": capture.source,
            "delta_base": selected.parent,
            "delta_base_bytes": selected.size,
            "project": project,
        }
        self.writer.commit(manifest)
        self._entries[commit.sha] = self.writer.commits[-1]
        return manifest

    def finalize(self, metadata: Mapping[str, object] | None = None) -> None:
        """Write and verify the final archive index.

        Args:
            metadata: Optional build-wide summary embedded in the final index.
        """
        self.writer.finalize({"diff_base": "minimum-compressed-git-parent", **dict(metadata or {})})
        self.writer.verify_appended()

    def close_incomplete(self) -> None:
        """Release the writer while preserving its latest durable checkpoint."""
        self.writer.close_incomplete()
