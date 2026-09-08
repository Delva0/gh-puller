"""Verify the per-commit build policy, graph capture, and KGA recorder seams."""

import os
import sqlite3

import pytest

import gh_puller.codebase.cbm_runner as runner_module
from gh_puller.codebase.archive import Archive
from gh_puller.codebase.build_plan import BuildPlan
from gh_puller.codebase.cbm_runner import CBMRunner
from gh_puller.codebase.graph_reader import GraphReader
from gh_puller.codebase.kga_recorder import KGACommit, KGARecorder

SCHEMA = """
CREATE TABLE projects(name TEXT PRIMARY KEY);
CREATE TABLE nodes (
 id INTEGER PRIMARY KEY, project TEXT, label TEXT, name TEXT, qualified_name TEXT,
 file_path TEXT, start_line INTEGER, end_line INTEGER, properties TEXT);
CREATE TABLE edges (
 id INTEGER PRIMARY KEY, project TEXT, source_id INTEGER, target_id INTEGER, type TEXT, properties TEXT,
 local_name_gen TEXT GENERATED ALWAYS AS (
   CASE WHEN type='IMPORTS' THEN coalesce(json_extract(properties,'$.local_name'),'') ELSE '' END));
"""


class FakeBinary:
    def __init__(self, path):
        self.path = path
        self.sha256 = "binary"
        self.checks = 0

    def verify_unchanged(self):
        self.checks += 1


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.closed = False

    def capabilities(self):
        return frozenset({"persistent-mcp", "granular-delta-controls", "force-full-route"})

    def index(self, tree, project, mode, *, force_full, incremental_controls):
        self.calls.append((tree, project, mode, force_full, incremental_controls))
        return {"route": "full" if force_full else "closure_repair"}

    def delete_project(self, project):
        return True, project

    def close(self):
        self.closed = True


def write_store(path, symbol, *, value):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO projects VALUES ('p')")
    connection.execute("INSERT INTO nodes VALUES (1,'p','Project','p','p','',0,0,'{}')")
    connection.execute(
        "INSERT INTO nodes VALUES (2,'p','Function',?,'p.'||?,'a.py',1,2,?)",
        (symbol, symbol, f'{{"value":{value}}}'),
    )
    connection.execute(
        "INSERT INTO edges(id,project,source_id,target_id,type,properties) "
        "VALUES (1,'p',1,2,'CONTAINS','{}')",
    )
    connection.commit()
    connection.close()


def test_build_plan_separates_analysis_mode_from_route():
    delta = BuildPlan(analysis_mode="fast")
    full = BuildPlan(route="full")

    assert delta.force_full is False
    assert delta.metadata()["cbm_requested_route"] == "delta"
    assert full.force_full is True
    assert full.required_capabilities() == {"granular-delta-controls", "force-full-route"}

    with pytest.raises(ValueError, match="analysis mode"):
        BuildPlan(analysis_mode="invalid")
    with pytest.raises(ValueError, match="build route"):
        BuildPlan(route="invalid")


def test_cbm_runner_reuses_transport_across_commit_plans(tmp_path, monkeypatch):
    transport = FakeTransport()
    monkeypatch.setattr(runner_module, "make_transport", lambda *_args, **_kwargs: transport)
    binary = FakeBinary(tmp_path / "cbm")
    runner = CBMRunner(
        binary,
        "p",
        tmp_path / "cache",
        tmp_path / "work",
        memory_limit=1 << 60,
    )

    assert runner.index(BuildPlan(analysis_mode="fast"))["route"] == "closure_repair"
    assert runner.index(BuildPlan(route="full"))["route"] == "full"
    runner.mark_archived("commit")
    assert runner.current_commit == "commit"
    assert [call[3] for call in transport.calls] == [False, True]
    assert binary.checks == 3
    runner.close()
    assert transport.closed


def test_graph_reader_and_recorder_preserve_full_and_diff_generations(tmp_path):
    database = tmp_path / "graph.db"
    replacement = tmp_path / "replacement.db"
    archive_path = tmp_path / "archive.kga"
    write_store(database, "a", value=1)
    reader = GraphReader(database, "p")
    recorder = KGARecorder(archive_path)

    first = reader.snapshot()
    first_manifest = recorder.append(
        KGACommit(0, "c1", (), None),
        first,
        project="p",
        metadata={"cbm_requested_route": "full"},
    )
    previous = reader.pin()
    assert previous is not None
    write_store(replacement, "b", value=2)
    os.replace(replacement, database)
    try:
        second = reader.capture(previous)
    finally:
        previous.close()
    second_manifest = recorder.append(
        KGACommit(1, "c2", ("c1",), 1),
        second,
        project="p",
        metadata={"cbm_requested_route": "delta"},
    )
    recorder.finalize()

    assert first_manifest["generation_diff_source"] == "full_snapshot"
    assert second_manifest["generation_diff_source"] == "full_generation"
    assert second_manifest["changed_rows"] == 4
    with Archive(archive_path) as archive:
        assert set(archive.load_rows("c1").nodes) == {"p", "p.a"}
        assert set(archive.load_rows("c2").nodes) == {"p", "p.b"}
        archive.verify_snapshot("c1")
        archive.verify_snapshot("c2")
