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

对齐项（相对 `graphify extract` CLI，与 cli.py extract 分支逐段对照）：
- 增量语义一致（早前的"每次全量重建"简化已移除）：graph.json 存在且非
  force 时自动走 detect_incremental + build_merge / merge_raw_extraction +
  save_manifest + stale prune（#1909/#2543），增量判定、replace/prune 语义、
  #479 shrink guard 与 CLI 完全同式；
- 构建配置持久化一致（#1886/#1971）：.graphify_build.json 的 excludes/gitignore
  在 warm 运行中缺省继承（显式参数覆盖并写回），规则与 CLI 同式。

仍不支持（有意保持在 CLI 之外的调用面）：
- postgres / cargo / google-workspace / --global / --dedup-llm / --timing；
- pathless（DB-only）调用：extract 恒要求 path；
- 不生成 GRAPH_REPORT.md 与 .graphify_labels.json（与 CLI extract 同约定，
  留给 cluster-only / guard 后续步骤自行生成）。

目录层语义（wrapper 与 CLI 刻意不同，可互用但不可互指）：
- CLI `--out DIR` 的最终输出目录为 DIR/<GRAPHIFY_OUT>，缓存锚定 DIR（父级）；
- wrapper 的 out_dir 即最终输出目录（不再拼接一层），内部一律
  cache_root=gout + cache_root_final=True —— 与 CLI 的（无 final、
  cache_root=父级）等价，唯一例外是库层 prune_semantic_cache 无 final 通道
  （cache.py root/GRAPHIFY_OUT），本模块在提取后内联清理（#1527），
  不调用该私有名。
