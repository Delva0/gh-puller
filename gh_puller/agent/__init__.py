"""Expose Agent adapters and observation configuration.

Concrete integrations live in ``adapters``; the canonical language and fold live in
``events``; common Context semantics live in ``context``; delivery channels live in
``sinks``; read-only measurements live in ``metrics``.
"""

from .adapters import (
    AGENTS,
    BaseAgent,
    ChatCompletion,
    ClaudeCode,
    ClaudeConfig,
    Codex,
    CodexConfig,
    Dsh,
    DshConfig,
    OpenAI,
    OpenAIConfig,
    OpenCode,
    OpenCodeConfig,
    RequestFailedError,
)
from .context import OPAQUE
from .metrics import read_events, summarize
from .sinks import configure, flush, session_path, shutdown

__all__ = [
    "AGENTS",
    "OPAQUE",
    "BaseAgent",
    "ChatCompletion",
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
    "configure",
    "flush",
    "read_events",
    "session_path",
    "shutdown",
    "summarize",
]
