"""Schedule resumable wiki generation for the DeepWiki WebUI.

This application layer owns task state, deduplication, concurrency, progress snapshots,
and pipeline orchestration. The engine owns generated content and cache primitives;
``generators`` owns indexing and MCP assembly. Importing this module creates the cache
root, process locks, and the registry singleton.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import time
from dataclasses import dataclass
from typing import Any

from gh_puller import deepwiki
from gh_puller.deepwiki import (
    WikiPage,
    WikiStructureModel,
    delete_resume_state,
    determine_structure,
    generate_page,
    hydrate_pages,
    needs_structure_regenerate,
    read_resume_state,
    read_wiki_cache,
    save_generated_wiki,
    write_error_page,
    write_resume_state,
)
from gh_puller.deepwiki.utils import (
    cache_generator_matches,
    cache_identity,
    generator_digest,
    log,
    resolve_generator,
)
from gh_puller.deepwiki.wiki import wiki_cache_dir
from gh_puller.utils import Repo, TaskStatus, detect_default_branch
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from generators import ensure_index, index_ready, runtime_config

# --- Runtime state ---

_MAX_CONCURRENT_WIKI_TASKS = int(os.environ.get(
    "DEEPWIKI_MAX_CONCURRENT_WIKI_TASKS", max(1, (os.cpu_count() or 2) // 2)))
_WIKI_PAGE_CONCURRENCY = max(1, int(os.environ.get("DEEPWIKI_WIKI_PAGE_CONCURRENCY", "4")))
_WIKI_PAGE_RETRIES = max(0, int(os.environ.get("DEEPWIKI_WIKI_PAGE_RETRIES", "2")))
_WIKI_TASK_TTL_SECONDS = int(os.environ.get("DEEPWIKI_WIKI_TASK_TTL_SECONDS", "300"))

# Serialize snapshots produced by concurrent page workers.
_state_write_lock = asyncio.Lock()
# Retain delayed-removal tasks until their completion callbacks release them.
_background_tasks: set[asyncio.Task] = set()
os.makedirs(wiki_cache_dir(), exist_ok=True)


# --- Task model ---


class WikiTask(BaseModel):
    """Hold in-process state and progress for one repository generation."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: TaskStatus = TaskStatus.PENDING
    error: str | None = None
    submitted_at: int = Field(default_factory=lambda: int(time.time() * 1000))
    task: asyncio.Task | None = Field(default=None, repr=False)

    request: dict[str, Any]
    pages_done: int = 0
    current_page_ids: list[str] = Field(default_factory=list)
    generated_pages: dict[str, WikiPage] = Field(default_factory=dict)
    wiki_structure: WikiStructureModel | None = None
    default_branch: str = "main"

    @computed_field
    @property
    def pages_total(self) -> int:
        if self.wiki_structure is not None:
            return len(self.wiki_structure.pages)
        return 0

    @classmethod
    def from_wiki_request(cls, request: dict) -> WikiTask:
        return cls(request=request)

    @property
    def repo_key(self) -> str:
        r = self.request
        return deepwiki.repo_key_of(r["type"], r["owner"], r["repo"])

    @property
    def key(self) -> str:
        """Return the repository key plus its credential-free target digest."""
        t = strip_creds(self.request["target"])
        return f"{self.repo_key}@{generator_digest(t.get('generator'), t.get('generator_config'))}"

    def to_status(self) -> dict:
        r = self.request
        return {
            "id": self.key,
            "owner": r["owner"],
            "repo": r["repo"],
            "repo_type": r["type"],
            "language": r["language"],
            "status": self.status,
            "pages_done": self.pages_done,
            "pages_total": self.pages_total,
            "current_page_ids": self.current_page_ids,
            "wiki_structure": dataclasses.asdict(self.wiki_structure) if self.wiki_structure else None,
            "error": self.error,
            "submitted_at": self.submitted_at,
            "name": f"{r['owner']}/{r['repo']}",
        }

    def to_summary(self) -> dict:
        r = self.request
        return {
            "id": self.key,
            "owner": r["owner"],
            "repo": r["repo"],
            "repo_type": r["type"],
            "language": r["language"],
            "status": self.status,
            "pages_done": self.pages_done,
            "pages_total": self.pages_total,
            "current_page_ids": self.current_page_ids,
            "error": self.error,
            "submitted_at": self.submitted_at,
            "name": f"{r['owner']}/{r['repo']}",
        }


# --- Task registry ---


