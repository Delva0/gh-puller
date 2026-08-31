"""MCP server semantics on top of the official `mcp` SDK.

The wire layer (stdio framing, JSON-RPC, handshake, notifications, unknown
methods) is the SDK's. What remains is codebase-memory-mcp-specific behavior,
expressed as pure functions that produce SDK models directly
(ListToolsResult / CallToolResult / GetPromptResult) plus a small `Server`
assembly:

* the verbatim tool surface (profiles, C-style pagination, annotations),
* the CallToolResult envelope rules (structuredContent three-state),
* the prompt templates and their -32602 validation,
* the `cli` passthrough bridge.

Intentional divergences from the C server (documented in README):
* tools/call with a missing name is rejected by the SDK (-32602 Invalid
  request parameters) instead of the C server's "missing tool name" envelope;
* resources/list & resources/templates/list are not served (the C server
  answers empty arrays; the SDK answers -32601) and are not advertised;
* the initialize capabilities carry the SDK's `experimental` key, and all wire
  keys are serialized in the SDK's (alphabetical) order.
"""

from __future__ import annotations

import json
import re
from functools import partial
from typing import Any

import anyio
import mcp_types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from gh_puller_mcp.backend import BackendError
from gh_puller_mcp.config import ServerConfig
from gh_puller_mcp.manifest import (
    ANALYSIS_TOOLS,
    DEFAULT_ANNOTATIONS,
    INSTRUCTIONS_ALL,
    INSTRUCTIONS_ANALYSIS,
    INSTRUCTIONS_SCOUT,
    PAGE_SIZE,
    PROMPTS,
    SCOUT_TOOLS,
    TRACE_CALL_PATH_ALIAS,
)
from gh_puller_mcp.tools import TOOL_ANNOTATIONS, TOOL_HANDLERS, TOOLS, tool_for
from gh_puller_mcp.tools.base import ToolDef, _call_tool_result, mcp_text_result

SERVER_NAME = "codebase-memory-mcp"
TOOL_COUNT = len(TOOLS)
INVALID_PARAMS = -32602
_PROFILES = ("all", "analysis", "scout")
_CURSOR_RE = re.compile(r"^\s*[+-]?[0-9]+$")
_INSTRUCTIONS = {
    "all": INSTRUCTIONS_ALL,
    "analysis": INSTRUCTIONS_ANALYSIS,
    "scout": INSTRUCTIONS_SCOUT,
}
_ALLOWED: dict[str, frozenset[str] | None] = {
    "all": None,
    "analysis": ANALYSIS_TOOLS,
    "scout": SCOUT_TOOLS,
}


