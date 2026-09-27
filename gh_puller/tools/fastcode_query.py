"""Run upstream FastCode QA with every LLM request mediated by the run owner.

The container holds no LLM credentials. A job suspends at the upstream client
boundary; the host records and executes that request, then supplies its answer.
"""

import copy
import hashlib
import threading
import time
from types import SimpleNamespace


class QueryCancelled(BaseException):
    """Escape upstream fallback handlers when the owning run stops."""


class QueryJob:
    def __init__(self, run, request):
        self.condition = threading.Condition()
        self.pending = self.response = self.result = None
        self.cancelled = False
        self.sequence = 0
        self.trace = []
        self.touched = time.monotonic()
        self.thread = threading.Thread(target=self.execute, args=(run, request), daemon=True)
        self.thread.start()

    def execute(self, run, request):
        try:
            result = run(request, self)
        except QueryCancelled:
            result = {"error": {"type": "Cancelled", "message": "The owning run stopped this query."}}
        except Exception as exc:
            result = {"error": {"type": type(exc).__name__, "message": str(exc)}}
        with self.condition:
            self.result = result
            self.condition.notify_all()

    def client(self, phase):
        def create(**body):
            with self.condition:
                if self.cancelled:
                    raise QueryCancelled
                self.sequence += 1
                self.pending = {"sequence": self.sequence, "phase": phase, "body": body}
                self.trace.append({"model_request": self.pending})
                self.condition.notify_all()
                # Reclaim abandoned work, without setting a deadline on the query.
                while self.response is None and not self.cancelled:
                    self.condition.wait(30)
                    if time.monotonic() - self.touched > 3600:
                        self.cancelled = True
                if self.cancelled:
                    raise QueryCancelled
                content = self.response
                self.response = self.pending = None
                self.trace.append({"model_response": {"sequence": self.sequence, "content": content}})
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def step(self, payload):
        with self.condition:
            self.touched = time.monotonic()
            if "response" in payload:
                if not self.pending or payload.get("sequence") != self.pending["sequence"]:
                    raise ValueError("Response does not match the pending FastCode model request")
                if self.response is not None:
                    raise ValueError("This FastCode model request already has a response")
                self.response = payload["response"]
                self.condition.notify_all()
            self.condition.wait_for(lambda: self.result is not None or
                                    (self.pending is not None and self.response is None), timeout=10)
            if self.result is not None:
                return {"status": "complete", "result": self.result, "trace": list(self.trace)}
            if self.pending is not None and self.response is None:
                return {"status": "model_request", **self.pending}
            return {"status": "running"}

    def cancel(self):
        with self.condition:
            self.cancelled = True
            self.condition.notify_all()


