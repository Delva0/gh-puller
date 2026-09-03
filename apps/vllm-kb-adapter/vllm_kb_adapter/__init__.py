"""Expose the vllm-kb adapter version and public assembly API."""

from vllm_kb_adapter.app import create_app

__all__ = ["create_app"]
__version__ = "0.1.0"
