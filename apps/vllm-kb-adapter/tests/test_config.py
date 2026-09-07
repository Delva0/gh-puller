"""Adapter process setting tests."""

from pathlib import Path

from vllm_kb_adapter.config import Settings


def test_snapshot_roots_default_to_process_home(monkeypatch) -> None:
    """Keep default snapshot roots independent of the service account name."""
    snapshot_home = Path("/home/xxx")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: snapshot_home))
    monkeypatch.delenv("VLLM_KB_ADAPTER_VLLM_ROOT", raising=False)
    monkeypatch.delenv("VLLM_KB_ADAPTER_VLLM_ASCEND_ROOT", raising=False)

    settings = Settings.from_env()

    assert settings.vllm_root == snapshot_home / "snapshots-vllm"
    assert settings.vllm_ascend_root == snapshot_home / "snapshots-vllm-ascend"


def test_snapshot_roots_accept_environment_overrides(monkeypatch) -> None:
    """Let deployments place either repository outside the account home."""
    monkeypatch.setenv("VLLM_KB_ADAPTER_VLLM_ROOT", "/srv/snapshots-vllm")
    monkeypatch.setenv(
        "VLLM_KB_ADAPTER_VLLM_ASCEND_ROOT",
        "/data/snapshots-vllm-ascend",
    )

    settings = Settings.from_env()

    assert settings.vllm_root == Path("/srv/snapshots-vllm")
    assert settings.vllm_ascend_root == Path("/data/snapshots-vllm-ascend")
