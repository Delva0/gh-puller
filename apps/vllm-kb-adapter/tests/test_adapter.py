"""JSON-RPC routing, version binding, and diff-impact contract tests."""

from typing import Any

import pytest

from vllm_kb_adapter.adapter import CHECKLIST_TOOLS, Adapter
from vllm_kb_adapter.snapshots import VLLM_ASCEND_PROJECT, VLLM_PROJECT, SnapshotRegistry


def _envelope(data: dict[str, Any], *, error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": "error" if error else "native"}],
        "structuredContent": data,
        "isError": error,
    }


class FakeUpstream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        properties = {
            "project": {"type": "string"},
            "base_branch": {"type": "string"},
            "scope": {"type": "string"},
            "direction": {"type": "string"},
            "depth": {"type": "integer"},
            "limit": {"type": "integer"},
        }
        return {
            "tools": [
                {
                    "name": name,
                    "description": name,
                    "inputSchema": {
                        "type": "object",
                        "properties": properties,
                        "required": ["project"],
                    },
                }
                for name in (*CHECKLIST_TOOLS, "list_projects")
            ],
        }

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name == "query_graph":
            query = arguments["query"]
            if "impact.qualified_name" not in query:
                if "pkg/imports.py" in query:
                    rows = [] if "seed.start_line" in query else [["pkg.imported", "pkg/imports.py"]]
                    return _envelope({"columns": ["seed_qn", "file"], "rows": rows})
                return _envelope(
                    {
                        "columns": ["seed_qn", "file"],
                        "rows": [
                            ["pkg.changed", "pkg/changed.py"],
                            ["pkg.lonely", "pkg/changed.py"],
                        ],
                    },
                )
            hop = 1 if "CALLS*1..1" in query else 2
            rows = (
                [
                    ["pkg.changed", "pkg.caller", '["Function"]', "pkg/caller.py"],
                    ["pkg.lonely", "pkg.changed", '["Method"]', "pkg/changed.py"],
                ]
                if hop == 1
                else [["pkg.changed", "pkg.root", ["Function"], "root.py"]]
            )
            return _envelope(
                {
                    "columns": ["seed_qn", "qn", "labels", "file"],
                    "rows": rows,
                },
            )
        if name == "get_architecture":
            return _envelope(
                {
                    "project": arguments["project"],
                    "clusters": {
                        "cols": ["label", "member_count"],
                        "rows": [["core", 4]],
                    },
                },
            )
        return _envelope(
            {
                "cols": ["qn", "label"],
                "rows": [["pkg.result", "Function"]],
            },
        )

    async def aclose(self) -> None:
        self.closed = True


def _call(name: str, arguments: dict[str, Any], request_id: int = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


@pytest.mark.asyncio
async def test_tools_list_exposes_only_checklist_contract(registry: SnapshotRegistry) -> None:
    upstream = FakeUpstream()
    response = await Adapter(registry, upstream).handle(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}},
    )

    tools = response["result"]["tools"]
    assert response["id"] == 7
    assert [tool["name"] for tool in tools] == list(CHECKLIST_TOOLS)
    assert all("version" in tool["inputSchema"]["properties"] for tool in tools)
    trace = next(tool for tool in tools if tool["name"] == "trace_path")
    assert "property nodes" in trace["description"]
    assert "repeat the same call" in trace["description"]
    detect = next(tool for tool in tools if tool["name"] == "detect_changes")
    assert detect["inputSchema"]["required"] == ["project", "diff"]
    assert set(detect["inputSchema"]["properties"]) == {
        "project",
        "version",
        "diff",
        "scope",
        "direction",
        "depth",
        "limit",
    }


@pytest.mark.asyncio
async def test_forwarded_call_resolves_latest_and_normalizes_rows(registry: SnapshotRegistry) -> None:
    upstream = FakeUpstream()
    response = await Adapter(registry, upstream).handle(
        _call("search_graph", {"project": VLLM_PROJECT, "query": "executor"}),
    )

    assert upstream.calls == [
        (
            "search_graph",
            {
                "project": "vllm-kb-vllm-0.23.0",
                "query": "executor",
                "format": "json",
            },
        ),
    ]
    assert response["result"]["structuredContent"]["rows"] == [
        {"qn": "pkg.result", "label": "Function"},
    ]


