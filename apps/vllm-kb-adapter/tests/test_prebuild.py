"""Offline index prebuild and production binding-audit tests."""

from pathlib import Path
from typing import Any

import pytest

from vllm_kb_adapter.prebuild import (
    PrebuildError,
    audit_indexes,
    ensure_bindings,
    prebuild_all,
)
from vllm_kb_adapter.snapshots import SnapshotRegistry


class FakeUpstream:
    def __init__(self, projects: dict[str, Path]) -> None:
        self.projects = projects
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name == "list_projects":
            rows = [{"name": project, "root_path": str(path)} for project, path in self.projects.items()]
            data = {"projects": rows, "has_more": False}
        else:
            data = {"project": arguments["name"]}
            self.projects[arguments["name"]] = Path(arguments["repo_path"])
        return {
            "content": [{"type": "text", "text": "ok"}],
            "structuredContent": data,
            "isError": False,
        }


@pytest.mark.asyncio
async def test_prebuild_skips_exact_binding_and_builds_every_missing_snapshot(
    registry: SnapshotRegistry,
) -> None:
    existing = registry.snapshots[0]
    upstream = FakeUpstream({existing.index_name: existing.path})
    events = []

    report = await prebuild_all(
        registry,
        upstream,
        mode="moderate",
        progress=lambda action, snapshot: events.append((action, snapshot.index_name)),
    )

    assert report.skipped == (existing,)
    assert report.built == registry.snapshots[1:]
    index_calls = [call for call in upstream.calls if call[0] == "index_repository"]
    assert len(index_calls) == len(registry.snapshots) - 1
    assert all(call[1]["mode"] == "moderate" for call in index_calls)
    assert events[0] == ("skip", existing.index_name)
    assert upstream.calls[-1][0] == "list_projects"


@pytest.mark.asyncio
async def test_prebuild_rejects_name_bound_to_another_snapshot(
    registry: SnapshotRegistry,
    tmp_path: Path,
) -> None:
    snapshot = registry.snapshots[0]
    upstream = FakeUpstream({snapshot.index_name: tmp_path / "wrong"})

    with pytest.raises(PrebuildError, match="points to"):
        await prebuild_all(registry, upstream)


@pytest.mark.asyncio
async def test_audit_reports_missing_and_mismatched_indexes(
    registry: SnapshotRegistry,
    tmp_path: Path,
) -> None:
    exact, mismatched, *_ = registry.snapshots
    upstream = FakeUpstream(
        {
            exact.index_name: exact.path,
            mismatched.index_name: tmp_path / "wrong",
        },
    )

    audit = await audit_indexes(registry, upstream)

    assert audit.ok is False
    assert audit.mismatched == (mismatched,)
    assert audit.missing == registry.snapshots[2:]


@pytest.mark.asyncio
async def test_online_binding_audit_allows_missing_but_rejects_mismatches(
    registry: SnapshotRegistry,
    tmp_path: Path,
) -> None:
    missing = await audit_indexes(registry, FakeUpstream({}))
    ensure_bindings(missing)

    snapshot = registry.snapshots[0]
    mismatched = await audit_indexes(
        registry,
        FakeUpstream({snapshot.index_name: tmp_path / "wrong"}),
    )
    with pytest.raises(PrebuildError, match="mismatched"):
        ensure_bindings(mismatched)
