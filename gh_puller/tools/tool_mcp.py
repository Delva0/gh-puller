"""Adapt an SDK MCP session to the existing registry and immutable result store."""

import asyncio
import base64
import copy
import json
import os
from collections import defaultdict
from contextlib import AsyncExitStack, suppress
from contextvars import ContextVar
from datetime import timedelta
from functools import partial
from pathlib import Path
from string import Template

import httpx
from mcp import ClientSession, McpError, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from pydantic import AnyUrl

from .registry import ToolProvider, ToolSpec
from .storage import ToolStorage
from .tool_offload import ToolOutput

CALL_ID = ContextVar("mcp_call_id", default=None)


def load_connection(path: str | Path, name: str, *, variables: dict[str, str] | None = None) -> dict:
    """Read one MCP connection; environment placeholders are resolved only in env and headers."""
    path = Path(path)
    config = json.loads(path.read_text())["mcpServers"][name]
    environment = defaultdict(str, dict(os.environ) | (variables or {}))

    def expand(values):
        return {key: expanded for key, value in values.items()
                if (expanded := Template(value).substitute(environment))}

    if ("command" in config) == ("url" in config):
        raise ValueError(f"MCP entry {name!r} must specify exactly one of command or url")
    if "url" in config:
        return {"url": config["url"], "headers": expand(config.get("headers", {}))}
    cwd = Path(config["cwd"]).expanduser() if config.get("cwd") else None
    if cwd is not None and not cwd.is_absolute():
        cwd = (path.parent / cwd).resolve()
    return {"stdio": StdioServerParameters(command=config["command"], args=config.get("args", []),
                                          env=expand(config.get("env", {})), cwd=cwd)}


def wire(value) -> dict:
    """Keep aliases, extension fields and explicitly returned nulls from SDK models."""
    return value.model_dump(mode="json", by_alias=True, exclude_unset=True)


