"""Test HTTP endpoints implementing the gh_puller.deepwiki backend contract.

The engine contract lives under the root ``tests/deepwiki`` suite. These tests avoid
Claude credentials and CLI calls by replacing index execution and routing DeepWiki
artifacts to a temporary root before importing the application.
"""

import json
import os
import time

from fastapi.testclient import TestClient
from gh_puller.utils import Repo, TaskStatus

import generators
import tasks
from app import app as server_app


def _write_corpus(root) -> str:
    """Create a minimal local repository whose utility module participates in indexing."""
    d = root / "corpus"
    d.mkdir()
    (d / "app.py").write_text(
        "import utils\n\n\ndef main():\n    return utils.hello('x')\n", encoding="utf-8",
    )
    (d / "utils.py").write_text("def hello(name):\n    return f'hi {name}'\n", encoding="utf-8")
    (d / "README.md").write_text("# Demo\n", encoding="utf-8")
    return str(d)


def _client():
    return TestClient(server_app)


# ---------------------------------------------------------------------------
# Contract endpoint smoke checks without a repository
# ---------------------------------------------------------------------------


def test_health_root():
    c = _client()
    assert c.get("/health").json()["status"] == "healthy"
    assert c.get("/").json()["version"] == "1.0.0"


def test_lang_config():
    lang = _client().get("/lang/config").json()
    assert {"en", "zh"} == set(lang["supported_languages"])
    assert lang["default"] == "en"


def test_generators_config():
    """Expose registry-derived target configuration without credentials.

    File-backed generators expose ``configKind`` and ``configDefault``. The default
    target follows the default generator's configuration shape.
    """
    cfg = _client().get("/generators/config").json()
    assert [g["id"] for g in cfg["generators"]] == ["cc", "dsh", "codex", "opencode"]
    by_id = {g["id"]: g for g in cfg["generators"]}
    assert by_id["cc"]["configKind"] == "file"
    assert by_id["dsh"]["configKind"] == "file"
    assert by_id["codex"]["configKind"] == "file"
    assert by_id["opencode"]["configKind"] == "file"
    assert by_id["cc"]["configDefault"] is not None  # ~/.claude/settings.json
    assert by_id["cc"]["providers"] == []  # File-backed generators expose no provider choice.
    assert by_id["codex"]["capability"] == "responses"
    assert by_id["opencode"]["capability"] == "opencode-cli"
    assert by_id["opencode"]["configDefault"] is None
    assert by_id["opencode"]["providers"] == []
    assert cfg["providers"] == []
    assert cfg["defaultGenerator"] == "cc"
    assert cfg["defaultTarget"]["generator"] == "cc"
    # The default file-backed generator supplies its configuration path.
    assert "config_path" in cfg["defaultTarget"]["generator_config"]
    blob = json.dumps(cfg)
    assert "api_key" not in blob and "base_url" not in blob and "KEY" not in blob[:200]


def test_auth():
    c = _client()
    assert c.get("/auth/status").json() == {"auth_required": False}
    assert c.post("/auth/validate", json={"code": "x"}).json() == {"success": False}


def test_index_status_not_indexed(tmp_path):
    raw_create = str(tmp_path / "empty_src")
    os.makedirs(raw_create)
    assert _client().get(
        "/repo/index/status", params={"repo_url": str(raw_create), "type": "local"},
    ).json() == {"ready": False}


def test_wiki_cache_empty():
    c = _client()
    assert (
        c.get(
            "/api/wiki_cache",
            params={"owner": "a", "repo": "b", "repo_type": "github", "language": "en"},
        ).json()
        is None
    )
    assert c.get("/api/processed_projects").json() == []
    assert c.get("/wiki/tasks").json() == []
    assert c.get("/wiki/tasks/nope").status_code == 404


