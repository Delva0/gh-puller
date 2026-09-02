"""Expose concrete adapters behind the common Agent contract.

Configuration is supplied at construction. A session may contain repeated calls;
``stream`` yields visible text deltas and ``result`` returns the final visible text.
Both methods require an active ``session`` context. ``AGENTS`` is the registry.
"""

from ..base import BaseAgent, RequestFailedError
from .cc import ClaudeCode, ClaudeConfig
from .codex import Codex, CodexConfig
from .dsh import Dsh, DshConfig
from .openai import OpenAI, OpenAIConfig
from .opencode import OpenCode, OpenCodeConfig

__all__ = [
    "AGENTS",
    "BaseAgent",
    "ClaudeCode",
    "ClaudeConfig",
    "Codex",
    "CodexConfig",
    "Dsh",
    "DshConfig",
    "OpenAI",
    "OpenAIConfig",
    "OpenCode",
    "OpenCodeConfig",
    "RequestFailedError",
]

AGENTS: dict[str, type[BaseAgent]] = {
    "cc": ClaudeCode,
    "dsh": Dsh,
    "codex": Codex,
    "opencode": OpenCode,
    "llm": OpenAI,
}
