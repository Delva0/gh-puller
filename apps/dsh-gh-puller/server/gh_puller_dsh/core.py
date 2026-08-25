"""graphify 工具纯逻辑(无协议、无 IO 端点):图路径约定与 deepwiki._graph_dir 完全同式。

图目录:<envs.DEEPWIKI_ROOT>/graphify/{repo_type}_{name}(name 经 gh_puller.utils.Repo 的
静态命名公式,URL 给 {owner}_{repo}、本地给 basename;构建 Repo 实例有 makedirs 副作用,
命名只用静态方法)。deepwiki 已建的图直接复用,反之亦然。
"""

from pathlib import Path

from gh_puller import envs
from gh_puller.graphify import extract, query
from gh_puller.utils import Repo, _path_is_url


def repo_dir(repo: str, repo_type: str | None) -> Path:
    """单仓库图产物目录(graphify.{t}_{name});纯命名公式,无克隆、无副作用。"""
    t = repo_type or ("github" if _path_is_url(repo) else "local")
    name = Repo._extract_repo_name(repo, t)  # 先定 t 再命名:None 对 URL 会退化为裸 repo 名
    return Path(envs.DEEPWIKI_ROOT) / "graphify" / f"{t}_{name}"


def graph_of(repo: str | None, repo_type: str | None, default_graph: str | None) -> Path | None:
    """工具级 repo 参数优先,其次启动缺省图;都无 → None(调用方给可操作错误文本)。"""
    if repo:
        return repo_dir(repo, repo_type) / "graph.json"
    return Path(default_graph) if default_graph else None


def query_text(question: str, repo: str | None, repo_type: str | None, default_graph: str | None) -> str:
    """graphify 问答文本(镜像 deepwiki._graphify_server 的图查询工具体;异常降级为可读文本)。"""
    try:
        graph_path = graph_of(repo, repo_type, default_graph)
        if graph_path is None:
            return "No graph configured: pass repo=<URL|local path> or start the worker with a default graph."
        result = query(question.strip(), graph_path=graph_path)
        text = result.get("answer") or ""
    except Exception as exc:
        return f"Graph query failed: {type(exc).__name__}: {exc}"
    return text.strip() or "(No matching results in code graph)"


def index_text(path: str, repo_type: str | None) -> str:
    """code_only AST 建图(纯本地,无 key);产物即 deepwiki 同约定目录,两者互相复用。"""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return f"Index failed: path not found: {p}"
    result = extract(path=p, code_only=True, out_dir=repo_dir(str(p), repo_type or "local"))
    if result.get("error"):
        return f"Index failed: {result['error']}"
    return f"indexed {result['nodes']} nodes / {result['edges']} edges -> {result['graph_json']}"
