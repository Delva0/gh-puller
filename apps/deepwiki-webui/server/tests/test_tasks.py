"""Test the WebUI wiki-task runtime without model calls.

The suite covers task joining, cache precedence, resume recovery, target isolation,
page concurrency, placeholder failures, and the index-to-cache workflow. Generation
functions are patched or satisfied by prewritten cache files, and ``tests/conftest.py``
routes artifacts to a temporary root. Engine-level contracts live in the root
``tests/deepwiki`` suite.

Patch facade-bound functions through ``tasks``, engine-owned generation through
``deepwiki.wiki``, and indexing through ``generators``. An empty target selects the
engine's built-in CC generator.
"""

import asyncio
import dataclasses
import json
import os
from pathlib import Path

import pytest
from gh_puller import deepwiki
from gh_puller.deepwiki import (
    WikiPage,
    WikiStructureModel,
    delete_resume_state,
    delete_wiki_cache,
    save_wiki_cache,
    write_resume_state,
)
from gh_puller.deepwiki.utils import generator_digest
from gh_puller.deepwiki.wiki import (
    _generator_cache_page_path,
    _generator_cache_structure_path,
    resume_state_path,
    wiki_cache_dir,
)
from gh_puller.utils import Repo, TaskStatus

import generators
import tasks

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_page(page_id: str) -> WikiPage:
    return WikiPage(
        id=page_id,
        title=f"Page {page_id}",
        content="",
        filePaths=["src/main.py"],
        importance="medium",
        relatedPages=[],
    )


def _make_structure(page_ids: list[str]) -> WikiStructureModel:
    return WikiStructureModel(
        id="wiki", title="T", description="", pages=[_make_page(p) for p in page_ids],
    )


def _make_request(owner: str, repo: str) -> dict:
    return {
        "repo_url": "/tmp/gh-puller-test-repo", "type": "local", "owner": owner,
        "repo": repo, "language": "en", "target": {}, "token": None,
        "comprehensive": True,
        "excluded_dirs": [], "excluded_files": [], "included_dirs": [], "included_files": [],
    }


def _digest_of(choice: "dict | None") -> str:
    """Return the stable generator digest shared with production code."""
    return generator_digest((choice or {}).get("generator"), (choice or {}).get("generator_config"))


def _proj(request) -> str:
    """Return the repository key represented by a request mapping."""
    return deepwiki.repo_key_of(request["type"], request["owner"], request["repo"])


def _prepared() -> tasks.PreparedRepo:
    return tasks.PreparedRepo(Repo("/tmp/gh-puller-test-repo", "local"), "main")


_STRUCT_XML = """<wiki_structure>
<title>T</title>
<page id="p1"><title>A</title><file_path>src/a.py</file_path></page>
<page id="p2"><title>B</title><file_path>src/b.py</file_path></page>
<page id="p3"><title>C</title><file_path>src/c.py</file_path></page>
</wiki_structure>"""


# ---------------------------------------------------------------------------
# Offline workflow orchestration
# ---------------------------------------------------------------------------


def test_pending_pages_subtracts_done():
    """Remove completed pages in structure order and ignore unknown completed IDs."""
    structure = _make_structure(["p1", "p2", "p3"])
    assert [p.id for p in tasks._pending_pages(structure, {"p1": _make_page("p1")})] == ["p2", "p3"]
    assert tasks._pending_pages(structure, {p.id: _make_page(p.id) for p in structure.pages}) == []
    assert [p.id for p in tasks._pending_pages(structure, {"pX": _make_page("pX")})] == ["p1", "p2", "p3"]


@pytest.mark.asyncio
async def test_generate_pages_concurrency_bounded(monkeypatch):
    """Read the page concurrency limit at call time and cap in-flight work at four."""
    active = 0
    max_active = 0

    async def fake_generate(task, page, prepared, gc=None):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return dataclasses.replace(page, content=f"content-{page.id}")

    monkeypatch.setattr(tasks, "_generate_page_with_retry", fake_generate)
    monkeypatch.setattr(tasks, "_WIKI_PAGE_CONCURRENCY", 4)
    task = tasks.WikiTask(request=_make_request("conc-io", "demo"))
    task.wiki_structure = _make_structure([f"p{i}" for i in range(8)])
    pages = await tasks._generate_pages(task, _prepared(), None)
    assert max_active == 4
    assert len(pages) == 8 and len(task.generated_pages) == 8
    assert task.pages_done == 8
    assert task.current_page_ids == []
    await delete_resume_state("conc-io", "demo", "local", "en")


