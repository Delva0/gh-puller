"""Construct normalized system-context semantics for Agent adapters.

``instruction``, ``tool_defs``, ``mcp``, and ``skill_list`` are shared conventions,
not a closed vocabulary. Adapters may emit any typed content part, and the canonical fold
preserves its payload and order. These helpers only construct common semantics; they never
interpret a complete Agent configuration.
"""

from collections.abc import Iterable

from .events import message_item


def instruction(text: str | None = None) -> dict:
    """Build an instruction whose text may be unavailable.

    Args:
        text: Exact instruction text, or ``None`` when only its existence is known.
    """
    part = {"type": "instruction"}
    if text is not None:
        part["text"] = text
    return part


def _tool_definition(value: str | dict) -> dict:
    if isinstance(value, str):
        return {"name": value}
    function = value.get("function") or value
    result = {}
    if isinstance(function.get("name"), str):
        result["name"] = function["name"]
    if isinstance(function.get("description"), str):
        result["description"] = function["description"]
    if "parameters" in function:
        result["inputSchema"] = function["parameters"]
    elif "inputSchema" in function:
        result["inputSchema"] = function["inputSchema"]
    return result


def tool_defs(values: Iterable[str | dict] = ()) -> dict:
    """Build the observable tool-definition collection.

    Args:
        values: Tool names or direct/OpenAI-shaped definitions in source order. Omission
            and an empty iterable both mean an observed empty collection. Use a definition
            named ``opaque`` when built-in tools may exist but cannot be enumerated.
    """
    return {"type": "tool_defs", "tools": [_tool_definition(value) for value in values]}


def mcp(name: str | None = None) -> dict:
    """Build one atomic MCP contribution.

    Args:
        name: Observable server identity, or ``None`` when unavailable.
    """
    part = {"type": "mcp"}
    if name is not None:
        part["name"] = name
    return part


def mcps(servers) -> list[dict]:
    """Project configured MCP server identities without exposing transport details.

    Args:
        servers: Mapping, sequence, or external configuration reference accepted by an
            adapter.

    Returns:
        Atomic MCP contributions in source order.
    """
    if not servers:
        return []
    if isinstance(servers, dict):
        return [mcp(str(name)) for name in servers]
    if isinstance(servers, list):
        return [mcp(spec.get("id") or spec.get("serverName") or spec.get("name") or None)
                if isinstance(spec, dict) else mcp()
                for spec in servers]
    return [mcp()]


def skill_list(skills: list[str] | str) -> dict:
    """Build the skill catalog exposed to an Agent.

    Args:
        skills: Exact names or the adapter's observable catalog selector.
    """
    return {"type": "skill_list", "skills": skills}


def system_message(content: list[dict]) -> dict:
    """Wrap ordered system semantics in one Context message.

    Args:
        content: Ordered semantic content parts.
    """
    return message_item("system", content)
