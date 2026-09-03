"""Discover immutable source snapshots and resolve logical project versions.

The two production snapshot roots are independent registries. Pairing between
vLLM Ascend and vLLM releases is deliberately outside this adapter contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    from pathlib import Path

VLLM_PROJECT = "vllm-project/vllm"
VLLM_ASCEND_PROJECT = "vllm-project/vllm-ascend"


class RegistryError(ValueError):
    """The snapshot layout or a requested project/version is invalid."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    logical_project: str
    repo: str
    version: str
    parsed_version: Version
    path: Path
    index_name: str


class SnapshotRegistry:
    """Immutable lookup from the vllm-kb project contract to source snapshots."""

    def __init__(self, snapshots: list[Snapshot]) -> None:
        """Validate and index a discovered snapshot collection.

        Args:
            snapshots: Snapshots keyed by logical project and normalized version.
        """
        self._snapshots = tuple(sorted(snapshots, key=lambda item: (item.repo, item.parsed_version)))
        self._by_project: dict[str, dict[Version, Snapshot]] = {}
        for snapshot in self._snapshots:
            versions = self._by_project.setdefault(snapshot.logical_project, {})
            if snapshot.parsed_version in versions:
                raise RegistryError(
                    f"duplicate normalized version {snapshot.version} for {snapshot.logical_project}",
                )
            versions[snapshot.parsed_version] = snapshot

    @classmethod
    def discover(cls, vllm_root: Path, vllm_ascend_root: Path) -> SnapshotRegistry:
        """Discover both production snapshot layouts.

        Args:
            vllm_root: Root containing ``<version>/vllm-<version>`` directories.
            vllm_ascend_root: Root containing
                ``v<version>/vllm-ascend-<version>`` directories.
        """
        snapshots = [
            *_discover_root(vllm_root, VLLM_PROJECT, "vllm", "vllm"),
            *_discover_root(
                vllm_ascend_root,
                VLLM_ASCEND_PROJECT,
                "vllm-ascend",
                "vllm-ascend",
            ),
        ]
        return cls(snapshots)

    @property
    def snapshots(self) -> tuple[Snapshot, ...]:
        return self._snapshots

    def resolve(self, project: str, version: str | None = None) -> Snapshot:
        """Resolve an exact version, or the highest available version when omitted.

        Args:
            project: Logical project identifier from the vllm-kb checklist.
            version: Exact PEP 440 version, with an optional leading ``v``.
        """
        versions = self._by_project.get(project)
        if versions is None:
            raise RegistryError(f"unsupported project: {project}")
        if version is None or not version.strip():
            return versions[max(versions)]
        try:
            parsed = Version(version)
        except InvalidVersion as exc:
            raise RegistryError(f"invalid version for {project}: {version}") from exc
        snapshot = versions.get(parsed)
        if snapshot is None:
            ordered = sorted(versions.values(), key=lambda item: item.parsed_version)
            available = ", ".join(item.version for item in ordered)
            raise RegistryError(
                f"version {version} is unavailable for {project}; available: {available}",
            )
        return snapshot


def _discover_root(
    root: Path,
    logical_project: str,
    repo: str,
    source_prefix: str,
) -> list[Snapshot]:
    if not root.is_dir():
        raise RegistryError(f"snapshot root does not exist: {root}")
    snapshots: list[Snapshot] = []
    for version_dir in sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name):
        try:
            parsed = Version(version_dir.name)
        except InvalidVersion as exc:
            raise RegistryError(f"invalid snapshot version directory: {version_dir}") from exc
        source_version = version_dir.name.removeprefix("v")
        source = version_dir / f"{source_prefix}-{source_version}"
        if not source.is_dir():
            raise RegistryError(f"snapshot source does not exist: {source}")
        version = str(parsed)
        snapshots.append(
            Snapshot(
                logical_project=logical_project,
                repo=repo,
                version=version,
                parsed_version=parsed,
                path=source.resolve(),
                index_name=f"vllm-kb-{repo}-{version}",
            ),
        )
    if not snapshots:
        raise RegistryError(f"snapshot root is empty: {root}")
    return snapshots
