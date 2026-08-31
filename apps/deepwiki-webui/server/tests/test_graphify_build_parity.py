"""从 graphify 库测试改编的建图 parity 用例。

源(对照):graphify/tests/test_relation_collapse_precedence.py、
test_build.py(方向块 #760/#1061/#2342)、test_dedup.py(纯函数块)、
test_merge_graphs_cli.py(#2261 方向标记)、test_query_cli.py(calls 方向渲染)。
断言的是 graphify 库在建图/导出/方向保留上的契约 —— graphify_wrapper 的
extract 路径必须与之逐点一致(方向性事实:extract 链从不传 directed,
构建无向 nx.Graph,方向真相经 _src/_tgt → 端点序保留)。
"""

import json
from pathlib import Path

import pytest
from graphify.build import build, build_from_json, build_merge, edge_data
from graphify.dedup import deduplicate_entities
from graphify.export import to_json
from graphify.extract import extract_js

from graphify_wrapper import _load_graph, extract, query

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# A. 边塌缩优先级(源:test_relation_collapse_precedence.py,逐字改编)
# ---------------------------------------------------------------------------

SPECIFIC = ["calls", "imports", "imports_from", "inherits", "implements",
            "method", "indirect_call", "re_exports", "contains"]
GENERIC = ["references", "uses", "mentions"]


def _extraction(edges):
    return {
        "nodes": [
            {"id": "a", "label": "a()", "file_type": "code", "source_file": "a.py"},
            {"id": "b", "label": "b()", "file_type": "code", "source_file": "b.py"},
        ],
        "edges": edges,
        "hyperedges": [],
    }


def _edge(rel, src="a", tgt="b", **kw):
    return {"source": src, "target": tgt, "relation": rel,
            "confidence": "EXTRACTED", **kw}


def _relation(G):
    return edge_data(G, "a", "b").get("relation")


@pytest.mark.parametrize("generic", GENERIC)
@pytest.mark.parametrize("specific", SPECIFIC)
@pytest.mark.parametrize("order", ["specific_first", "generic_first"])
def test_generic_never_overwrites_specific(specific, generic, order):
    pair = [_edge(specific), _edge(generic)]
    if order == "generic_first":
        pair.reverse()
    G = build_from_json(_extraction(pair))
    assert _relation(G) == specific, (
        f"{generic!r} overwrote {specific!r} when added {order.replace('_', ' ')}")


def test_the_reported_case_keeps_calls():
    G = build_from_json(_extraction([
        _edge("calls", source_location="L10"),
        _edge("references", source_location="L10"),
    ]))
    assert _relation(G) == "calls"


def test_reverse_direction_guard_still_holds():
    """#1061: 同对点同 relation 但反向 —— 先见方向胜出。"""
    G = build_from_json(_extraction([
        _edge("calls", src="a", tgt="b"),
        _edge("calls", src="b", tgt="a"),
    ]))
    d = edge_data(G, "a", "b")
    assert (d.get("_src"), d.get("_tgt")) == ("a", "b")


def test_direction_metadata_survives_the_demotion():
    """幸存边保留 specific 边自己的方向与属性,而非被降级边的。"""
    G = build_from_json(_extraction([
        _edge("calls", src="a", tgt="b", source_location="L1"),
        _edge("references", src="b", tgt="a", source_location="L2"),
    ]))
    d = edge_data(G, "a", "b")
    assert d.get("relation") == "calls"
    assert (d.get("_src"), d.get("_tgt")) == ("a", "b")
    assert d.get("source_location") == "L1"


def test_collapse_still_yields_exactly_one_edge():
    G = build_from_json(_extraction([
        _edge("calls"), _edge("references"), _edge("uses"), _edge("calls"),
    ]))
    assert G.number_of_edges() == 1
    assert G.number_of_nodes() == 2


# ---------------------------------------------------------------------------
# B. 方向性事实(extract 链缺省无向;directed=True 才是 DiGraph)
# ---------------------------------------------------------------------------


def test_build_default_is_undirected_graph():
    G = build_from_json(_extraction([_edge("calls")]))
    assert type(G).__name__ == "Graph"
    assert G.is_directed() is False
    d = edge_data(G, "a", "b")
    assert d.get("_src") == "a" and d.get("_tgt") == "b"  # 标记无条件写


def test_build_directed_mode_is_digraph():
    G = build_from_json(_extraction([_edge("calls")]), directed=True)
    assert G.is_directed() is True


# ---------------------------------------------------------------------------
# C. #760/#2342:build → to_json → build_merge 往返后方向不翻转
# ---------------------------------------------------------------------------


