"""Implement the scoped vllm-kb MCP contract over versioned graph indexes.

Only tools/list and the six checklist tools are public. Index mutation stays
in the offline prebuild workflow; online calls resolve an already-built graph.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import PurePosixPath
from typing import Any

from vllm_kb_adapter.diffs import (
    ChangedFile,
    build_impact_query,
    build_seed_query,
    parse_unified_diff,
)
from vllm_kb_adapter.normalize import normalize_result
from vllm_kb_adapter.snapshots import RegistryError, Snapshot, SnapshotRegistry
from vllm_kb_adapter.upstream import MCPUpstream, structured_content, tool_error_text

CHECKLIST_TOOLS = (
    "search_graph",
    "search_code",
    "trace_path",
    "query_graph",
    "get_architecture",
    "detect_changes",
)
_JSON_FORMAT_TOOLS = frozenset(("search_graph", "search_code", "trace_path", "query_graph", "get_architecture"))
_IMPACT_CEILING = 5000
_MAX_CHANGED_FILES = 128
_MAX_DEPTH = 10


class Adapter:
    """Route public MCP calls to the matching prebuilt project version."""

    def __init__(self, registry: SnapshotRegistry, upstream: MCPUpstream) -> None:
        """Bind snapshot resolution to an upstream MCP client.

        Args:
            registry: Complete immutable production snapshot registry.
            upstream: Client for the unchanged gh-puller-mcp service.
        """
        self.registry = registry
        self.upstream = upstream

    async def handle(self, request: Any) -> dict[str, Any]:
        """Handle one stateless JSON-RPC request.

        Args:
            request: Decoded HTTP JSON body from vllm-kb.
        """
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params")
        if method == "tools/list":
            return _rpc_result(request_id, await self._list_tools())
        if method != "tools/call":
            return _rpc_error(request_id, -32601, "Method not found")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _rpc_error(request_id, -32602, "Invalid request parameters")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _rpc_error(request_id, -32602, "Invalid request parameters")
        result = await self._call_tool(params["name"], arguments)
        return _rpc_result(request_id, result)

    async def _list_tools(self) -> dict[str, Any]:
        upstream = await self.upstream.request("tools/list", {})
        tools = upstream.get("tools")
        if not isinstance(tools, list):
            return {"tools": []}
        exposed = []
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("name") not in CHECKLIST_TOOLS:
                continue
            exposed.append(_public_tool(tool))
        return {"tools": exposed}

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in CHECKLIST_TOOLS:
            return _tool_error(f"unknown tool: {name}")
        project = arguments.get("project")
        version = arguments.get("version")
        if not isinstance(project, str):
            return _tool_error("project is required")
        if version is not None and not isinstance(version, str):
            return _tool_error("version must be a string")
        try:
            snapshot = self.registry.resolve(project, version)
        except RegistryError as exc:
            return _tool_error(str(exc))
        if name == "detect_changes":
            return await self._detect_changes(snapshot, arguments)
        forwarded = dict(arguments)
        forwarded.pop("version", None)
        forwarded["project"] = snapshot.index_name
        if name in _JSON_FORMAT_TOOLS:
            forwarded["format"] = "json"
        result = await self.upstream.call_tool(name, forwarded)
        return normalize_result(name, result)

    async def _detect_changes(
        self,
        snapshot: Snapshot,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        diff = arguments.get("diff")
        if not isinstance(diff, str):
            return _tool_error("diff is required")
        scope = arguments.get("scope", "impact")
        direction = arguments.get("direction", "inbound")
        depth = arguments.get("depth", 2)
        limit = arguments.get("limit", 20)
        if scope not in {"files", "impact"}:
            return _tool_error('scope must be "files" or "impact"')
        if direction not in {"inbound", "outbound", "both"}:
            return _tool_error('direction must be "inbound", "outbound", or "both"')
        if not isinstance(depth, int) or isinstance(depth, bool) or not 1 <= depth <= _MAX_DEPTH:
            return _tool_error(f"depth must be an integer from 1 to {_MAX_DEPTH}")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _IMPACT_CEILING:
            return _tool_error(f"limit must be an integer from 1 to {_IMPACT_CEILING}")

        changes = parse_unified_diff(diff)
        base = {
            "project": snapshot.logical_project,
            "version": snapshot.version,
            "base": snapshot.version,
            "direction": direction,
            "changed_files": [change.path for change in changes],
            "seed_symbols": 0,
            "impacted_total": 0,
            "impacted_shown": 0,
            "impacted": [],
            "impacted_modules": [],
            "truncated": False,
        }
        if scope == "files" or not changes:
            return _tool_result(base)

        selected = changes[:_MAX_CHANGED_FILES]
        seed_rows = await self._seed_rows(snapshot, selected)
        if isinstance(seed_rows, str):
            return _tool_error(f"detect_changes seed query failed: {seed_rows}")
        seen_files = {row["file"] for row in seed_rows if isinstance(row.get("file"), str) and row["file"]}
        fallback_paths = {change.path for change in selected if change.ranges and change.path not in seen_files}
        if fallback_paths:
            fallback_changes = tuple(
                ChangedFile(path=change.path, ranges=()) for change in selected if change.path in fallback_paths
            )
            fallback_rows = await self._seed_rows(snapshot, fallback_changes)
            if isinstance(fallback_rows, str):
                return _tool_error(f"detect_changes seed fallback failed: {fallback_rows}")
            seed_rows.extend(fallback_rows)
        effective_changes = tuple(
            ChangedFile(path=change.path, ranges=()) if change.path in fallback_paths else change for change in selected
        )
        seed_qns = {row["seed_qn"] for row in seed_rows if isinstance(row.get("seed_qn"), str) and row["seed_qn"]}
        impacts: dict[str, dict[str, Any]] = {}
        ceiling_reached = len(seed_rows) >= _IMPACT_CEILING
        for hop in range(1, depth + 1):
            query = build_impact_query(effective_changes, direction=direction, hop=hop)
            result = await self.upstream.call_tool(
                "query_graph",
                {
                    "project": snapshot.index_name,
                    "query": query,
                    "max_rows": _IMPACT_CEILING,
                    "format": "json",
                },
            )
            rows = _query_rows(result)
            if isinstance(rows, str):
                return _tool_error(f"detect_changes graph query failed: {rows}")
            ceiling_reached |= len(rows) >= _IMPACT_CEILING
            for row in rows:
                qn = row.get("qn")
                if not isinstance(qn, str) or not qn:
                    continue
                current = impacts.get(qn)
                if current is None or hop < current["hop"]:
                    impacts[qn] = {
                        "qn": qn,
                        "label": _label(row.get("labels")),
                        "file": row.get("file") or "",
                        "hop": hop,
                    }
        for seed_qn in seed_qns:
            impacts.pop(seed_qn, None)
        ordered = sorted(impacts.values(), key=lambda item: (item["hop"], item["qn"]))
        shown = ordered[:limit]
        modules = _module_rollup(ordered)
        base.update(
            seed_symbols=len(seed_qns),
            impacted_total=len(ordered),
            impacted_shown=len(shown),
            impacted=shown,
            impacted_modules=modules,
            truncated=(len(changes) > len(selected) or ceiling_reached or len(ordered) > len(shown)),
        )
        return _tool_result(base)

    async def _seed_rows(
        self,
        snapshot: Snapshot,
        changes: tuple[ChangedFile, ...],
    ) -> list[dict[str, Any]] | str:
        result = await self.upstream.call_tool(
            "query_graph",
            {
                "project": snapshot.index_name,
                "query": build_seed_query(changes),
                "max_rows": _IMPACT_CEILING,
                "format": "json",
            },
        )
        return _query_rows(result)


def _public_tool(tool: dict[str, Any]) -> dict[str, Any]:
    public = dict(tool)
    schema = public.get("inputSchema")
    if not isinstance(schema, dict):
        return public
    schema = json.loads(json.dumps(schema))
    properties = schema.setdefault("properties", {})
    properties["version"] = {
        "type": "string",
        "description": "Exact snapshot version; omitted selects the highest available version.",
    }
    if public.get("name") == "detect_changes":
        public["description"] = (
            "Evaluate a supplied unified Git diff against an immutable prebuilt snapshot. "
            "scope=files returns changed paths; scope=impact selects changed definitions "
            "and traverses inbound, outbound, or both CALLS directions."
        )
        properties["diff"] = {
            "type": "string",
            "description": "Unified Git diff evaluated against the selected prebuilt snapshot.",
        }
        properties["scope"]["default"] = "impact"
        properties["direction"]["default"] = "inbound"
        properties["depth"].update({"default": 2, "minimum": 1, "maximum": _MAX_DEPTH})
        properties["limit"].update({"default": 20, "minimum": 1, "maximum": _IMPACT_CEILING})
        properties = {
            key: value
            for key, value in properties.items()
            if key in {"project", "version", "diff", "scope", "direction", "depth", "limit"}
        }
        schema["properties"] = properties
        schema["required"] = ["project", "diff"]
    public["inputSchema"] = schema
    return public


def _label(value: Any) -> str:
    if isinstance(value, list) and value:
        return str(value[0])
    if isinstance(value, str):
        try:
            labels = json.loads(value)
        except ValueError:
            return value
        if isinstance(labels, list) and labels:
            return str(labels[0])
    return ""


def _module_rollup(impacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(_module(item["file"]) for item in impacts if isinstance(item.get("file"), str) and item["file"])
    return [
        {"module": module, "count": count}
        for module, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _module(file: str) -> str:
    parts = PurePosixPath(file).parts
    return "/".join(parts[:2])


def _query_rows(result: dict[str, Any]) -> list[dict[str, Any]] | str:
    if result.get("isError"):
        return tool_error_text(result)
    data = structured_content(result)
    if data is None:
        return "query returned no structured rows"
    rows = data.get("rows")
    columns = data.get("columns") or data.get("cols")
    if not isinstance(rows, list) or not isinstance(columns, list):
        return "query returned invalid rows"
    return [
        raw if isinstance(raw, dict) else dict(zip(columns, raw, strict=False))
        for raw in rows
        if isinstance(raw, (dict, list))
    ]


def _rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _tool_result(data: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": data,
        "isError": False,
    }


def _tool_error(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": {"error": message},
        "isError": True,
    }