class TaskSubmitResult(BaseModel):
    task_id: str
    status: TaskStatus | str
    created: bool = False
    joined: bool = False
    from_cache: bool = False
    resumed: bool = False

    @field_validator("status", mode="before")
    @classmethod
    def _status_validate(cls, value):
        if isinstance(value, str):
            return TaskStatus(value.lower())
        return value


WikiTaskSubmitResult = TaskSubmitResult


class TaskRegistry:
    """Deduplicate, resume, limit, and eventually evict asynchronous tasks."""

    def __init__(self, max_concurrent: int = 1, ttl_seconds: float = 300):
        self._tasks: dict[str, WikiTask] = {}
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))

    def get(self, task_id: str) -> WikiTask | None:
        return self._tasks.get(task_id)

    def active(self) -> list[WikiTask]:
        return [t for t in self._tasks.values() if not t.status.is_terminal()]

    async def remove(self, task_id: str) -> WikiTask | None:
        async with self._lock:
            return self._tasks.pop(task_id, None)

    async def shutdown(self) -> None:
        """Cancel and join every task owned by the registry."""
        async with self._lock:
            running = [task.task for task in self._tasks.values()
                       if task.task is not None and not task.task.done()]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

    async def submit(self, task: WikiTask) -> TaskSubmitResult:
        key = task.key
        async with self._lock:
            exist_task = self.get(key)
            if exist_task and not exist_task.status.is_terminal():
                return TaskSubmitResult(task_id=key, status=exist_task.status, joined=True)
            if await self.is_cached(task):
                # A complete cache wins over stale resume state left by a crash.
                await self.on_cache_hit(task)
                return TaskSubmitResult(task_id=key, status=TaskStatus.COMPLETED, from_cache=True)
            resumed = await self.load_resume(task)
            if resumed is not None:
                task = resumed
            task.task = asyncio.create_task(self._run(task))
            self._tasks[key] = task
            return TaskSubmitResult(
                task_id=key, status=task.status, created=True, resumed=resumed is not None,
            )

    async def _run(self, task: WikiTask) -> None:
        async with self._semaphore:
            await self.run(task)
        self._schedule_remove(task)

    def _schedule_remove(self, task: WikiTask) -> None:
        async def remove() -> None:
            await asyncio.sleep(self._ttl_seconds())
            if self.get(task.key) is task and task.status.is_terminal():
                await self.remove(task.key)

        bg_task = asyncio.create_task(remove())
        _background_tasks.add(bg_task)
        bg_task.add_done_callback(_background_tasks.discard)

    # --- Subclass hooks ---

    async def run(self, task: WikiTask) -> None:
        """Run a task; subclasses must implement this hook."""
        raise NotImplementedError

    async def is_cached(self, task: WikiTask) -> bool:
        """Return whether a complete result is already cached."""
        return False

    async def on_cache_hit(self, task: WikiTask) -> None:
        """Clean up after a cache hit; the base implementation does nothing."""
        return

    async def load_resume(self, task: WikiTask) -> WikiTask | None:
        """Rebuild a task from persisted state when available."""
        return None

    def _ttl_seconds(self) -> float:
        """Resolve the eviction delay at scheduling time."""
        return self._ttl


class WikiTaskRegistry(TaskRegistry):
    """Implement cache, resume, generation, and dynamic TTL hooks for wiki tasks."""

    async def run(self, task: WikiTask) -> None:
        await generate_repo_wiki(task)  # Resolve at call time for test substitution.

    async def is_cached(self, task: WikiTask) -> bool:
        r = task.request
        t = strip_creds(r["target"])
        cache = await read_wiki_cache(
            r["owner"], r["repo"], r["type"], r["language"],
            digest=generator_digest(
                t.get("generator"), t.get("generator_config"),
            ),
        )
        if cache is None:
            return False
        # Legacy records without matching identity must regenerate.
        if cache_generator_matches(cache, t.get("generator"), t.get("generator_config")):
            return True
        log(
            f"成品缓存 target 不匹配({cache_identity(cache)!r} vs "
            f"{resolve_generator(r['target'])!r}),忽略并重新生成: {r['owner']}/{r['repo']}",
        )
        return False

    async def on_cache_hit(self, task: WikiTask) -> None:
        r = task.request
        t = strip_creds(r["target"])
        await delete_resume_state(
            r["owner"], r["repo"], r["type"], r["language"],
            digest=generator_digest(
                t.get("generator"), t.get("generator_config"),
            ),
        )

    async def load_resume(self, task: WikiTask) -> WikiTask | None:
        r = task.request
        t = strip_creds(r["target"])
        state = await read_resume_state(
            r["owner"], r["repo"], r["type"], r["language"],
            digest=generator_digest(
                t.get("generator"), t.get("generator_config"),
            ),
        )
        if state is None:
            return None
        # Resume files are isolated by target digest; credentials come from this request.
        merged = {**state["request"], "target": merge_creds(state["request"].get("target"), r["target"])}
        return WikiTask(
            request=merged,
            status=(
                TaskStatus.GENERATING
                if state.get("wiki_structure") is not None
                else TaskStatus.DETERMINING_STRUCTURE
            ),
            pages_done=len(state.get("generated_pages") or {}),
            wiki_structure=deepwiki.wiki_structure_of(state.get("wiki_structure")),
            default_branch=state.get("default_branch", "main"),
            submitted_at=state["submitted_at"],
            generated_pages={
                k: deepwiki.WikiPage(**v) for k, v in (state.get("generated_pages") or {}).items()
            },
        )

    def _ttl_seconds(self) -> float:
        # Resolve at call time so tests can replace the module setting.
        return _WIKI_TASK_TTL_SECONDS