def test_build_merge_preserves_call_edge_direction(tmp_path):
    """#760:被调方先于调用方定义时,无向存储会把边迭代成 (b, a),导出必须

    以 _src/_tgt 弹回真实端点序(a calls b)。
    """
    src_file = tmp_path / "x.js"
    src_file.write_text("function b() {}\nfunction a() { b(); }\n", encoding="utf-8")
    extraction = extract_js(src_file)
    assert "error" not in extraction
    call_edges = [e for e in extraction["edges"] if e["relation"] == "calls"]
    assert len(call_edges) == 1
    truth_src, truth_tgt = call_edges[0]["source"], call_edges[0]["target"]

    G1 = build([extraction], dedup=False)
    graph_path = tmp_path / "graph.json"
    assert to_json(G1, {}, str(graph_path), force=True)
    saved = json.loads(graph_path.read_text(encoding="utf-8"))
    saved_calls = [e for e in saved.get("links", []) if e.get("relation") == "calls"]
    assert saved_calls[0]["source"] == truth_src
    assert saved_calls[0]["target"] == truth_tgt
    # 模拟 update:warm 无新 chunk 的 build_merge 再回读
    G2 = build_merge([], graph_path, dedup=False)
    assert to_json(G2, {}, str(graph_path), force=True)
    reloaded = json.loads(graph_path.read_text(encoding="utf-8"))
    reloaded_calls = [e for e in reloaded.get("links", []) if e.get("relation") == "calls"]
    assert reloaded_calls[0]["source"] == truth_src, "calls 端点翻转!"
    assert reloaded_calls[0]["target"] == truth_tgt


def test_build_merge_inherits_directed_flag_from_disk(tmp_path):
    """#2342:build_merge 不带 directed= 时继承磁盘图的 directed 标志。"""
    ext = {
        "nodes": [{"id": "a", "label": "a", "file_type": "concept",
                   "source_file": "x.md", "source_location": "L1"}],
        "edges": [],
    }
    graph_path = tmp_path / "graph.json"
    G1 = build([ext], directed=True, dedup=False)
    assert to_json(G1, {}, str(graph_path), force=True)
    assert build_merge([], graph_path, dedup=False).is_directed() is True
    G3 = build([ext], directed=False, dedup=False)
    assert to_json(G3, {}, str(graph_path), force=True)
    assert build_merge([], graph_path, dedup=False).is_directed() is False


# ---------------------------------------------------------------------------
# D. 封装整链方向保留:extract 产物 graph.json 方向 + 增量二跑后标记仍在(#2261 精神)
# ---------------------------------------------------------------------------


def _js_corpus(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "x.js").write_text("function b() {}\nfunction a() { b(); }\n", encoding="utf-8")
    (d / "y.js").write_text("function d() {}\nfunction c() { d(); }\n", encoding="utf-8")
    return d


def test_extract_graph_direction_pipeline(tmp_path):
    """整链:extract → graph.json 是无向存储(directed: false)但 calls 端点

    顺序即真实调用方向(无向存储 + _src/_tgt 弹回)。
    """
    corpus = _js_corpus(tmp_path / "repo")
    r = extract(corpus, code_only=True)
    assert r["error"] is None
    raw = json.loads(Path(r["graph_json"]).read_text(encoding="utf-8"))
    assert raw["directed"] is False  # 方向性事实:extract 链从不传 directed
    labels = {n["id"]: n.get("label", "") for n in raw["nodes"]}
    calls = [ln for ln in raw["links"] if ln.get("relation") == "calls"]
    assert len(calls) == 2
    for link in calls:
        assert labels[link["source"]].startswith("a") or labels[link["source"]].startswith("c")
        assert labels[link["target"]].startswith("b") or labels[link["target"]].startswith("d")


def test_extract_warm_keeps_directional_markers(tmp_path):
    """#2261 精神:增量二跑(改文件)后,方向标记仍在——_load_graph 的

    preserve_direction 回填 _src/_tgt,且端点真实有序。
    """
    corpus = _js_corpus(tmp_path / "repo")
    r1 = extract(corpus, code_only=True)
    assert r1["error"] is None
    (corpus / "x.js").write_text(
        "function b() {}\nfunction a() { b(); }\nfunction e() {}\n",
        encoding="utf-8",
    )
    r2 = extract(corpus, code_only=True)
    assert r2["error"] is None and r2["incremental"] is True
    G, data = _load_graph(r2["graph_json"], preserve_direction=True)
    labels = {i: G.nodes[i].get("label", i) for i in G.nodes}
    calls = [ln for ln in data["links"] if ln.get("relation") == "calls"]
    assert len(calls) == 2
    for link in calls:
        assert link["_src"] == link["source"] and link["_tgt"] == link["target"]
        assert labels[link["source"]].startswith("a") or labels[link["source"]].startswith("c")


# ---------------------------------------------------------------------------
# E. to_json 形状(来自 wrapper 产物)+ 确定性
# ---------------------------------------------------------------------------


