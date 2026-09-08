import json

import pytest

from gh_puller.codebase.archive import Archive, ArchiveError, ArchiveWriter, RadixTree, graph_digest
from gh_puller.codebase.cbm_build import (
    GRAPH_FIDELITY_VERSION,
    BuildError,
    BuildOptions,
    _legacy_plan,
    _validate_resume_binary,
    _validate_resume_fidelity,
    prepare_output_dir,
)
from gh_puller.codebase.cbm_transport import index_execution_from_envelope
from gh_puller.codebase.store import GraphRows


def make_build(directory):
    directory.mkdir()
    writer = ArchiveWriter(directory / "archive.kga")
    root = RadixTree(writer, "nodes").build(GraphRows({"a": {"v": 1}}, {}).nodes.items())
    writer.commit(
        {
            "sha": "c1",
            "parents": [],
            "node_root": root.to_json(),
            "edge_root": None,
            "nodes": 1,
            "edges": 0,
            "graph_digest": graph_digest(root, None),
        },
    )
    writer.finalize()
    (directory / "summary.json").write_text(json.dumps({"archive_format_version": 5}))


def test_out_dir_copies_native_archive_without_linking(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    make_build(source)
    assert prepare_output_dir(source, destination) == destination
    assert (source / "archive.kga").stat().st_ino != (destination / "archive.kga").stat().st_ino
    assert len(Archive(destination / "archive.kga")) == 1


def test_out_dir_rejects_unsupported_archive_bytes(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    (source / "archive.kga").write_bytes(b"not-an-archive")
    with pytest.raises((BuildError, ArchiveError, OSError)):
        prepare_output_dir(source, destination)


def test_index_execution_parses_cli_text_envelope():
    execution = {"route": "closure_repair", "changed_files": 1, "elapsed_ms": 1729}
    envelope = {"content": [{"type": "text", "text": json.dumps({"index_execution": execution})}]}
    assert index_execution_from_envelope(envelope) == execution


def test_index_execution_returns_none_when_old_cbm_does_not_report_it():
    assert index_execution_from_envelope({"content": [{"type": "text", "text": "{}"}]}) is None


def test_legacy_force_full_becomes_one_constant_commit_plan(tmp_path):
    options = BuildOptions(tmp_path, tmp_path, mode="fast", force_full=True)
    plan = _legacy_plan(options)

    assert plan.analysis_mode == "fast"
    assert plan.route == "full"


def test_resume_pins_cbm_digest_unless_upgrade_is_explicit():
    assert _validate_resume_binary(None, "new", False) is False
    assert _validate_resume_binary({"cbm_binary_sha256": "same"}, "same", False) is False
    assert _validate_resume_binary({"sha": "legacy"}, "new", True) is True
    assert _validate_resume_binary({"cbm_binary_sha256": "old"}, "new", True) is True

    with pytest.raises(BuildError, match="predates"):
        _validate_resume_binary({"sha": "legacy"}, "new", False)
    with pytest.raises(BuildError, match="differs"):
        _validate_resume_binary({"cbm_binary_sha256": "old"}, "new", False)


def test_resume_requires_explicitly_migrated_graph_identity():
    _validate_resume_fidelity(None, "p")
    trusted = {
        "graph_fidelity_version": GRAPH_FIDELITY_VERSION,
        "cbm_binary_sha256": "same",
        "cbm_project": "p",
    }
    _validate_resume_fidelity(trusted, "p")

    with pytest.raises(BuildError, match="one-time graph fidelity migration"):
        _validate_resume_fidelity({"sha": "legacy"}, "p")
    with pytest.raises(BuildError, match="project differs"):
        _validate_resume_fidelity(trusted, "renamed")
