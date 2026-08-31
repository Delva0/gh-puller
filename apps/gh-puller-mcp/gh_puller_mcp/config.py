"""Server configuration (kept import-cycle-free for gh_puller_mcp.tools.base)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from gh_puller_mcp.backend import Backend

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class ServerConfig:
    """Everything that shapes the server; pure data, everything else derives from it."""

    profile: str = "all"
    backend: Backend = field(default_factory=Backend)
    version: str | None = None  # override serverInfo.version
    call_tool: Callable[[str, dict], dict] | None = None  # tests inject a stub