def test_extract_graph_json_shape(tmp_path):
    corpus = _js_corpus(tmp_path / "repo")
    r = extract(corpus, code_only=True)
    assert r["error"] is None
    raw = json.loads(Path(r["graph_json"]).read_text(encoding="utf-8"))
    assert set(raw) >= {"directed", "multigraph", "graph", "nodes", "links", "hyperedges"}
    assert list(raw["links"][0])[:3] == ["source", "target", "relation"]  # 规范键序
    assert list(raw["nodes"][0])[:2] == ["id", "label"]
    assert raw["hyperedges"] == []
    assert raw["graph"] is not None


def test_extract_deterministic_bytes(tmp_path):
    """同一语料两次 fresh 提取到不同 out_dir:graph.json 字节级一致

    (非 git 语料 → built_at_commit 两侧均缺省)。
    """
    corpus = _js_corpus(tmp_path / "repo")
    r1 = extract(corpus, code_only=True, out_dir=str(tmp_path / "out1"))
    r2 = extract(corpus, code_only=True, out_dir=str(tmp_path / "out2"))
    assert r1["error"] is None and r2["error"] is None
    assert Path(r1["graph_json"]).read_bytes() == Path(r2["graph_json"]).read_bytes()


def test_build_from_fixture_extraction_json():
    """fixtures/extraction.json(源 graphify/tests)直接喂 build_from_json。"""
    ext = json.loads((_FIXTURES / "extraction.json").read_text(encoding="utf-8"))
    G = build_from_json(ext)
    assert G.number_of_nodes() == 4
    assert G.number_of_edges() == 4


# ---------------------------------------------------------------------------
# F. dedup 纯函数块(源:test_dedup.py 基础用例)
# ---------------------------------------------------------------------------


def _make_nodes(*labels):
    return [{"id": label.lower().replace(" ", "_"), "label": label,
             "source_file": "test.md"} for label in labels]


def test_exact_duplicates_merged():
    nodes = _make_nodes("UserService", "userservice", "User Service")
    result_nodes, _ = deduplicate_entities(nodes, [], communities={})
    assert len(result_nodes) == 1


def test_typo_merged():
    result_nodes, _ = deduplicate_entities(
        _make_nodes("GraphExtractor", "Graph Extractor"), [], communities={})
    assert len(result_nodes) == 1


def test_unrelated_not_merged():
    result_nodes, _ = deduplicate_entities(
        _make_nodes("UserService", "OrderService"), [], communities={})
    assert len(result_nodes) == 2


def test_short_low_entropy_not_merged():
    result_nodes, _ = deduplicate_entities(
        _make_nodes("AI", "ML"), [], communities={})
    assert len(result_nodes) == 2


def test_edges_rewired_after_merge():
    nodes = _make_nodes("GraphExtractor", "Graph Extractor", "Parser")
    edges = [{"source": "graph_extractor", "target": "parser", "relation": "uses"}]
    result_nodes, result_edges = deduplicate_entities(nodes, edges, communities={})
    assert len(result_nodes) == 2
    assert len(result_edges) == 1  # loser 的边重连到 winner


def test_self_loops_dropped_after_merge():
    nodes = _make_nodes("GraphExtractor", "Graph Extractor")
    edges = [{"source": "graphextractor", "target": "graph_extractor", "relation": "same"}]
    _, result_edges = deduplicate_entities(nodes, edges, communities={})
    assert result_edges == []


# ---------------------------------------------------------------------------
# G. query 方向渲染(源:test_query_cli.py calls 方向用例,经 query() 封装)
# ---------------------------------------------------------------------------


def _write_calls_graph(tmp_path: Path) -> Path:
    import networkx as nx
    from networkx.readwrite import json_graph

    G = nx.Graph()
    G.add_node("caller", label="caller_fn", source_file="a.py",
               source_location="L1", community=0)
    G.add_node("callee", label="callee_fn", source_file="b.py",
               source_location="L1", community=1)
    G.add_edge("caller", "callee", relation="calls", confidence="EXTRACTED",
               context="call")
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(json_graph.node_link_data(G, edges="links")),
                  encoding="utf-8")
    return gp


@pytest.mark.parametrize("seed", ["callee_fn", "caller_fn"])
def test_query_renders_calls_caller_to_callee(tmp_path, seed):
    """query 从被调方或调用方种子出发,渲染都是 caller_fn -> callee_fn

    (种子在被调方时,无向遍历仍须按端点方向渲染 —— #2213/#2309 精神)。
    """
    r = query(seed, graph_path=str(_write_calls_graph(tmp_path)), token_budget=2000)
    assert r["error"] is None
    assert "caller_fn --calls" in r["answer"]
    assert "callee_fn --calls" not in r["answer"]
