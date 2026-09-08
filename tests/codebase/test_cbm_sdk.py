from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gh_puller.codebase import CBMClient, CBMTransportError, default_cbm_cache


def write_query_cbm(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

if sys.argv[1:] == ["--version"]:
    print("codebase-memory-mcp 0.test")
    raise SystemExit(0)

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {},
            "instructions": "Query before reading source.",
        }
    elif method == "tools/list":
        result = {"tools": [
            {"name": "search_graph"},
            {"name": "query_graph"},
            {"name": "get_graph_schema"},
            {"name": "trace_path"},
            {"name": "get_architecture"},
            {"name": "index_status"},
            {"name": "compare_graphs"},
        ]}
    elif method == "tools/call":
        name = request["params"]["name"]
        arguments = request["params"].get("arguments", {})
        if name == "fail":
            result = {"content": [{"type": "text", "text": "expected failure"}], "isError": True}
        elif name == "tree":
            result = {"content": [{"type": "text", "text": "root\\n  child"}], "isError": False}
        else:
            body = {
                "tool": name,
                "arguments": arguments,
                "pid": os.getpid(),
                "request_id": request["id"],
            }
            result = {
                "content": [{"type": "text", "text": json.dumps(body)}],
                "isError": False,
            }
            if name != "legacy_json":
                result["structuredContent"] = body
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""",
    )
    path.chmod(0o755)


def test_client_starts_once_and_exposes_json_queries(tmp_path):
    binary = tmp_path / "fake-cbm"
    write_query_cbm(binary)

    with CBMClient(binary, cache_root=tmp_path / "cache", timeout=5) as client:
        pid = client.pid
        graph = client.daemon_graph("demo")
        search = client.search_graph(graph, label="Function", limit=4)
        code = client.search_code(graph, pattern="needle", mode="compact")
        query = client.query_graph(graph, query="MATCH (n) RETURN n LIMIT 1")
        schema = client.get_graph_schema(graph)
        trace = client.trace_path(graph, function_name="demo.main", direction="outbound")
        snippet = client.get_code_snippet(graph, qualified_name="demo.main")
        architecture = client.get_architecture(graph, aspects=["structure"])
        changes = client.detect_changes(graph, base_branch="HEAD", scope="files")
        adr = client.manage_adr(graph)
        traces = client.ingest_traces(
            graph,
            [{"caller": "demo.main", "callee": "demo.work", "count": 2}],
        )
        status = client.index_status(graph, verbose=True)
        comparison = client.compare_graphs(
            client.daemon_graph("base"),
            graph,
            limit=3,
            scan_limit=50,
        )

        assert client.instructions == "Query before reading source."
        assert client.binary.path == binary.resolve()
        assert client.cache_root == (tmp_path / "cache").resolve()
        assert [tool["name"] for tool in client.list_tools()] == [
            "search_graph",
            "query_graph",
            "get_graph_schema",
            "trace_path",
            "get_architecture",
            "index_status",
            "compare_graphs",
        ]
        assert {
            search["pid"],
            code["pid"],
            query["pid"],
            schema["pid"],
            trace["pid"],
            snippet["pid"],
            architecture["pid"],
            changes["pid"],
            adr["pid"],
            traces["pid"],
            status["pid"],
            comparison["pid"],
        } == {pid}
        assert search["arguments"] == {
            "project": "demo",
            "label": "Function",
            "limit": 4,
            "format": "json",
        }
        assert query["arguments"]["query"] == "MATCH (n) RETURN n LIMIT 1"
        assert query["arguments"]["format"] == "json"
        assert code["arguments"] == {
            "project": "demo",
            "pattern": "needle",
            "mode": "compact",
        }
        assert schema["arguments"] == {"project": "demo"}
        assert trace["arguments"]["format"] == "json"
        assert snippet["arguments"] == {
            "project": "demo",
            "qualified_name": "demo.main",
        }
        assert architecture["arguments"] == {
            "project": "demo",
            "aspects": ["structure"],
            "format": "json",
        }
        assert changes["arguments"] == {
            "project": "demo",
            "base_branch": "HEAD",
            "scope": "files",
            "format": "json",
        }
        assert adr["arguments"] == {"project": "demo", "mode": "get"}
        assert traces["arguments"] == {
            "project": "demo",
            "traces": [{"caller": "demo.main", "callee": "demo.work", "count": 2}],
        }
        assert status["arguments"] == {"project": "demo", "verbose": True}
        assert comparison["arguments"] == {
            "base_project": "base",
            "target_project": "demo",
            "limit": 3,
            "scan_limit": 50,
        }

    assert client._daemon_backend.process.returncode == 0


def test_json_call_supports_legacy_text_and_rejects_tree_output(tmp_path):
    binary = tmp_path / "fake-cbm"
    write_query_cbm(binary)

    with CBMClient(binary, cache_root=tmp_path / "cache", timeout=5) as client:
        result = client.call_json_tool("legacy_json", {"value": 42})
        assert result["arguments"] == {"value": 42}
        assert client.call_tool("tree")["content"][0]["text"] == "root\n  child"
        with pytest.raises(CBMTransportError, match="did not return a JSON object"):
            client.call_json_tool("tree")
        with pytest.raises(CBMTransportError, match="expected failure"):
            client.call_tool("fail")


def test_client_serializes_concurrent_calls_on_one_session(tmp_path):
    binary = tmp_path / "fake-cbm"
    write_query_cbm(binary)

    with (
        CBMClient(binary, cache_root=tmp_path / "cache", timeout=5) as client,
        ThreadPoolExecutor(max_workers=8) as pool,
    ):
        results = list(pool.map(lambda value: client.call_json_tool("echo", {"value": value}), range(32)))

    assert sorted(result["arguments"]["value"] for result in results) == list(range(32))
    assert len({result["request_id"] for result in results}) == 32


def test_client_uses_environment_cache_override(tmp_path):
    binary = tmp_path / "fake-cbm"
    cache = tmp_path / "from-environment"
    write_query_cbm(binary)

    with CBMClient(binary, timeout=5, environment={"CBM_CACHE_DIR": str(cache)}) as client:
        assert client.cache_root == cache.resolve()
    assert default_cbm_cache({"CBM_CACHE_DIR": str(cache)}) == cache


def test_client_rejects_nonpositive_timeout():
    with pytest.raises(ValueError, match="must be positive"):
        CBMClient(timeout=0)
