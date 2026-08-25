"""graphify 工具的进程内封装。

将 graphify 的三条 CLI 命令（extract / export / query）改造为可直接调用的
模块函数：本模块是 graphifyy 包（import 名 graphify）之上的薄封装层，
参数缺省值、输出目录与文件序列均以 graphify/cli.py 同名分支（dispatch_command）
为唯一权威参照。库调用全部使用显式路径参数，不依赖 cwd 或 GRAPHIFY_OUT 的
导入时快照（graphify.paths.GRAPHIFY_OUT 在 import 时读取一次），因此任何函数
都可在不改变工作目录的前提下运行。LLM 凭据完全来自环境变量（OPENAI_API_KEY /
OPENAI_BASE_URL / OPENAI_MODEL 等由 graphify 库层读取），本模块不接收也不落盘
任何 API key。graphify 是本项目的底层三方依赖（见 pyproject），因此导入一律
在模块顶部直接 import，不做缺包防御。

已知简化（相对 `graphify extract` CLI）：
- 不做增量：v1 只做全量 detect → build（不调 detect_incremental / build_merge /
  save_manifest）。二次运行的语义成本已被 check_semantic_cache 的内容哈希挡住，
  本地 build 为毫秒级；
- 不支持 postgres / cargo / google-workspace / --global / --dedup-llm / import cache prune；
- 不生成 GRAPH_REPORT.md 与 .graphify_labels.json（与 CLI extract 同约定，
  留给 cluster-only / guard 后续步骤自行生成）。
"""

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from graphify import querylog
from graphify.analyze import god_nodes, surprising_connections
from graphify.build import (
    build,
    dedupe_edges,
    dedupe_nodes,
    disambiguate_file_labels_in_nodes,
    graph_has_legacy_ids,
)
from graphify.cache import check_semantic_cache, save_semantic_cache
from graphify.callflow_html import write_callflow_html
from graphify.cluster import cluster, score_all
from graphify.detect import detect
from graphify.export import (
    MALFORMED_GRAPH,
    backup_if_protected,
    existing_graph_node_count,
    push_to_falkordb,
    to_cypher,
    to_html,
    to_json,
    to_svg,
)
from graphify.extract import extract as _ast_extract
from graphify.llm import (
    BACKENDS,
    _extraction_system,
    _format_backend_env_keys,
    _get_backend_api_key,
    _partial_source_files,
    _strip_partial_markers,
    detect_backend,
    estimate_cost,
    extract_corpus_parallel,
)
from graphify.paths import write_json_atomic, write_text_atomic
from graphify.security import check_graph_file_size_cap
from graphify.serve import _query_graph_text
from graphify.tree_html import write_tree_html
from graphify.watch import _git_head
from networkx.readwrite import json_graph

_GRAPHIFY_OUT = "graphify-out"  # GRAPHIFY_OUT 缺省值（与 graphify.paths.py 同式）
_DEFAULT_TOKEN_BUDGET = 60_000  # 与 CLI --token-budget 缺省一致
_DEFAULT_TOKEN_BUDGET_QUERY = 2000  # 与 CLI query --budget 缺省一致


def _out_name() -> str:
    """输出目录名：与 graphify.paths.GRAPHIFY_OUT 同一取值公式（import 时快照）。"""
    return os.environ.get("GRAPHIFY_OUT", _GRAPHIFY_OUT)


def _default_graph_path() -> Path:
    """导出/查询的默认图路径：cwd 下 <GRAPHIFY_OUT>/graph.json（同 CLI 默认）。"""
    return Path.cwd() / _out_name() / "graph.json"


def _log(stage: str, msg: str) -> None:
    """进度日志走 stderr——stdout 留给调用方做机器结果（CLI 同样约定 [#698]）。"""
    print(f"[graphify {stage}] {msg}", file=sys.stderr, flush=True)