class RecordedSession(ClientSession):
    """Record SDK responses before output-schema validation; never retry a request."""

    def __init__(self, *args, storage: ToolStorage, server: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.storage, self.server = storage, server
        self.requests = {}

    async def _handle_response(self, message):
        """Tap the parsed JSON-RPC envelope before SDK result validation/coercion."""
        body = wire(message.message)
        owner = self.requests.get(self._normalize_request_id(body["id"]), {})
        self.storage.record(f"{self.storage.allocate('mcp-response')}.json", body, server=self.server, **owner)
        await super()._handle_response(message)

    async def send_request(self, request, result_type, *args, **kwargs):
        operation = self.storage.allocate("mcp-request")
        owner = {"server": self.server, "call_id": CALL_ID.get()}
        # SDK 1.30.0 allocates this ID before its first await, including concurrent requests.
        request_id = self._request_id
        self.requests[request_id] = {"request_operation": operation, "call_id": CALL_ID.get()}
        self.storage.record(f"{operation}.request.json", wire(request), request_id=request_id, **owner)
        try:
            result = await super().send_request(request, result_type, *args, **kwargs)
        except asyncio.CancelledError:
            self.storage.event("mcp/cancelled", operation=operation, request_id=request_id, **owner)
            # SDK 1.x drops the local waiter but does not notify the server on cancellation.
            with suppress(Exception):
                async with asyncio.timeout(2):
                    await self.send_notification(types.ClientNotification(types.CancelledNotification(
                        params=types.CancelledNotificationParams(requestId=request_id, reason="Caller cancelled"),
                    )))
            raise
        except McpError as exc:
            self.storage.record(f"{operation}.error.json", wire(exc.error), **owner)
            raise
        except Exception as exc:
            self.storage.record(f"{operation}.failure.json", {"type": type(exc).__name__, "message": str(exc)}, **owner)
            raise
        return result


class MCPTools(ToolProvider):
    """Own SDK scopes in one task so startup, calls and shutdown may have different callers."""

    def __init__(self, storage: ToolStorage, *, server: str, stdio: StdioServerParameters | None = None,
                 url: str | None = None, headers: dict[str, str] | None = None, concurrency: int = 8,
                 is_llm_multi_modal: bool = True, startup_timeout: float = 30, request_timeout: float = 300):
        if (stdio is None) == (url is None):
            raise ValueError("Choose exactly one MCP transport: stdio or Streamable HTTP")
        self.storage, self.server, self.stdio, self.url, self.headers = storage, server, stdio, url, headers
        self.limit = asyncio.Semaphore(concurrency)
        self.is_llm_multi_modal = is_llm_multi_modal
        self.startup_timeout, self.request_timeout = startup_timeout, request_timeout
        self.session = self.task = None
        self.stop = asyncio.Event()
        self.initialized = asyncio.Event()
        self.failure = None
        self.instructions = None
        self.catalog = []
        self.tool_specs = ()
        self.handlers = {}

    async def connect(self):
        if self.task is not None:
            raise RuntimeError("MCP connection has already been started")
        self.task = asyncio.create_task(self._serve(), name=f"mcp:{self.server}")
        try:
            async with asyncio.timeout(self.startup_timeout):
                await self.initialized.wait()
            if self.failure:
                raise self.failure
        except BaseException:
            await self.aclose()
            raise
        return self

    async def _serve(self):
        try:
            async with AsyncExitStack() as stack:
                if self.stdio:
                    # Server diagnostics are not protocol evidence and may contain credentials.
                    stderr = stack.enter_context(open(os.devnull, "w"))  # noqa: ASYNC230, SIM115 — ExitStack owns /dev/null.
                    streams = await stack.enter_async_context(stdio_client(self.stdio, errlog=stderr))
                else:
                    client = await stack.enter_async_context(httpx.AsyncClient(
                        headers=self.headers, timeout=httpx.Timeout(self.request_timeout, connect=20),
                    ))
                    streams = await stack.enter_async_context(streamable_http_client(self.url, http_client=client))
                self.session = await stack.enter_async_context(RecordedSession(
                    *streams[:2], storage=self.storage, server=self.server,
                    read_timeout_seconds=timedelta(seconds=self.request_timeout),
                ))
                info = await self.session.initialize()
                self.instructions = info.instructions
                await self._discover(info)
                self.initialized.set()
                await self.stop.wait()
        except BaseException as exc:
            while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
                exc = exc.exceptions[0]
            self.failure = exc
            self.storage.event("mcp/connection_error", server=self.server, error=type(exc).__name__, message=str(exc))
        finally:
            self.session = None
            self.initialized.set()
            self.storage.event("mcp/closed", server=self.server)

    async def aclose(self):
        if self.task is None:
            return
        self.stop.set()
        if not self.initialized.is_set():
            self.task.cancel()
        # Closing in the owner task also avoids crossing the SDK's AnyIO cancel scopes.
        try:
            await asyncio.shield(self.task)
        except asyncio.CancelledError:
            await asyncio.shield(self.task)
            raise

    def clear_context(self) -> None:
        """The connection and catalog belong to the session, not the conversation context."""

    def handler(self, name: str):
        return self.handlers[name]

    async def _discover(self, info):
        specs, seen = [], set()
        if info.capabilities.tools is not None:
            cursor = None
            while True:
                page = await self.session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
                for item in page.tools:
                    if item.name in self.handlers:
                        raise ValueError(f"Duplicate MCP tool: {item.name}")
                    self.catalog.append(wire(item))
                    specs.append(ToolSpec(item.name, item.description or "", copy.deepcopy(item.inputSchema),
                                          {"type": "object"}))
                    self.handlers[item.name] = partial(self._invoke, "tools/call", item.name)
                cursor = page.nextCursor
                if cursor is None:
                    break
                if cursor in seen:
                    raise ValueError("MCP tools/list returned a repeated pagination cursor")
                seen.add(cursor)
        if info.capabilities.resources is not None:
            for method, fields, required, description in (
                ("list_resources", {"cursor": {"type": "string"}}, [],
                 "List resources from this MCP server. Pass nextCursor as cursor for the next page."),
                ("list_resource_templates", {"cursor": {"type": "string"}}, [],
                 "List URI templates from this MCP server. Pass nextCursor as cursor for the next page."),
                ("read_resource", {"uri": {"type": "string"}}, ["uri"],
                 "Read a resource URI from this MCP server, including URIs built from its resource templates."),
            ):
                name = f"mcp_{self.server}_{method}"
                if name in self.handlers:
                    raise ValueError(f"MCP resource entry conflicts with tool: {name}")
                specs.append(ToolSpec(name, description,
                                      {"type": "object", "properties": fields, "required": required,
                                       "additionalProperties": False}, {"type": "object"}))
                self.handlers[name] = partial(self._invoke, method, name)
        self.tool_specs = tuple(specs)
        self.storage.record(f"{self.storage.allocate('mcp-catalog')}.json", {
            "server": self.server, "initialize": wire(info), "tools": self.catalog,
            "resource_entries": [spec.name for spec in specs if spec.name.startswith(f"mcp_{self.server}_")],
        })

    async def _invoke(self, method, name, call_id, /, **arguments) -> ToolOutput:
        token = CALL_ID.set(call_id)
        try:
            async with self.limit:
                if self.session is None or self.stop.is_set():
                    raise RuntimeError("MCP connection is closed")
                if method == "tools/call":
                    result = await self.session.call_tool(name, arguments)
                elif method == "read_resource":
                    result = await self.session.read_resource(AnyUrl(arguments["uri"]))
                else:
                    result = await getattr(self.session, method)(
                        params=types.PaginatedRequestParams(**arguments),
                    )
                return self._output(wire(result), call_id)
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            if isinstance(exc, McpError):
                error.update(wire(exc.error))
            return ToolOutput(json.dumps({"error": error}, ensure_ascii=False))
        finally:
            CALL_ID.reset(token)

    def _output(self, raw: dict, call_id: str) -> ToolOutput:
        result = copy.deepcopy(raw)
        images, observations = [], []

        def attachment(part, key):
            data = base64.b64decode(part.pop(key), validate=True)
            media = part.get("mimeType", "application/octet-stream")
            path = self.storage.write(f"{self.storage.allocate('mcp-attachment')}.body", data, call_id=call_id,
                                  media_type=media)
            part.update(artifact=path, size=len(data))
            if self.is_llm_multi_modal and media.startswith("image/"):
                label = f"Image from MCP server {self.server}, call {call_id}"
                images.extend([{"type": "text", "text": label}, {"type": "image_url", "image_url": {
                    "url": f"data:{media};base64,{base64.b64encode(data).decode('ascii')}", "detail": "auto",
                }}])
                observations.extend([{"type": "input_text", "text": label}, {
                    "type": "input_image", "image_url": path, "media_type": media,
                }])

        for part in result.get("content", []):
            if part.get("type") in {"image", "audio"}:
                attachment(part, "data")
            elif part.get("type") == "resource" and "blob" in part["resource"]:
                attachment(part["resource"], "blob")
        for part in result.get("contents", []):
            if "blob" in part:
                attachment(part, "blob")
        if result.get("isError"):
            result["error"] = {"type": "MCPToolError", "message": "\n".join(
                part["text"] for part in result.get("content", []) if part.get("type") == "text"
            ) or "MCP server returned isError=true"}
        return ToolOutput(json.dumps(result, ensure_ascii=False), images=images, observations=observations)
