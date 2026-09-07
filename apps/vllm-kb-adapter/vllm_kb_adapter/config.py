"""Resolve adapter settings from environment and account-local defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    """Process-level paths, transport addresses, and timeouts."""

    upstream_url: str
    vllm_root: Path
    vllm_ascend_root: Path
    host: str
    port: int
    path: str
    upstream_timeout: float

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the ``VLLM_KB_ADAPTER_*`` environment contract."""
        snapshot_home = Path.home()
        return cls(
            upstream_url=os.environ.get(
                "VLLM_KB_ADAPTER_UPSTREAM_URL",
                "http://127.0.0.1:8788/mcp",
            ),
            vllm_root=Path(
                os.environ.get(
                    "VLLM_KB_ADAPTER_VLLM_ROOT",
                    str(snapshot_home / "snapshots-vllm"),
                ),
            ),
            vllm_ascend_root=Path(
                os.environ.get(
                    "VLLM_KB_ADAPTER_VLLM_ASCEND_ROOT",
                    str(snapshot_home / "snapshots-vllm-ascend"),
                ),
            ),
            host=os.environ.get("VLLM_KB_ADAPTER_HOST", "127.0.0.1"),
            port=int(os.environ.get("VLLM_KB_ADAPTER_PORT", "8787")),
            path=os.environ.get("VLLM_KB_ADAPTER_PATH", "/gh-puller/graph"),
            upstream_timeout=float(
                os.environ.get("VLLM_KB_ADAPTER_UPSTREAM_TIMEOUT", "25"),
            ),
        )