def _load_graph(graph_path: str | Path, *, preserve_direction: bool = False) -> tuple[Any, dict]:
    """显式路径加载 graph.json → (图, 原始 dict)。复刻 CLI 前置与归一：
    存在性(FileNotFoundError) → 后缀 .json(ValueError) → 大小上限(ValueError 传播)
    → links/edges 归一；preserve_direction 时保留 _src/_tgt 端点标记（#2309，
    不覆盖已存在的标记），供 query 渲染真实调用方向。"""
    gp = Path(graph_path).expanduser()
    if not gp.exists():
        raise FileNotFoundError(f"graph file not found: {gp}")
    if gp.suffix != ".json":
        raise ValueError(f"graph file must be a .json file: {gp}")
    check_graph_file_size_cap(gp)
    raw = json.loads(gp.read_text(encoding="utf-8"))
    if "links" not in raw and "edges" in raw:
        raw = dict(raw, links=raw["edges"])
    if preserve_direction:
        raw = dict(
            raw,
            links=[
                {
                    **link,
                    "_src": link.get("_src", link.get("source")),
                    "_tgt": link.get("_tgt", link.get("target")),
                }
                for link in raw.get("links", [])
            ],
        )
    try:
        G = json_graph.node_link_graph(raw, edges="links")
    except TypeError:
        G = json_graph.node_link_graph(raw)
    return G, raw


def _communities_from_graph(graph_path: Path, G) -> tuple[dict[int, list[str]], dict[int, str]]:
    """从 graph.json 侧工地重构 communities 与 labels。

    .graphify_analysis.json 是规范来源；但 post-commit / watch 重建路径不重新
    生成它（CLI 注释 #1019），此时节点上的 community 属性佐证仍可重建社区——
    缺失则各导出格式会退化。labels 同理读 .graphify_labels.json。"""
    an_path = Path(graph_path).parent / ".graphify_analysis.json"
    communities: dict[int, list[str]] = {}
    if an_path.exists():
        an = json.loads(an_path.read_text(encoding="utf-8"))
        communities = {int(k): v for k, v in an.get("communities", {}).items()}
    if not communities:
        reconstructed: dict[int, list[str]] = {}
        for node_id, data in G.nodes(data=True):
            cid_raw = data.get("community")
            if cid_raw is None:
                continue  # 未聚类节点（no_cluster 导出或旧图）不参与重构
            try:
                cid = int(cid_raw)
            except (TypeError, ValueError):
                continue
            reconstructed.setdefault(cid, []).append(str(node_id))
        communities = reconstructed
    labels: dict[int, str] = {}
    lb_path = Path(graph_path).parent / ".graphify_labels.json"
    if lb_path.exists():
        labels = {
            int(k): v
            for k, v in json.loads(lb_path.read_text(encoding="utf-8")).items()
        }
    return communities, labels


