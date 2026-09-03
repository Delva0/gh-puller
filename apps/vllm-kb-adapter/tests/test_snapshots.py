"""Snapshot discovery and semantic-version resolution tests."""

from pathlib import Path

import pytest

from vllm_kb_adapter.snapshots import (
    VLLM_ASCEND_PROJECT,
    VLLM_PROJECT,
    RegistryError,
    SnapshotRegistry,
)


def test_resolve_exact_and_latest(registry: SnapshotRegistry) -> None:
    assert registry.resolve(VLLM_PROJECT).version == "0.23.0"
    assert registry.resolve(VLLM_PROJECT, "v0.9.2").version == "0.9.2"
    assert registry.resolve(VLLM_ASCEND_PROJECT).version == "0.25.1rc1"
    assert registry.resolve(VLLM_ASCEND_PROJECT, "0.23.0").path.name == "vllm-ascend-0.23.0"
    assert registry.resolve(VLLM_ASCEND_PROJECT, "v0.7.3.post1").version == "0.7.3.post1"


@pytest.mark.parametrize(
    ("project", "version", "message"),
    [
        ("unknown/repo", None, "unsupported project"),
        (VLLM_PROJECT, "banana", "invalid version"),
        (VLLM_PROJECT, "1.0.0", "available: 0.9.2, 0.23.0"),
    ],
)
def test_resolve_rejects_unknown_values(
    registry: SnapshotRegistry,
    project: str,
    version: str | None,
    message: str,
) -> None:
    with pytest.raises(RegistryError, match=message):
        registry.resolve(project, version)


def test_discover_rejects_missing_source(tmp_path: Path) -> None:
    vllm_root = tmp_path / "vllm"
    ascend_root = tmp_path / "ascend"
    (vllm_root / "0.23.0").mkdir(parents=True)
    (ascend_root / "v0.25.1rc1" / "vllm-ascend-0.25.1rc1").mkdir(parents=True)

    with pytest.raises(RegistryError, match="snapshot source does not exist"):
        SnapshotRegistry.discover(vllm_root, ascend_root)
