"""Expose agent generators and observation configuration.

Generators live in ``generators``; the canonical protocol and replay fold live in
``events``; delivery channels live in ``sinks``.
"""

from .generators import (
    GENERATORS,
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
from .sinks import configure

__all__ = [
    "GENERATORS",
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