@pytest.mark.asyncio
async def test_forwarded_call_resolves_explicit_rc_version(registry: SnapshotRegistry) -> None:
    upstream = FakeUpstream()
    await Adapter(registry, upstream).handle(
        _call(
            "trace_path",
            {
                "project": VLLM_ASCEND_PROJECT,
                "version": "v0.23.0",
                "function_name": "pkg.f",
            },
        ),
    )

    assert upstream.calls[0][0] == "search_graph"
    assert upstream.calls[-1][1]["project"] == "vllm-kb-vllm-ascend-0.23.0"
    assert "version" not in upstream.calls[-1][1]


@pytest.mark.asyncio
async def test_property_trace_includes_attribute_relationships(registry: SnapshotRegistry) -> None:
    class PropertyUpstream(FakeUpstream):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((name, arguments))
            if name == "search_graph":
                return _envelope(
                    {
                        "rows": [
                            {
                                "name": "registry",
                                "qn": "vllm-kb-vllm-0.23.0.vllm.config.ModelConfig.registry",
                                "label": "Method",
                                "decorator_tags": '["property"]',
                            },
                        ],
                    },
                )
            return _envelope({"callers": {"cols": ["qn"], "rows": []}})

    upstream = PropertyUpstream()
    await Adapter(registry, upstream).handle(
        _call(
            "trace_path",
            {
                "project": VLLM_PROJECT,
                "version": "0.23.0",
                "function_name": "vllm-kb-vllm-0.23.0.vllm.config.ModelConfig.registry",
                "direction": "inbound",
            },
        ),
    )

    assert upstream.calls == [
        (
            "search_graph",
            {
                "project": "vllm-kb-vllm-0.23.0",
                "name_pattern": "^registry$",
                "fields": ["decorator_tags", "decorators"],
                "limit": 51,
                "format": "json",
                "qn_pattern": "^vllm\\-kb\\-vllm\\-0\\.23\\.0\\.vllm\\.config\\.ModelConfig\\.registry$",
            },
        ),
        (
            "trace_path",
            {
                "project": "vllm-kb-vllm-0.23.0",
                "function_name": "vllm-kb-vllm-0.23.0.vllm.config.ModelConfig.registry",
                "direction": "inbound",
                "format": "json",
                "edge_types": ["CALLS", "USAGE", "WRITES"],
            },
        ),
    ]


