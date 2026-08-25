"""core/worker 离线测试:真实 graphify.extract(code_only=True) 纯本地建图(零 token、零网络)。

头部按 gh-puller 惯例:导入任何 gh_puller 模块前不碰 ANTHROPIC 相关 env(本模块链路无 LLM);
envs 为导入时快照,运行期经 monkeypatch.setattr(gh_puller_envs, "DEEPWIKI_ROOT", tmp) 变更。
"""

import asyncio
import json

import pytest
from gh_puller import envs

from gh_puller_dsh import core, worker


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """微型代码库(同 tests/test_graphify.py 形状):README.md 应被 code_only 跳过。"""
    d = tmp_path_factory.mktemp("corpus")
    (d / "app.py").write_text("import utils\n\n\ndef main():\n    return utils.hello('x')\n", encoding="utf-8")
    (d / "utils.py").write_text("def hello(name):\n    return f'hi {name}'\n", encoding="utf-8")
    (d / "README.md").write_text("# Demo\n", encoding="utf-8")
    return d


@pytest.fixture
def root(tmp_path, monkeypatch):
    """graph 产物根(tmp):所有图都落这里,不写用户 ~/.gh-puller。"""
    monkeypatch.setattr(envs, "DEEPWIKI_ROOT", str(tmp_path))
    return tmp_path


def test_repo_dir_naming(root):
    assert core.repo_dir("https://github.com/o/r.git", None).name == "github_o_r"
    assert core.repo_dir("https://gitlab.io/group/proj.git", "gitlab").name == "gitlab_group_proj"
    assert core.repo_dir("/x/y", None).name == "local_y"


def test_index_roundtrip(corpus, root):
    text = core.index_text(str(corpus), None)
    assert "indexed" in text and "graph.json" in text
    assert (root / "graphify" / f"local_{corpus.name}" / "graph.json").exists()


def test_query_after_index(corpus, root):
    core.index_text(str(corpus), None)
    out = core.query_text("main", str(corpus), None, None)
    assert out and "main" in out and "hello" in out


def test_query_no_graph_configured():
    out = core.query_text("问题", None, None, None)
    assert "No graph configured" in out


def test_query_missing_graph(corpus, root):
    out = core.query_text("问题", str(corpus), None, None)
    assert out.startswith("Graph query failed: FileNotFoundError")


def test_index_missing_path():
    out = core.index_text("/nonexistent/repo", None)
    assert "path not found" in out


def test_handle_line_query(corpus, root):
    core.index_text(str(corpus), None)
    line = json.dumps({"id": 1, "action": "query", "question": "main", "repo": str(corpus)})
    resp = json.loads(worker.handle_line(line))
    assert resp["ok"] is True and "main" in resp["text"]


def test_handle_line_unknown_action():
    resp = json.loads(worker.handle_line(json.dumps({"id": 7, "action": "boom"})))
    assert resp == {"id": 7, "ok": False, "error": "unknown action: 'boom'"}


def test_handle_line_bad_json_ignored():
    assert worker.handle_line("{not json") is None
    assert worker.handle_line("") is None


def test_loop_pumps_and_exits_on_eof(corpus, root, tmp_path):
    """协议主循环端到端(假 stdin):build 请求→响应行→EOF 退出,无真实子进程。"""
    core.index_text(str(corpus), None)
    lines = [
        json.dumps({"id": 1, "action": "query", "question": "main", "repo": str(corpus)}) + "\n",
        json.dumps({"id": 2, "action": "nope"}) + "\n",
    ]
    iterator = iter(lines)
    written: list[str] = []

    def readline() -> str:
        return next(iterator, "")

    asyncio.run(worker._loop(readline, written.append))
    assert len(written) == 2
    assert json.loads(written[0])["ok"] is True
    assert json.loads(written[1])["ok"] is False
