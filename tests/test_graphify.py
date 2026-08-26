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


def test_extract_no_cluster(tmp_path):
    """no_cluster 全量路径。必须用独立语料:模块级 corpus 已被 graph_json fixture
    建图,温跑会落入"无变化早退"(skipped)分支而不是全量 raw 写。"""
    corpus = _tiny_corpus(tmp_path / "repo")
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


# ---------------------------------------------------------------------------
# cache_root_final:out_dir=最终目录时缓存落 <out_dir>/cache(960ab73 遗留 bug 回归)
# ---------------------------------------------------------------------------


def _tiny_corpus(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "app.py").write_text("def main():\n    return 42\n", encoding="utf-8")
    (d / "util.py").write_text("def util():\n    return 1\n", encoding="utf-8")
    return d


def test_extract_out_dir_cache_layout(tmp_path):
    """给出 out_dir(=最终输出目录)时:graph.json 在 out_dir 下,缓存落 <out_dir>/cache,
    不再出现 <out_dir>/graphify-out 层;二次运行命中新位置缓存(真实 lib,AST 本地无费用)。"""
    corpus = _tiny_corpus(tmp_path / "repo")
    out = tmp_path / "out"
    r = extract(corpus, code_only=True, out_dir=str(out))
    assert r["error"] is None
    assert Path(r["graph_json"]).parent == out
    assert (out / "cache").is_dir()
    assert not (out / "graphify-out").exists()
    assert not (corpus / "graphify-out").exists()
    # 二跑命中 <out>/cache 的 AST 缓存(无语义文件,纯本地)
    r2 = extract(corpus, code_only=True, out_dir=str(out))
    assert r2["error"] is None
    assert any((out / "cache").rglob("*.json"))


def test_extract_default_cache_layout_unchanged(tmp_path):
    """未给出 out_dir 时行为不变:缓存仍在 <target>/graphify-out/cache。"""
    corpus = _tiny_corpus(tmp_path / "repo")
    r = extract(corpus, code_only=True)
    assert r["error"] is None
    assert (corpus / "graphify-out" / "cache").is_dir()
    assert Path(r["graph_json"]).parent == corpus / "graphify-out"


# ---------------------------------------------------------------------------
# 增量语义（与 CLI 同式：graph.json 存在且非 force 即走 detect_incremental +
# build_merge / merge_raw_extraction + save_manifest）
# ---------------------------------------------------------------------------


def test_extract_warm_incremental_no_change(tmp_path):
    """温跑未变:incremental=True,图内容一致,manifest 已生成。"""
    corpus = _tiny_corpus(tmp_path / "repo")
    r1 = extract(corpus, code_only=True)
    assert r1["error"] is None and r1["incremental"] is False
    gout = Path(r1["graph_json"]).parent
    assert (gout / "manifest.json").exists()
    r2 = extract(corpus, code_only=True)
    assert r2["error"] is None
    assert r2["incremental"] is True and r2["skipped"] is False
    assert r2["nodes"] == r1["nodes"] and r2["edges"] == r1["edges"]
    assert (gout / "manifest.json").exists()


def test_extract_warm_incremental_changed_file(tmp_path):
    """改文件:该文件的旧节点按 tier 替换,无重复。"""
    corpus = _tiny_corpus(tmp_path / "repo")
    r1 = extract(corpus, code_only=True)
    assert r1["error"] is None
    (corpus / "app.py").write_text(
        "import util\n\ndef main():\n    return util.hello('x')\n\n\n# extra\ndef extra():\n    return 1\n",
        encoding="utf-8",
    )
    r2 = extract(corpus, code_only=True)
    assert r2["error"] is None and r2["incremental"] is True
    raw = json.loads(Path(r2["graph_json"]).read_text(encoding="utf-8"))
    ids = [n["id"] for n in raw["nodes"]]
    assert len(ids) == len(set(ids))  # 无重复节点
    assert r2["files"]["code"] == 1  # 只重提取了改动的 app.py
    assert r2["files_unchanged"] == 1  # util.py 未变


def test_extract_warm_deleted_file_pruned(tmp_path):
    """删文件:节点按 deleted+stale 双通道剪除。"""
    corpus = _tiny_corpus(tmp_path / "repo")
    r1 = extract(corpus, code_only=True)
    assert r1["error"] is None
    n1 = r1["nodes"]
    (corpus / "app.py").unlink()
    r2 = extract(corpus, code_only=True)
    assert r2["error"] is None and r2["incremental"] is True
    raw = json.loads(Path(r2["graph_json"]).read_text(encoding="utf-8"))
    sfs = {n.get("source_file") or "" for n in raw["nodes"]}
    assert not any(s.endswith("app.py") for s in sfs)
    assert len(raw["nodes"]) < n1


def test_extract_no_cluster_warm_no_change(tmp_path):
    """no_cluster 温跑未变:早退 skipped=True,graph.json 未被重写。"""
    corpus = _tiny_corpus(tmp_path / "repo")
    r1 = extract(corpus, code_only=True, no_cluster=True)
    assert r1["error"] is None
    gp = Path(r1["graph_json"])
    mtime = gp.stat().st_mtime
    r2 = extract(corpus, code_only=True, no_cluster=True)
    assert r2["error"] is None and r2["skipped"] is True
    assert r2["nodes"] == r1["nodes"]
    assert gp.stat().st_mtime == mtime  # 未重写