@pytest.mark.asyncio
async def test_explicit_trace_edge_types_bypass_property_adaptation(registry: SnapshotRegistry) -> None:
    upstream = FakeUpstream()
    await Adapter(registry, upstream).handle(
        _call(
            "trace_path",
            {
                "project": VLLM_PROJECT,
                "function_name": "vllm.config.ModelConfig.registry",
                "edge_types": ["CALLS"],
            },
        ),
    )

    assert upstream.calls == [
        (
            "trace_path",
            {
                "project": "vllm-kb-vllm-0.23.0",
                "function_name": "vllm.config.ModelConfig.registry",
                "edge_types": ["CALLS"],
                "format": "json",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_new_checklist_tools_bind_snapshot_and_request_structured_format(
    registry: SnapshotRegistry,
) -> None:
    upstream = FakeUpstream()
    adapter = Adapter(registry, upstream)

    search = await adapter.handle(
        _call(
            "search_code",
            {
                "project": VLLM_ASCEND_PROJECT,
                "version": "v0.23.0",
                "pattern": "DispatchFFNCombine",
                "mode": "full",
            },
        ),
    )
    architecture = await adapter.handle(
        _call(
            "get_architecture",
            {
                "project": VLLM_ASCEND_PROJECT,
                "version": "v0.23.0",
                "aspects": ["clusters"],
            },
        ),
    )

    assert upstream.calls == [
        (
            "search_code",
            {
                "project": "vllm-kb-vllm-ascend-0.23.0",
                "pattern": "DispatchFFNCombine",
                "mode": "full",
                "format": "json",
            },
        ),
        (
            "get_architecture",
            {
                "project": "vllm-kb-vllm-ascend-0.23.0",
                "aspects": ["clusters"],
                "format": "json",
            },
        ),
    ]
    assert search["result"]["structuredContent"]["rows"] == [
        {"qn": "pkg.result", "label": "Function"},
    ]
    assert architecture["result"]["structuredContent"]["clusters"] == [
        {"label": "core", "member_count": 4},
    ]


@pytest.mark.asyncio
async def test_detect_changes_excludes_seeds_and_orders_nearest_first(
    registry: SnapshotRegistry,
) -> None:
    upstream = FakeUpstream()
    response = await Adapter(registry, upstream).handle(
        _call(
            "detect_changes",
            {
                "project": VLLM_ASCEND_PROJECT,
                "version": "0.23.0",
                "diff": "diff --git a/pkg/changed.py b/pkg/changed.py\n+line",
                "scope": "impact",
                "direction": "inbound",
                "depth": 2,
                "limit": 10,
            },
        ),
    )

    data = response["result"]["structuredContent"]
    assert data["version"] == "0.23.0"
    assert data["changed_files"] == ["pkg/changed.py"]
    assert data["seed_symbols"] == 2
    assert data["impacted"] == [
        {"qn": "pkg.caller", "label": "Function", "file": "pkg/caller.py", "hop": 1},
        {"qn": "pkg.root", "label": "Function", "file": "root.py", "hop": 2},
    ]
    assert data["impacted_modules"] == [
        {"module": "pkg/caller.py", "count": 1},
        {"module": "root.py", "count": 1},
    ]
    assert len(upstream.calls) == 3
    assert all(call[1]["project"] == "vllm-kb-vllm-ascend-0.23.0" for call in upstream.calls)


@pytest.mark.asyncio
async def test_detect_changes_files_scope_needs_no_graph_query(registry: SnapshotRegistry) -> None:
    upstream = FakeUpstream()
    response = await Adapter(registry, upstream).handle(
        _call(
            "detect_changes",
            {
                "project": VLLM_PROJECT,
                "diff": "diff --git a/vllm/a.py b/vllm/a.py\n+line",
                "scope": "files",
            },
        ),
    )

    assert response["result"]["structuredContent"]["changed_files"] == ["vllm/a.py"]
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_detect_changes_falls_back_to_file_seeds_when_hunk_misses_definitions(
    registry: SnapshotRegistry,
) -> None:
    upstream = FakeUpstream()
    await Adapter(registry, upstream).handle(
        _call(
            "detect_changes",
            {
                "project": VLLM_PROJECT,
                "diff": (
                    "diff --git a/pkg/imports.py b/pkg/imports.py\n"
                    "--- a/pkg/imports.py\n"
                    "+++ b/pkg/imports.py\n"
                    "@@ -1 +1 @@\n"
                ),
                "depth": 1,
            },
        ),
    )

    assert len(upstream.calls) == 3
    assert "seed.start_line" in upstream.calls[0][1]["query"]
    assert "seed.start_line" not in upstream.calls[1][1]["query"]
    assert "seed.start_line" not in upstream.calls[2][1]["query"]


@pytest.mark.asyncio
async def test_tool_and_json_rpc_validation(registry: SnapshotRegistry) -> None:
    adapter = Adapter(registry, FakeUpstream())

    invalid = await adapter.handle([])
    unknown_method = await adapter.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize"})
    unavailable = await adapter.handle(
        _call("search_graph", {"project": VLLM_PROJECT, "version": "1.0.0"}),
    )
    extra_tool = await adapter.handle(_call("get_code_snippet", {"project": VLLM_PROJECT}))

    assert invalid["error"]["code"] == -32600
    assert unknown_method["error"]["code"] == -32601
    assert unavailable["result"]["isError"] is True
    assert extra_tool["result"]["structuredContent"] == {"error": "unknown tool: get_code_snippet"}
