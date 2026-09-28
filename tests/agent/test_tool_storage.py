"""Runtime files stay usable without an experiment recorder or archive layout."""

import base64
import hashlib
import json

import pytest

from gh_puller.tools.storage import ToolStorage


def test_storage_is_independent_of_diagnostics_and_keeps_partial_files(tmp_path):
    events = []
    storage = ToolStorage(tmp_path / "work", observer=lambda kind, **data: events.append((kind, data)))
    storage.record("request.json", {"query": "diagnostic only"})
    assert not (storage.root / "request.json").exists()
    saved = storage.write("result.json", {"value": 42})
    assert saved == "result.json" and (storage.root / saved).read_text().strip().startswith("{")
    def interrupted():
        with storage.binary("partial.body") as output:
            output.write(b"partial bytes\x00")
            raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        interrupted()
    assert (storage.root / "partial.body").read_bytes() == b"partial bytes\x00"
    assert events[-1] == ("artifact/saved", {"path": "partial.body", "size": 14, "complete": False,
                                           "sha256": hashlib.sha256(b"partial bytes\x00").hexdigest()})
    with pytest.raises(FileExistsError):
        storage.write("result.json", "cannot overwrite")
    with pytest.raises(ValueError, match="outside"):
        storage.write("../escaped.txt", "cannot escape")


def test_disabled_observation_still_allocates_and_stores_files(tmp_path):
    storage = ToolStorage(tmp_path)
    first, second = storage.allocate("read"), storage.allocate("read")
    assert first != second
    storage.record(first + ".request.json", {"diagnostic": True})
    result = storage.write(second + ".body", b"exact evidence")
    assert [p.name for p in tmp_path.iterdir()] == [result]


def test_large_artifacts_are_references_and_resolvers_verify_content(tmp_path):
    events = []
    original = ToolStorage(tmp_path / "source", observer=lambda kind, **data: events.append(
        {"type": kind, "data": data}))
    body = b"large immutable evidence" * 100000
    original.write("evidence.body", body)
    assert len(json.dumps(events)) < 250
    target = ToolStorage(tmp_path / "target")
    with pytest.raises(ValueError, match="unavailable"):
        target.load_events(events)
    with pytest.raises(ValueError, match="storage limit"):
        target.load_events(events, max_bytes=100)
    with pytest.raises(ValueError, match="does not match"):
        target.load_events(events, resolve_artifact=lambda _: b"tampered")
    target.load_events(events, resolve_artifact=original.read_artifact)
    assert target.read("evidence.body") == body
    assert target.artifacts == original.artifacts


def test_legacy_inline_artifacts_replay_as_references(tmp_path):
    events = []
    target = ToolStorage(tmp_path, observer=lambda kind, **data: events.append({"type": kind, "data": data}))
    body = b"legacy evidence"
    target.load_events([{"type": "artifact/saved", "data": {
        "path": "old.body", "content": base64.b64encode(body).decode(), "size": len(body), "complete": True,
    }}])
    assert target.read("old.body") == body
    assert "content" not in events[0]["data"]
    assert target.read_artifact(events[0]["data"]["sha256"]) == body