def test_extract_manifest_missing_warm_run_succeeds(tmp_path):
    """#1925:manifest.json 缺失不关闭增量——旧图作为基线,节点保留、不空图。"""
    corpus = _tiny_corpus(tmp_path / "repo")
    r1 = extract(corpus, code_only=True)
    assert r1["error"] is None
    gout = Path(r1["graph_json"]).parent
    (gout / "manifest.json").unlink()
    r2 = extract(corpus, code_only=True)
    assert r2["error"] is None and r2["incremental"] is True
    raw = json.loads(Path(r2["graph_json"]).read_text(encoding="utf-8"))
    assert len(raw["nodes"]) == r1["nodes"]


def test_extract_force_full_rescan(tmp_path):
    """force:关闭增量门并跳过语义缓存读（同 CLI --force / GRAPHIFY_FORCE）。"""
    corpus = _tiny_corpus(tmp_path / "repo")
    r1 = extract(corpus, code_only=True)
    assert r1["error"] is None
    r2 = extract(corpus, code_only=True, force=True)
    assert r2["error"] is None and r2["incremental"] is False
    assert r2["files"]["code"] == 2  # 全量重分类


def test_extract_force_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_FORCE", "1")
    corpus = _tiny_corpus(tmp_path / "repo")
    r1 = extract(corpus, code_only=True)
    assert r1["error"] is None
    r2 = extract(corpus, code_only=True)
    assert r2["error"] is None and r2["incremental"] is False  # env 强制全量


def test_extract_no_dedup_maps_build_merge_valueerror(tmp_path, monkeypatch):
    """build_merge 的 #479 ValueError（仅 --no-dedup 时 arm）映射为 error dict，
    旧图不被写（镜像 cli.py:3977-3983）。必须 patch 本模块的 build_merge 别名。
    """
    corpus = _tiny_corpus(tmp_path / "repo")
    r1 = extract(corpus, code_only=True)
    assert r1["error"] is None
    gp = Path(r1["graph_json"])
    before = gp.read_text(encoding="utf-8")

    import gh_puller.graphify as gfx

    def _boom(*a, **kw):
        raise ValueError(
            "graphify: build_merge would drop 1 node(s) from sources that were "
            "neither re-extracted nor pruned this run (#479)"
        )

    monkeypatch.setattr(gfx, "build_merge", _boom)
    r2 = extract(corpus, code_only=True, no_dedup=True)
    assert r2["error"] is not None and "#479" in r2["error"]
    assert gp.read_text(encoding="utf-8") == before  # 旧图完好


def test_extract_no_dedup_passes_dedup_false(monkeypatch, tmp_path):
    """--no-dedup 透传 build(dedup=False)。spy 必须 patch 本模块的 build 别名
    （import 绑定早于 monkeypatch，patch graphify.build.build 无效）。"""
    corpus = _tiny_corpus(tmp_path / "repo")
    import gh_puller.graphify as gfx

    calls = {}
    real = gfx.build

    def spy(*a, **kw):
        calls["kw"] = kw
        return real(*a, **kw)

    monkeypatch.setattr(gfx, "build", spy)
    r = extract(corpus, code_only=True, no_dedup=True)
    assert r["error"] is None and calls["kw"]["dedup"] is False


def test_extract_allow_partial_ast_failure(monkeypatch, tmp_path):
    """#2445:整段 AST 丢失默认致命(错误态、不写图);allow_partial 才降级续跑。"""
    corpus = _tiny_corpus(tmp_path / "repo")
    import gh_puller.graphify as gfx

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(gfx, "_ast_extract", _boom)
    r = extract(corpus, code_only=True)
    assert r["error"] is not None
    assert not (corpus / "graphify-out" / "graph.json").exists()
    # allow_partial 下 AST 空 → 纯代码语料 0 节点 → 空图错误（与 CLI exit 1 等价）
    r2 = extract(corpus, code_only=True, allow_partial=True)
    assert r2["error"] is not None and "empty" in r2["error"]


def test_extract_build_config_persistence(tmp_path):
    """#1971:--exclude 持久化到 .graphify_build.json,warm 无 flag 运行继承排除。"""
    corpus = _tiny_corpus(tmp_path / "repo")
    r = extract(corpus, code_only=True, extra_excludes=["util.py"])
    assert r["error"] is None
    cfg = corpus / "graphify-out" / ".graphify_build.json"
    assert cfg.exists()
    assert json.loads(cfg.read_text(encoding="utf-8"))["excludes"] == ["util.py"]
    r2 = extract(corpus, code_only=True)  # 不传 → 继承 ["util.py"]
    assert r2["error"] is None
    raw = json.loads(Path(r2["graph_json"]).read_text(encoding="utf-8"))
    assert not any((n.get("source_file") or "").endswith("util.py")
                   for n in raw["nodes"])
