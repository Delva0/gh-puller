"""Query an operator-prepared FastCode service in the connected container."""

import asyncio
import copy
import json
import uuid
from dataclasses import replace

from jsonschema import Draft202012Validator, ValidationError

from .registry import BATCH_OUTPUT, ToolProvider, input_error, tool


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


INDEX = {"type": "string", "description": "Prepared repository@version."}
QUERY = {"type": "string", "minLength": 1, "maxLength": 4000}
SELECTOR = {"id": {"type": "string", "description": "ID returned by codebase."},
            "path": {"type": "string", "description": "Repository-relative file or directory (browse)."},
            "symbol": {"type": "string", "description": "Exact symbol or Class.method; path disambiguates."}}
HAS_SELECTOR = {"anyOf": [{"required": [key]} for key in SELECTOR]}
VIEW = {"view": {"type": "string", "enum": ["outline", "source"],
                 "description": "Outline: signatures and ranges. Source: numbered original lines."},
        "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000,
                      "default": 24000, "description": "Per body; truncated output includes a continuation."},
        "deduplicate": {"type": "boolean", "default": True,
                        "description": "Refer to already returned covering bodies instead of repeating them."}}
PAGE = {"limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "offset": {"type": "integer", "minimum": 0, "default": 0}}
RELATIONS = {"relations": {"type": "array", "minItems": 1, "uniqueItems": True,
                           "items": {"type": "string", "enum": ["calls", "called_by", "depends_on",
                                       "depended_on_by", "inherits", "inherited_by"]},
                           "description": "Directions from the selected symbol; default all."},
             "depth": {"type": "integer", "minimum": 1, "maximum": 5, "default": 1},
             **PAGE, **VIEW}
SEARCH_SCHEMA = obj({"index": INDEX, "query": QUERY,
    "mode": {"type": "string", "enum": ["hybrid", "semantic", "keyword", "symbol", "text", "regex"],
             "default": "hybrid",
             "description": "Hybrid semantic+BM25; symbol names; text/regex filenames and contents."},
    "filters": obj({"path": {"type": "string", "description": "Path substring or glob (text/regex: glob)."},
                    "type": {"type": "string", "enum": ["file", "class", "function", "documentation"]},
                    "language": {"type": "string"}}),
    "keywords": {"type": "array", "items": {"type": "string"}, "description": "Optional BM25 terms for hybrid search."},
    "pseudocode": {"type": "string", "description": "Optional code sketch for hybrid semantic retrieval."},
    "case_sensitive": {"type": "boolean", "default": False, "description": "For text/regex."},
    "expand": obj(RELATIONS), **PAGE, **VIEW}, ["index", "query"])
OPERATIONS = {
    "search": SEARCH_SCHEMA,
    "browse": obj({"index": INDEX, "path": SELECTOR["path"],
                   "recursive": {"type": "boolean", "default": False}, **PAGE}),
    "read": {**obj({"index": INDEX, **SELECTOR, **VIEW,
                     "start_line": {"type": "integer", "minimum": 1},
                     "end_line": {"type": "integer", "minimum": 1}}, ["index"]), **HAS_SELECTOR},
    "relations": {**obj({"index": INDEX, **SELECTOR, **RELATIONS,
                          "to": {**obj(SELECTOR), **HAS_SELECTOR}}, ["index"]), **HAS_SELECTOR},
    "query": obj({"indexes": {"type": "array", "minItems": 1, "uniqueItems": True, "items": INDEX},
                  "query": QUERY,
                  "strategy": {"type": "string", "enum": ["iterative", "standard"], "default": "iterative"},
                  "output": {"type": "string", "enum": ["answer", "evidence"], "default": "answer"},
                  "select_files": {"type": "boolean", "default": True, "description": "Standard query only."},
                  "history": {"type": "array", "items": obj({"query": {"type": "string"},
                              "summary": {"type": "string"}}, ["query", "summary"])}}, ["indexes", "query"]),
}
LEGACY_OPERATIONS = {"search": obj({"index": INDEX, "query": QUERY,
    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}}, ["index", "query"])}


