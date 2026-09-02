"""Construct normalized system-context semantics for Agent adapters.

``instruction``, ``tool_defs``, ``mcp``, and ``skill_list`` are shared conventions,
not a closed vocabulary. Adapters may emit any typed content part, and the canonical fold
preserves its payload and order. These helpers only construct common semantics; they never
interpret a complete Agent configuration. ``<opaque>`` means that at least one value is
known to exist while its content or exact cardinality is unavailable.
"""

from collections.abc import Iterable

from .events import message_item

OPAQUE = "<opaque>"


def instruction(text: str = OPAQUE) -> dict:
    """Build an exact or opaque instruction.

    Args:
        text: Exact instruction text, or ``OPAQUE`` when at least one instruction exists
            but its content is unavailable.
    """
    return {"type": "instruction", "text": text}


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
            and an empty iterable both mean an observed empty collection. Include a
            definition named ``OPAQUE`` when at least one tool cannot be enumerated.
    """
    return {"type": "tool_defs", "tools": [_tool_definition(value) for value in values]}


def mcp(name: str = OPAQUE) -> dict:
    """Build one exact or opaque atomic MCP contribution.

    Args:
        name: Observable server identity, or ``OPAQUE`` when at least one contribution
            exists but its identity is unavailable.
    """
    return {"type": "mcp", "name": name}


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
        return [
            mcp(spec.get("id") or spec.get("serverName") or spec.get("name") or OPAQUE)
            if isinstance(spec, dict) else mcp()
            for spec in servers
        ]
    return [mcp()]


def skill_list(skills: Iterable[str] = ()) -> dict:
    """Build the skill catalog exposed to an Agent.

    Args:
        skills: Exact names in source order. Include ``OPAQUE`` when at least one skill
            exists but its identity is unavailable.
    """
    return {"type": "skill_list", "skills": list(skills)}


def system_message(content: list[dict]) -> dict:
    """Wrap ordered system semantics in one Context message.

    Args:
        content: Ordered semantic content parts.
    """
    return message_item("system", content)