registry = WikiTaskRegistry(
    max_concurrent=_MAX_CONCURRENT_WIKI_TASKS,
    ttl_seconds=_WIKI_TASK_TTL_SECONDS,
)


# --- Progress persistence ---


def strip_creds(config: dict) -> dict:
    """Disk form: copy and strip api_key/base_url out of generator_config (config path is not a credential, kept)."""
    out = dict(config)
    gc = dict(out.get("generator_config") or {})
    gc.pop("api_key", None)
    gc.pop("base_url", None)
    out["generator_config"] = gc
    return out


def merge_creds(base: dict, other: dict | None) -> dict:
    """Disk form keeps itself; credentials (api_key/base_url inside generator_config) come from other."""
    out = dict(base)
    oc = dict((other or {}).get("generator_config") or {})
    merged = dict(out.get("generator_config") or {})
    for key in ("base_url", "api_key"):
        if oc.get(key):
            merged[key] = oc[key]
    out["generator_config"] = merged
    return out


async def _persist_state(task: WikiTask) -> None:
    """Persist a credential-free task snapshot under the module write lock."""
    req = dict(task.request)
    req["target"] = strip_creds(task.request["target"])
    state = {
        "version": 1,
        "request": req,
        "status": task.status,
        "wiki_structure": dataclasses.asdict(task.wiki_structure) if task.wiki_structure else None,
        "generated_pages": {pid: dataclasses.asdict(pg) for pid, pg in task.generated_pages.items()},
        "default_branch": task.default_branch,
        "submitted_at": task.submitted_at,
        "error": task.error,
    }
    async with _state_write_lock:
        await write_resume_state(
            req["owner"], req["repo"], req["type"], req["language"], state,
            digest=generator_digest(req["target"].get("generator"), req["target"].get("generator_config")),
        )


# --- Generation pipeline ---


@dataclass
class PreparedRepo:
    """Hold repository state prepared once for the generation pipeline."""

    repo: Repo
    default_branch: str


async def _prepare_repo(repo: Repo) -> PreparedRepo:
    """Resolve repository state shared by structure and page generation."""
    default_branch = await asyncio.to_thread(detect_default_branch, repo.save_path)
    return PreparedRepo(repo=repo, default_branch=default_branch)


