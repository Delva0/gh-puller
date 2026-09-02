"""Provide provider adapters behind one session, stream, and result contract.

Configuration is supplied at construction. A session may contain repeated calls;
``stream`` yields visible text deltas and ``result`` returns the final visible text.
Both methods require an active ``session`` context. ``GENERATORS`` is the registry.
"""

from .base import BaseGenerator
from .cc import ClaudeCode, ClaudeConfig
from .codex import Codex, CodexConfig
from .dsh import Dsh, DshConfig
from .openai import OpenAI, OpenAIConfig
from .opencode import OpenCode, OpenCodeConfig
from .utils import RequestFailedError

__all__ = [
    "GENERATORS",
    "BaseGenerator",
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

GENERATORS: dict[str, type[BaseGenerator]] = {"cc": ClaudeCode, "dsh": Dsh,
                                             "codex": Codex, "opencode": OpenCode,
                                             "llm": OpenAI}
