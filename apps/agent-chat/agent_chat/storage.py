"""Retain only bounded, scrubbed tool files in a session-owned temporary directory."""

import json
from contextlib import contextmanager
from threading import RLock

from gh_puller.tools.storage import ToolStorage


class StorageLimitError(RuntimeError):
    """Stop a query before temporary tool storage exceeds its session budget."""


class PrivateStorage(ToolStorage):
    def __init__(self, root, scrubber, limit):
        super().__init__(root)
        self.scrubber, self.limit = scrubber, limit
        self.used = 0
        self.quota_lock = RLock()

    def reserve(self, size):
        with self.quota_lock:
            if self.used + size > self.limit:
                raise StorageLimitError("会话临时文件达到上限，请新建会话")
            self.used += size

    def scrub_bytes(self, data):
        for secret in sorted(self.scrubber.secrets, key=len, reverse=True):
            data = data.replace(secret.encode(), b"[redacted]")
        return data

    def read(self, name):
        return self.scrub_bytes(super().read(name))

    def write(self, name, value, **metadata):
        data = value if isinstance(value, bytes) else (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        ).encode()
        data = self.scrub_bytes(data)
        self.reserve(len(data))
        return super().write(name, data, **metadata)

    @contextmanager
    def binary(self, name, **metadata):
        with self.path(name).open("xb") as target:
            writer = ScrubbedWriter(target, self)
            try:
                yield writer
            finally:
                writer.finish()


class ScrubbedWriter:
    """Withhold credential prefixes so arbitrary download chunk boundaries cannot leak them."""

    def __init__(self, target, storage):
        self.target, self.storage = target, storage
        self.pending = b""

    def write(self, data):
        merged = self.storage.scrub_bytes(self.pending + data)
        keep = 0
        for secret in self.storage.scrubber.secrets:
            value = secret.encode()
            for length in range(1, min(len(value), len(merged) + 1)):
                if merged.endswith(value[:length]):
                    keep = max(keep, length)
        self.pending = merged[-keep:] if keep else b""
        output = merged[:-keep] if keep else merged
        self.storage.reserve(len(output))
        self.target.write(output)
        return len(data)

    def finish(self):
        output = self.storage.scrub_bytes(self.pending)
        self.pending = b""
        self.storage.reserve(len(output))
        self.target.write(output)
