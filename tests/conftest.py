"""Isolate import-time runtime roots under one temporary test-run directory.

The environment module snapshots process variables during import, so these roots
must be installed before test collection. Pytest teardown removes the complete run.
"""

import contextlib
import os
import sys
import tempfile
from pathlib import Path

_TEMP_DIRS = contextlib.ExitStack()
_TEMP_BASE = Path(tempfile.gettempdir()) / "gh-puller" / "pytest"
_TEMP_BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
_TEMP_ROOT = Path(
    _TEMP_DIRS.enter_context(tempfile.TemporaryDirectory(prefix="root-", dir=_TEMP_BASE)),
)


def _temp_dir(name: str) -> str:
    path = _TEMP_ROOT / name
    path.mkdir()
    return str(path)


os.environ["DEEPWIKI_ROOT"] = _temp_dir("deepwiki")
os.environ["AGENT_MONITOR_DIR"] = _temp_dir("agent-monitor")
sys.modules.pop("gh_puller.envs", None)
# A from-import may retain the module on its package after sys.modules eviction.
with contextlib.suppress(AttributeError, KeyError):
    delattr(sys.modules["gh_puller"], "envs")


def pytest_unconfigure() -> None:
    _TEMP_DIRS.close()