"""

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from graphify import querylog
from graphify.analyze import god_nodes, surprising_connections
from graphify.build import (
    _is_ast_tier,
    build,
    build_merge,
    dedupe_edges,
    dedupe_nodes,
    disambiguate_file_labels_in_nodes,
    graph_has_legacy_ids,
    merge_raw_extraction,
)
from graphify.cache import check_semantic_cache, file_hash, save_semantic_cache
from graphify.callflow_html import write_callflow_html
from graphify.cli import (
    _prune_graph_json_sources,
    _stale_graph_sources,
    _stamped_manifest_files,
    _zero_node_stamped_code_sources,
)
from graphify.cluster import cluster, score_all
from graphify.detect import detect, detect_incremental, save_manifest as _save_manifest
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
from graphify.watch import (
    _git_head,
    _read_build_excludes,
    _read_build_gitignore,
    _write_build_config,
)
from networkx.readwrite import json_graph

from .utils import _log as _utils_log

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
    _utils_log(msg, prefix=f"graphify {stage}")


def _dedupe_ordered(items) -> list:
    """保留顺序的去重（CLI extract 分支里以手写循环实现，这里同位等价）。"""
    out: list = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


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


def _ast_resolution_context(graph_path: Path, target: Path, detection: dict) -> tuple[list[dict], list[dict]]:
    """增量 AST 提取的只读解析上下文（#2437/#2438，镜像 cli.py extract 分支）。

    增量重扫只提取改动的代码文件，跨文件解析器看不到未改动文件里的被调方，
    changed→unchanged 的调用边会在合并时静默消失。从已持久化的图取 AST 层
    节点（含 _callable/_callable_class 标记，#2438）与 contains/method 边
    （#2437），作用域限定在"未改动且存活"的 corpus —— 已删/已排除/重提取
    文件中的符号绝不参与。图不可读时 fail-open（返回空集，即修复前行为）。"""
    try:
        check_graph_file_size_cap(graph_path)
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except Exception:
        return [], []
    _root = Path(os.path.abspath(str(target)))

    def _identity(source_file) -> str | None:
        # graph.json 的 source_file 相对扫描根（root=target）；detect 的
        # unchanged_files 保持扫描时的形态。统一转绝对 posix 比较。
        if not source_file:
            return None
        _p = Path(str(source_file))
        if not _p.is_absolute():
            _p = _root / _p
        return Path(os.path.abspath(_p)).as_posix()

    _live = {
        _identity(f)
        for _flist in detection.get("unchanged_files", {}).values()
        for f in _flist
    }
    _live.discard(None)
    nodes: list[dict] = []
    edges: list[dict] = []
    for node in data.get("nodes", []):
        if not node.get("id") or not _is_ast_tier(node):
            continue
        sf = node.get("source_file")
        if not sf or _identity(sf) not in _live:
            continue
        ctx: dict = {
            "id": node["id"],
            "label": node.get("label"),
            "source_file": sf,
            "file_type": node.get("file_type"),
            "type": node.get("type"),
        }
        for marker in ("_callable", "_callable_class"):
            if node.get(marker):
                ctx[marker] = node[marker]
        nodes.append(ctx)
    for edge in data.get("links", data.get("edges", [])):
        if edge.get("relation") not in ("contains", "method"):
            continue
        if not _is_ast_tier(edge):
            continue
        sf = edge.get("source_file")
        if not sf or _identity(sf) not in _live:
            continue
        edges.append({
            "source": edge.get("source"),
            "target": edge.get("target"),
            "relation": edge.get("relation"),
            "source_file": sf,
        })
    return nodes, edges


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
    force: bool = False,  # 全量重扫 + 跳过语义缓存读（同 CLI --force / GRAPHIFY_FORCE）
    no_cluster: bool = False,  # 原始合并直写 graph.json，无社区
    no_dedup: bool = False,  # build(dedup=False)/build_merge(dedup=False)，放掉模糊合并
    no_gitignore: bool = False,  # detect(gitignore=False)
    resolution: float = 1.0,  # cluster(resolution)
    exclude_hubs: float | None = None,  # cluster(exclude_hubs_percentile)
    extra_excludes: list[str] | None = None,  # detect(extra_excludes)；缺省继承 .graphify_build.json
    deep_mode: bool = False,  # 深层语义提取（独立缓存命名空间 semantic-deep, #1894）
    allow_partial: bool = False,  # 提取失败时允许覆盖完整旧图（#479 守卫豁免）
    log: Callable[[str], None] | None = None,  # 进度回调；None → stderr 中文日志
) -> dict:
    """等价于 `graphify extract <path> --backend …` 的进程内版本。

    流水线：detect（增量判定同 CLI：graph.json 存在且非 force 即增量）→
    代码 AST 提取（纯本地）→ 语义提取（缓存优先，未命中走 LLM）→ 合并 →
    build / build_merge → 聚类 → 写出 graph.json、.graphify_analysis.json、
    manifest.json。返回可 JSON 序列化的汇总 dict；失败路径 catch-all 降级为
    同结构错误态（"error" 字段），供调用方存活消费。
    """
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(f"path not found: {target}")
    # out_dir 语义:给出即为最终输出目录(不再拼接 /<GRAPHIFY_OUT>);缺省 <path>/<GRAPHIFY_OUT>
    gout = Path(out_dir).expanduser().resolve() if out_dir else target / _out_name()
    # cache_root 统一为最终基座(gout):经 cache_root_final=True 让 lib 不拼
    # <GRAPHIFY_OUT> 层,缓存落 <gout>/cache;缺省 out_dir 时同样落
    # <target>/<GRAPHIFY_OUT>/cache(与旧行为一致)。CLI 侧等价:--out X 的
    # cache_root=X(父级),最终目录 X/graphify-out —— 有效缓存同一位置。
    # 历史上(960ab73 期间)的错位缓存 <gout>/graphify-out/cache 不迁移,已人工清理。
    cache_root = gout
    gout.mkdir(parents=True, exist_ok=True)
    _msg = log or (lambda m: _log("extract", m))
    t0 = time.perf_counter()
    # --api-timeout/--max-workers 在 CLI 中写回环境变量后再被库层读取，保持同式
    if api_timeout is not None:
        os.environ["GRAPHIFY_API_TIMEOUT"] = str(api_timeout)
    if max_workers is not None:
        os.environ["GRAPHIFY_MAX_WORKERS"] = str(max_workers)
    # --force 与 CLI 同式（cli.py:2940-2942）：flag 或 GRAPHIFY_FORCE env
    force = force or os.environ.get("GRAPHIFY_FORCE", "").lower() in ("1", "true", "yes")
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
        "incremental": False,
        "skipped": False,
        "files_unchanged": 0,
        "elapsed_seconds": 0.0,
        "error": None,
    }

    try:
        graph_json_path = gout / "graph.json"
        analysis_path = gout / ".graphify_analysis.json"
        manifest_path = gout / "manifest.json"
        existing_graph_path = graph_json_path

        # ---- 构建配置持久化（#1886/#1971，镜像 cli.py:3088-3109）----
        # 显式 --no-gitignore 持久化 False;flag 缺席时读取持久化值,防止
        # 后续无 flag 运行把 gitignore 默默改回 True(#1971)
        _effective_gitignore = False if no_gitignore else _read_build_gitignore(gout)
        # 显式 list 覆盖持久化值;缺省复用(cli.py:3104)
        _effective_excludes = (extra_excludes or _read_build_excludes(gout)) or None
        _write_build_config(
            gout,
            excludes=extra_excludes or None,
            gitignore=False if no_gitignore else None,
        )

        # ---- 增量判定（镜像 cli.py:3118-3139，#1925）----
        # manifest.json 缺失不降级为丢弃语义层的全量扫描:现有 graph.json 即是
        # 充分增量基线,detect_incremental 视缺失 manifest 为"全部新",旧图节点
        # 由 build_merge/_stale_graph_sources 调和保留。
        incremental_mode = existing_graph_path.exists()
        # --force: 全量扫描、跳过语义缓存读(否则 warm 未变树会派发 0 文件,#1894)
        incremental_mode = incremental_mode and not force
        if force:
            _msg("--force: full re-scan, semantic cache reads skipped")
        elif incremental_mode and not manifest_path.exists():
            _msg("manifest.json missing; using existing graph.json as the incremental "
                 "baseline (all files re-checked; nodes for files outside this run's "
                 "scope are preserved)")

        # ---- detect 分叉（镜像 cli.py:3152-3215）----
        if incremental_mode:
            _msg(f"incremental scan of {target}")
            detection = detect_incremental(
                target,
                manifest_path=str(manifest_path),
                google_workspace=None,
                extra_excludes=_effective_excludes,
                gitignore=_effective_gitignore,
            )
            files_by_type = detection.get("files", {})
            new_by_type = detection.get("new_files", {})
            code_files = [Path(p) for p in new_by_type.get("code", [])]
            doc_files = [Path(p) for p in new_by_type.get("document", [])]
            paper_files = [Path(p) for p in new_by_type.get("paper", [])]
            image_files = [Path(p) for p in new_by_type.get("image", [])]
            deleted_files = list(detection.get("deleted_files", []))
            excluded_files = list(detection.get("excluded_files", []))
            unchanged_total = sum(
                len(v) for v in detection.get("unchanged_files", {}).values()
            )
            # #1909:prune 集从图自身的 source_file 推导,不绑定 manifest——
            # 从未入过 manifest 的新排除文件也会因不在当前 corpus 而被摘除
            _seen_files = {f for _fl in files_by_type.values() for f in _fl}
            _seen_files.update(detection.get("unclassified", []))
            graph_stale_sources = _stale_graph_sources(
                existing_graph_path, target, _seen_files, detection=detection
            )
            # #2543 heal:陈旧"成功戳"的代码文件(提取当年失败仍被戳为完成)
            # 重新入列;若本轮再失败则本次不留戳,不会死循环
            _healed_sources = _zero_node_stamped_code_sources(
                existing_graph_path,
                target,
                detection.get("unchanged_files", {}).get("code", []),
            )
            if _healed_sources:
                _msg(
                    f"re-queuing {len(_healed_sources)} manifest-stamped code file(s) "
                    "with no nodes in graph.json (prior failed extraction, #2543)"
                )
                code_files.extend(Path(p) for p in _healed_sources)
        else:
            _msg(f"scanning {target}")
            detection = detect(target, extra_excludes=_effective_excludes,
                               cache_root=cache_root, gitignore=_effective_gitignore,
                               cache_root_final=True)
            files_by_type = detection.get("files", {})
            code_files = [Path(p) for p in files_by_type.get("code", [])]
            doc_files = [Path(p) for p in files_by_type.get("document", [])]
            paper_files = [Path(p) for p in files_by_type.get("paper", [])]
            image_files = [Path(p) for p in files_by_type.get("image", [])]
            deleted_files = []
            excluded_files = []
            graph_stale_sources = []
            unchanged_total = 0

        semantic_files = doc_files + paper_files + image_files
        # --code-only 跳过全部语义文件并在日志给出计数（#1734）
        if code_only and semantic_files:
            _msg(
                f"--code-only: skipping {len(semantic_files)} non-code file(s) "
                f"({len(doc_files)} docs, {len(paper_files)} papers, {len(image_files)} images)"
            )
            semantic_files = []
            doc_files = paper_files = image_files = []
        if deep_mode and incremental_mode and not code_only:
            # #1894:deep 命名空间(semantic-deep/)下 manifest 的 changed 门
            # 不是覆盖的有效代理 —— 温跑未变树会派发 0 个文件。扩大到全量
            # live 集合,由 deep 缓存决定命中(镜像 cli.py:3232-3255)
            _deep_all = [
                Path(p)
                for _ftype in ("document", "paper", "image")
                for p in files_by_type.get(_ftype, [])
            ]
            if len(_deep_all) != len(semantic_files):
                _msg(
                    f"deep mode: widening semantic pass from {len(semantic_files)} "
                    f"changed to {len(_deep_all)} live doc/paper/image file(s); the "
                    "deep semantic cache decides what is re-extracted"
                )
            semantic_files = _deep_all
        _unclassified = detection.get("unclassified", [])
        if _unclassified:
            _names = ", ".join(sorted({Path(p).name for p in _unclassified})[:6])
            _msg(f"{len(_unclassified)} file(s) not classified (no supported extension), skipped: {_names}")
        if incremental_mode:
            _excl_note = f"; {len(excluded_files)} excluded" if excluded_files else ""
            _msg(
                f"{len(code_files)} code, {len(doc_files)} docs, "
                f"{len(paper_files)} papers, {len(image_files)} images changed; "
                f"{unchanged_total} unchanged; {len(deleted_files)} deleted{_excl_note}"
            )
        else:
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
            _ast_kwargs: dict = {"cache_root": cache_root, "root": target, "cache_root_final": True}
            if max_workers is not None:
                _ast_kwargs["max_workers"] = max_workers
            # #2437/#2438(镜像 cli.py:3415-3496):增量只提取改动文件,跨文件
            # 解析器看不到未改动文件里的被调方,changed→unchanged 调用边会在
            # 合并时消失。从已持久化的图喂只读解析上下文;图不可读时 fail-open。
            if incremental_mode and existing_graph_path.exists():
                _ctx_nodes, _ctx_edges = _ast_resolution_context(
                    existing_graph_path, target, detection
                )
                if _ctx_nodes:
                    _ast_kwargs["resolution_context_nodes"] = _ctx_nodes
                if _ctx_edges:
                    _ast_kwargs["resolution_context_edges"] = _ctx_edges
            try:
                ast_result = _ast_extract(code_files, **_ast_kwargs)
            except Exception as exc:
                _msg(f"AST extraction failed: {exc}")
                # #2445:整段 AST 丢失默认致命;--allow-partial 才降级续跑
                if not allow_partial:
                    raise
                ast_result = {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}
                _extraction_incomplete = True
        # #2543:AST 失败源(failed_sources)须同时清 ast_hash,防陈旧戳
        _failed_ast_sources = list(ast_result.get("failed_sources") or [])

        # ---- 语义提取（缓存优先，未命中走 LLM）----
        sem_result: dict = {"nodes": [], "edges": [], "hyperedges": [],
                            "input_tokens": 0, "output_tokens": 0}
        # 本轮结果截断的语义文件:不入戳,使下次 detect_incremental 重新入队
        # (镜像 cli.py:3523-3527 的 #933 机制);在入库前捕获(标记随后被剥离)
        _partial_semantic_files: set[str] = set()
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
                _partial_semantic_files = set(_partial_source_files(fresh))
                try:
                    save_semantic_cache(fresh.get("nodes", []), fresh.get("edges", []),
                                        fresh.get("hyperedges", []), root=target,
                                        cache_root=cache_root, allowed_source_files=uncached_paths,
                                        mode=sem_cache_mode, prompt=prompt,
                                        partial_source_files=_partial_semantic_files or None,
                                        cache_root_final=True)
                except Exception as exc:
                    _msg(f"warning: could not write semantic cache: {exc}")
                _strip_partial_markers(fresh)
                sem_result["nodes"].extend(fresh.get("nodes", []))
                sem_result["edges"].extend(fresh.get("edges", []))
                sem_result["hyperedges"].extend(fresh.get("hyperedges", []))
                sem_result["input_tokens"] += fresh.get("input_tokens", 0)
                sem_result["output_tokens"] += fresh.get("output_tokens", 0)

        # #1527 孤儿清扫:语义缓存按内容哈希键、无版本,从不被 AST 版本清理
        # 清扫,内容变更/删除会留下永久孤儿。镜像 cli.py:3655-3686 —— 唯一
        # 内联处:prune_semantic_cache 无 cache_root_final 通道
        # (cache.py root/GRAPHIFY_OUT),对自定义 out_dir 会扫错目录;以 live
        # 哈希(<gout>/cache/{semantic,semantic-deep})等价实现,且 live 集取
        # files_by_type 全量(非增量 changed 子集,否则会删掉未变文档的有效条目)。
        try:
            _live_hashes: set[str] = set()
            for _kind in ("document", "paper", "image"):
                for _fp in files_by_type.get(_kind, []):
                    _abs = Path(_fp)
                    if not _abs.is_absolute():
                        _abs = Path(target) / _abs
                    if not _abs.is_file():
                        continue  # 已删除/缺失:留白使其条目被清扫
                    try:
                        _live_hashes.add(file_hash(_abs, target, cache_root=cache_root,
                                                   cache_root_final=True))
                    except OSError:
                        pass
            for _namespace in ("semantic", "semantic-deep"):
                _sem_dir = gout / "cache" / _namespace
                if not _sem_dir.is_dir():
                    continue
                for _entry in _sem_dir.rglob("*.json"):
                    if _entry.stem not in _live_hashes:
                        try:
                            _entry.unlink()
                        except OSError:
                            pass
        except Exception as exc:
            _msg(f"warning: could not prune semantic cache: {exc}")

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
        result["incremental"] = incremental_mode
        result["files_unchanged"] = unchanged_total
        result["tokens"] = {
            "input": merged["input_tokens"],
            "output": merged["output_tokens"],
            "cost_usd": estimate_cost(backend_eff, merged["input_tokens"],
                                      merged["output_tokens"]),
        }

        # ---- manifest 数据流（镜像 cli.py:3728-3773）----
        _manifest_files = _stamped_manifest_files(
            files_by_type, sem_result, target,
            partial_source_files=_partial_semantic_files,
            failed_ast_sources=_failed_ast_sources,
        )
        # 本轮派发但被 _stamped_manifest_files 丢弃的文件(失败 chunk / LLM 遗漏)
        # 仍在磁盘 manifest 上留着旧 semantic_hash,会掩盖遗漏(#1948)。
        # 以 semantic_files(=本轮实际派发集合,非全量 corpus)推导 cleared 集。
        _stamped_semantic = {f for _fl in _manifest_files.values() for f in _fl}
        _cleared_semantic = {str(p) for p in semantic_files} - _stamped_semantic
        # #2543:AST 失败须同时清 ast_hash(clear_ast)
        _cleared_ast = set(_failed_ast_sources)
        # 全量 save_manifest 为留在 corpus 但离开扫描集的文件剪除行(#1908);
        # 必须是 detect 的 RAW 全量输出,而非 #933 过滤后的 _manifest_files
        _scan_corpus = {f for _fl in files_by_type.values() for f in _fl}

        if no_cluster:
            # 高温零变化早退(镜像 cli.py:3796-3829):exclusion-only 变更也会
            # 走到这里(excluded 不在 deleted 里,#1908),须就地剪除新排除源
            if (incremental_mode and not code_files and not semantic_files
                    and not deleted_files):
                if graph_stale_sources:
                    _n_pruned = _prune_graph_json_sources(
                        existing_graph_path, graph_stale_sources
                    )
                    if _n_pruned:
                        _msg(
                            f"pruned {_n_pruned} node(s) from "
                            f"{len(graph_stale_sources)} source file(s) no longer "
                            "in the scan (deleted or excluded)."
                        )
                _msg("no incremental changes detected (--no-cluster); outputs left untouched.")
                try:
                    _save_manifest(_manifest_files, manifest_path=str(manifest_path),
                                   kind="both", root=target, scan_corpus=_scan_corpus,
                                   clear_semantic=_cleared_semantic,
                                   clear_ast=_cleared_ast or None)
                except Exception as exc:
                    _msg(f"warning: could not write manifest: {exc}")
                result["nodes"] = existing_graph_node_count(graph_json_path)
                result["skipped"] = True
                result["elapsed_seconds"] = time.perf_counter() - t0
                return result
            if incremental_mode:
                # #2169:raw 路径增量若只写"本轮的 changed 文件",未变更文件的
                # 全部节点会被静默丢弃。先把现有图合并前移(replace/prune 语义
                # 与 build_merge 一致;幸存者 prepend 保证下面 dedupe 保留 fresh 属性)
                _raw_prune_sources = _dedupe_ordered(
                    list(deleted_files) + list(excluded_files) + graph_stale_sources
                )
                try:
                    merged = merge_raw_extraction(
                        merged,
                        graph_path=existing_graph_path,
                        prune_sources=_raw_prune_sources or None,
                        root=target,
                    )
                except RuntimeError as exc:
                    raise RuntimeError(str(exc)) from exc  # 旧图不可解析:拒绝覆盖
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
            try:
                # #2012:记录扫描根,供后续 build_merge 相对化删除路径
                (gout / ".graphify_root").write_text(str(target), encoding="utf-8")
            except OSError:
                pass
            try:
                _save_manifest(_manifest_files, manifest_path=str(manifest_path),
                               kind="both", root=target, scan_corpus=_scan_corpus,
                               clear_semantic=_cleared_semantic,
                               clear_ast=_cleared_ast or None)
            except Exception as exc:
                _msg(f"warning: could not write manifest: {exc}")
            result["nodes"] = len(merged["nodes"])
            result["edges"] = len(merged["edges"])
        else:
            if incremental_mode:
                # 剪掉当前扫描不再覆盖的一切:真正删除的 manifest 行、活着但
                # 被排除的行(#1908)、图自身的 stale 源(#1909)
                _prune_sources = _dedupe_ordered(
                    list(deleted_files) + list(excluded_files) + graph_stale_sources
                )
                try:
                    G = build_merge([merged], graph_path=existing_graph_path,
                                    prune_sources=_prune_sources or None,
                                    dedup=not no_dedup, root=target)
                except ValueError as exc:
                    # #2881:--no-dedup 会 arm build_merge 的 #479 shrink guard,
                    # 拒绝丢弃本轮既未重提取也未剪枝的文件的所有者节点
                    # (镜像 cli.py:3977-3983)。旧图未动、本轮不写图 → 错误态。
                    raise RuntimeError(str(exc)) from exc
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
            try:
                (gout / ".graphify_root").write_text(str(target), encoding="utf-8")
            except OSError:
                pass
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
            try:
                _save_manifest(_manifest_files, manifest_path=str(manifest_path),
                               kind="both", root=target, scan_corpus=_scan_corpus,
                               clear_semantic=_cleared_semantic,
                               clear_ast=_cleared_ast or None)
            except Exception as exc:
                _msg(f"warning: could not write manifest: {exc}")
            result["nodes"] = G.number_of_nodes()
            result["edges"] = G.number_of_edges()
            result["communities"] = len(communities)
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
