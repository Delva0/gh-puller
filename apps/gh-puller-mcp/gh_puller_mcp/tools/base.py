"""Per-tool unit: each tool module bundles its verbatim definition + implementation.

* ToolDef carries the byte-identical surface data (name/title/description/
  input_schema/annotations) extracted from the C server's TOOLS[] table.
* `register` binds a module's TOOL to its implementation fn (fn name must
  equal the tool name); `passthrough` is the default implementation — run the
  tool through `codebase-memory-mcp cli --json` and forward its envelope.
* The CallToolResult envelope (structuredContent three-state) is built here so
  the C server's rules live at exactly one place.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import mcp_types as types

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

from gh_puller_mcp.backend import BackendError

if TYPE_CHECKING:
    from gh_puller_mcp.config import ServerConfig

HANDLERS: dict[str, Callable[..., types.CallToolResult]] = {}


@dataclass(frozen=True)
class ToolDef:
    """One tool definition, verbatim from the C TOOLS[] table."""

    name: str
    title: str
    description: str
    input_schema: str  # raw JSON text
    annotations: tuple[bool, bool, bool, bool]  # readOnly, destructive, idempotent, openWorld


def register(fn):
    """Bind fn to the TOOL defined in the same module (fn name must equal it)."""
    module: ModuleType = sys.modules[fn.__module__]
    tool = getattr(module, "TOOL", None)
    if not isinstance(tool, ToolDef):
        raise TypeError(f"{fn.__module__} has no TOOL definition to register")
    if fn.__name__ != tool.name:
        raise ValueError(f"fn {fn.__name__!r} != tool name {tool.name!r} in {fn.__module__}")
    HANDLERS[tool.name] = fn
    return fn


def _invoke(config: ServerConfig, name: str, arguments: dict) -> dict:
    if config.call_tool is not None:
        return config.call_tool(name, arguments)
    return config.backend.call_tool(name, arguments)


def mcp_text_result(text: str, is_error: bool) -> dict:
    """cbm_mcp_text_result: structuredContent = parsed object | {"error": ...} | omitted."""
    envelope: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    try:
        candidate = json.loads(text)
    except ValueError:
        candidate = None
    if isinstance(candidate, dict):
        envelope["structuredContent"] = candidate
    elif is_error:
        envelope["structuredContent"] = {"error": text}
    return envelope


def _call_tool_result(envelope: dict) -> types.CallToolResult:
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text=envelope["content"][0]["text"])],
        is_error=envelope["isError"],
    )
    if "structuredContent" in envelope:
        result.structured_content = envelope["structuredContent"]
    return result


def passthrough(tool: ToolDef, arguments: dict, config: ServerConfig) -> types.CallToolResult:
    """Default implementation: delegate to the cbm CLI and forward its envelope."""
    try:
        return _call_tool_result(
            _invoke(config, tool.name, arguments if arguments is not None else {}),
        )
    except BackendError as exc:
        return _call_tool_result(mcp_text_result(f"backend error: {exc}", True))
