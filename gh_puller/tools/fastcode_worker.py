"""Frozen version indexes and upstream FastCode capabilities inside Docker."""

import copy
import fnmatch
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path

try:
    from .fastcode_query import QueryJobs
except ImportError:  # Standalone container worker.
    from fastcode_query import QueryJobs

UPSTREAM = Path("/opt/fastcode")
MODEL = Path("/opt/fastcode-model")
MODEL_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
MODEL_SHA256 = "eaa086f0ffee582aeb45b36e34cdd1fe2d6de2bef61f8a559a1bbc9bd955917b"
MAX_SOURCE_CHARS = 24000


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def configuration(directory, worktree):
    import yaml

    config = yaml.safe_load((UPSTREAM / "config/config.yaml").read_text())
    config["repo_root"] = str(worktree)
    config["embedding"].update(model=str(MODEL), device="cpu")
    config["indexing"]["generate_repo_overview"] = False
    config["vector_store"]["persist_directory"] = str(directory)
    config["retrieval"].update(enable_agency_mode=False, select_repos_by_overview=False,
                                enable_two_stage_retrieval=False, max_results=10)
    return config


def components(config, embedder):
    from fastcode.graph_builder import CodeGraphBuilder
    from fastcode.retriever import HybridRetriever
    from fastcode.vector_store import VectorStore

    vectors, graph = VectorStore(config), CodeGraphBuilder(config)
    return vectors, graph, HybridRetriever(config, vectors, embedder, graph)


def source_unit(element, worktree):
    """Store exact lines from the indexed snapshot, not parser-normalized code."""
    path = Path(worktree) / element.relative_path
    encoding = "utf-8"
    try:
        content = path.read_text(encoding=encoding)
    except UnicodeDecodeError:
        # Match the upstream loader's fallback without silently replacing bytes.
        encoding = "latin-1"
        content = path.read_text(encoding=encoding)
    lines = content.split("\n")
    if lines[-1] == "":
        lines.pop()
    start, end = element.start_line, element.end_line
    # Upstream counts the empty field after a final newline as a file line.
    if element.type == "file":
        end = min(end, len(lines))
    elif element.type == "documentation":
        # Upstream docstring units have placeholder 1:1 ranges. Return their
        # containing file, whose lines we can substantiate, instead of that span.
        start, end = 1, len(lines)
    if not (1 <= start <= end <= len(lines) or (not lines and start == 1 and end == 0)):
        raise ValueError(f"Invalid FastCode source range: {path}:{start}-{end}")
    metadata = getattr(element, "metadata", {}) or {}
    return {"id": element.id, "type": element.type, "name": element.name,
            "signature": getattr(element, "signature", None),
            "language": getattr(element, "language", None),
            "class_name": metadata.get("class_name"),
            "path": element.relative_path, "file_path": str(path), "start_line": start, "end_line": end,
            "file_sha256": digest(path), "encoding": encoding,
            "source_scope": "file" if element.type in {"file", "documentation"} else "element",
            "lines": lines[start - 1:end]}