# ---------------------------------------------------------------------------
# Scheduling, cache precedence, resume, and target isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_resume_restores_task(monkeypatch):
    """Restore structure and progress from a persisted snapshot on resubmission.

    Persisted public target fields take precedence, while credentials come from the
    current submission and never enter the snapshot.
    """
    structure = _make_structure(["p1", "p2", "p3"])
    state = {
        "version": 1,
        "request": {**_make_request("resume-io", "demo"), "target": {
            "generator": "llm", "generator_config": {"provider": "openai", "model": "m1"}}},
        "status": TaskStatus.GENERATING,
        "wiki_structure": dataclasses.asdict(structure),
        "generated_pages": {"p1": dataclasses.asdict(_make_page("p1")), "p2": dataclasses.asdict(_make_page("p2"))},
        "default_branch": "main",
        "submitted_at": 9876543210,
        "error": None,
    }
    req_target = state["request"]["target"]
    digest = generator_digest(req_target.get("generator"), req_target.get("generator_config"))
    assert await write_resume_state("resume-io", "demo", "local", "en", state, digest=digest) is True

    async def fake_generate(task):
        task.status = TaskStatus.COMPLETED

    monkeypatch.setattr(tasks, "generate_repo_wiki", fake_generate)
    monkeypatch.setattr(tasks, "_WIKI_TASK_TTL_SECONDS", 0.2)

    key = f"local_resume-io_demo@{digest}"
    fresh = _make_request("resume-io", "demo")
    fresh["comprehensive"] = False  # The persisted request must take precedence.
    fresh["target"] = {"generator": "llm", "generator_config": {
        "provider": "openai", "model": "m1",  # Public fields match the snapshot digest.
        "api_key": "sk-live-1", "base_url": "https://custom/v1"}}
    try:
        res = await tasks.registry.submit(tasks.WikiTask.from_wiki_request(fresh))
        assert res.created is True
        assert res.resumed is True
        assert res.status == TaskStatus.GENERATING
        task = tasks.registry.get(key)
        assert task is not None
        assert [p.id for p in task.wiki_structure.pages] == ["p1", "p2", "p3"]
        assert task.pages_done == 2
        assert task.submitted_at == 9876543210
        assert task.request["comprehensive"] is True
        # Resume combines persisted public fields with current object-target credentials.
        assert task.request["target"]["generator_config"]["api_key"] == "sk-live-1"
        assert task.request["target"]["generator_config"]["base_url"] == "https://custom/v1"
        assert task.request["target"]["generator_config"]["provider"] == "openai"
        await task.task
        assert task.status == TaskStatus.COMPLETED
    finally:
        await tasks.registry.remove(key)
        await delete_resume_state("resume-io", "demo", "local", "en", digest=digest)
        await asyncio.sleep(0.25)  # Let TTL cleanup finish without pending-task warnings.


@pytest.mark.asyncio
async def test_resume_isolated_by_target(monkeypatch):
    """Isolate resume snapshots by the public target digest.

    Changing generators creates a fresh task instead of reviving or joining another
    target's snapshot.
    """
    state = {
        "version": 1,
        "request": _make_request("gen-switch", "demo"),
        "status": TaskStatus.GENERATING,
        "wiki_structure": dataclasses.asdict(_make_structure(["p1", "p2"])),
        "generated_pages": {"p1": dataclasses.asdict(_make_page("p1"))},
        "default_branch": "main",
        "submitted_at": 424242,
        "error": None,
    }
    digest_cc = generator_digest(None, None)  # An empty target selects the CC default.
    assert await write_resume_state("gen-switch", "demo", "local", "en", state, digest=digest_cc) is True

    async def fake_generate(task):
        task.status = TaskStatus.COMPLETED

    monkeypatch.setattr(tasks, "generate_repo_wiki", fake_generate)
    monkeypatch.setattr(tasks, "_WIKI_TASK_TTL_SECONDS", 0.2)
    key_cc = f"local_gen-switch_demo@{digest_cc}"
    try:
        # Resubmitting the same default target resumes its snapshot.
        res_ok = await tasks.registry.submit(
            tasks.WikiTask.from_wiki_request(_make_request("gen-switch", "demo")),
        )
        assert res_ok.resumed is True
        task_ok = tasks.registry.get(key_cc)
        await task_ok.task
        assert task_ok.status == TaskStatus.COMPLETED
        await tasks.registry.remove(key_cc)

        # An explicit LLM target starts fresh while the CC snapshot remains isolated.
        res = await tasks.registry.submit(
            tasks.WikiTask.from_wiki_request(
                {**_make_request("gen-switch", "demo"), "target": {"generator": "llm"}},
            ),
        )
        assert res.created is True
        assert res.resumed is False
        cc_state_path = resume_state_path("gen-switch", "demo", "local", "en", digest=digest_cc)
        assert os.path.exists(cc_state_path)  # noqa: ASYNC240 - Tiny synchronous test I/O.
        key_llm = "local_gen-switch_demo@" + generator_digest(
            "llm", None)
        # A target digest namespaces each repository task slot.
        task = tasks.registry.get(key_llm)
        assert task.key != key_cc
        await task.task
        assert task.status == TaskStatus.COMPLETED
    finally:
        await tasks.registry.remove(key_cc)
        await tasks.registry.remove("local_gen-switch_demo@" + generator_digest(
            "llm", None))
        await delete_resume_state("gen-switch", "demo", "local", "en", digest=digest_cc)
        await asyncio.sleep(0.25)  # Let TTL cleanup finish without pending-task warnings.


