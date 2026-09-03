"""Build and audit every versioned snapshot index before serving traffic."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vllm_kb_adapter.snapshots import Snapshot, SnapshotRegistry
from vllm_kb_adapter.upstream import MCPUpstream, UpstreamError, structured_content, tool_error_text

Progress = Callable[[str, Snapshot], None]


class PrebuildError(RuntimeError):
    """The prebuilt index set does not satisfy the snapshot registry."""


@dataclass(frozen=True, slots=True)
class PrebuildReport:
    built: tuple[Snapshot, ...]
    skipped: tuple[Snapshot, ...]


@dataclass(frozen=True, slots=True)
class IndexAudit:
    missing: tuple[Snapshot, ...]
    mismatched: tuple[Snapshot, ...]

    @property
    def ok(self) -> bool:
        return not self.missing and not self.mismatched


def ensure_indexes(audit: IndexAudit) -> None:
    """Reject an incomplete or incorrectly bound production index set.

    Args:
        audit: Complete snapshot-to-index comparison.

    Raises:
        PrebuildError: One or more snapshot indexes are missing or mismatched.
    """
    if audit.ok:
        return
    missing = ", ".join(snapshot.index_name for snapshot in audit.missing)
    mismatched = ", ".join(snapshot.index_name for snapshot in audit.mismatched)
    raise PrebuildError(
        f"prebuilt index audit failed; missing=[{missing}] mismatched=[{mismatched}]",
    )


async def indexed_projects(upstream: MCPUpstream) -> dict[str, Path]:
    """Read every indexed project and its bound source root.

    Args:
        upstream: gh-puller-mcp endpoint used for paginated project discovery.
    """
    projects: dict[str, Path] = {}
    offset = 0
    while True:
        result = await upstream.call_tool(
            "list_projects",
            {"limit": 100, "offset": offset},
        )
        if result.get("isError"):
            raise UpstreamError(tool_error_text(result))
        data = structured_content(result)
        if data is None or not isinstance(data.get("projects"), list):
            raise UpstreamError("list_projects returned no structured project list")
        page = data["projects"]
        for item in page:
            if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("root_path"), str):
                projects[item["name"]] = _resolved_path(item["root_path"])
        offset += len(page)
        if not data.get("has_more") or not page:
            return projects


async def audit_indexes(registry: SnapshotRegistry, upstream: MCPUpstream) -> IndexAudit:
    """Compare the complete snapshot registry with upstream index bindings.

    Args:
        registry: All production snapshots that must be prebuilt.
        upstream: gh-puller-mcp endpoint used for project discovery.
    """
    projects = await indexed_projects(upstream)
    missing = tuple(snapshot for snapshot in registry.snapshots if snapshot.index_name not in projects)
    mismatched = tuple(
        snapshot
        for snapshot in registry.snapshots
        if snapshot.index_name in projects and projects[snapshot.index_name] != snapshot.path
    )
    return IndexAudit(missing=missing, mismatched=mismatched)


async def prebuild_all(
    registry: SnapshotRegistry,
    upstream: MCPUpstream,
    *,
    mode: str = "full",
    refresh: bool = False,
    progress: Progress | None = None,
) -> PrebuildReport:
    """Build every missing versioned index in deterministic order.

    Args:
        registry: Complete immutable snapshot registry.
        upstream: gh-puller-mcp endpoint running the unrestricted tool profile.
        mode: Native index mode forwarded to ``index_repository``.
        refresh: Rebuild indexes already bound to the expected snapshot path.
        progress: Optional callback receiving ``build`` or ``skip`` events.
    """
    projects = await indexed_projects(upstream)
    built: list[Snapshot] = []
    skipped: list[Snapshot] = []
    for snapshot in registry.snapshots:
        bound_root = projects.get(snapshot.index_name)
        if bound_root is not None and bound_root != snapshot.path and not refresh:
            raise PrebuildError(
                f"index {snapshot.index_name} points to {bound_root}, expected {snapshot.path}",
            )
        if bound_root == snapshot.path and not refresh:
            skipped.append(snapshot)
            if progress is not None:
                progress("skip", snapshot)
            continue
        if progress is not None:
            progress("build", snapshot)
        result = await upstream.call_tool(
            "index_repository",
            {
                "repo_path": str(snapshot.path),
                "name": snapshot.index_name,
                "mode": mode,
            },
        )
        if result.get("isError"):
            raise PrebuildError(
                f"index {snapshot.index_name} failed: {tool_error_text(result)}",
            )
        built.append(snapshot)
    ensure_indexes(await audit_indexes(registry, upstream))
    return PrebuildReport(built=tuple(built), skipped=tuple(skipped))


def _resolved_path(value: str) -> Path:
    return Path(value).resolve()