def prepare(spec, root):
    import numpy as np
    from fastcode.embedder import CodeEmbedder
    from fastcode.global_index_builder import GlobalIndexBuilder
    from fastcode.indexer import CodeIndexer
    from fastcode.loader import RepositoryLoader
    from fastcode.module_resolver import ModuleResolver
    from fastcode.parser import CodeParser
    from fastcode.symbol_resolver import SymbolResolver

    started = time.monotonic()
    root.mkdir(parents=True, exist_ok=False)
    entries = {}
    if digest(MODEL / "model.safetensors") != MODEL_SHA256:
        raise ValueError("Embedding model checksum mismatch")
    embedder = CodeEmbedder(configuration(root, root))
    model_seconds = time.monotonic() - started
    for item in spec:
        tick = time.monotonic()
        name = item["index"]
        if name in entries:
            raise ValueError(f"Duplicate index: {name}")
        revision = git(item["repository"], "rev-parse", "--verify", item["revision"] + "^{commit}")
        directory = root / hashlib.sha256((name + revision).encode()).hexdigest()[:20]
        directory.mkdir()
        worktree = directory / "source"
        git(item["repository"], "worktree", "add", "--detach", str(worktree), revision)
        if git(worktree, "status", "--porcelain"):
            raise ValueError(f"Index source is not clean: {worktree}")
        config = configuration(directory, worktree)
        loader = RepositoryLoader(config)
        loader.load_from_path(str(worktree), target_dir=str(directory))
        vectors, graph, retriever = components(config, embedder)
        elements = CodeIndexer(config, loader, CodeParser(config), embedder, vectors).index_repository(
            repo_name=name, repo_url=item["url"])
        if not elements:
            raise ValueError(f"FastCode produced an empty index: {name}")
        vectors.initialize(embedder.embedding_dim)
        vectors.add_vectors(np.array([element.metadata["embedding"] for element in elements]),
                            [element.to_dict() for element in elements])
        maps = GlobalIndexBuilder(config)
        maps.build_maps(elements, str(worktree))
        resolver = ModuleResolver(maps)
        graph.build_graphs(elements, resolver, SymbolResolver(maps, resolver))
        retriever.index_for_bm25(elements)
        vectors.save("index")
        if not graph.save("index"):
            raise RuntimeError(f"Failed to save graph: {name}")
        if not retriever.save_bm25("index"):
            raise RuntimeError(f"Failed to save BM25: {name}")
        units = {element.id: source_unit(element, worktree) for element in elements}
        write(directory / "sources.json", units)
        write(directory / "config.json", config)
        entries[name] = {"repository": item["url"], "revision": item["revision"], "commit": revision,
                         "worktree": str(worktree), "directory": str(directory), "elements": len(elements),
                         "files": len({unit["path"] for unit in units.values()}),
                         "graphs": {kind: {"nodes": value.number_of_nodes(), "edges": value.number_of_edges()}
                                    for kind, value in (("calls", graph.call_graph),
                                                        ("dependencies", graph.dependency_graph),
                                                        ("inheritance", graph.inheritance_graph))},
                         "artifacts": {p.name: digest(p) for p in sorted(directory.iterdir()) if p.is_file()},
                         "build_seconds": time.monotonic() - tick}
    repositories = root / "repositories"
    repositories.mkdir()
    for name, item in entries.items():
        alias = repositories / name
        if not alias.resolve().is_relative_to(repositories.resolve()):
            raise ValueError(f"Invalid index name: {name}")
        alias.parent.mkdir(parents=True, exist_ok=True)
        alias.symlink_to(item["worktree"], target_is_directory=True)
    catalog = {"format": 2, "upstream_commit": git(UPSTREAM, "rev-parse", "HEAD"),
               "worker_sha256": digest(__file__), "model": MODEL_ID, "model_revision": MODEL_REVISION,
               "support_sha256": digest(Path(__file__).with_name("fastcode_query.py")),
               "repositories_root": str(repositories),
               "model_sha256": MODEL_SHA256,
               "indexing_llm_calls": False,
               "retrieval": "semantic, BM25, symbols, text, graphs; optional host-mediated QA",
               "model_load_seconds": model_seconds, "indexes": entries,
               "prepared_at": datetime.now(UTC).isoformat(), "indexing_seconds": time.monotonic() - started}
    write(root / "catalog.json", catalog)
    return catalog


def render_source(unit, *, view="source", start_line=None, end_line=None, max_chars=MAX_SOURCE_CHARS):
    result = {key: value for key, value in unit.items() if key != "lines"}
    if view == "outline":
        return result
    start = unit["start_line"] if start_line is None else max(start_line, unit["start_line"])
    end = unit["end_line"] if end_line is None else min(end_line, unit["end_line"])
    if start > end and unit["lines"]:
        raise ValueError(f"Requested lines are outside {unit['start_line']}-{unit['end_line']}")
    rendered, size = [], 0
    for number, line in enumerate(unit["lines"], unit["start_line"]):
        if number < start or number > end:
            continue
        numbered = f"{number}: {line}"
        if rendered and size + len(numbered) + 1 > max_chars:
            break
        rendered.append(numbered)
        size += len(numbered) + 1
    returned_end = start + len(rendered) - 1
    result.update(source="\n".join(rendered), returned_start_line=start, returned_end_line=returned_end,
                  truncated=returned_end < end)
    if result["truncated"]:
        result["next_line"] = result["returned_end_line"] + 1
    return result