@pytest.mark.asyncio
async def test_cache_hit_respects_target(monkeypatch, tmp_path):
    """Match completed caches against the complete public target identity.

    A generator change starts a new cache generation without invalidating the existing
    artifact for the other target.
    """
    cfg = tmp_path / "settings.json"
    cfg.write_text("{}", encoding="utf-8")
    cc_target = {"generator": "cc", "generator_config": {"config_path": str(cfg)}}
    cache = {
        "wiki_structure": dataclasses.asdict(_make_structure(["p1"])),
        "generated_pages": {"p1": dataclasses.asdict(_make_page("p1"))},
        "repo_url": "/tmp/gh-puller-test-repo",
        "repo": {
            "owner": "gen-cache", "repo": "demo", "type": "local",
            "token": None, "localPath": None, "repoUrl": "/tmp/gh-puller-test-repo",
        },
        "generator": "cc",
        "generator_config": {"config_path": str(cfg)},
    }
    digest_cc = generator_digest((cc_target or {}).get("generator"), (cc_target or {}).get("generator_config"))
    assert await save_wiki_cache("gen-cache", "demo", "local", "en", cache, digest=digest_cc) is True

    calls = []

    async def fake_run(task):
        calls.append(task.key)
        task.status = TaskStatus.COMPLETED

    monkeypatch.setattr(tasks, "generate_repo_wiki", fake_run)
    monkeypatch.setattr(tasks, "_WIKI_TASK_TTL_SECONDS", 0.2)
    key_cc = f"local_gen-cache_demo@{digest_cc}"
    try:
        # The same configuration path identifies a cache hit without execution.
        res_ok = await tasks.registry.submit(
            tasks.WikiTask.from_wiki_request(
                {**_make_request("gen-cache", "demo"), "target": cc_target},
            ),
        )
        assert res_ok.from_cache is True
        assert res_ok.status == TaskStatus.COMPLETED
        assert calls == []

        # The LLM target misses and regenerates without reusing or deleting the CC cache.
        res = await tasks.registry.submit(
            tasks.WikiTask.from_wiki_request(
                {**_make_request("gen-cache", "demo"),
                 "target": {"generator": "llm"}},
            ),
        )
        assert res.from_cache is False
        assert res.created is True
        # The LLM task occupies its own digest-scoped registry slot.
        assert tasks.registry.get(key_cc) is None
        task_llm = tasks.registry.get("local_gen-cache_demo@" + generator_digest(
            "llm", None))
        assert task_llm is not None
        await task_llm.task
        assert task_llm.status == TaskStatus.COMPLETED
        assert calls == [task_llm.key]
    finally:
        await tasks.registry.remove(key_cc)
        await tasks.registry.remove("local_gen-cache_demo@" + generator_digest(
            "llm", None))
        await delete_wiki_cache("gen-cache", "demo", "local", "en", digest=digest_cc)
        await asyncio.sleep(0.25)  # Let TTL cleanup finish without pending-task warnings.


# ---------------------------------------------------------------------------
# Offline generator pipeline with prewritten caches and patched execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_determine_structure_single_pipeline(monkeypatch):
    """Use one facade-bound structure function and propagate failures to the task."""
    calls = []

    async def fake_determine(**kwargs):
        calls.append("determine")
        return _make_structure(["p1"])

    monkeypatch.setattr(tasks, "determine_structure", fake_determine)
    request = _make_request("dispatch-io", "demo")
    task = tasks.WikiTask(request=request)

    await tasks._determine_structure(task, _prepared(), None)
    assert calls == ["determine"]
    assert task.default_branch == "main"  # Structure resolution records the branch.


