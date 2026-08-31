"""Shared fixtures for gh_puller_mcp tests."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def write_script(tmp_path: Path, body: str, name: str = "codebase-memory-mcp") -> str:
    """Create an executable Python shim standing in for the C binary."""
    path = tmp_path / name
    path.write_text("#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def shim(tmp_path):
    def _shim(body: str, name: str = "codebase-memory-mcp") -> str:
        return write_script(tmp_path, body, name)

    return _shim


@pytest.fixture
def stub_call_tool():
    """Stub backend: records (tool, arguments); returns a fixed envelope."""

    def _make(envelope: dict):
        calls: list[tuple[str, dict]] = []

        def _call(tool: str, arguments: dict) -> dict:
            calls.append((tool, arguments))
            return dict(envelope)

        _call.calls = calls
        return _call

    return _make
