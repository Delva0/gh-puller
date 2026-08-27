"""gh_puller.deepwiki 后端契约的 HTTP 端点测试(引擎在 gh_puller,见根 tests/test_deepwiki.py)。

不调 Claude agent(不依赖 API key / CLI):
- 环境变量要求:ANTHROPIC_API_KEY 不设;DEEPWIKI_ROOT 指向临时目录(见文件头)。
- 覆盖:契约端点 smoke(prepare 走真实 graphify.extract,code_only 纯本地)、
  未索引的错误语义(chat 425 / codemap NDJSON error)。
"""

import json
import os
import tempfile
import time

# envs 在模块导入时单点读取 —— 必须在 import gh_puller.deepwiki 前把产物根指向临时目录
os.environ.setdefault("DEEPWIKI_ROOT", tempfile.mkdtemp(prefix="deepwiki-app-test-"))

from fastapi.testclient import TestClient
from gh_puller.deepwiki.cache import _graph_path
from gh_puller.utils import Repo, TaskStatus

import tasks
from app import app as server_app


def _write_corpus(root) -> str:
    """构造最小本地仓库(带 utils 子包,纳入代码 AST 提取)。"""
    d = root / "corpus"
    d.mkdir()
    (d / "app.py").write_text(
        "import utils\n\n\ndef main():\n    return utils.hello('x')\n", encoding="utf-8"
    )
    (d / "utils.py").write_text("def hello(name):\n    return f'hi {name}'\n", encoding="utf-8")
    (d / "README.md").write_text("# Demo\n", encoding="utf-8")
    return str(d)


def _client():
    return TestClient(server_app)


# ---------------------------------------------------------------------------
# 契约端点 smoke(无仓库时)
# ---------------------------------------------------------------------------


def test_health_root():
    c = _client()
    assert c.get("/health").json()["status"] == "healthy"
    assert c.get("/").json()["version"] == "1.0.0"


def test_lang_config():
    lang = _client().get("/lang/config").json()
    assert {"en", "zh"} == set(lang["supported_languages"])
    assert lang["default"] == "en"


def test_models_config():
    """旧契约 = object 类投影(deprecated):file 类 provider 随配置文件,不设请求轴。"""
    cfg = _client().get("/models/config").json()
    assert cfg["defaultProvider"] == ""  # 默认 generator(cc)为 file 类,无 provider 轴
    assert [p["id"] for p in cfg["providers"]] == ["openai"]  # 仅 object 类入口
    assert all(p["supportsCustomModel"] for p in cfg["providers"])


def test_generators_config():
    """统一 target 配置:注册表直出(generators/providers/default target);凭证不出现。

    generator → generator_config 契约:configKind 分 file(cc/dsh/codex,configDefault/
    configPathEnv)/object(llm,providers 列表);defaultTarget.generator_config 按默认
    generator 的 kind 给出(cc = config_path)。
    """
    cfg = _client().get("/generators/config").json()
    assert [g["id"] for g in cfg["generators"]] == ["cc", "dsh", "codex", "llm"]
    by_id = {g["id"]: g for g in cfg["generators"]}
    assert by_id["cc"]["configKind"] == "file"
    assert by_id["dsh"]["configKind"] == "file"
    assert by_id["codex"]["configKind"] == "file"
    assert by_id["llm"]["configKind"] == "object"
    assert by_id["cc"]["configDefault"] is not None  # ~/.claude/settings.json
    assert by_id["cc"]["configPathEnv"] == "DEEPWIKI_CC_CONFIG"
    assert by_id["cc"]["providers"] == []  # file 类不再暴露 provider 选择
    assert by_id["llm"]["providers"] == ["openai"]
    assert by_id["codex"]["capability"] == "responses"
    prov = {p["id"]: p for p in cfg["providers"]}
    assert prov["openai"]["apiKeyEnv"] == "OPENAI_API_KEY"
    assert prov["openai"]["baseUrlDefault"] == "https://api.openai.com/v1"
    assert cfg["defaultGenerator"] == "cc"
    assert cfg["defaultTarget"]["generator"] == "cc"
    # file 类默认 generator → generator_config = {"config_path": ...}
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
        "/repo/index/status", params={"repo_url": str(raw_create), "type": "local"}
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
    """POST /wiki/tasks 契约(server registry):created 提交 → 后台调度 → GET 轮询至 completed。"""
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
    time.sleep(0.3)  # 让 TTL 移除计时器自然收尾,不留终端任务


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


# ---------------------------------------------------------------------------
# /repo/prepare:本地小仓库真实建图(SSE 事件流 + graph.json 落盘)
# ---------------------------------------------------------------------------


def test_prepare_local_repo(tmp_path):
    raw = _write_corpus(tmp_path)
    r = _client().post("/repo/prepare", json={"repo_url": raw, "type": "local"})
    assert r.status_code == 200
    assert "event: done" in r.text
    assert "data: ok" in r.text
    # 图产物在 DEEPWIKI_ROOT/graphify/<repo_key>/graph.json,源目录零残留
    assert _graph_path(Repo(raw, "local")).exists()
    assert not os.path.exists(os.path.join(raw, "graphify-out"))
    # 索引后就绪探针翻转
    assert _client().get(
        "/repo/index/status", params={"repo_url": raw, "type": "local"}
    ).json() == {"ready": True}


def test_prepare_idempotent(tmp_path):
    """已索引再次 prepare → ready 事件短路,不重跑 extract。"""
    raw = _write_corpus(tmp_path)
    c = _client()
    c.post("/repo/prepare", json={"repo_url": raw, "type": "local"})
    r2 = c.post("/repo/prepare", json={"repo_url": raw, "type": "local"})
    assert "event: ready" in r2.text