@pytest.mark.asyncio
async def test_determine_structure_skips_when_file_exists(monkeypatch):
    """Parse an existing structure file during resume without starting a generator."""
    request = _make_request("cc-struct", "demo")
    struct_path = _generator_cache_structure_path(
        _proj(request), request["target"].get("generator"), request["target"].get("generator_config"),
    )
    struct_path.parent.mkdir(parents=True, exist_ok=True)
    struct_path.write_text(_STRUCT_XML, encoding="utf-8")

    async def boom(*args, **kwargs):
        raise AssertionError("generator must not be called")

    monkeypatch.setattr(deepwiki.wiki, "_produce_file", boom)
    repo = Repo("/tmp/gh-puller-test-repo", "local")
    s = await deepwiki.determine_structure(
        generator=request["target"].get("generator"), generator_config=request["target"].get("generator_config"),
        repo=repo, owner=request["owner"], repo_name=request["repo"],
        comprehensive=request["comprehensive"],
        language=request["language"], run_id=_proj(request),
    )
    assert [p.id for p in s.pages] == ["p1", "p2", "p3"]


@pytest.mark.asyncio
async def test_registry_shutdown_cancels_owned_tasks():
    started = asyncio.Event()

    class BlockingRegistry(tasks.TaskRegistry):
        async def run(self, task):
            started.set()
            await asyncio.Event().wait()

    registry = BlockingRegistry()
    task = tasks.WikiTask(request=_make_request("shutdown", "demo"))
    await registry.submit(task)
    await started.wait()
    await registry.shutdown()
    assert task.task is not None and task.task.cancelled()