def schema_for(operations):
    # One flat request vocabulary; validate the selected action's fields at dispatch.
    fields = {key: value for schema in operations.values() for key, value in schema["properties"].items()}
    return obj({"action": {"type": "string", "enum": list(operations),
                           "description": "Operation to perform; always specify it explicitly."},
                "requests": {"type": "array", "minItems": 1, "maxItems": 8, "items": obj(fields)}},
               ["action", "requests"])


SCHEMA = schema_for(OPERATIONS)
GUIDANCE = (
    "Search and read code in prepared repository versions. When investigating an indexed version, "
    "your first tool call must be codebase. Retrieve relevant code here before using other tools; "
    "then choose tools freely. To locate behavior or symbols, describe what the code does or ask a "
    "concrete code question. Search returns "
    "the matching source itself, with exact commit, paths and numbered lines; no checkout, path discovery "
    "or preliminary directory browse is needed. Batch independent queries and versions together "
    "in a single requests array. "
    "Always specify action.\n"
)
DESCRIPTION = GUIDANCE + (
    "search: index + query. Default hybrid semantic+BM25 retrieval; modes also include semantic, keyword, "
    "symbol, text and regex. Optional path/type/language filters, keywords/pseudocode hints, and expand "
    "to include callers, callees or dependencies in the same response. Default: source, 5 matches.\n"
    "read: index + id, path or symbol (Class.method supported); read full source directly without a prior "
    "search. Optional line ranges or view=outline for signatures and contained symbols.\n"
    "relations: index + id/path/symbol; follow calls/called_by, depends_on/depended_on_by, "
    "inherits/inherited_by, with depth or a to selector for shortest paths. Default: outlines.\n"
    "browse: list versions by omitting index, or give index + path for directories and file outlines.\n"
    "query: indexes + query for full FastCode QA; iterative or standard strategy, answer or evidence output, "
    "optional history. Only this action makes additional, recorded model calls.\n"
    "Use returned continue_request/next_request as complete codebase arguments. Ranked searches and "
    "static graph gaps do not prove absence; read the surrounding code needed for each conclusion. "
    "All actions inspect frozen code without running it. Use github/Git for history and developer "
    "discussions, and local tools for unindexed versions."
)
LEGACY_DESCRIPTION = GUIDANCE + (
    "search: index + query for semantic+BM25 retrieval, limit defaults to 5. Results include source and "
    "static graph neighbors. This older service supports search only; it does not run code or call an LLM. "
    "Use local tools for surrounding source and unindexed versions, and github/Git for history and discussions."
)