def extract(
    path: str | Path = ".",
    *,
    backend: str | None = "openai",  # None → graphify.llm.detect_backend()（需语义时）
    model: str | None = None,  # 透传 extract_corpus_parallel；未设时库层读 OPENAI_MODEL
    code_only: bool = False,  # 纯本地 AST，跳过 doc/paper/image 语义（无 key 可跑）
    token_budget: int = _DEFAULT_TOKEN_BUDGET,
    max_concurrency: int | None = None,  # None → 库缺省 4
    max_workers: int | None = None,  # AST 并发；经 GRAPHIFY_MAX_WORKERS env 透传（同 CLI）
    api_timeout: float | None = None,  # 经 GRAPHIFY_API_TIMEOUT env 透传（同 CLI）
    out_dir: str | Path | None = None,  # 给出即最终输出目录;None → <path>/<GRAPHIFY_OUT>
    force: bool = False,  # 跳过语义缓存读取（同 CLI --force）
    no_cluster: bool = False,  # 原始合并直写 graph.json，无社区
    no_dedup: bool = False,  # build(dedup=False)，放掉模糊合并
    no_gitignore: bool = False,  # detect(gitignore=False)
    resolution: float = 1.0,  # cluster(resolution)
    exclude_hubs: float | None = None,  # cluster(exclude_hubs_percentile)
    extra_excludes: list[str] | None = None,  # detect(extra_excludes)
    deep_mode: bool = False,  # 深层语义提取（独立缓存命名空间 semantic-deep, #1894）
    allow_partial: bool = False,  # 提取失败时允许覆盖完整旧图（#479 守卫豁免）
    log: Callable[[str], None] | None = None,  # 进度回调；None → stderr 中文日志
) -> dict:
    """等价于 `graphify extract <path> --backend …` 的进程内版本。

    流水线：detect 分类 → 代码 AST 提取（纯本地）→ 语义提取（缓存优先，
    未命中走 LLM）→ 合并 → build → 聚类 → 写出 graph.json 与
    .graphify_analysis.json。返回可 JSON 序列化的汇总 dict；失败路径
    catch-all 降级为同结构错误态（"error" 字段），供调用方存活消费。
    """
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(f"path not found: {target}")
    # out_dir 语义:给出即为最终输出目录(不再拼接 /<GRAPHIFY_OUT>);缺省 <path>/<GRAPHIFY_OUT>
    gout = Path(out_dir).expanduser().resolve() if out_dir else target / _out_name()
    # cache_root 统一为最终基座(gout):经 cache_root_final=True 让 lib 不拼
    # <GRAPHIFY_OUT> 层,缓存落 <gout>/cache;缺省 out_dir 时同样落
    # <target>/<GRAPHIFY_OUT>/cache(与旧行为一致)。历史上(960ab73 期间)的
    # 错位缓存 <gout>/graphify-out/cache 不迁移,已人工清理。
    cache_root = gout
    gout.mkdir(parents=True, exist_ok=True)
    _msg = log or (lambda m: _log("extract", m))
    t0 = time.perf_counter()
    # --api-timeout/--max-workers 在 CLI 中写回环境变量后再被库层读取，保持同式
    if api_timeout is not None:
        os.environ["GRAPHIFY_API_TIMEOUT"] = str(api_timeout)
    if max_workers is not None:
        os.environ["GRAPHIFY_MAX_WORKERS"] = str(max_workers)
    result = {
        "graph_json": str(gout / "graph.json"),
        "analysis_json": None,
        "nodes": 0,
        "edges": 0,
        "communities": 0,
        "tokens": {"input": 0, "output": 0, "cost_usd": 0.0},
        "files": {"code": 0, "document": 0, "paper": 0, "image": 0},
        "semantic_cache": {"hits": 0, "misses": 0},
        "incomplete": False,
        "elapsed_seconds": 0.0,
        "error": None,
    }

    try:
        graph_json_path = gout / "graph.json"
        analysis_path = gout / ".graphify_analysis.json"

        _msg(f"scanning {target}")
        detection = detect(target, extra_excludes=extra_excludes or None,
                           cache_root=cache_root, gitignore=not no_gitignore,
                           cache_root_final=True)
        files_by_type = detection.get("files", {})
        code_files = [Path(p) for p in files_by_type.get("code", [])]
        doc_files = [Path(p) for p in files_by_type.get("document", [])]
        paper_files = [Path(p) for p in files_by_type.get("paper", [])]
        image_files = [Path(p) for p in files_by_type.get("image", [])]
        semantic_files = doc_files + paper_files + image_files
        # --code-only 跳过全部语义文件并在日志给出计数（#1734）
        if code_only and semantic_files:
            _msg(
                f"--code-only: skipping {len(semantic_files)} non-code file(s) "
                f"({len(doc_files)} docs, {len(paper_files)} papers, {len(image_files)} images)"
            )
            semantic_files = []
            doc_files = paper_files = image_files = []
        _unclassified = detection.get("unclassified", [])
        if _unclassified:
            _names = ", ".join(sorted({Path(p).name for p in _unclassified})[:6])
            _msg(f"{len(_unclassified)} file(s) not classified (no supported extension), skipped: {_names}")
        _msg(
            f"found {len(code_files)} code, {len(doc_files)} docs, "
            f"{len(paper_files)} papers, {len(image_files)} images"
        )
        # 扫描未完整（权限/IO 错误）与后方部分失败同属"不完整"：最终写图
        # 交给 #479 守卫拒绝覆盖更大的完整图，除非 allow_partial
        _extraction_incomplete = bool(detection.get("walk_errors"))

        # ---- AST 提取（纯本地，无需 API key）----
        ast_result: dict = {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}
        if code_files:
            _msg(f"AST extraction on {len(code_files)} code files...")
            try:
                _ast_kwargs: dict = {"cache_root": cache_root, "root": target, "cache_root_final": True}
                if max_workers is not None:
                    _ast_kwargs["max_workers"] = max_workers
                ast_result = _ast_extract(code_files, **_ast_kwargs)
            except Exception as exc:
                _msg(f"AST extraction failed: {exc}")
                # #2445：整段 AST 丢失默认致命；--allow-partial 才降级续跑
                if not allow_partial:
                    raise
                _extraction_incomplete = True

        # ---- 语义提取（缓存优先，未命中走 LLM）----
        sem_result: dict = {"nodes": [], "edges": [], "hyperedges": [],
                            "input_tokens": 0, "output_tokens": 0}
        sem_cache_hits = 0
        sem_cache_misses = 0
        sem_cache_mode = "deep" if deep_mode else None  # deep 独立缓存命名空间（#1894）
        backend_eff: str | None = backend
        if semantic_files:
            backend_eff = backend or detect_backend()
            if backend_eff is None:
                raise RuntimeError(
                    "no LLM backend detected (doc/paper/image files need semantic "
                    "extraction); pass backend=... or extract(code_only=True)"
                )
            if backend_eff not in BACKENDS:
                raise ValueError(f"unknown backend: {backend_eff}")
            if not _get_backend_api_key(backend_eff):
                raise RuntimeError(
                    f"backend '{backend_eff}' requires {_format_backend_env_keys(backend_eff)}"
                )
            # prompt 是语义缓存的键组分之一：读与写必须同一 prompt，
            # 否则写入的条目永远匹配不上下一次读取（#1939）
            prompt = _extraction_system(deep=deep_mode)

            paths_str = [str(p) for p in semantic_files]
            if force:
                cached_nodes, cached_edges, cached_hyperedges = [], [], []
                uncached_paths = list(paths_str)
            else:
                cached_nodes, cached_edges, cached_hyperedges, uncached_paths = (
                    check_semantic_cache(paths_str, root=target, cache_root=cache_root,
                                         mode=sem_cache_mode, prompt=prompt,
                                         cache_root_final=True)
                )
            sem_cache_hits = len(semantic_files) - len(uncached_paths)
            sem_cache_misses = len(uncached_paths)
            sem_result["nodes"].extend(cached_nodes)
            sem_result["edges"].extend(cached_edges)
            sem_result["hyperedges"].extend(cached_hyperedges)
            if sem_cache_hits:
                _msg(f"semantic cache: {sem_cache_hits} hit / {sem_cache_misses} miss")

            if uncached_paths:
                _msg(f"semantic extraction on {len(uncached_paths)} files via {backend_eff}...")
                corpus_kwargs: dict = {"backend": backend_eff, "model": model,
                                       "root": target, "cache_root": cache_root,
                                       "cache_root_final": True}
                if deep_mode:
                    corpus_kwargs["deep_mode"] = True
                if max_concurrency is not None:
                    corpus_kwargs["max_concurrency"] = max_concurrency
                # on_chunk_done 仅在成功 chunk 后触发 → 以它统计成败
                _chunk_stats = {"total": 0, "succeeded": 0}

                def _progress(idx: int, total: int, _res: dict) -> None:
                    _chunk_stats["total"] = total
                    _chunk_stats["succeeded"] += 1
                    _msg(f"chunk {idx + 1}/{total} done")

                try:
                    fresh = extract_corpus_parallel([Path(p) for p in uncached_paths],
                                                    token_budget=token_budget, on_chunk_done=_progress,
                                                    **corpus_kwargs)
                except ImportError as exc:
                    raise RuntimeError(str(exc)) from exc  # 缺 SDK 包，同 CLI exit 1
                except Exception as exc:
                    _msg(f"semantic extraction failed: {exc}")
                    fresh = {"nodes": [], "edges": [], "hyperedges": [],
                             "input_tokens": 0, "output_tokens": 0}
                    _extraction_incomplete = True  # 整个语义 pass 崩溃
                if uncached_paths and _chunk_stats["succeeded"] == 0:
                    raise RuntimeError(
                        f"all semantic chunks failed for backend '{backend_eff}' "
                        f"({len(uncached_paths)} uncached files)"
                    )
                if _chunk_stats["total"] and _chunk_stats["succeeded"] < _chunk_stats["total"]:
                    _extraction_incomplete = True
                # 截断文件的标记须在入库前剥离（内部标记不泄漏进 graph.json）
                _partial_semantic = set(_partial_source_files(fresh))
                try:
                    save_semantic_cache(fresh.get("nodes", []), fresh.get("edges", []),
                                        fresh.get("hyperedges", []), root=target,
                                        cache_root=cache_root, allowed_source_files=uncached_paths,
                                        mode=sem_cache_mode, prompt=prompt,
                                        partial_source_files=_partial_semantic or None,
                                        cache_root_final=True)
                except Exception as exc:
                    _msg(f"warning: could not write semantic cache: {exc}")
                _strip_partial_markers(fresh)
                sem_result["nodes"].extend(fresh.get("nodes", []))
                sem_result["edges"].extend(fresh.get("edges", []))
                sem_result["hyperedges"].extend(fresh.get("hyperedges", []))
                sem_result["input_tokens"] += fresh.get("input_tokens", 0)
                sem_result["output_tokens"] += fresh.get("output_tokens", 0)

        # ---- 合并（AST 在前：语义节点属性冲突时语义胜出）----
        merged = {
            "nodes": list(ast_result.get("nodes", [])) + list(sem_result.get("nodes", [])),
            "edges": list(ast_result.get("edges", [])) + list(sem_result.get("edges", [])),
            "hyperedges": list(sem_result.get("hyperedges", [])),
            "input_tokens": ast_result.get("input_tokens", 0) + sem_result.get("input_tokens", 0),
            "output_tokens": ast_result.get("output_tokens", 0) + sem_result.get("output_tokens", 0),
        }
        result["files"] = {"code": len(code_files), "document": len(doc_files),
                           "paper": len(paper_files), "image": len(image_files)}
        result["semantic_cache"] = {"hits": sem_cache_hits, "misses": sem_cache_misses}
        result["incomplete"] = _extraction_incomplete
        result["tokens"] = {
            "input": merged["input_tokens"],
            "output": merged["output_tokens"],
            "cost_usd": estimate_cost(backend_eff, merged["input_tokens"],
                                      merged["output_tokens"]),
        }

        if no_cluster:
            merged["nodes"] = dedupe_nodes(merged["nodes"])
            merged["edges"] = dedupe_edges(merged["edges"])
            # 此路径绕过 build_from_json：基线歧义消除与端点 source_file
            # 回填需自行应用（#2032/#1279）
            disambiguate_file_labels_in_nodes(merged["nodes"])
            _node_sf = {n.get("id"): n.get("source_file") for n in merged["nodes"]}
            for _e in merged["edges"]:
                if not _e.get("source_file"):
                    _e["source_file"] = _node_sf.get(_e.get("source")) or _node_sf.get(_e.get("target")) or ""
            # raw 路径无 to_json 的 #479 守卫：部分结果不得覆盖更完整的旧图
            if _extraction_incomplete and not allow_partial:
                _existing_n = existing_graph_node_count(graph_json_path)
                _malformed = _existing_n is MALFORMED_GRAPH
                _shrinks = isinstance(_existing_n, int) and len(merged["nodes"]) < _existing_n
                if _malformed or _shrinks:
                    raise RuntimeError(
                        "extraction was incomplete and the resulting graph "
                        "may shrink: refusing to overwrite a complete graph with "
                        "a partial one; pass allow_partial=True to override"
                    )
            backup_if_protected(gout)
            write_json_atomic(graph_json_path, merged, indent=2)
            result["nodes"] = len(merged["nodes"])
            result["edges"] = len(merged["edges"])
        else:
            G = build([merged], dedup=not no_dedup, root=target)
            if G.number_of_nodes() == 0:
                raise RuntimeError(
                    "graph is empty — extraction produced no nodes (possible causes: "
                    "all files skipped, binary-only corpus, or LLM returned no edges)"
                )
            communities = cluster(G, resolution=resolution, exclude_hubs_percentile=exclude_hubs)
            cohesion = score_all(G, communities)
            # 分析产物非致命：失败时以空列表继续，与 CLI 一致
            try:
                gods = god_nodes(G)
            except Exception:
                gods = []
            try:
                surprises = surprising_connections(G, communities)
            except Exception:
                surprises = []
            # 完整性：guard 例外只给 allow_partial（#479）
            backup_if_protected(gout)
            _wrote = to_json(G, communities, str(graph_json_path),
                             force=allow_partial or not _extraction_incomplete,
                             built_at_commit=_git_head(cwd=target))
            if not _wrote:
                raise RuntimeError(
                    "extraction was incomplete and the resulting graph is smaller "
                    "than the existing one: refusing to overwrite a complete graph "
                    "with a partial one; pass allow_partial=True to override"
                )
            result["nodes"] = G.number_of_nodes()
            result["edges"] = G.number_of_edges()
            result["communities"] = len(communities)
        # 记录扫描根：后续 update/build_merge 相对化删除路径时依赖它（#2012）
        try:
            (gout / ".graphify_root").write_text(str(target), encoding="utf-8")
        except OSError:
            pass
        if not no_cluster:
            if merged.get("output_tokens", 0) > 0:
                (gout / ".graphify_semantic_marker").write_text(
                    json.dumps({"output_tokens": merged["output_tokens"]}), encoding="utf-8")
            write_json_atomic(
                analysis_path,
                {
                    "communities": {str(k): v for k, v in communities.items()},
                    "cohesion": {str(k): v for k, v in cohesion.items()},
                    "gods": gods,
                    "surprises": surprises,
                    "tokens": {"input": merged["input_tokens"], "output": merged["output_tokens"]},
                },
                indent=2,
            )
            result["analysis_json"] = str(analysis_path)
        _msg(f"wrote {graph_json_path}: {result['nodes']} nodes, "
             f"{result['edges']} edges, {result['communities']} communities")
        result["elapsed_seconds"] = time.perf_counter() - t0
        return result
    except Exception as exc:
        result["elapsed_seconds"] = time.perf_counter() - t0
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result