class SearchService:
    def __init__(self, catalog_path):
        from fastcode.embedder import CodeEmbedder

        self.catalog = json.loads(catalog_path.read_text())
        self.identity = digest(catalog_path)
        if self.catalog["upstream_commit"] != git(UPSTREAM, "rev-parse", "HEAD"):
            raise ValueError("FastCode source changed since indexing")
        if self.catalog["worker_sha256"] != digest(__file__):
            raise ValueError("FastCode worker changed since indexing")
        if self.catalog.get("support_sha256") != digest(Path(__file__).with_name("fastcode_query.py")):
            raise ValueError("FastCode query worker changed since indexing")
        self.engines = {}
        embedder = None
        for name, item in self.catalog["indexes"].items():
            directory = Path(item["directory"])
            for filename, expected in item["artifacts"].items():
                if digest(directory / filename) != expected:
                    raise ValueError(f"Index artifact changed: {name}/{filename}")
            config = json.loads((directory / "config.json").read_text())
            if embedder is None:
                embedder = CodeEmbedder(config)
            vectors, graph, retriever = components(config, embedder)
            if not all((vectors.load("index"), graph.load("index"), retriever.load_bm25("index"))):
                raise RuntimeError(f"Failed to load prepared index: {name}")
            self.engines[name] = (retriever, graph, json.loads((directory / "sources.json").read_text()))
        # Warm tokenization and inference outside the timed agent run.
        embedder.embed_batch(["repository source code"])
        self.queries = QueryJobs(self)

    def engine(self, name):
        if name not in self.engines:
            raise ValueError(f"Unknown index: {name}; available: {list(self.engines)}")
        return self.engines[name]

    def identity_for(self, name):
        item = self.catalog["indexes"][name]
        return {"index": name, **{key: item[key] for key in ("repository", "revision", "commit")}}

    @staticmethod
    def filtered(unit, filters):
        path = filters.get("path")
        return (not path or fnmatch.fnmatchcase(unit["path"], path) or path in unit["path"]) and all(
            not filters.get(key) or unit.get(key) == filters[key] for key in ("type", "language"))

    def select(self, name, request):
        units = self.engine(name)[2]
        if not any(request.get(key) for key in ("id", "path", "symbol")):
            raise ValueError("Specify id, path, or symbol")
        if request.get("id"):
            selected = [units[request["id"]]] if request["id"] in units else []
        else:
            selected = list(units.values())
            if request.get("path"):
                selected = [u for u in selected if u["path"] == request["path"]]
            if request.get("symbol"):
                symbol = request["symbol"]
                selected = [u for u in selected if symbol in (u["name"],
                    f"{u.get('class_name')}.{u['name']}")]
            elif request.get("path"):
                selected = [u for u in selected if u["type"] == "file"]
        if len(selected) != 1:
            return {"error": {"type": "AmbiguousSymbol" if selected else "NotFound",
                              "message": "Specify an exact id or path plus symbol.",
                              "candidates": [render_source(u, view="outline") for u in selected]}}
        return selected[0]

    @staticmethod
    def page(items, request):
        offset, limit = request.get("offset", 0), request.get("limit", 20)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("offset must be nonnegative; limit must be 1-100")
        page = {"total": len(items), "offset": offset, "has_more": offset + limit < len(items)}
        if page["has_more"]:
            page["next_request"] = {**request, "offset": offset + limit}
        return items[offset:offset + limit], page

    @staticmethod
    def render(units, request):
        rendered = [render_source(u, view=request.get("view", "source"),
                                  start_line=request.get("start_line"), end_line=request.get("end_line"),
                                  max_chars=request.get("max_chars", MAX_SOURCE_CHARS)) for u in units]
        for item in rendered:
            if item.get("truncated"):
                item["continue_request"] = {"index": request["index"], "id": item["id"],
                                            "start_line": item["next_line"], "view": "source"}
                if request.get("end_line"):
                    item["continue_request"]["end_line"] = request["end_line"]
        # A source reference is safe only if the parent body was actually returned.
        if request.get("deduplicate", True):
            covered = []
            for item in sorted(rendered, key=lambda u: -(u.get("returned_end_line", 0)
                                                        - u.get("returned_start_line", 0))):
                if "source" not in item:
                    continue
                parent = next((p for p in covered if p["path"] == item["path"]
                               and p["returned_start_line"] <= item["returned_start_line"]
                               and item["returned_end_line"] <= p["returned_end_line"]), None)
                if parent:
                    del item["source"]
                    item["source_ref"] = {"id": parent["id"], "start_line": item["returned_start_line"],
                                          "end_line": item["returned_end_line"]}
                else:
                    covered.append(item)
        return rendered

    def search(self, request):
        name, query, limit = request["index"], request["query"], request.get("limit", 5)
        retriever, _, units = self.engine(name)
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ValueError("query must contain 1-4000 characters")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        started = time.monotonic()
        filters, mode = request.get("filters", {}), request.get("mode", "hybrid")
        if mode == "symbol":
            q = query.casefold()
            found = [u for u in units.values() if q in
                     (f"{u.get('class_name')}.{u['name']}" if u.get("class_name") else u["name"]).casefold()]
            found.sort(key=lambda u: (u["name"].casefold() != q, not u["name"].casefold().startswith(q),
                                      u["path"], u["start_line"]))
            matches = [{"element": {"id": u["id"], "repo_name": name}, "total_score": None} for u in found]
        elif mode in ("text", "regex"):
            from fastcode.agent_tools import AgentTools

            for u in units.values():
                if u["type"] == "file" and digest(u["file_path"]) != u["file_sha256"]:
                    raise ValueError(f"Indexed worktree changed: {u['path']}")
            found = AgentTools(self.catalog["indexes"][name]["worktree"]).search_codebase(
                query, file_pattern=filters.get("path", "*"), use_regex=mode == "regex",
                case_sensitive=request.get("case_sensitive", False), max_results=100)
            if not found.get("success"):
                raise ValueError(found.get("error", "Text search failed"))
            files = {}
            for u in units.values():
                if u["type"] == filters.get("type", "file"):
                    files.setdefault(u["path"], []).append(u)
            matches = []
            for m in found["results"]:
                for u in files.get(m["file"], []):
                    lines = [line for line in m["matches"]
                             if u["start_line"] <= line["line_number"] <= u["end_line"]]
                    if lines or u["type"] == "file":
                        matches.append({"element": {"id": u["id"], "repo_name": name}, "total_score": None,
                                        "text_matches": lines, "match_type": m["match_type"],
                                        "file_match_limit_reached": len(m["matches"]) == 20})
        else:
            local = copy.copy(retriever)  # Request options must not mutate the shared engine.
            local.max_results = max(30, limit + request.get("offset", 0))
            if mode == "hybrid":
                if request.get("keywords") or request.get("pseudocode"):
                    from fastcode.query_processor import ProcessedQuery

                    query = ProcessedQuery(query, query, request.get("keywords", []), "unknown", [], {},
                                           pseudocode_hints=request.get("pseudocode"))
                matches = local.retrieve(query, enable_file_selection=False, use_agency_mode=False)
            elif mode in ("semantic", "keyword"):
                found = (local._semantic_search(query, top_k=100) if mode == "semantic"
                         else local._keyword_search(query, top_k=100))
                matches = [{"element": m, "total_score": float(score)} for m, score in found]
            else:
                raise ValueError(f"Unknown search mode: {mode}")
        selected, metadata = [], {}
        for match in matches:
            element = match["element"]
            if element["repo_name"] != name:
                raise RuntimeError("FastCode returned an element from another version")
            unit = units[element["id"]]
            if self.filtered(unit, filters):
                selected.append(unit)
                metadata[unit["id"]] = {k: v for k, v in match.items() if k != "element"}
        selected, page = self.page(selected, {**request, "limit": limit})
        rendered = self.render(selected, request)
        for result in rendered:
            result.update(metadata[result["id"]])
        output = {**self.identity_for(name), "query": request["query"], "mode": mode, "matches": rendered,
                  "page": page, "exhaustive": mode == "symbol", "search_seconds": time.monotonic() - started}
        if mode in ("text", "regex"):
            output["text_limits"] = {"files": 100, "matches_per_file": 20, "chars_per_line": 200}
        if request.get("expand"):
            output["expansion"] = [self.relations({"index": name, "id": u["id"], **request["expand"]})
                                   for u in selected]
        return output

    def read(self, request):
        name = request["index"]
        unit = self.select(name, request)
        if "error" in unit:
            return {**self.identity_for(name), **unit}
        selected = [unit]
        if request.get("view") == "outline" and unit["type"] in ("file", "class"):
            selected += [u for u in self.engine(name)[2].values() if u["id"] != unit["id"]
                         and u["type"] in ("function", "class") and u["path"] == unit["path"]
                         and unit["start_line"] <= u["start_line"] <= u["end_line"] <= unit["end_line"]]
        return {**self.identity_for(name), "matches": self.render(selected, request)}

    def browse(self, request):
        if not request.get("index"):
            return {"indexes": [{**self.identity_for(name), **{k: item[k] for k in ("files", "elements", "graphs")}}
                                for name, item in self.catalog["indexes"].items()]}
        name = request["index"]
        units = self.engine(name)[2]
        path = request.get("path", "").strip("/")
        if any(u["path"] == path and u["type"] == "file" for u in units.values()):
            return self.read({**request, "view": "outline"})
        prefix = path + "/" if path else ""
        entries = {}
        for u in units.values():
            if u["type"] != "file" or not u["path"].startswith(prefix):
                continue
            suffix = u["path"][len(prefix):]
            if "/" in suffix and not request.get("recursive", False):
                child = prefix + suffix.split("/", 1)[0]
                entries[child] = {"path": child, "type": "directory"}
            else:
                entries[u["path"]] = render_source(u, view="outline")
        selected, page = self.page([entries[k] for k in sorted(entries)], request)
        return {**self.identity_for(name), "entries": selected, "page": page,
                "scope": "indexed files", "graphs": self.catalog["indexes"][name]["graphs"]}

    def relations(self, request):
        name = request["index"]
        _, graph, units = self.engine(name)
        source = self.select(name, request)
        if "error" in source:
            return {**self.identity_for(name), **source}
        kinds = {"calls": ("call_graph", False), "called_by": ("call_graph", True),
                 "depends_on": ("dependency_graph", False), "depended_on_by": ("dependency_graph", True),
                 "inherits": ("inheritance_graph", False), "inherited_by": ("inheritance_graph", True)}
        requested = request.get("relations", list(kinds))
        if not requested or set(requested) - kinds.keys():
            raise ValueError(f"relations must be chosen from {list(kinds)}")
        edges = []
        if request.get("to"):
            target = self.select(name, request["to"])
            if "error" in target:
                return {**self.identity_for(name), **target}
            paths = []
            for kind in requested:
                attribute, reverse = kinds[kind]
                import networkx as nx

                g = getattr(graph, attribute).subgraph(units)
                g = g.reverse() if reverse else g
                try:
                    path = nx.shortest_path(g, source["id"], target["id"])
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                paths.append({"relation": kind, "ids": path})
                edges.extend({"from": a, "to": b, "relation": kind} for a, b in pairwise(path))
        else:
            paths = None
            frontier, visited = [source["id"]], {source["id"]}
            depth = request.get("depth", 1)
            if type(depth) is not int or not 1 <= depth <= 5:
                raise ValueError("depth must be 1-5")
            for _ in range(depth):
                upcoming = []
                for eid in frontier:
                    for kind in requested:
                        attribute, reverse = kinds[kind]
                        g = getattr(graph, attribute)
                        neighbors = (g.predecessors(eid) if reverse else g.successors(eid)) if eid in g else []
                        for other in sorted(neighbors):
                            if other not in units:
                                continue
                            edges.append({"from": eid, "to": other, "relation": kind})
                            if other not in visited:
                                visited.add(other)
                                upcoming.append(other)
                frontier = upcoming
        edges = [json.loads(s) for s in dict.fromkeys(json.dumps(e, sort_keys=True) for e in edges)]
        selected_edges, page = self.page(edges, request)
        visible = dict.fromkeys([source["id"], *(e[k] for e in selected_edges for k in ("from", "to"))])
        return {**self.identity_for(name), "root": source["id"], "edges": selected_edges,
                "nodes": self.render([units[i] for i in visible], {"view": "outline", **request}),
                "page": page, **({"paths": paths} if paths is not None else {}),
                "scope": "Static indexed relationships; missing edges do not establish absence."}

    def respond(self, path, payload):
        if payload.get("catalog_sha256") != self.identity:
            return {"error": "FastCode catalog changed; prepare a new run"}
        result = {"catalog_sha256": self.identity}
        if path == "/health":
            return {**result, "indexes": self.catalog["indexes"], "api_version": 2,
                    "capabilities": ["search", "browse", "read", "relations", "query"],
                    "llm_calls": "query only; host-mediated"}
        if path.startswith("/query/"):
            return {**result, **self.queries.respond(path, payload)}
        handlers = {"/search": self.search, "/browse": self.browse, "/read": self.read,
                    "/relations": self.relations}
        if path not in handlers or not isinstance(payload.get("requests"), list):
            return {"error": "Expected a supported operation with a requests array"}
        if not 1 <= len(payload["requests"]) <= 8:
            return {"error": "Expected 1-8 search requests"}
        results = []
        for request in payload["requests"]:
            try:
                results.append(handlers[path](request))
            except Exception as exc:
                logging.getLogger(__name__).exception("FastCode search failed")
                results.append({"request": request, "error": {"type": type(exc).__name__, "message": str(exc)}})
        return {**result, "results": results}


def serve(catalog, port):
    service = SearchService(catalog)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                response = service.respond(self.path, payload)
            except Exception as exc:
                response = {"error": f"{type(exc).__name__}: {exc}"}
            body = json.dumps(response, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    # Prevent upstream load_dotenv() from importing repository or host credentials.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    for credential in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        os.environ.pop(credential, None)
    sys.path.insert(0, str(UPSTREAM))
    logging.basicConfig(level=logging.INFO)
    command, location = sys.argv[1:3]
    if command == "prepare":
        prepare(json.load(sys.stdin), Path(location))
    elif command == "serve":
        serve(Path(location), int(sys.argv[3]))
    else:
        raise SystemExit("Usage: fastcode_worker.py prepare ROOT | serve CATALOG PORT")
