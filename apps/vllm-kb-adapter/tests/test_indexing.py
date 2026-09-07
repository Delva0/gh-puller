"""Asynchronous online-index state and business-query isolation tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from vllm_kb_adapter.adapter import Adapter
from vllm_kb_adapter.indexing import IndexCoordinator
from vllm_kb_adapter.snapshots import VLLM_PROJECT, SnapshotRegistry


def _envelope(data: dict[str, Any], *, error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": str(data)}],
        "structuredContent": data,
        "isError": error,
    }


def _call() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "search_graph",
            "arguments": {"project": VLLM_PROJECT, "version": "0.23.0", "query": "scheduler"},
        },
    }


class QueryUpstream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return _envelope({"cols": ["qn", "label"], "rows": [["vllm.scheduler", "Module"]]})


class IndexUpstream:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.projects: dict[str, Path] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.index_started = asyncio.Event()
        self.release_index = asyncio.Event()
        self.verify_started = asyncio.Event()
        self.release_verify = asyncio.Event()
        self.fail_first = fail_first
        self.closed = False
        self._list_calls = 0

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name == "list_projects":
            self._list_calls += 1
            if self._list_calls == 2:
                self.verify_started.set()
                await self.release_verify.wait()
            projects = [{"name": project, "root_path": str(path)} for project, path in self.projects.items()]
            return _envelope({"projects": projects, "has_more": False})
        self.index_started.set()
        if self.fail_first:
            self.fail_first = False
            return _envelope({"error": "index failed"}, error=True)
        await self.release_index.wait()
        self.projects[arguments["name"]] = Path(arguments["repo_path"])
        return _envelope({"project": arguments["name"]})

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_missing_index_reports_states_then_runs_original_query(
    registry: SnapshotRegistry,
) -> None:
    query = QueryUpstream()
    index = IndexUpstream()
    coordinator = IndexCoordinator(index, ())
    adapter = Adapter(registry, query, coordinator)

    queued = await adapter.handle(_call())
    snapshot = registry.resolve(VLLM_PROJECT, "0.23.0")
    job = coordinator._jobs[snapshot.index_name]
    await asyncio.wait_for(index.index_started.wait(), timeout=1)
    running = await adapter.handle(_call())
    index.release_index.set()
    await asyncio.wait_for(index.verify_started.wait(), timeout=1)
    verifying = await adapter.handle(_call())
    index.release_verify.set()
    await asyncio.wait_for(job.task, timeout=1)
    ready = await adapter.handle(_call())
    await coordinator.aclose()

    assert queued["result"]["structuredContent"] == {
        "kind": "index_repository",
        "state": "queued",
        "project": VLLM_PROJECT,
        "version": "0.23.0",
    }
    assert queued["result"]["isError"] is False
    assert running["result"]["structuredContent"]["state"] == "running"
    assert verifying["result"]["structuredContent"]["state"] == "verifying"
    assert "retry_after_seconds" not in verifying["result"]["structuredContent"]
    assert ready["result"]["structuredContent"]["rows"] == [
        {"qn": "vllm.scheduler", "label": "Module"},
    ]
    assert query.calls == [
        (
            "search_graph",
            {
                "project": snapshot.index_name,
                "query": "scheduler",
                "format": "json",
            },
        ),
    ]
    assert [name for name, _arguments in index.calls].count("index_repository") == 1
    assert index.closed is True


@pytest.mark.asyncio
async def test_failed_index_is_reported_once_and_requeued(registry: SnapshotRegistry) -> None:
    index = IndexUpstream(fail_first=True)
    coordinator = IndexCoordinator(index, ())
    adapter = Adapter(registry, QueryUpstream(), coordinator)

    await adapter.handle(_call())
    snapshot = registry.resolve(VLLM_PROJECT, "0.23.0")
    failed_job = coordinator._jobs[snapshot.index_name]
    await asyncio.wait_for(failed_job.task, timeout=1)
    failed = await adapter.handle(_call())
    requeued = await adapter.handle(_call())
    await coordinator.aclose()

    assert failed["result"]["structuredContent"] == {
        "kind": "index_repository",
        "state": "failed",
        "project": VLLM_PROJECT,
        "version": "0.23.0",
        "error": "index failed",
    }
    assert failed["result"]["isError"] is True
    assert requeued["result"]["structuredContent"]["state"] in {"queued", "running"}
