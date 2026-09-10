import json

import pytest

from gh_puller.codebase.archive import Archive, ArchiveError, ArchiveWriter, RadixTree, graph_digest
from gh_puller.codebase.build import (
    BuildError,
    BuildOptions,
    _constant_plan,
    _validate_resume_engine,
    _validate_resume_project,
    prepare_output_dir,
)
from gh_puller.codebase.cbm.transports.utils import index_execution_from_envelope
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
    (directory / "summary.json").write_text(json.dumps({"format": "kga"}))


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


def test_force_full_becomes_one_constant_commit_plan(tmp_path):
    options = BuildOptions(tmp_path, tmp_path, mode="fast", force_full=True)
    plan = _constant_plan(options)

    assert plan.analysis_mode == "fast"
    assert plan.route == "full"


def test_resume_pins_engine_digest():
    assert _validate_resume_engine(None, "new", False) is False
    assert _validate_resume_engine({"cbm_engine_sha256": "same"}, "same", False) is False
    assert _validate_resume_engine({"cbm_engine_sha256": "old"}, "new", True) is True

    with pytest.raises(BuildError, match="new build directory"):
        _validate_resume_engine({"sha": "missing"}, "new", False)
    with pytest.raises(BuildError, match="new build directory"):
        _validate_resume_engine({"sha": "missing"}, "new", True)
    with pytest.raises(BuildError, match="differs"):
        _validate_resume_engine({"cbm_engine_sha256": "old"}, "new", False)


def test_resume_requires_the_same_project_identity():
    _validate_resume_project(None, "p")
    trusted = {"project": "p"}
    _validate_resume_project(trusted, "p")

    with pytest.raises(BuildError, match="project differs"):
        _validate_resume_project(trusted, "renamed")
