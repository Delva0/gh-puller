"""Store files needed by tools, independently of observation and experiment archives.

Callers own directory lifetime and artifact transport. Events contain content-addressed
references, never file bodies; recovery resolves them through a caller-provided reader.
"""

import base64
import hashlib
import json
import re
import threading
from contextlib import contextmanager
from pathlib import Path, PurePosixPath


class ToolStorage:
    """Own runtime files and operation identifiers within one caller-selected directory."""

    def __init__(self, root: Path, *, observer=None):
        """Create storage without configuring any event sink.

        Args:
            root: Working directory for tool files, created when absent.
            observer: Optional synchronous callback receiving a kind and keyword data.
        """
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.sequence = 0
        self.lock = threading.RLock()
        self.observer = observer
        self.artifacts: dict[str, str] = {}

    def allocate(self, kind: str) -> str:
        """Allocate a unique operation identifier.

        Args:
            kind: Tool-selected suffix describing the operation.
        """
        with self.lock:
            self.sequence += 1
            self.event("artifact/allocated", sequence=self.sequence)
            return f"{self.sequence:04d}-{kind}"

    def path(self, name: str) -> Path:
        """Resolve a relative runtime file and create its parent directories.

        Args:
            name: Relative path confined to this storage directory.
        """
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"Tool file is outside its directory: {name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write(self, name: str, value, **metadata) -> str:
        """Create a file that remains available independently of observation.

        Args:
            name: Relative path; existing files are never overwritten.
            value: Bytes, UTF-8 text, or a JSON-serializable value.
            metadata: Optional observation details associated with the file.

        Returns:
            Path relative to the storage directory.
        """
        path = self.path(name)
        body = value if isinstance(value, bytes) else (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        ).encode("utf-8")
        with path.open("xb") as output:
            output.write(body)
        self.describe(name, **metadata)
        return path.relative_to(self.root).as_posix()

    def read(self, name: str) -> bytes:
        """Read runtime evidence through caller-owned storage policy.

        Args:
            name: Relative path confined to this storage directory.
        """
        return self.path(name).read_bytes()

    @contextmanager
    def binary(self, name: str, **metadata):
        """Create a streamed file, retaining partial bytes on failure or cancellation.

        Args:
            name: Relative path; existing files are never overwritten.
            metadata: Optional observation details associated with the file.
        """
        path = self.path(name)
        self.event("artifact/open", path=path.relative_to(self.root).as_posix(), **metadata)
        complete = False
        try:
            with path.open("xb") as output:
                yield output
            complete = True
        finally:
            if path.is_file():
                self.describe(name, complete=complete, **metadata)

    def describe(self, name: str, *, complete: bool = True, **metadata) -> None:
        """Observe a file written by this storage or a streaming producer.

        Args:
            name: Relative path of an existing file.
            complete: Whether the producer finished writing.
            metadata: Additional observation details.
        """
        path = self.path(name)
        body = self.read(name)
        digest = hashlib.sha256(body).hexdigest()
        self.artifacts[digest] = name
        self.event("artifact/saved", path=path.relative_to(self.root).as_posix(),
                   size=len(body), complete=complete, sha256=digest, **metadata)

    def read_artifact(self, digest: str) -> bytes:
        """Resolve an immutable artifact already owned by this storage.

        Args:
            digest: SHA-256 content address observed by ``artifact/saved``.
        """
        if digest not in self.artifacts:
            raise ValueError("Observed artifact is unavailable; supply its attachment or a resolver")
        body = self.read(self.artifacts[digest])
        if hashlib.sha256(body).hexdigest() != digest:
            raise ValueError("Observed artifact content changed")
        return body

    @staticmethod
    def event_files(events, *, max_bytes=64 * 1024 * 1024):
        """Validate bounded artifact references without reading external resources.

        Args:
            events: Ordered tool observations. Older path-only observations are ignored.
            max_bytes: Maximum total referenced evidence size.
        """
        files, size = {}, 0
        for event in events:
            data = event["data"]
            if event["type"] != "artifact/saved" or not {"sha256", "content"}.intersection(data):
                continue
            name = data["path"]
            path = PurePosixPath(name)
            if (not path.parts or len(name) > 256 or path.is_absolute() or ".." in path.parts
                    or "\\" in name or path.as_posix() != name):
                raise ValueError("Invalid observed file path")
            if "content" in data:
                if len(data["content"]) > (max_bytes + 2) // 3 * 4:
                    raise ValueError("Observed files exceed the storage limit")
                body = base64.b64decode(data["content"], validate=True)
                data = {**data, "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}
            if (not re.fullmatch(r"[0-9a-f]{64}", data["sha256"])
                    or type(data["size"]) is not int or data["size"] < 0):
                raise ValueError("Invalid observed artifact reference")
            if name in files and any(files[name][key] != data[key] for key in ("sha256", "size")):
                raise ValueError("Observed immutable file changed")
            size += data["size"] if name not in files else 0
            if size > max_bytes:
                raise ValueError("Observed files exceed the storage limit")
            files[name] = data
        return files

    def load_events(self, events, *, resolve_artifact=None, max_bytes=64 * 1024 * 1024):
        """Restore tool evidence into fresh storage and publish the restored files.

        Args:
            events: Ordered observations from the owning Agent.
            resolve_artifact: Reader mapping SHA-256 addresses to bytes, without implicit
                filesystem or network access. Omission uses this storage's own artifacts.
            max_bytes: Maximum total referenced evidence size.
        """
        files = self.event_files(events, max_bytes=max_bytes)
        resolve_artifact = resolve_artifact or self.read_artifact
        for event in events:
            if event["type"] == "artifact/allocated":
                sequence = event["data"]["sequence"]
                if type(sequence) is not int or sequence < 0:
                    raise ValueError("Invalid tool operation sequence")
                self.sequence = max(self.sequence, sequence)
        for name, data in files.items():
            body = (base64.b64decode(data["content"], validate=True) if "content" in data
                    else resolve_artifact(data["sha256"]))
            if len(body) != data["size"] or hashlib.sha256(body).hexdigest() != data["sha256"]:
                raise ValueError("Observed artifact does not match its reference")
            metadata = {key: value for key, value in data.items()
                        if key not in {"path", "content", "size", "sha256"}}
            if self.path(name).exists():
                if self.read(name) != body:
                    raise ValueError("Observed file conflicts with current tool storage")
                self.describe(name, **metadata)
            else:
                self.write(name, body, **metadata)
        self.event("artifact/allocated", sequence=self.sequence)

    def reserve_context_ids(self, items):
        """Avoid reusing file IDs still mentioned by imported Context.

        Args:
            items: Shared Context items, including references to foreign tools.
        """
        self.sequence = max(self.sequence, max(map(int, re.findall(
            r"\b(\d{4,12})-[a-z]", json.dumps(items))), default=0))

    def record(self, name: str, value, **metadata) -> None:
        """Observe diagnostic material without creating a runtime file.

        Args:
            name: Suggested diagnostic filename for an external recorder.
            value: Diagnostic bytes, text or JSON-serializable data.
            metadata: Additional observation details.
        """
        self.event("diagnostic", name=name, value=value, **metadata)

    def event(self, kind: str, **data) -> None:
        """Forward optional tool observations to the caller.

        Args:
            kind: Recovery facts use the tool namespace defined by agent.events;
                other routes are caller-only diagnostics.
            data: Observed details; no storage layout or delivery is implied.
        """
        if self.observer is not None:
            self.observer(kind, **data)
