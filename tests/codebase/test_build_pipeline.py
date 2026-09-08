"""Exercise the repository pipeline through its single-commit build operation."""

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import gh_puller.codebase.cbm._runner as runner_module
from gh_puller.codebase.archive import Archive
from gh_puller.codebase.build import BuildOptions, build_repository
from gh_puller.codebase.cbm import BuildPlan

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


def git(repo, *arguments):
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def write_generation(path, project, symbol):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO projects VALUES (?)", (project,))
    connection.execute(
        "INSERT INTO nodes VALUES (1,?,'Project',?,?, '',0,0,'{}')",
        (project, project, project),
    )
    connection.execute(
        "INSERT INTO nodes VALUES (2,?,'Function',?,?||'.'||?,'symbol.txt',1,1,'{}')",
        (project, symbol, project, symbol),
    )
    connection.execute(
        "INSERT INTO edges(id,project,source_id,target_id,type,properties) "
        "VALUES (1,?,1,2,'CONTAINS','{}')",
        (project,),
    )
    connection.commit()
    connection.close()


class PublishingClient:
    def __init__(self, cache_root):
        self.cache_root = cache_root
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
        self.cache_root.mkdir(parents=True, exist_ok=True)
        database = self.cache_root / f"{project}.db"
        replacement = self.cache_root / f".{project}.next"
        write_generation(replacement, project, (tree / "symbol.txt").read_text().strip())
        os.replace(replacement, database)
        self.calls.append((mode, force_full, incremental_controls, target_projects))
        return {"route": "full" if force_full else "closure_repair"}

    def delete_project(self, project):
        return True, project

    def close(self):
        self.closed = True


def test_repository_pipeline_selects_a_plan_for_each_commit(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    commits = []
    for symbol in "abc":
        (repo / "symbol.txt").write_text(symbol)
        git(repo, "add", ".")
        git(repo, "commit", "-m", symbol)
        commits.append(git(repo, "rev-parse", "HEAD"))

    binary = tmp_path / "codebase-memory-mcp"
    binary.write_text("#!/bin/sh\necho codebase-memory-mcp test\n")
    binary.chmod(0o755)
    cache = tmp_path / "cache"
    clients = []

    def make_client(_binary, *, cache_root, **_kwargs):
        client = PublishingClient(cache_root)
        clients.append(client)
        return client

    monkeypatch.setattr(runner_module, "CBMClient", make_client)
    monkeypatch.setenv("CBM_CACHE_DIR", str(cache))
    routes = ("full", "delta", "full")
    options = BuildOptions(
        repo,
        tmp_path / "build",
        binary=binary,
        project_name=f"pipeline-{tmp_path.name}",
        memory_limit=1 << 60,
    )

    assert build_repository(options, lambda target: BuildPlan(route=routes[target.ordinal])) == 0

    with Archive(tmp_path / "build" / "archive.kga") as archive:
        manifests = [archive.manifest(commit) for commit in commits]
        assert [item["cbm_requested_route"] for item in manifests] == list(routes)
        assert [item["generation_diff_source"] for item in manifests] == [
            "full_snapshot",
            "full_generation",
            "full_generation",
        ]
        assert set(archive.load_rows(commits[-1]).nodes) == {
            f"pipeline-{tmp_path.name}",
            f"pipeline-{tmp_path.name}.c",
        }
    summary = json.loads((tmp_path / "build" / "summary.json").read_text())
    assert summary["cbm_build_plan"] == "per-commit"
    assert sum(summary["cbm_plan_counts"].values()) == 3
    assert len(clients) == 1
    assert len(clients[0].calls) == 3
    assert clients[0].closed


@pytest.mark.integration
def test_repository_pipeline_uses_native_index_sdk_end_to_end(tmp_path):
    binary = os.environ.get("GH_PULLER_TEST_CBM_BINARY")
    helper = os.environ.get("GH_PULLER_TEST_CBM_NATIVE_INDEX_HELPER")
    if binary is None or helper is None:
        pytest.skip("real CBM binary and native index helper not configured")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    commits = []
    for symbol in ("first_symbol", "second_symbol"):
        (repo / "main.py").write_text(f"def {symbol}():\n    return 1\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", symbol)
        commits.append(git(repo, "rev-parse", "HEAD"))
    project = f"native-pipeline-{tmp_path.name}"
    options = BuildOptions(
        repo,
        tmp_path / "build",
        binary=Path(binary),
        project_name=project,
        memory_limit=1 << 60,
        cbm_transport="native",
    )

    assert build_repository(
        options,
        lambda target: BuildPlan(route="full" if target.ordinal == 0 else "delta"),
    ) == 0

    with Archive(tmp_path / "build" / "archive.kga") as archive:
        assert archive.commit_ids() == tuple(commits)
        rows = archive.load_rows(commits[-1])
    summary = json.loads((tmp_path / "build" / "summary.json").read_text())
    assert any(node["name"] == "second_symbol" for node in rows.nodes.values())
    assert not any(node["name"] == "first_symbol" for node in rows.nodes.values())
    assert summary["cbm_transport"] == "native"
    assert summary["cleanup"]["project_deleted"] is True
