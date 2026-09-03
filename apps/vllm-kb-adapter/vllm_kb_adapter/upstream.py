"""Call the unchanged gh-puller-mcp Streamable HTTP endpoint.

This client owns the protocol difference that matters to vllm-kb: every
upstream request explicitly advertises ``Accept: application/json``.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

import httpx


class UpstreamError(RuntimeError):
    """The upstream HTTP or JSON-RPC exchange failed."""


class MCPUpstream:
    """Minimal stateless JSON-RPC client for gh-puller-mcp."""

    def __init__(
        self,
        url: str,
        *,
        timeout: float | None = 25,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Configure the stateless upstream transport.

        Args:
            url: Complete gh-puller-mcp Streamable HTTP endpoint.
            timeout: Per-request HTTP timeout; ``None`` waits indefinitely.
            transport: Optional HTTPX transport used by tests or custom runtimes.
        """
        self.url = url
        self._ids = itertools.count(1)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send one stateless MCP request and return its result object.

        Args:
            method: MCP method such as ``tools/list`` or ``tools/call``.
            params: Method parameters forwarded to gh-puller-mcp.

        Raises:
            UpstreamError: The endpoint is unavailable or returns an invalid
                HTTP/JSON-RPC response.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params,
        }
        try:
            response = await self._client.post(self.url, json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamError(f"gh-puller-mcp request failed: {exc}") from exc
        if not isinstance(body, dict):
            raise UpstreamError("gh-puller-mcp returned a non-object response")
        if body.get("error") is not None:
            raise UpstreamError(f"gh-puller-mcp JSON-RPC error: {body['error']}")
        result = body.get("result")
        if not isinstance(result, dict):
            raise UpstreamError("gh-puller-mcp response is missing result")
        return result

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call an MCP tool and preserve its CallToolResult envelope.

        Args:
            name: Upstream tool name.
            arguments: Tool arguments.
        """
        return await self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def structured_content(result: dict[str, Any]) -> dict[str, Any] | None:
    """Decode structuredContent, including the MCP text fallback contract."""
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        return None
    text = content[0].get("text")
    if not isinstance(text, str):
        return None
    try:
        decoded = json.loads(text)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def tool_error_text(result: dict[str, Any]) -> str:
    """Extract the useful message from an upstream tool-error envelope."""
    structured = structured_content(result)
    if structured is not None and structured.get("error"):
        return str(structured["error"])
    content = result.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        text = content[0].get("text")
        if isinstance(text, str) and text:
            return text
    return "upstream tool failed"
