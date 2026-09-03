"""Expose the version-aware adapter as one permissive JSON-RPC HTTP endpoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from vllm_kb_adapter.adapter import Adapter
from vllm_kb_adapter.upstream import MCPUpstream, UpstreamError

if TYPE_CHECKING:
    from vllm_kb_adapter.snapshots import SnapshotRegistry


def create_app(
    registry: SnapshotRegistry,
    upstream: MCPUpstream,
    *,
    path: str = "/gh-puller/graph",
) -> FastAPI:
    """Assemble the scoped HTTP adapter.

    Args:
        registry: Prebuilt snapshot lookup used for every tool call.
        upstream: Client for the unchanged gh-puller-mcp service.
        path: Public stateless MCP endpoint configured in vllm-kb.
    """
    adapter = Adapter(registry, upstream)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await upstream.aclose()

    app = FastAPI(
        title="vllm-kb gh-puller adapter",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.post(path)
    async def mcp_endpoint(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            )
        try:
            return JSONResponse(await adapter.handle(body))
        except UpstreamError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=502)

    return app
