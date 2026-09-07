"""Streamable HTTP transport: in-process semantics + process-level CLI smoke.

In-process: `streamable_http_app` via starlette TestClient (no real socket, no
real backend — `ServerConfig(call_tool=...)` stub). Covers the custom path,
stateless bare calls (no initialize), plain-JSON responses, envelope passthrough
and the host semantics (localhost auto DNS-rebinding protection vs 0.0.0.0).
Process-level: the real `python -m gh_puller_mcp --http ...` subprocess with a
persistent fake backend frontend, driven over a real socket.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from gh_puller_mcp.server import ServerConfig, build_server

PROJECT_ROOT = Path(__file__).resolve().parents[2]

HTTP_SHIM = """
import json, sys

if "--version" in sys.argv:
    print("codebase-memory-mcp 0.10.8")
    raise SystemExit

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "notifications/initialized":
        continue
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05",
                  "serverInfo": {"name": "codebase-memory-mcp", "version": "0.10.8"}}
    else:
        tool = request["params"]["name"]
        payload = request["params"]["arguments"]
        data = json.dumps({"tool": tool, "args": payload}, ensure_ascii=False, separators=(",", ":"))
        result = {"content": [{"type": "text", "text": data}],
                  "structuredContent": json.loads(data), "isError": False}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""


def make_app(config: ServerConfig, path: str = "/gh-puller/graph", **kwargs):
    # fixed HTTP-mode semantics (see run_server_http) + open bind so the
    # TestClient's "testserver" Host header passes (localhost binds auto-enable
    # DNS rebinding protection, see host-semantics test)
    kwargs.setdefault("host", "0.0.0.0")
    return build_server(config).streamable_http_app(
        streamable_http_path=path, json_response=True, stateless_http=True, **kwargs,
    )


def rpc(method: str, params: dict | None = None, id_: int = 1) -> dict:
    request: dict = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        request["params"] = params
    return request


def test_http_stateless_bare_call_custom_path(stub_call_tool) -> None:
    envelope = {"content": [{"type": "text", "text": '{"ok": true}'}], "isError": False,
                "structuredContent": {"ok": True}}
    stub = stub_call_tool(envelope)
    app = make_app(ServerConfig(call_tool=stub))

    with TestClient(app) as client:
        payload = rpc("tools/call", {"name": "list_projects", "arguments": {"limit": 1}})
        r = client.post("/gh-puller/graph", json=payload)
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/json"
        result = r.json()["result"]
        assert result["structuredContent"] == {"ok": True}
        assert result["isError"] is False
        assert stub.calls == [("list_projects", {"limit": 1})]


def test_http_tools_list_and_unknown_tool(stub_call_tool) -> None:
    config = ServerConfig(call_tool=stub_call_tool({"content": [], "isError": False}))
    app = make_app(config, host="0.0.0.0")

    with TestClient(app) as client:
        tools = client.post("/gh-puller/graph", json=rpc("tools/list", {})).json()["result"]["tools"]
        assert len(tools) == 15
        assert tools[0]["name"] == "index_repository"

        unknown = client.post(
            "/gh-puller/graph", json=rpc("tools/call", {"name": "bogus", "arguments": {}}),
        ).json()["result"]
        assert unknown["isError"] is True
        assert unknown["content"][0]["text"] == "unknown tool: bogus"
        assert unknown["structuredContent"] == {"error": "unknown tool: bogus"}


def test_slow_tool_call_does_not_block_http_event_loop() -> None:
    entered = threading.Event()
    release = threading.Event()

    def slow_call(tool: str, arguments: dict) -> dict:
        entered.set()
        release.wait(timeout=2)
        return {"content": [{"type": "text", "text": '{"ok":true}'}], "isError": False}

    app = make_app(ServerConfig(version="0.10.8", call_tool=slow_call))
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            client.post,
            "/gh-puller/graph",
            json=rpc("tools/call", {"name": "list_projects", "arguments": {}}),
        )
        assert entered.wait(timeout=1)
        timer = threading.Timer(0.8, release.set)
        timer.start()
        started = time.monotonic()
        tools = client.post("/gh-puller/graph", json=rpc("tools/list", {}))
        elapsed = time.monotonic() - started
        release.set()
        timer.join()
        assert pending.result(timeout=1).status_code == 200
        assert tools.status_code == 200
        assert elapsed < 0.4


def test_http_profile_scout_filters_surface(stub_call_tool) -> None:
    config = ServerConfig(profile="scout", call_tool=stub_call_tool({"content": [], "isError": False}))
    app = make_app(config)

    with TestClient(app) as client:
        tools = client.post("/gh-puller/graph", json=rpc("tools/list", {})).json()["result"]["tools"]
        assert len(tools) == 7
        blocked = client.post(
            "/gh-puller/graph", json=rpc("tools/call", {"name": "query_graph", "arguments": {}}),
        ).json()["result"]
        assert blocked["isError"] is True
        assert blocked["content"][0]["text"] == "tool 'query_graph' is not available in the scout tool profile"


def test_http_host_semantics_local_guard_vs_open_bind(stub_call_tool) -> None:
    config = ServerConfig(call_tool=stub_call_tool({"content": [], "isError": False}))
    request = rpc("tools/list", {})

    # default (127.0.0.1): DNS rebinding protection on -> foreign Host header is 421
    with TestClient(make_app(config, host="127.0.0.1")) as client:
        assert client.post("/gh-puller/graph", json=request).status_code == 421
        local = client.post("/gh-puller/graph", json=request, headers={"Host": "localhost:8787"})
        assert local.status_code == 200

    # 0.0.0.0: protection off -> cross-machine Host headers pass
    with TestClient(make_app(config, host="0.0.0.0")) as client:
        assert client.post("/gh-puller/graph", json=request).status_code == 200


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def post(port: int, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        # the transport requires Accept to cover both types (curl's */* also passes)
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


@pytest.mark.integration
def test_http_persistent_backend_smoke_real_socket(shim) -> None:
    port = free_port()
    env = os.environ.copy()
    env["GH_PULLER_MCP_BINARY"] = shim(HTTP_SHIM)
    proc = subprocess.Popen(
        [sys.executable, "-m", "gh_puller_mcp", "--http", "--port", str(port), "--path", "/gh-puller/graph"],
        cwd=PROJECT_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.25).close()
                break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError(f"server exited early: {proc.stderr.read().decode()}") from None
                if time.monotonic() > deadline:
                    raise TimeoutError("HTTP server never became ready") from None
                time.sleep(0.1)

        tools = post(port, "/gh-puller/graph", rpc("tools/list", {}))["result"]["tools"]
        assert len(tools) == 15

        call = post(port, "/gh-puller/graph", rpc("tools/call", {"name": "list_projects", "arguments": {"limit": 1}}))
        result = call["result"]
        assert result["structuredContent"] == {"tool": "list_projects", "args": {"limit": 1}}
        assert result["isError"] is False
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        # uvicorn >=0.52 shuts down gracefully on SIGTERM but then re-raises the
        # captured signal, so the process deliberately dies with -SIGTERM
        assert proc.returncode == -signal.SIGTERM
