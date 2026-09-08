"""Verify the per-commit build policy, graph capture, and KGA recorder seams."""

import os
import sqlite3
import subprocess

import pytest

import gh_puller.codebase.cbm._runner as runner_module
from gh_puller.codebase.archive import Archive, KGACommit, KGARecorder
from gh_puller.codebase.build import CommitTarget, build_commit
from gh_puller.codebase.cbm import BuildPlan, CBMRunner
from gh_puller.codebase.store import GraphReader

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


class FakeClient:
    def __init__(self):
        self.calls = []
        self.closed = False

    def capabilities(self):
        return frozenset({"persistent-mcp", "granular-delta-controls", "force-full-route"})

    def index_repository(
        self,
        tree,
        project,
        mode,
        *,
        force_full,
        incremental_controls,
        target_projects,
    ):
        self.calls.append((tree, project, mode, force_full, incremental_controls, target_projects))
        return {"route": "full" if force_full else "closure_repair"}

    def delete_project(self, project):
        return True, project

    def close(self):
        self.closed = True


class PublishingRunner:
    def __init__(self, root):
        self.project = "p"
        self.tree = root / "tree"
        self.db_path = root / "graph.db"
        self.binary = FakeBinary(root / "cbm")
        self.current_commit = None
        self.pending_commit = None
        self.plans = []

    def begin_commit(self, sha):
        self.pending_commit = sha

    def index(self, plan):
        symbol = (self.tree / "symbol.txt").read_text().strip()
        replacement = self.db_path.with_suffix(".next")
        write_store(replacement, symbol, value=ord(symbol))
        os.replace(replacement, self.db_path)
        self.plans.append(plan)
        return {"route": "full" if plan.force_full else "closure_repair"}

    def mark_archived(self, sha):
        self.current_commit = sha
        self.pending_commit = None


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


def git(repo, *arguments):
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_build_plan_separates_analysis_mode_from_route():
    delta = BuildPlan(analysis_mode="fast")
    full = BuildPlan(route="full")
    cross_repo = BuildPlan(
        analysis_mode="cross-repo-intelligence",
        target_projects=("target",),
    )

    assert delta.force_full is False
    assert delta.metadata()["cbm_requested_route"] == "delta"
    assert full.force_full is True
    assert full.required_capabilities() == {"granular-delta-controls", "force-full-route"}
    assert cross_repo.metadata()["cbm_target_projects"] == ["target"]

    with pytest.raises(ValueError, match="analysis mode"):
        BuildPlan(analysis_mode="invalid")
    with pytest.raises(ValueError, match="build route"):
        BuildPlan(route="invalid")
    with pytest.raises(ValueError, match="requires target projects"):
        BuildPlan(analysis_mode="cross-repo-intelligence")
    with pytest.raises(ValueError, match="no full-build route"):
        BuildPlan(
            analysis_mode="cross-repo-intelligence",
            route="full",
            target_projects=("target",),
        )


def test_cbm_runner_reuses_client_across_commit_plans(tmp_path, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(runner_module, "CBMClient", lambda *_args, **_kwargs: client)
    binary = FakeBinary(tmp_path / "cbm")
    work = tmp_path / "work"
    work.mkdir()
    (work / "current-sha").write_text("untrusted-legacy-state")
    runner = CBMRunner(
        binary,
        "p",
        tmp_path / "cache",
        work,
        memory_limit=1 << 60,
    )

    assert runner.current_commit is None
    assert runner.index(BuildPlan(analysis_mode="fast"))["route"] == "closure_repair"
    assert runner.index(BuildPlan(route="full"))["route"] == "full"
    runner.mark_archived("commit")
    assert runner.current_commit == "commit"
    assert [call[3] for call in client.calls] == [False, True]
    runner.close()
    assert client.closed


def test_build_commit_is_the_shared_full_and_delta_operation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "symbol.txt").write_text("a")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "first")
    first = git(repo, "rev-parse", "HEAD")
    (repo / "symbol.txt").write_text("b")
    git(repo, "commit", "-am", "second")
    second = git(repo, "rev-parse", "HEAD")

    runner = PublishingRunner(tmp_path / "runner")
    recorder = KGARecorder(tmp_path / "archive.kga")
    full = build_commit(
        CommitTarget(0, first, (), None),
        BuildPlan(route="full"),
        recorder,
        repo=repo,
        runner=runner,
    )
    delta = build_commit(
        CommitTarget(1, second, (first,), first),
        BuildPlan(route="delta"),
        recorder,
        repo=repo,
        runner=runner,
    )
    recorder.finalize()

    assert full.manifest["generation_diff_source"] == "full_snapshot"
    assert delta.manifest["generation_diff_source"] == "full_generation"
    assert [plan.route for plan in runner.plans] == ["full", "delta"]
    with Archive(tmp_path / "archive.kga") as archive:
        assert set(archive.load_rows(first).nodes) == {"p", "p.a"}
        assert set(archive.load_rows(second).nodes) == {"p", "p.b"}


def test_build_commit_recovers_a_cbm_generation_pending_kga_capture(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "symbol.txt").write_text("a")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "first")
    first = git(repo, "rev-parse", "HEAD")
    (repo / "symbol.txt").write_text("b")
    git(repo, "commit", "-am", "second")
    second = git(repo, "rev-parse", "HEAD")

    runner = PublishingRunner(tmp_path / "runner")
    recorder = KGARecorder(tmp_path / "archive.kga")
    build_commit(
        CommitTarget(0, first, (), None),
        BuildPlan(route="full"),
        recorder,
        repo=repo,
        runner=runner,
    )
    runner.pending_commit = second
    result = build_commit(
        CommitTarget(1, second, (first,), first),
        BuildPlan(route="delta"),
        recorder,
        repo=repo,
        runner=runner,
    )
    recorder.finalize()

    assert result.manifest["generation_diff_source"] == "full_snapshot"
    assert runner.current_commit == second
    assert runner.pending_commit is None
    with Archive(tmp_path / "archive.kga") as archive:
        assert set(archive.load_rows(second).nodes) == {"p", "p.b"}


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