class QueryJobs:
    def __init__(self, service):
        self.service = service
        self.jobs = {}
        self.lock = threading.Lock()

    def respond(self, path, payload):
        if path not in ("/query/start", "/query/step", "/query/cancel"):
            raise ValueError(f"Unknown query operation: {path}")
        with self.lock:
            if path == "/query/start":
                for key, job in list(self.jobs.items()):
                    if time.monotonic() - job.touched > 3600:
                        job.cancel()
                        del self.jobs[key]
                request = payload["request"]
                if not request.get("indexes") or len(set(request["indexes"])) != len(request["indexes"]):
                    raise ValueError("Choose distinct prepared indexes")
                for name in request["indexes"]:
                    self.service.engine(name)
                job_id = payload["job_id"]
                if job_id in self.jobs:
                    raise ValueError("FastCode query job already exists")
                self.jobs[job_id] = QueryJob(self.run, request)
            else:
                job_id = payload["job_id"]
            job = self.jobs[job_id]
            if path == "/query/cancel":
                job.cancel()
                del self.jobs[job_id]
                return {"status": "cancelled", "trace": list(job.trace)}
        result = job.step(payload)
        if result["status"] == "complete":
            with self.lock:
                self.jobs.pop(job_id, None)
        return {"job_id": job_id, **result}

    def retriever(self, names, config):
        import networkx as nx
        from fastcode.graph_builder import CodeGraphBuilder
        from fastcode.retriever import HybridRetriever
        from fastcode.vector_store import VectorStore

        engines = [self.service.engine(name) for name in names]
        vectors, graph = VectorStore(config), CodeGraphBuilder(config)
        vectors.initialize(engines[0][0].embedder.embedding_dim)
        for original, _, units in engines:
            store = original.vector_store
            vectors.add_vectors(store.index.reconstruct_n(0, store.index.ntotal), copy.deepcopy(store.metadata))
            files = sorted(u["path"] for u in units.values() if u["type"] == "file")
            repo_name = store.metadata[0]["repo_name"]
            vectors._in_memory_repo_overviews[repo_name] = {"metadata": {
                "summary": "Prepared source snapshot", "structure_text": "\n".join(files), "file_structure": {}}}
        for attribute in ("call_graph", "dependency_graph", "inheritance_graph"):
            setattr(graph, attribute, nx.compose_all([getattr(e[1], attribute) for e in engines]))
        graph.element_by_id = {k: v for _, g, _ in engines for k, v in g.element_by_id.items()}
        graph.element_by_name = {k: v for _, g, _ in engines for k, v in g.element_by_name.items()}
        retriever = HybridRetriever(config, vectors, engines[0][0].embedder, graph)
        retriever.index_for_bm25([e for r, _, _ in engines for e in r.full_bm25_elements])
        retriever.current_loaded_repos = list(names)
        return retriever

    def run(self, request, job):
        from fastcode.answer_generator import AnswerGenerator
        from fastcode.iterative_agent import IterativeAgent
        from fastcode.query_processor import QueryProcessor
        from fastcode.repo_selector import RepositorySelector

        def bound(base, initializer, phase):
            return type(base.__name__, (base,), {initializer: lambda _self: job.client(phase)})

        names, query = request["indexes"], request["query"]
        def verify_sources():
            for name in names:
                for unit in self.service.engine(name)[2].values():
                    if unit["type"] == "file":
                        with open(unit["file_path"], "rb") as stream:
                            if hashlib.file_digest(stream, "sha256").hexdigest() != unit["file_sha256"]:
                                raise ValueError(f"Indexed worktree changed: {name}/{unit['path']}")

        verify_sources()
        config = copy.deepcopy(self.service.engine(names[0])[0].config)
        config["vector_store"]["in_memory"] = True
        config["generation"].update(provider="openai", enable_multi_turn=bool(request.get("history")))
        config["query"].update(use_llm_enhancement=True, llm_enhancement_mode="always")
        retriever = self.retriever(names, config)
        selector = bound(RepositorySelector, "_initialize_client", "file_selection")(config)
        selector.model = request["model"]
        retriever.repo_selector = selector
        history = request.get("history")
        processor = bound(QueryProcessor, "_initialize_llm_client", "query_enhancement")(config)
        processor.model = request["model"]
        strategy = request.get("strategy", "iterative")
        processed = processor.process(query, dialogue_history=history, use_llm_enhancement=strategy == "standard")
        query_info = processed.to_dict()
        if strategy == "iterative":
            agent = bound(IterativeAgent, "_initialize_client", "iterative_retrieval")(
                config, retriever, self.service.catalog["repositories_root"], retriever.full_bm25_elements)
            agent.model = request["model"]
            agent.set_repo_stats(retriever._calculate_repo_stats())
            for method in ("search_codebase", "list_directory", "read_file_content", "get_file_structure_summary"):
                original = getattr(agent.tools, method)

                def traced(*args, _fn=original, _name=method, **kwargs):
                    result = _fn(*args, **kwargs)
                    job.trace.append({"tool": _name, "args": args, "kwargs": kwargs, "result": result})
                    return result

                setattr(agent.tools, method, traced)
            elements, metadata = agent.retrieve_with_iteration(query, processed, query_info, names, history)
            job.trace.append({"iteration_metadata": metadata, "tool_call_history": agent.tool_call_history})
        elif strategy == "standard":
            elements = retriever.retrieve(processed, repo_filter=names, use_agency_mode=False,
                                          enable_file_selection=request.get("select_files", True),
                                          dialogue_history=history)
        else:
            raise ValueError(f"Unknown query strategy: {strategy}")
        verify_sources()
        # Substantiate every returned source against its frozen index, including
        # file-level elements synthesized by the iterative agent's filesystem tools.
        sources, exact = [], []
        for match in elements:
            element = match["element"]
            name = element["repo_name"]
            if name not in names:
                raise ValueError("FastCode query returned an unselected repository version")
            units = self.service.engine(name)[2]
            unit = units.get(element["id"])
            if unit is None:
                unit = self.service.select(name, {"path": element["relative_path"],
                                                  **({"symbol": element["name"]}
                                                     if element["type"] != "file" else {})})
            if "error" in unit:
                raise ValueError(f"Cannot substantiate retrieved source: {element['relative_path']}")
            exact.append({**match, "element": {**element,
                                               "id": unit["id"], "relative_path": unit["path"],
                                               "file_path": unit["file_path"],
                                               "metadata": {k: v for k, v in element.get("metadata", {}).items()
                                                            if k != "embedding"},
                                               "code": "\n".join(unit["lines"]),
                                               "start_line": unit["start_line"], "end_line": unit["end_line"]}})
            sources.append({**self.service.identity_for(name), **{k: v for k, v in unit.items() if k != "lines"}})
        result = {"strategy": strategy, "sources": sources, "query_info": query_info}
        job.trace.append({"retrieved": exact})
        if request.get("output", "answer") == "evidence":
            result["evidence"] = exact
        else:
            generator = bound(AnswerGenerator, "_initialize_client", "answer_generation")(config)
            generator.model = request["model"]
            generated = generator.generate(query, exact, query_info, history)
            job.trace.append({"generated": generated})
            result.update({k: v for k, v in generated.items() if k != "sources"})
        return result
