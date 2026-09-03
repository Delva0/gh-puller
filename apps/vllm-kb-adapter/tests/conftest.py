"""Shared immutable snapshot fixtures for adapter tests."""

from pathlib import Path

import pytest

from vllm_kb_adapter.snapshots import SnapshotRegistry


def _source(root: Path, version_dir: str, source_name: str) -> None:
    (root / version_dir / source_name).mkdir(parents=True)


@pytest.fixture
def registry(tmp_path: Path) -> SnapshotRegistry:
    vllm_root = tmp_path / "snapshots-vllm"
    ascend_root = tmp_path / "snapshots-vllm-ascend"
    _source(vllm_root, "0.9.2", "vllm-0.9.2")
    _source(vllm_root, "0.23.0", "vllm-0.23.0")
    _source(ascend_root, "v0.7.3.post1", "vllm-ascend-0.7.3.post1")
    _source(ascend_root, "v0.23.0", "vllm-ascend-0.23.0")
    _source(ascend_root, "v0.25.1rc1", "vllm-ascend-0.25.1rc1")
    return SnapshotRegistry.discover(vllm_root, ascend_root)