def export(
    format: Literal["html", "svg", "falkordb", "tree", "callflow-html"],
    *,
    graph_path: str | Path | None = None,  # None → cwd 下 <GRAPHIFY_OUT>/graph.json
    output: str | Path | None = None,  # html/svg/tree/callflow-html 的目标文件；falkordb 无 push 时忽略
    node_limit: int = 5000,  # html 聚合视图节点上限（超限降级聚合, #1019）
    lang: str = "auto",
    max_sections: int = 15,
    diagram_scale: float = 1.0,
    max_diagram_nodes: int = 18,
    max_diagram_edges: int = 24,  # callflow-html
    max_children: int | None = None,  # tree；None → 库缺省 200
    top_k_edges: int = 0,
    project_label: str | None = None,  # tree
    push_uri: str | None = None,  # falkordb：None → 本地产出 cypher.txt
    push_user: str = "neo4j",
    push_password: str | None = None,  # None → FALKORDB_PASSWORD env（F-031 同式）
) -> dict:
    """等价于 `graphify export <format>` 或 `graphify tree` 的进程内版本。

    每个 format 直接映射到 graphify 的库函数（均要求 graph_path 已存在）；
    执行期异常降级为含 "error" 的 dict，不向调用方抛出。
    """
    gp = Path(graph_path).expanduser().resolve() if graph_path else _default_graph_path().resolve()
    result = {
        "format": format,
        "graph_path": str(gp),
        "output": None,
        "nodes": 0,
        "edges": 0,
        "pushed_nodes": None,
        "pushed_edges": None,
        "error": None,
    }
    try:
        if format == "tree":
            out = Path(output) if output else gp.parent / "GRAPH_TREE.html"
            res = write_tree_html(graph_path=gp, output_path=out, root=None,
                                  max_children=max_children or 200,
                                  top_k_edges=top_k_edges, project_label=project_label)
            result["output"] = str(res)
            return result
        if format == "callflow-html":
            res = write_callflow_html(
                graph=gp, report=gp.parent / "GRAPH_REPORT.md",
                labels=gp.parent / ".graphify_labels.json", sections=None,
                output=str(Path(output)) if output else None, lang=lang,
                max_sections=max_sections, diagram_scale=diagram_scale,
                max_diagram_nodes=max_diagram_nodes, max_diagram_edges=max_diagram_edges,
                verbose=False,
            )
            result["output"] = str(res)
            return result
        # html/svg/falkordb 需要图 + 社区
        G, _ = _load_graph(gp)
        result["nodes"] = G.number_of_nodes()
        result["edges"] = G.number_of_edges()
        communities, labels = _communities_from_graph(gp, G)
        if format == "html":
            # 超限大图降级聚合视图（#1019）：社区视图仍可用，不能硬失败
            try:
                check_graph_file_size_cap(gp)
            except ValueError:
                node_limit = 5000
            out = str(Path(output)) if output else str(gp.parent / "graph.html")
            to_html(G, communities, out, community_labels=labels or None, node_limit=node_limit)
            result["output"] = out
            return result
        if format == "svg":
            out = str(Path(output)) if output else str(gp.parent / "graph.svg")
            to_svg(G, communities, out, community_labels=labels or None)
            result["output"] = out
            return result
        # format == "falkordb"：本地产出 cypher.txt，或 push 到 FalkorDB
        if push_uri:
            res = push_to_falkordb(G, uri=push_uri, user=push_user,
                                   password=push_password or os.environ.get("FALKORDB_PASSWORD"),
                                   communities=communities)
            result["pushed_nodes"] = res["nodes"]
            result["pushed_edges"] = res["edges"]
        else:
            out = str(gp.parent / "cypher.txt")
            to_cypher(G, out)
            result["output"] = out
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result


