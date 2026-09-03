"""Upstream HTTP compatibility and JSON-RPC failure tests."""

import json

import httpx
import pytest

from vllm_kb_adapter.upstream import MCPUpstream, UpstreamError


@pytest.mark.asyncio
async def test_upstream_sets_json_accept_header() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "application/json"
        assert request.headers["content-type"] == "application/json"
        body = json.loads(request.content)
        assert body["method"] == "tools/list"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}})

    upstream = MCPUpstream("http://upstream.test/mcp", transport=httpx.MockTransport(handler))
    try:
        assert await upstream.request("tools/list", {}) == {"tools": []}
    finally:
        await upstream.aclose()


@pytest.mark.asyncio
async def test_upstream_rejects_json_rpc_error() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -1}})

    upstream = MCPUpstream("http://upstream.test/mcp", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError, match="JSON-RPC error"):
            await upstream.request("tools/list", {})
    finally:
        await upstream.aclose()