def continuations(value, action):
    """Make service cursors directly callable through the single model tool."""
    if isinstance(value, list):
        return [continuations(item, action) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {"continue_request", "next_request"} and isinstance(item, dict):
            result[key] = {"action": "read" if key == "continue_request" else action, "requests": [item]}
        else:
            result[key] = continuations(item, "relations" if key == "expansion" else action)
    return result


# stdlib only in the transport process; the persistent service owns the model/indexes.
CLIENT = """import json, sys, urllib.request
body = json.load(sys.stdin)
url = 'http://127.0.0.1:' + str(body.pop('port'))
path = body.pop('path')
request = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=120) as response:
    sys.stdout.buffer.write(response.read())
"""


class FastCodeTool(ToolProvider):
    def __init__(self, sandbox, config, *, complete=None, storage=None, model=None):
        self.sandbox, self.config = sandbox, config
        self.complete, self.storage, self.model = complete, storage, model
        if not isinstance(config.get("port"), int) or not 1 <= config["port"] <= 65535:
            raise ValueError("FastCode requires a container-local service port")
        if not config.get("catalog_sha256") or not config.get("indexes"):
            raise ValueError("FastCode requires the prepared catalog identity and indexes")
        modern = config.get("api_version", 1) >= 2
        operations = copy.deepcopy(OPERATIONS if modern else LEGACY_OPERATIONS)
        for schema in operations.values():
            properties = schema["properties"]
            if "index" in properties:
                properties["index"]["enum"] = config["indexes"]
            if "indexes" in properties:
                properties["indexes"]["items"]["enum"] = config["indexes"]
        self.validators = {action: Draft202012Validator(schema) for action, schema in operations.items()}
        self.tool_specs = tuple(replace(spec, parameters=schema_for(operations),
                                       description=DESCRIPTION if modern else LEGACY_DESCRIPTION)
                                for spec in self.tool_specs)

    async def request(self, path, **payload):
        data = {"port": self.config["port"], "path": path,
                "catalog_sha256": self.config["catalog_sha256"], **payload}
        raw = await self.sandbox.docker("exec", "-i", self.sandbox.identity["container_id"],
                                        "python3", "-c", CLIENT,
                                        body=json.dumps(data).encode(), wait=125)
        result = json.loads(raw)
        if "error" in result:
            raise RuntimeError(result["error"])
        if result.get("catalog_sha256") != self.config["catalog_sha256"]:
            raise RuntimeError("FastCode catalog changed; prepare a new run")
        return result

    async def connect(self):
        result = await self.request("/health")
        if list(result["indexes"]) != self.config["indexes"]:
            raise RuntimeError("FastCode index catalog changed")
        if result.get("api_version", 1) != self.config.get("api_version", 1):
            raise RuntimeError("FastCode service protocol changed; prepare a new run")
        return result

    @tool(description=DESCRIPTION, parameters=SCHEMA, returns=BATCH_OUTPUT, batch_parameter="requests")
    async def fastcode(self, call_id, action, requests):
        results, valid, positions = [], [], []
        for index, request in enumerate(requests):
            try:
                self.validators[action].validate(request)
            except ValidationError as exc:
                results.append({"request": request, "error": input_error(exc, prefix=("requests", index))})
            else:
                positions.append(index)
                valid.append(request)
                results.append(None)
        if valid:
            if action == "query":
                tasks = [asyncio.create_task(self.query(call_id, **item)) for item in valid]
                try:
                    returned = await asyncio.gather(*tasks)
                except BaseException:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
            else:
                returned = (await self.request("/" + action, requests=valid))["results"]
            for position, result in zip(positions, returned, strict=True):
                results[position] = continuations(result, action)
        return {"catalog_sha256": self.config["catalog_sha256"], "results": results}

    async def query(self, call_id, **request):
        if self.complete is None or self.storage is None:
            raise ValueError("Codebase query requires the agent's recorded model connection")
        operation = self.storage.allocate("fastcode")
        self.storage.event("fastcode/query_start", operation=operation, call_id=call_id)
        self.storage.record(operation + ".request.json", request)
        state, job_id = None, uuid.uuid4().hex
        try:
            state = await self.request("/query/start", job_id=job_id, request={**request, "model": self.model})
            job_id = state["job_id"]
            while state["status"] != "complete":
                payload = {"job_id": job_id}
                if state["status"] == "model_request":
                    payload.update(sequence=state["sequence"], response=await self.complete(call_id, state))
                state = await self.request("/query/step", **payload)
            self.storage.record(operation + ".trace.json", state["trace"])
            self.storage.record(operation + ".result.json", state["result"])
            self.storage.event("fastcode/query_end", operation=operation, call_id=call_id)
            return state["result"]
        except BaseException:
            if job_id and (not state or state["status"] != "complete"):
                try:
                    cancelled = await self.request("/query/cancel", job_id=job_id)
                    self.storage.record(operation + ".trace.json", cancelled.get("trace", []))
                except Exception as exc:
                    self.storage.event("fastcode/cancel_error", operation=operation, message=str(exc))
            raise