async def generate_repo_wiki(task: WikiTask) -> None:
    """Drive indexing, structure, pages, and cache while persisting resumable progress."""
    r = task.request
    try:
        await _persist_state(task)
        repo = Repo(r["repo_url"], r["type"], access_token=r.get("token"))
        gc = runtime_config(r["target"].get("generator"), r["target"].get("generator_config"), repo=repo)
        if not index_ready(repo):
            task.status = TaskStatus.INDEXING
            log(f"索引中: {task.repo_key}")
            await ensure_index(repo)
        prepared = await _prepare_repo(repo)

        if task.wiki_structure is None or needs_structure_regenerate(
            project_key=task.repo_key,
            generator=r["target"].get("generator"), generator_config=gc,
        ):
            task.status = TaskStatus.DETERMINING_STRUCTURE
            task.wiki_structure = await _determine_structure(task, prepared, gc)
            await _persist_state(task)

        task.status = TaskStatus.GENERATING
        task.generated_pages.update(
            await hydrate_pages(
                project_key=task.repo_key,
                generator=r["target"].get("generator"), generator_config=gc,
                repo=prepared.repo,
                structure=task.wiki_structure, default_branch=prepared.default_branch,
            ),
        )
        pages = await _generate_pages(task, prepared, gc)

        if not await save_generated_wiki(
            r["owner"], r["repo"], r["type"], r["repo_url"],
            task.wiki_structure, pages, language=r["language"],
            generator=r["target"].get("generator"),
            # Persist public selection identity, not runtime-injected tool configuration.
            generator_config=strip_creds(r["target"]).get("generator_config"),
        ):
            raise RuntimeError("写 wiki 缓存失败")  # Keep resume state so a resubmit retries the write.
        t = strip_creds(r["target"])
        await delete_resume_state(
            r["owner"], r["repo"], r["type"], r["language"],
            digest=generator_digest(
                t.get("generator"), t.get("generator_config"),
            ),
        )
        task.status = TaskStatus.COMPLETED
        log(f"wiki 任务完成: {task.repo_key}")
    except asyncio.CancelledError:  # Persist once during shutdown, then preserve cancellation.
        await _persist_state(task)
        raise
    except Exception as e:
        task.status = TaskStatus.FAILED
        task.error = str(e)
        await _persist_state(task)
        log(f"wiki 任务失败: {task.repo_key} - {e}")


async def _determine_structure(
    task: WikiTask, prepared: PreparedRepo, gc: dict,
) -> WikiStructureModel:
    """Determine the wiki structure and propagate failures to the task state machine."""
    r = task.request
    task.default_branch = prepared.default_branch
    return await determine_structure(
        generator=r["target"].get("generator"), generator_config=gc,
        repo=prepared.repo, owner=r["owner"], repo_name=r["repo"],
        comprehensive=r["comprehensive"], language=r["language"], run_id=task.repo_key,
    )


async def _generate_page(
    task: WikiTask, page: WikiPage, prepared: PreparedRepo, gc: dict,
) -> WikiPage:
    """Generate one finalized page through the engine."""
    r = task.request
    content = await generate_page(
        generator=r["target"].get("generator"), generator_config=gc,
        repo=prepared.repo, page=page,
        language=r["language"], default_branch=prepared.default_branch, run_id=task.repo_key,
    )
    return dataclasses.replace(page, content=content)


async def _generate_page_with_retry(
    task: WikiTask, page: WikiPage, prepared: PreparedRepo, gc: dict,
) -> WikiPage:
    last_error: Exception | None = None
    for attempt in range(_WIKI_PAGE_RETRIES + 1):
        try:
            return await _generate_page(task, page, prepared, gc)
        except Exception as e:
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError from e
            last_error = e
            log(f"页面 {page.id} 生成失败(尝试 {attempt + 1}/{_WIKI_PAGE_RETRIES + 1}): {e}")
    # A persisted placeholder lets the rest of the wiki complete after retry exhaustion.
    content = f"Error generating content: {last_error}"
    r = task.request
    write_error_page(
        project_key=task.repo_key,
        generator=r["target"].get("generator"), generator_config=gc,
        page=page, content=content,
    )
    return dataclasses.replace(page, content=content)


def _pending_pages(structure: WikiStructureModel, done: dict[str, WikiPage]) -> list[WikiPage]:
    """Return unfinished pages in structure order."""
    return [p for p in structure.pages if p.id not in done]


async def _generate_pages(
    task: WikiTask, prepared: PreparedRepo, gc: dict,
) -> dict[str, WikiPage]:
    """Generate unfinished pages with bounded concurrency and per-page retries."""
    structure = task.wiki_structure
    assert structure is not None  # noqa: S101 - Narrow an invariant established upstream.
    sema = asyncio.Semaphore(_WIKI_PAGE_CONCURRENCY)
    task.pages_done = len(task.generated_pages)
    pending = _pending_pages(structure, task.generated_pages)

    async def one(page: WikiPage) -> None:
        async with sema:
            task.current_page_ids.append(page.id)
            try:
                task.generated_pages[page.id] = await _generate_page_with_retry(
                    task, page, prepared, gc,
                )
            finally:
                with contextlib.suppress(ValueError):
                    task.current_page_ids.remove(page.id)
                task.pages_done += 1
            await _persist_state(task)

    await asyncio.gather(*(one(page) for page in pending))
    return task.generated_pages
