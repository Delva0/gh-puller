"""Retain only bounded, scrubbed tool files in a session-owned temporary directory."""

import base64
import hashlib
import json
import re
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
        digest = hashlib.sha256(data).hexdigest()
        if digest in self.artifacts:
            self.path(name).hardlink_to(self.path(self.artifacts[digest]))
            self.describe(name, **metadata)
            return name
        self.reserve(len(data))
        return super().write(name, data, **metadata)

    def import_artifacts(self, artifacts):
        for digest, encoded in artifacts.items():
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or len(encoded) > (self.limit + 2) // 3 * 4:
                raise ValueError("Invalid artifact attachment")
            data = base64.b64decode(encoded, validate=True)
            if hashlib.sha256(data).hexdigest() != digest or self.scrub_bytes(data) != data:
                raise ValueError("Artifact attachment does not match its reference")
            if digest not in self.artifacts:
                self.write(".attachments/" + digest, data)

    def copy_artifacts(self, source):
        for digest in source.artifacts:
            if digest not in self.artifacts:
                self.write(".attachments/" + digest, source.read_artifact(digest))

    def attachment(self, digest):
        return base64.b64encode(self.read_artifact(digest)).decode()

    @contextmanager
    def binary(self, name, **metadata):
        with super().binary(name, **metadata) as target:
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
