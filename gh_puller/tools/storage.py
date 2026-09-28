"""Store files needed by tools, independently of observation and experiment archives.

Callers own the directory lifetime. File storage and operation identifiers work
without an observer; optional observation receives diagnostic events.
"""

import json
import threading
from contextlib import contextmanager
from pathlib import Path


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

    def allocate(self, kind: str) -> str:
        """Allocate a unique operation identifier.

        Args:
            kind: Tool-selected suffix describing the operation.
        """
        with self.lock:
            self.sequence += 1
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
        self.event("artifact/saved", path=path.relative_to(self.root).as_posix(),
                   size=path.stat().st_size, complete=complete, **metadata)

    def record(self, name: str, value, **metadata) -> None:
        """Observe diagnostic material without creating a runtime file.

        Args:
            name: Suggested diagnostic filename for an external recorder.
            value: Diagnostic bytes, text or JSON-serializable data.
            metadata: Additional observation details.
        """
        self.event("diagnostic", name=name, value=value, **metadata)

    def event(self, kind: str, **data) -> None:
        """Forward optional tool diagnostics to the caller.

        Args:
            kind: Tool-defined diagnostic kind, independent of canonical agent events.
            data: Diagnostic details; no storage layout or delivery is implied.
        """
        if self.observer is not None:
            self.observer(kind, **data)
