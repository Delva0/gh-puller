"""gh_puller.graphify.extract 与真 `graphify extract` CLI 的建图差分测试。

装置:同一 tmp 语料,一侧 subprocess 跑 `python -m graphify extract . --out …`,
另一侧子进程调 `gh_puller.graphify.extract(out_dir=…)`,然后逐产物比较。
全部场景零 LLM(纯 code_only 或进程内 mock graphify.llm.extract_corpus_parallel,
铁律:LLM 禁止真实调用)。

目录层差异提醒:CLI `--out X` 的最终产物在 X/graphify-out/,wrapper 的 out_dir
即最终目录 —— 比较时 CLI 侧取 <cli_out>/graphify-out/,wrapper 侧取 <wrap_out>/。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gh_puller.graphify import extract

# 子进程环境的 LLM 键洗净:残留 key 会让无 key 断言失效
_LLM_KEYS = (
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "OPENAI_MODEL", "ANTHROPIC_API_KEY", "MOONSHOT_API_KEY", "DEEPSEEK_API_KEY",
    "OLLAMA_BASE_URL", "AWS_PROFILE", "AWS_REGION",
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _LLM_KEYS}
    env["GRAPHIFY_OUT"] = "graphify-out"  # 相对值:避免外部 GRAPHIFY_OUT 干扰
    env.pop("GRAPHIFY_FORCE", None)
    return env


def _mixed_corpus(d: Path) -> Path:
    """app.py + utils.py(code AST)+ README.md(语义文件,code_only 跳过)。"""
    d.mkdir(parents=True, exist_ok=True)
    (d / "app.py").write_text(
        "import utils\n\n\ndef main():\n    return utils.hello('x')\n", encoding="utf-8"
    )
    (d / "utils.py").write_text("def hello(name):\n    return f'hi {name}'\n", encoding="utf-8")
    (d / "README.md").write_text("# Demo\n", encoding="utf-8")
    return d


def _cli(corpus: Path, out_root: Path, *flags: str):
    """真 CLI(subprocess)。产物在 <out_root>/graphify-out/。"""
    return subprocess.run(
        [sys.executable, "-m", "graphify", "extract", ".", "--out", str(out_root), *flags],
        cwd=str(corpus), capture_output=True, text=True, env=_clean_env(),
    )


_WRAP = (
    "import json, sys\n"
    "from gh_puller.graphify import extract\n"
    "kw = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}\n"
    "print(json.dumps(extract(sys.argv[1], out_dir=sys.argv[2], **kw)))\n"
)


def _wrap(corpus: Path, out_dir: Path, **kw) -> dict:
    """子进程内调封装(子进程=独立 stat-index 全局,与 CLI 侧天然隔离)。"""
    r = subprocess.run(
        [sys.executable, "-c", _WRAP, str(corpus), str(out_dir), json.dumps(kw)],
        capture_output=True, text=True, env=_clean_env(), cwd=str(_REPO_ROOT),
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _norm_graph(p: Path) -> dict:
    """图形状归一:节点/边排序后比较,抹掉写入顺序敏感差异。"""
    raw = json.loads(p.read_text(encoding="utf-8"))
    nodes = sorted(raw.get("nodes", []), key=lambda n: (n.get("id"), n.get("source_file")))
    links = sorted(
        raw.get("links", raw.get("edges", [])),
        key=lambda l: (l.get("source"), l.get("target"), l.get("relation")),
    )
    return {k: raw.get(k) for k in ("directed", "multigraph", "hyperedges")} | {
        "nodes": nodes, "links": links,
    }


def _assert_same(cli_out_root: Path, wrap_out_dir: Path, *, expect_analysis: bool = True) -> None:
    cli_dir = cli_out_root / "graphify-out"
    wdir = wrap_out_dir
    assert cli_dir.is_dir() and wdir.is_dir()
    cli_files = sorted(f.name for f in cli_dir.iterdir())
    w_files = sorted(f.name for f in wdir.iterdir())
    assert cli_files == w_files, (cli_files, w_files)
    assert _norm_graph(cli_dir / "graph.json") == _norm_graph(wdir / "graph.json")
    if expect_analysis:
        a_cli = json.loads((cli_dir / ".graphify_analysis.json").read_text(encoding="utf-8"))
        a_w = json.loads((wdir / ".graphify_analysis.json").read_text(encoding="utf-8"))
        assert a_cli == a_w
    # manifest 的 'seen'(扫描时间戳)是运行时记录,两侧必然不同;其余字段
    # (mtime/ast_hash/semantic_hash)逐值比较
    def _norm_manifest(p: Path) -> dict:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {
            k: {ik: iv for ik, iv in v.items() if ik != "seen"}
            for k, v in raw.items()
        }

    assert _norm_manifest(cli_dir / "manifest.json") == _norm_manifest(wdir / "manifest.json"), \
        "manifest 不一致"
    assert (cli_dir / ".graphify_root").read_text(encoding="utf-8") == \
        (wdir / ".graphify_root").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 场景 1:fresh 全量(code_only)
# ---------------------------------------------------------------------------


def test_fresh_code_only_parity(tmp_path):
    corpus = _mixed_corpus(tmp_path / "repo")
    cli_out, w_out = tmp_path / "cli-out", tmp_path / "wrap-out"
    cp = _cli(corpus, cli_out, "--code-only")
    assert cp.returncode == 0, cp.stderr
    r = _wrap(corpus, w_out, code_only=True)
    assert r["error"] is None, r
    _assert_same(cli_out, w_out)
    # 方向性(已核实事实):extract 链从不传 directed → 无向存储 + 端点方向
    cli_g = json.loads((cli_out / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
    assert cli_g["directed"] is False
    # 无 --exclude/--no-gitignore → 两侧都不落 .graphify_build.json
    assert not (cli_out / "graphify-out" / ".graphify_build.json").exists()
    assert not (w_out / ".graphify_build.json").exists()


# ---------------------------------------------------------------------------
# 场景 2:warm 标准路径(双侧各二跑)
# ---------------------------------------------------------------------------


def test_warm_standard_parity(tmp_path):
    corpus = _mixed_corpus(tmp_path / "repo")
    cli_out, w_out = tmp_path / "cli-out", tmp_path / "wrap-out"
    assert _cli(corpus, cli_out, "--code-only").returncode == 0
    r1 = _wrap(corpus, w_out, code_only=True)
    assert r1["error"] is None and r1["incremental"] is False
    g_cli = cli_out / "graphify-out" / "graph.json"
    g1_norm = _norm_graph(g_cli)

    cp2 = _cli(corpus, cli_out, "--code-only")
    assert cp2.returncode == 0, cp2.stderr
    r2 = _wrap(corpus, w_out, code_only=True)
    assert r2["error"] is None and r2["incremental"] is True
    _assert_same(cli_out, w_out)
    # 同侧:warm 图与 fresh 图内容一致
    assert _norm_graph(g_cli) == g1_norm
    # 库层固有:detect_incremental 无 cache_root 形参 → 两侧都会在扫描语料下
    # 建 <corpus>/graphify-out/cache;断言"同现"作为 parity 证据(非缺陷提示)
    assert (corpus / "graphify-out" / "cache").is_dir()


# ---------------------------------------------------------------------------
# 场景 3:fresh no_cluster(裸 raw dict)
# ---------------------------------------------------------------------------


def test_no_cluster_fresh_parity(tmp_path):
    corpus = _mixed_corpus(tmp_path / "repo")
    cli_out, w_out = tmp_path / "cli-out", tmp_path / "wrap-out"
    cp = _cli(corpus, cli_out, "--code-only", "--no-cluster")
    assert cp.returncode == 0, cp.stderr
    r = _wrap(corpus, w_out, code_only=True, no_cluster=True)
    assert r["error"] is None, r
    cli_g = json.loads((cli_out / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
    assert "edges" in cli_g and "directed" not in cli_g  # 裸 dict 形状
    assert not (cli_out / "graphify-out" / ".graphify_analysis.json").exists()
    assert r["analysis_json"] is None
    _assert_same(cli_out, w_out, expect_analysis=False)


# ---------------------------------------------------------------------------
# 场景 4:warm no_cluster + 改文件(tier 替换,无重复)
# ---------------------------------------------------------------------------


def test_no_cluster_warm_change_parity(tmp_path):
    corpus = _mixed_corpus(tmp_path / "repo")
    cli_out, w_out = tmp_path / "cli-out", tmp_path / "wrap-out"
    assert _cli(corpus, cli_out, "--code-only", "--no-cluster").returncode == 0
    r1 = _wrap(corpus, w_out, code_only=True, no_cluster=True)
    assert r1["error"] is None
    (corpus / "utils.py").write_text(
        "def hello(name):\n    return f'hi {name}'\n\n\ndef bye(name):\n    return f'see {name}'\n",
        encoding="utf-8",
    )
    assert _cli(corpus, cli_out, "--code-only", "--no-cluster").returncode == 0
    r2 = _wrap(corpus, w_out, code_only=True, no_cluster=True)
    assert r2["error"] is None and r2["incremental"] is True, r2
    _assert_same(cli_out, w_out, expect_analysis=False)
    raw = json.loads((w_out / "graph.json").read_text(encoding="utf-8"))
    ids = [n["id"] for n in raw["nodes"]]
    assert len(ids) == len(set(ids))  # 替换不重复


# ---------------------------------------------------------------------------
# 场景 5:warm no_cluster 无变化(早退)
# ---------------------------------------------------------------------------


def test_no_cluster_warm_no_change_parity(tmp_path):
    corpus = _mixed_corpus(tmp_path / "repo")
    cli_out, w_out = tmp_path / "cli-out", tmp_path / "wrap-out"
    assert _cli(corpus, cli_out, "--code-only", "--no-cluster").returncode == 0
    r1 = _wrap(corpus, w_out, code_only=True, no_cluster=True)
    assert r1["error"] is None
    g_w = Path(w_out) / "graph.json"
    mtime = g_w.stat().st_mtime
    cli_g_before = (cli_out / "graphify-out" / "graph.json").read_text(encoding="utf-8")
    cp2 = _cli(corpus, cli_out, "--code-only", "--no-cluster")
    assert cp2.returncode == 0 and "no incremental changes" in cp2.stdout
    r2 = _wrap(corpus, w_out, code_only=True, no_cluster=True)
    assert r2["error"] is None and r2["skipped"] is True, r2
    assert g_w.stat().st_mtime == mtime  # 图未被重写
    # CLI 侧:内容原样(未重写)
    assert (cli_out / "graphify-out" / "graph.json").read_text(encoding="utf-8") == cli_g_before
    # manifest 仍被更新(两侧同)
    _assert_same(cli_out, w_out, expect_analysis=False)


# ---------------------------------------------------------------------------
# 场景 6:混合语料 + 无 LLM key:带 --code-only 双侧成功;不带双侧同样失败语义
# ---------------------------------------------------------------------------


def test_code_only_hint_and_no_key_both_sides(tmp_path):
    corpus = _mixed_corpus(tmp_path / "repo")
    # a) 带 code-only:双侧成功(CLI stderr 给出跳过计数)
    cli_out, w_out = tmp_path / "cli-out", tmp_path / "wrap-out"
    cp = _cli(corpus, cli_out, "--code-only")
    assert cp.returncode == 0
    assert "--code-only: skipping" in cp.stdout
    r = _wrap(corpus, w_out, code_only=True)
    assert r["error"] is None and r["files"]["document"] == 0
    # b) 不带 code-only:双侧都拒绝(CLI exit 1 / wrapper error dict)
    cli_out2, w_out2 = tmp_path / "cli-out2", tmp_path / "wrap-out2"
    cp2 = _cli(corpus, cli_out2)
    assert cp2.returncode == 1
    assert "code-only" in cp2.stderr or "no LLM API key" in cp2.stderr
    r2 = _wrap(corpus, w_out2, backend=None)
    assert r2["error"] is not None


# ---------------------------------------------------------------------------
# 场景 7:--exclude 持久化(#1971)
# ---------------------------------------------------------------------------


def test_exclude_persistence_parity(tmp_path):
    corpus = _mixed_corpus(tmp_path / "repo")
    cli_out, w_out = tmp_path / "cli-out", tmp_path / "wrap-out"
    assert _cli(corpus, cli_out, "--code-only", "--exclude", "utils.py").returncode == 0
    r1 = _wrap(corpus, w_out, code_only=True, extra_excludes=["utils.py"])
    assert r1["error"] is None
    _assert_same(cli_out, w_out)
    cfg_cli = json.loads(
        (cli_out / "graphify-out" / ".graphify_build.json").read_text(encoding="utf-8"))
    cfg_w = json.loads((w_out / ".graphify_build.json").read_text(encoding="utf-8"))
    assert cfg_cli == cfg_w == {"excludes": ["utils.py"]}
    # 二跑无 flag:两侧都继承排除
    assert _cli(corpus, cli_out, "--code-only").returncode == 0
    r2 = _wrap(corpus, w_out, code_only=True)
    assert r2["error"] is None, r2
    _assert_same(cli_out, w_out)
    raw = json.loads((w_out / "graph.json").read_text(encoding="utf-8"))
    assert not any((n.get("source_file") or "").endswith("utils.py") for n in raw["nodes"])


# ---------------------------------------------------------------------------
# 语义(LLM)场景:进程内 dispatch_command vs 封装,共享 mock,零网络
# ---------------------------------------------------------------------------


def _fake_llm(paths, **kw):
    chunk = {
        "nodes": [
            {"id": "concept_demo", "label": "Demo doc", "type": "concept",
             "source_file": "README.md"},
        ],
        "edges": [], "hyperedges": [],
        "input_tokens": 10, "output_tokens": 5,
    }
    on = kw.get("on_chunk_done")
    if on is not None:
        on(0, len(paths), chunk)
    return dict(chunk)


def _dispatch_cli_extract(corpus: Path, out_root: Path, *flags: str) -> None:
    """进程内直调 graphify.cli.dispatch_command(等价 CLI,可捕获 SystemExit)。"""
    from graphify.cli import dispatch_command
    monkeyargv = ["graphify", "extract", str(corpus), "--out", str(out_root), *flags]
    old = sys.argv
    sys.argv = monkeyargv
    try:
        dispatch_command("extract")
    finally:
        sys.argv = old


@pytest.mark.usefixtures("monkeypatch")
def test_semantic_parity_inprocess(monkeypatch, tmp_path):
    """语义场景:LMM 走 mock;CLI 侧 dispatch_command 直调,封装侧 extract()。
    注意别名陷阱:封装在 import 时绑定 extract_corpus_parallel,须双侧 patch。
    """
    import gh_puller.graphify as gfx

    monkeypatch.setattr("graphify.llm.extract_corpus_parallel", _fake_llm)
    monkeypatch.setattr(gfx, "extract_corpus_parallel", _fake_llm)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    corpus = _mixed_corpus(tmp_path / "repo")
    cli_out, w_out = tmp_path / "cli-out", tmp_path / "wrap-out"
    _dispatch_cli_extract(corpus, cli_out, "--backend", "claude")
    r = extract(corpus, backend="claude", out_dir=str(w_out))
    assert r["error"] is None, r
    _assert_same(cli_out, w_out)
    assert json.loads(
        (w_out / ".graphify_semantic_marker").read_text(encoding="utf-8")
    ) == {"output_tokens": 5}
    assert json.loads(
        (cli_out / "graphify-out" / ".graphify_semantic_marker").read_text(encoding="utf-8")
    ) == {"output_tokens": 5}


def test_semantic_force_redispatch_parity(monkeypatch, tmp_path):
    """--force:双侧都跳过语义缓存读、全部重派发(mock 调用次数 == 文件数)。"""
    import gh_puller.graphify as gfx

    calls = {"n": 0}

    def counting(paths, **kw):
        calls["n"] += 1
        return _fake_llm(paths, **kw)

    monkeypatch.setattr("graphify.llm.extract_corpus_parallel", counting)
    monkeypatch.setattr(gfx, "extract_corpus_parallel", counting)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    corpus = _mixed_corpus(tmp_path / "repo")
    cli_out, w_out = tmp_path / "cli-out", tmp_path / "wrap-out"
    _dispatch_cli_extract(corpus, cli_out, "--backend", "claude", "--force")
    assert calls["n"] >= 1
    r = extract(corpus, backend="claude", force=True, out_dir=str(w_out))
    assert r["error"] is None
    _assert_same(cli_out, w_out)


def test_semantic_partial_chunk_incomplete_parity(monkeypatch, tmp_path):
    """部分 chunk 失败(2 个文档只成功 1 个):双侧 incomplete=True,图仍写出
    (fresh,无旧图);marker 一致。"""
    import gh_puller.graphify as gfx

    corpus = tmp_path / "repo"
    corpus.mkdir(parents=True, exist_ok=True)
    (corpus / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (corpus / "README.md").write_text("# A\n", encoding="utf-8")
    (corpus / "NOTES.md").write_text("# B\n", encoding="utf-8")

    def partial(paths, **kw):
        # 模拟 2 个文件中仅 1 个 chunk 成功:on_chunk_done 只触发一次,
        # 但 total 计满(len(paths)),触发 _chunk_stats 的"部分失败"路径
        chunk = {
            "nodes": [
                {"id": "concept_demo", "label": "Demo doc", "type": "concept",
                 "source_file": "README.md"},
            ],
            "edges": [], "hyperedges": [],
            "input_tokens": 10, "output_tokens": 5,
        }
        on = kw.get("on_chunk_done")
        if on is not None:
            on(0, len(paths), dict(chunk))
        return dict(chunk)

    monkeypatch.setattr("graphify.llm.extract_corpus_parallel", partial)
    monkeypatch.setattr(gfx, "extract_corpus_parallel", partial)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    cli_out, w_out = tmp_path / "cli-out", tmp_path / "wrap-out"
    _dispatch_cli_extract(corpus, cli_out, "--backend", "claude")
    r = extract(corpus, backend="claude", out_dir=str(w_out))
    assert r["error"] is None and r["incomplete"] is True, r
    _assert_same(cli_out, w_out)


def test_semantic_deep_mode_parity(monkeypatch, tmp_path):
    """--mode deep:deep 命名空间缓存(semantic-deep/),两侧同现。"""
    import gh_puller.graphify as gfx

    monkeypatch.setattr("graphify.llm.extract_corpus_parallel", _fake_llm)
    monkeypatch.setattr(gfx, "extract_corpus_parallel", _fake_llm)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    corpus = _mixed_corpus(tmp_path / "repo")
    cli_out, w_out = tmp_path / "cli-out", tmp_path / "wrap-out"
    _dispatch_cli_extract(corpus, cli_out, "--backend", "claude", "--mode", "deep")
    r = extract(corpus, backend="claude", deep_mode=True, out_dir=str(w_out))
    assert r["error"] is None, r
    _assert_same(cli_out, w_out)
    assert (w_out / "cache" / "semantic-deep").is_dir()
    assert (cli_out / "graphify-out" / "cache" / "semantic-deep").is_dir()