@pytest.mark.asyncio
async def test_determine_structure_calls_generator_no_inline(tmp_path, monkeypatch):
    """Generate and read the structure file without embedding repository contents."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("SECRET_CODE_BODY", encoding="utf-8")
    (tmp_path / "README.md").write_text("SECRET_README_BODY", encoding="utf-8")
    request = {
        "repo_url": str(tmp_path), "type": "local", "owner": "local",
        "repo": "demo", "language": "en", "target": {}, "token": None,
        "comprehensive": True,
    }
    captured = {}

    async def fake_produce(adapter, prompt, out_path, label=None, run_id=None):
        captured["prompt"] = prompt
        captured["run_id"] = run_id
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(_STRUCT_XML, encoding="utf-8")  # noqa: ASYNC240 - Tiny synchronous stub I/O.
        return _STRUCT_XML

    monkeypatch.setattr(deepwiki.wiki, "_produce_file", fake_produce)
    repo = Repo(str(tmp_path), "local")
    s = await deepwiki.determine_structure(
        generator=request["target"].get("generator"), generator_config=request["target"].get("generator_config"),
        repo=repo, owner=request["owner"], repo_name=request["repo"],
        comprehensive=request["comprehensive"],
        language=request["language"], run_id=_proj(request),
    )
    assert [p.id for p in s.pages] == ["p1", "p2", "p3"]
    assert "<file_tree>" not in captured["prompt"]  # Prompts do not inline the tree or README.
    assert "SECRET_CODE_BODY" not in captured["prompt"]
    assert "SECRET_README_BODY" not in captured["prompt"]
    assert captured["run_id"] == _proj(request)  # The task key groups the generator session.


@pytest.mark.asyncio
async def test_page_skips_when_file_exists(monkeypatch):
    """Treat an existing page file as authoritative during resume."""
    request = _make_request("cc-page", "demo")
    out = _generator_cache_page_path(
        _proj(request), "p1", request["target"].get("generator"), request["target"].get("generator_config"),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("## PA-REAL\n\nbody\n", encoding="utf-8")

    async def boom(*args, **kwargs):
        raise AssertionError("generator must not be called")

    monkeypatch.setattr(deepwiki.wiki, "_produce_file", boom)
    task = tasks.WikiTask(request=request)
    got = await tasks._generate_page(task, _make_page("p1"), _prepared(), None)
    assert "PA-REAL" in got.content
    assert got.id == "p1"


@pytest.mark.asyncio
async def test_page_calls_generator_and_reads_file(monkeypatch):
    """Read a generated page from disk without embedding file contents in its prompt."""
    request = _make_request("cc-page2", "demo")
    captured = {}

    async def fake_produce(adapter, prompt, out_path, label=None, run_id=None):
        captured["prompt"] = prompt
        captured["run_id"] = run_id
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text("## PB-REAL\n\ncontent\n", encoding="utf-8")  # noqa: ASYNC240 - Tiny synchronous stub I/O.
        return "## PB-REAL\n\ncontent\n"

    monkeypatch.setattr(deepwiki.wiki, "_produce_file", fake_produce)
    task = tasks.WikiTask(request=request)
    got = await tasks._generate_page(task, _make_page("p2"), _prepared(), None)
    assert "PB-REAL" in got.content
    assert "- [src/main.py](src/main.py)" in captured["prompt"]
    assert "DELIVERABLE" in captured["prompt"]
    assert "SECRET" not in captured["prompt"]


@pytest.mark.asyncio
async def test_generate_page_with_retry_exhausted_writes_placeholder(tmp_path, monkeypatch):
    """Persist an unformatted error placeholder after retries are exhausted."""
    request = _make_request("retry-io", "demo")

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(tasks, "generate_page", boom)
    task = tasks.WikiTask(request=request)
    monkeypatch.setattr(tasks, "_WIKI_PAGE_RETRIES", 1)
    got = await tasks._generate_page_with_retry(task, _make_page("p1"), _prepared(), None)
    assert got.content.startswith("Error generating content:")
    out = _generator_cache_page_path(
        _proj(request), "p1", request["target"].get("generator"), request["target"].get("generator_config"),
    )
    assert out.exists()
    assert out.read_text(encoding="utf-8").startswith("Error generating content:")


@pytest.mark.asyncio
async def test_generate_page_with_retry_does_not_restart_after_cancellation(monkeypatch):
    """A wrapped cancellation must not open another generator session."""
    calls = 0

    async def wrapped_cancel(*args, **kwargs):
        nonlocal calls
        calls += 1
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise RuntimeError("wrapped cancellation") from None

    monkeypatch.setattr(tasks, "_generate_page", wrapped_cancel)
    task = tasks.WikiTask(request=_make_request("cancel-retry", "demo"))
    runner = asyncio.create_task(
        tasks._generate_page_with_retry(task, _make_page("p1"), _prepared(), None),
    )
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert calls == 1


@pytest.mark.asyncio
async def test_generate_repo_wiki_assemble_and_resume(tmp_path, monkeypatch):
    """Complete the offline workflow entirely from prewritten structure and page files.

    Cached JSON mirrors the files, resume state is removed, and page progress is
    hydrated from disk.
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    request = {
        "repo_url": str(repo_dir), "type": "local", "owner": "local",
        "repo": "demo", "language": "en", "target": {}, "token": None,
        "comprehensive": True,
    }
    # A project-named database marks the fake index as ready.
    fake_repo = Repo(str(repo_dir), "local")
    cdir = generators._cbm_cache_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / f"{generators.project_name(fake_repo)}.db").touch()
    # Runtime configuration participates consistently in every prewritten cache path.
    gc = generators.runtime_config(
        request["target"].get("generator"), request["target"].get("generator_config"), repo=fake_repo,
    )
    struct_path = _generator_cache_structure_path(
        _proj(request), request["target"].get("generator"), gc,
    )
    struct_path.parent.mkdir(parents=True, exist_ok=True)
    struct_path.write_text(_STRUCT_XML, encoding="utf-8")
    for pid in ("p1", "p2", "p3"):
        page_path = _generator_cache_page_path(
            _proj(request), pid, request["target"].get("generator"), gc,
        )
        page_path.write_text(f"## {pid}-REAL\n\nbody\n", encoding="utf-8")

    async def boom(*args, **kwargs):
        raise AssertionError("generator must not be called")

    monkeypatch.setattr(deepwiki.wiki, "_produce_file", boom)
    task = tasks.WikiTask(request=request)
    await tasks.generate_repo_wiki(task)
    assert task.status == TaskStatus.COMPLETED
    digest = generator_digest(request["target"].get("generator"), request["target"].get("generator_config"))
    cache_path = Path(wiki_cache_dir()) / "local_local_demo" / f"cache_local_local_demo_en_{digest}.json"
    assert cache_path.exists()
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert set(data["generated_pages"]) == {"p1", "p2", "p3"}
    assert data["generator"] == "cc"  # Completed caches store only the public target.
    assert data["generator_config"] == {}  # Public generator configuration defines identity.
    assert "api_key" not in json.dumps(data) and "base_url" not in json.dumps(data)
    for pid in ("p1", "p2", "p3"):
        assert f"{pid}-REAL" in data["generated_pages"][pid]["content"]
    resume_path = Path(resume_state_path("local", "demo", "local", "en", digest=digest))
    assert not resume_path.exists()  # noqa: ASYNC240 - Tiny synchronous test I/O.
    assert task.pages_done == 3