def test_wiki_task_submit_and_get_contract(tmp_path, monkeypatch):
    """Submit a registered task, schedule it, and poll it through completion."""
    async def fake_generate(task):
        task.status = TaskStatus.COMPLETED

    monkeypatch.setattr(tasks, "generate_repo_wiki", fake_generate)
    monkeypatch.setattr(tasks, "_WIKI_TASK_TTL_SECONDS", 0.2)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    r = _client().post(
        "/wiki/tasks",
        json={"repo_url": str(repo_dir), "type": "local",
              "owner": "smoke-1", "repo": "demo", "language": "en"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True and body["joined"] is False
    got = None
    for _ in range(40):
        got = _client().get(f"/wiki/tasks/{body['task_id']}")
        if got.status_code == 200 and got.json().get("status") == "completed":
            break
        time.sleep(0.05)
    assert got is not None and got.status_code == 200
    assert got.json()["status"] == "completed"
    assert "pages_total" in got.json()
    time.sleep(0.3)  # Let TTL cleanup finish so no terminal task remains scheduled.


def test_wiki_task_submit_invalid_target_400():
    r = _client().post(
        "/wiki/tasks",
        json={"repo_url": "/x", "type": "local", "owner": "smoke-2", "repo": "demo",
              "language": "en", "target": {"generator": "nope"}},
    )
    assert r.status_code == 400


def test_local_repo_structure_errors():
    c = _client()
    assert c.get("/local_repo/structure").status_code == 400
    assert c.get("/local_repo/structure", params={"path": "/nope"}).status_code == 404


def test_chat_not_indexed_425(tmp_path):
    raw = str(tmp_path / "empty_src")
    os.makedirs(raw)
    r = _client().post(
        "/chat/completions/stream",
        json={
            "repo_url": raw,
            "type": "local",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 425
    assert "尚未索引" in r.json()["detail"]


def test_chat_validation_400():
    c = _client()
    assert (
        c.post(
            "/chat/completions/stream",
            json={"repo_url": "/x", "type": "local", "messages": []},
        ).status_code
        == 400
    )


def test_codemap_not_indexed_425(tmp_path):
    """Guard the codemap endpoint with the same 425 response as chat."""
    raw = str(tmp_path / "empty_src")
    os.makedirs(raw)
    r = _client().post(
        "/codemap/stream",
        json={"repo_url": raw, "type": "local", "question": "how do I X?"},
    )
    assert r.status_code == 425
    assert "尚未索引" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Local repository preparation through SSE and a stubbed index runner
# ---------------------------------------------------------------------------


def test_prepare_local_repo(tmp_path, monkeypatch):
    """Persist the index database and report readiness after repository preparation."""
    raw = _write_corpus(tmp_path)
    repo = Repo(raw, "local")
    seen = {}

    async def fake_run_index(repo):
        seen["repo"] = repo
        (generators._cbm_cache_dir() / f"{generators.project_name(repo)}.db").touch()

    monkeypatch.setattr(generators, "_run_index", fake_run_index)
    r = _client().post("/repo/prepare", json={"repo_url": raw, "type": "local"})
    assert r.status_code == 200
    assert "event: done" in r.text
    assert "data: ok" in r.text
    # Indexing receives the repository path and leaves no graph artifacts in the source.
    assert str(seen["repo"].save_path) == os.path.abspath(raw)
    assert (generators._cbm_cache_dir() / f"{generators.project_name(repo)}.db").exists()
    assert not os.path.exists(os.path.join(raw, "graphify-out"))
    # The readiness probe changes only after the index database appears.
    assert _client().get(
        "/repo/index/status", params={"repo_url": raw, "type": "local"},
    ).json() == {"ready": True}


def test_prepare_idempotent(tmp_path, monkeypatch):
    """Short-circuit repeated preparation when the repository is already indexed."""
    raw = _write_corpus(tmp_path)
    c = _client()

    async def fake_run_index(repo):
        (generators._cbm_cache_dir() / f"{generators.project_name(repo)}.db").touch()

    monkeypatch.setattr(generators, "_run_index", fake_run_index)
    c.post("/repo/prepare", json={"repo_url": raw, "type": "local"})
    r2 = c.post("/repo/prepare", json={"repo_url": raw, "type": "local"})
    assert "event: ready" in r2.text
