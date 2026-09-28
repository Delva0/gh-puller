"""Runtime files stay usable without an experiment recorder or archive layout."""

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
                                           "content": "cGFydGlhbCBieXRlcwA="})
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