def query(
    question: str,
    *,
    graph_path: str | Path | None = None,  # None → cwd 下 <GRAPHIFY_OUT>/graph.json
    token_budget: int = _DEFAULT_TOKEN_BUDGET_QUERY,
    mode: Literal["bfs", "dfs"] = "bfs",
    depth: int = 2,
    context_filters: list[str] | None = None,
) -> dict:
    """等价于 `graphify query "..."` 的进程内版本。

    载入 graph.json 后无向遍历（同时探索调用方与被调方，保证调用侧结果不丢，
    同 CLI #2213 注释），生成面向问答的子图文本——纯本地执行，无需 API key。
    前置校验（图缺失/非 json/超限）抛内置异常；执行期异常同样直接上抛。
    """
    t0 = time.perf_counter()
    gp = Path(graph_path).expanduser().resolve() if graph_path else _default_graph_path().resolve()
    G, raw = _load_graph(gp, preserve_direction=True)
    try:
        # 旧节点 ID 方案提示（仅提示，不阻断，#1504）
        if graph_has_legacy_ids(raw.get("nodes", [])):
            _log("query", "note: graph uses the pre-#1504 node-ID scheme; "
                          "rebuild with extract(force=True) to get path-qualified IDs")
    except Exception:
        pass
    answer = _query_graph_text(G, question, mode=mode, depth=depth,
                               token_budget=token_budget,
                               context_filters=context_filters or [], graph_path=str(gp))
    duration_ms = (time.perf_counter() - t0) * 1000
    # 关联记录与 query 时间戳均为尽力而为：失败不阻断回答
    try:
        querylog.log_query(kind="query", question=question, corpus=str(gp),
                           result=answer, mode=mode, depth=depth,
                           token_budget=token_budget, duration_ms=duration_ms)
    except Exception:
        pass
    try:
        stamp = gp.parent / "cache" / "last_query_stamp"
        stamp.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(stamp, str(time.time()))
    except Exception:
        pass
    return {
        "question": question,
        "answer": answer,
        "graph_path": str(gp),
        "graph_nodes": G.number_of_nodes(),
        "mode": mode,
        "depth": depth,
        "token_budget": token_budget,
        "duration_ms": duration_ms,
        "error": None,
    }
