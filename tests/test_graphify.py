"""gh_puller.graphify 封装层的本地集成测试。

不 mock graphify 库：用真实库在 tmp_path 临时语料上跑本地流水线
（code_only，无 LLM 成本、无网络）。覆盖 extract 主链路与降级、export 五种
格式、query，以及复刻 CLI 归一逻辑的私有辅助函数（_load_graph / _out_name）。
"""

import json
from pathlib import Path

import pytest

from gh_puller.graphify import _default_graph_path, _load_graph, export, extract, query

_HTML = "<!DOCTYPE html>"  # html/tree/callflow-html 共同标记


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    d = tmp_path_factory.mktemp("corpus")
    (d / "app.py").write_text("import utils\n\n\ndef main():\n    return utils.hello('x')\n", encoding="utf-8")
    (d / "utils.py").write_text("def hello(name):\n    return f'hi {name}'\n", encoding="utf-8")
    (d / "README.md").write_text("# Demo\n", encoding="utf-8")  # 语义文件：code_only 应跳过
    return d


@pytest.fixture(scope="module")
def graph_json(corpus):
    """模块级共享：一次 code_only 提取供 export/query 用例复用。"""
    r = extract(corpus, code_only=True)
    assert r["error"] is None
    return r["graph_json"]


def test_extract_code_only(corpus):
    r = extract(corpus, code_only=True)
    assert r["error"] is None
    assert r["incomplete"] is False
    assert r["nodes"] > 0 and r["edges"] > 0 and r["communities"] > 0
    assert r["files"] == {"code": 2, "document": 0, "paper": 0, "image": 0}
    assert r["tokens"] == {"input": 0, "output": 0, "cost_usd": 0.0}
    assert Path(r["graph_json"]).exists()
    assert Path(r["analysis_json"]).exists()


def test_extract_missing_path_raises(tmp_path):
    # 前置校验直接上抛（非降级 error dict）
    with pytest.raises(FileNotFoundError):
        extract(tmp_path / "nope")


def test_extract_no_cluster(corpus):
    r = extract(corpus, code_only=True, no_cluster=True)
    assert r["error"] is None
    assert r["analysis_json"] is None
    assert r["nodes"] > 0 and r["edges"] > 0
    assert Path(r["graph_json"]).exists()


_SUFFIX = {"html": "html", "svg": "svg", "tree": "html", "callflow-html": "html"}


@pytest.mark.parametrize("fmt", ["html", "svg", "tree", "callflow-html"])
def test_export_formats(fmt, graph_json, tmp_path):
    out = tmp_path / f"out-{fmt}.{_SUFFIX[fmt]}"
    r = export(fmt, graph_path=graph_json, output=str(out))
    assert r["error"] is None
    if fmt in ("html", "svg"):
        assert r["nodes"] > 0 and r["edges"] > 0  # tree/callflow-html 不报计数
    assert r["output"] == str(out)
    body = out.read_text(encoding="utf-8")
    assert ("<!DOCTYPE svg" if fmt == "svg" else _HTML) in body


def test_export_falkordb_cypher(graph_json):
    r = export("falkordb", graph_path=graph_json)
    assert r["error"] is None
    assert r["pushed_nodes"] is None
    cypher = Path(r["output"])
    assert cypher.name == "cypher.txt" and cypher.stat().st_size > 0
    assert "MERGE" in cypher.read_text(encoding="utf-8")


def test_export_missing_graph(tmp_path):
    # 与 query 不同：export 降级为 error dict，不抛异常
    r = export("html", graph_path=str(tmp_path / "nope.json"))
    assert r["error"] is not None
    assert r["error"].startswith("FileNotFoundError")


def test_query_local(graph_json):
    r = query("app.py 是如何工作的", graph_path=graph_json, token_budget=1000)
    assert r["error"] is None
    assert isinstance(r["answer"], str) and r["answer"]
    assert r["graph_nodes"] > 0
    assert r["mode"] == "bfs" and r["depth"] == 2


def test_query_missing_graph_raises(tmp_path):
    # query 前置校验（图缺失/非 json/超限）抛内置异常，与 export 的降级语义区分
    with pytest.raises(FileNotFoundError):
        query("问个问题", graph_path=str(tmp_path / "nope.json"))


def test_load_graph_normalizes_edges(tmp_path):
    # 旧图 "edges" 键归一为 "links"；已存在的 _src/_tgt 标记不被覆盖（#2309）
    raw = {
        "nodes": [],
        "edges": [{"source": "a", "target": "b", "label": "uses", "_src": "s", "_tgt": "t"}],
    }
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(raw), encoding="utf-8")
    G, data = _load_graph(gp, preserve_direction=True)
    # 归一化是补 "links" 键（原 "edges" 键保留），不删除
    assert len(data["links"]) == 1
    assert data["links"][0]["_src"] == "s" and data["links"][0]["_tgt"] == "t"
    assert G.has_edge("a", "b")


def test_load_graph_direction_fallback(tmp_path):
    # 无标记时 _src/_tgt 回填为 source/target
    raw = {"nodes": [], "links": [{"source": "a", "target": "b"}]}
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(raw), encoding="utf-8")
    _, data = _load_graph(gp, preserve_direction=True)
    assert data["links"][0]["_src"] == "a" and data["links"][0]["_tgt"] == "b"


def test_load_graph_suffix_validation(tmp_path):
    gp = tmp_path / "graph.xml"
    gp.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        _load_graph(gp)


def test_default_graph_path_env(monkeypatch):
    monkeypatch.delenv("GRAPHIFY_OUT", raising=False)
    assert _default_graph_path() == Path.cwd() / "graphify-out" / "graph.json"


def test_default_graph_path_env_override(monkeypatch):
    # env 覆盖生效：封装的默认路径自行读 env，不依赖 graphify.paths 的导入时快照
    monkeypatch.setenv("GRAPHIFY_OUT", "custom-out")
    assert _default_graph_path() == Path.cwd() / "custom-out" / "graph.json"
