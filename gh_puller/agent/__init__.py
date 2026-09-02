"""Expose Agent adapters and observation configuration.

Concrete integrations live in ``adapters``; the canonical language and fold live in
``events``; common Context semantics live in ``context``; delivery channels live in
``sinks``.
"""

from .adapters import (
    AGENTS,
    BaseAgent,
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
from .sinks import configure

__all__ = [
    "AGENTS",
    "OPAQUE",
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
    "configure",
]
