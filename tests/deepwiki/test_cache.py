"""Test persisted DeepWiki cache and resume-state contracts."""

import dataclasses
import os
from pathlib import Path

import pytest

from gh_puller import deepwiki
from gh_puller.deepwiki import (
    delete_resume_state,
    delete_wiki_cache,
    list_wiki_cache,
    read_resume_state,
    save_wiki_cache,
    write_resume_state,
)
from gh_puller.deepwiki.utils import generator_digest
from gh_puller.deepwiki.wiki import resume_state_path
from gh_puller.utils import TaskStatus
from tests.deepwiki._support import _digest_of, _make_page, _make_request, _make_structure


@pytest.mark.asyncio
async def test_wiki_task_state_roundtrip_atomic():
    """Round-trip resume state atomically without exposing it as finished cache."""
    request = _make_request("state-io", "demo")
    state = {
        "version": 1,
        "request": request,
        "status": TaskStatus.GENERATING,
        "wiki_structure": dataclasses.asdict(_make_structure(["p1", "p2"])),
        "generated_pages": {"p1": dataclasses.asdict(_make_page("p1"))},
        "default_branch": "main",
        "submitted_at": 1234567890,
        "error": None,
    }
    digest = generator_digest(request["target"])
    assert await write_resume_state("state-io", "demo", "local", "en", state, digest=digest) is True
    path = resume_state_path("state-io", "demo", "local", "en", digest=digest)
    assert os.path.exists(path)  # noqa: ASYNC240 - A local stat is negligible in this test.
    assert not os.path.exists(f"{path}.tmp")  # noqa: ASYNC240 - A local stat is negligible in this test.
    loaded = await read_resume_state("state-io", "demo", "local", "en", digest=digest)
    assert loaded is not None
    assert loaded == state  # String-valued enums preserve equality through JSON.
    # Resume files must not appear in the finished-wiki cache listing.
    assert await list_wiki_cache() == []
    assert await delete_resume_state("state-io", "demo", "local", "en", digest=digest) is True
    assert not os.path.exists(path)  # noqa: ASYNC240 - A local stat is negligible in this test.


@pytest.mark.asyncio
async def test_cache_new_layout_by_project_dir():
    """Store finished caches under a repository directory with reversible names.

    Sibling repository, graph, and generator-cache artifacts must not enter the
    finished-wiki listing.
    """
    root = Path(deepwiki.envs.DEEPWIKI_ROOT)
    d1 = _digest_of({"generator": "cc"})
    d2 = _digest_of({"generator": "dsh"})
    record = {"wiki_structure": dataclasses.asdict(_make_structure(["p1"])), "generated_pages": {}}
    assert await save_wiki_cache("layout-io", "demo", "local", "en", record, digest=d1) is True
    assert await save_wiki_cache("layout-io", "demo", "local", "zh", record, digest=d2) is True
    proj_dir = root / "wiki" / "local_layout-io_demo"
    assert (proj_dir / f"cache_local_layout-io_demo_en_{d1}.json").exists()
    assert (proj_dir / f"cache_local_layout-io_demo_zh_{d2}.json").exists()
    # Seed sibling artifacts to prove the listing remains scoped to finished wiki JSON.
    (root / "repos" / "cache_local_layout-io_demo_en_zzzzzzzz.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "repos" / "cache_local_layout-io_demo_en_zzzzzzzz.json").write_text("{}")
    (root / "graphify").mkdir(exist_ok=True)
    (proj_dir / "generator_cache").mkdir(parents=True, exist_ok=True)
    (proj_dir / "generator_cache" / "local_layout-io_demo_00000000-structure.md").write_text("x")
    entries = await list_wiki_cache()
    assert len(entries) == 2, [e["id"] for e in entries]
    by_lang = {(e["owner"], e["repo"], e["repo_type"], e["language"]): e for e in entries}
    assert by_lang[("layout-io", "demo", "local", "en")]["digest"] == d1
    assert by_lang[("layout-io", "demo", "local", "zh")]["digest"] == d2


@pytest.mark.asyncio
async def test_cache_delete_removes_whole_project():
    """Delete the complete project cache and report a repeated deletion as absent."""
    request = _make_request("layout-io", "demo")
    d1 = generator_digest(request["target"])
    record = {"wiki_structure": dataclasses.asdict(_make_structure(["p1"])), "generated_pages": {}}
    assert await save_wiki_cache("layout-io", "demo", "local", "en", record, digest=d1) is True
    proj_dir = Path(deepwiki.envs.DEEPWIKI_ROOT) / "wiki" / "local_layout-io_demo"
    (proj_dir / "generator_cache").mkdir(parents=True, exist_ok=True)
    (proj_dir / "generator_cache" / "local_layout-io_demo_x-structure.md").write_text("x")
    assert await delete_wiki_cache("layout-io", "demo", "local", "en") is True
    assert not proj_dir.exists()
    assert await delete_wiki_cache("layout-io", "demo", "local", "en") is False