class PromptError(Exception):
    """A prompts/get failure, surfaced as a -32602 JSON-RPC error."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def instructions_for(profile: str) -> str:
    return _INSTRUCTIONS.get(profile, INSTRUCTIONS_ALL)


def tools_for(profile: str) -> list[ToolDef]:
    """Profile-filtered tool list in manifest order (unknown profile = all)."""
    allowed = _ALLOWED.get(profile)
    return list(TOOLS) if allowed is None else [t for t in TOOLS if t.name in allowed]


def tool_allowed(profile: str, name: str) -> bool:
    allowed = _ALLOWED.get(profile)
    return allowed is None or name in allowed


def _tool_for(name: str) -> ToolDef | None:
    return tool_for(name)


def _cursor_offset(cursor: Any) -> int:
    """mcp_tools_cursor_offset: strtol-consume fully, >= 0, clamp to TOOL_COUNT."""
    if not isinstance(cursor, str) or not cursor or _CURSOR_RE.fullmatch(cursor) is None:
        return TOOL_COUNT
    parsed = int(cursor)
    return TOOL_COUNT if parsed < 0 else min(parsed, TOOL_COUNT)


def tools_list_page(profile: str, params: Any) -> types.ListToolsResult:
    """Exact C algorithm: no cursor key -> the FULL list, no nextCursor.

    With a cursor key (even non-string) pagination engages: a page of 8.
    """
    available = tools_for(profile)
    if not isinstance(params, dict) or "cursor" not in params:
        return types.ListToolsResult(tools=[_tool_model(t) for t in available])
    offset = min(_cursor_offset(params["cursor"]), len(available))
    end = min(offset + PAGE_SIZE, len(available))
    return types.ListToolsResult(
        tools=[_tool_model(t) for t in available[offset:end]],
        next_cursor=str(end) if end < len(available) else None,
    )


def _tool_model(defn: ToolDef) -> types.Tool:
    read_only, destructive, idempotent, open_world = TOOL_ANNOTATIONS.get(
        defn.name, DEFAULT_ANNOTATIONS,
    )
    return types.Tool(
        name=defn.name,
        title=defn.title,
        description=defn.description,
        input_schema=json.loads(defn.input_schema),
        annotations=types.ToolAnnotations(
            read_only_hint=read_only,
            destructive_hint=destructive,
            idempotent_hint=idempotent,
            open_world_hint=open_world,
        ),
    )


def dispatch_tool_call(name: str, arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """tools/call in C dispatch order; every path returns an SDK CallToolResult.

    The implementation is the per-tool fn registered via @register (default:
    passthrough to the cbm CLI); this layer keeps the protocol semantics
    (profile filtering, legacy alias, unknown-tool envelope, error synthesis).
    """
    if not tool_allowed(config.profile, name):
        label = "scout" if config.profile == "scout" else "analysis"
        message = f"tool '{name}' is not available in the {label} tool profile"
        return _call_tool_result(mcp_text_result(message, True))
    if name == TRACE_CALL_PATH_ALIAS:
        name = "trace_path"
    if _tool_for(name) is None:
        return _call_tool_result(mcp_text_result(f"unknown tool: {name}", True))
    try:
        return TOOL_HANDLERS[name](arguments if arguments is not None else {}, config)
    except BackendError as exc:
        return _call_tool_result(mcp_text_result(f"backend error: {exc}", True))


def prompt_models() -> list[types.Prompt]:
    return [
        types.Prompt(
            name=prompt.name,
            title=prompt.title,
            description=prompt.description,
            arguments=[
                types.PromptArgument(
                    name=arg.name,
                    title=arg.title,
                    description=arg.description,
                    required=arg.required,
                )
                for arg in prompt.arguments
            ],
        )
        for prompt in PROMPTS
    ]


def _prompt_arg(arguments: dict, name: str) -> str | None:
    """mcp_prompt_string_argument: non-empty string, else None."""
    value = arguments.get(name)
    return value if isinstance(value, str) and value else None


def prompt_result(name: str, arguments: Any) -> types.GetPromptResult:
    """prompts/get with verbatim template rendering; raises PromptError (-32602)."""
    prompt = next((p for p in PROMPTS if p.name == name), None)
    if prompt is None:
        raise PromptError("Invalid prompt name")
    arguments = arguments if isinstance(arguments, dict) else {}
    project = _prompt_arg(arguments, "project")
    request_arg = _prompt_arg(arguments, "question" if prompt.name == "explore_codebase" else "change")
    if project is None or request_arg is None:
        raise PromptError("Missing required prompt arguments")
    if prompt.name == "review_change_impact":
        base_branch = "main"
        value = arguments.get("base_branch")
        if value is not None:
            if not isinstance(value, str) or not value:
                raise PromptError("Invalid prompt arguments")
            base_branch = value
        text = prompt.template % (project, request_arg, base_branch)
    else:
        text = prompt.template % (project, request_arg)
    return types.GetPromptResult(
        description=prompt.get_description,
        messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=text))],
    )


# ── SDK assembly ─────────────────────────────────────────────────────────────


async def _on_list_tools(config: ServerConfig, handler_ctx, params) -> types.ListToolsResult:
    cursor = getattr(params, "cursor", None) if params is not None else None
    return tools_list_page(config.profile, {"cursor": cursor} if cursor is not None else None)


async def _on_call_tool(config: ServerConfig, handler_ctx, params) -> types.CallToolResult:
    arguments = params.arguments if params.arguments is not None else {}
    return dispatch_tool_call(params.name, arguments, config)


async def _on_list_prompts(config: ServerConfig, handler_ctx, params) -> types.ListPromptsResult:
    return types.ListPromptsResult(prompts=prompt_models())


async def _on_get_prompt(config: ServerConfig, handler_ctx, params) -> types.GetPromptResult:
    try:
        return prompt_result(params.name, params.arguments)
    except PromptError as exc:
        raise MCPError(code=INVALID_PARAMS, message=exc.message) from None


def build_server(config: ServerConfig | None = None) -> Server:
    """Bind the codebase-memory-mcp behavior to an SDK server."""
    config = config or ServerConfig()
    return Server(
        SERVER_NAME,
        version=config.version or config.backend.version(),
        instructions=instructions_for(config.profile),
        on_list_tools=partial(_on_list_tools, config),
        on_call_tool=partial(_on_call_tool, config),
        on_list_prompts=partial(_on_list_prompts, config),
        on_get_prompt=partial(_on_get_prompt, config),
    )


def run_server(config: ServerConfig | None = None) -> None:
    """Serve MCP over stdio until the client closes the connection (exit 0)."""
    config = config or ServerConfig()

    async def serve() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await build_server(config).run(read_stream, write_stream, initialization_options=None)

    anyio.run(serve)
