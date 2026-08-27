"""wiki 任务调度与执行(runtime 包装;webui 后端专属,不在 gh_puller 包内)。

携带 App 进程级状态并驱动 gh_puller 引擎的 wiki 主流程:
- 任务注册表/单例 registry(进程级任务表、同 key join 去重、并发信号量、TTL 迟移除);
- 任务运行时 WikiTask(内存态:状态/进度/运行时 asyncio.Task 引用);
- 主流程 generate_repo_wiki 一套(索引→结构→页面→成品缓存)与进度落盘投影
  _persist_state(模块级写锁,与页生成并发串行化)。

职责边界(与包的边界):
- 生成协议/提示词/交付件、契约模型、内容渲染与引用后处理(render)、缓存与状态文件 IO、
  判等摘要族、索引服务属引擎(gh_puller.deepwiki,本模块白名单直连);cache/pipeline/
  render 保持各自职责的纯化,任务 runtime 语义(状态机/去重/续跑合并/进度投影)都在本模块。
- **零内容业务**:本模块不拼链接、不剥围栏、不做引用后处理、不组装缓存模型
  (save_generated_wiki 在引擎缓存层)—— 编排只做"取 pipeline 实例 → 调用 → 回写进度"。
- 本模块是外部消费者而非包内模块:引擎符号顶部绑名 import(同 app.py 现状);
  模块自身符号(如 _WIKI_TASK_TTL_SECONDS、generate_repo_wiki、_persist_state)在内部
  一律**调用时经模块全局解析**(测试 monkeypatch 位点,不得实例捕获或模块级快照)。

导入副作用(都在本模块;引擎导入零副作用):envs 调度常量快照、状态写锁、
成品缓存目录创建、registry 单例。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gh_puller import deepwiki, envs
from gh_puller.deepwiki import (
    WikiPage,
    WikiStructureModel,
    delete_resume_state,
    ensure_index,
    read_resume_state,
    read_wiki_cache,
    save_generated_wiki,
    write_resume_state,
)
from gh_puller.deepwiki.cache import (
    _cache_generator_matches,
    _cache_identity,
    _generator_digest,
    _index_ready,
    _wiki_cache_dir,
)
from gh_puller.deepwiki.wiki import _wiki_pipeline
from gh_puller.deepwiki.utils import _log, _merge_creds, _resolve_generator, _strip_creds
from gh_puller.utils import (
    Repo,
    TaskStatus,
    detect_default_branch,
    read_repo_file_tree,
)
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

if TYPE_CHECKING:
    from gh_puller.deepwiki import WikiPipeline

# ---------------------------------------------------------------------------
# 任务调度常量(导入期 envs 快照,同引擎原式;monkeypatch 模块全局)与进程级状态
# ---------------------------------------------------------------------------

_MAX_CONCURRENT_WIKI_TASKS = envs.MAX_CONCURRENT_WIKI_TASKS
_WIKI_PAGE_CONCURRENCY = max(1, envs.WIKI_PAGE_CONCURRENCY)
_WIKI_PAGE_RETRIES = max(0, envs.WIKI_PAGE_RETRIES)
_WIKI_TASK_TTL_SECONDS = envs.WIKI_TASK_TTL_SECONDS

# 状态写锁:并发页生成器的落盘写串行化(asyncio 3.10+ 的 Lock 不再绑定 loop,模块级安全)
_state_write_lock = asyncio.Lock()
# 引擎导入零副作用(不再建目录),缓存目录创建由本模块(App 进程)负责
os.makedirs(_wiki_cache_dir(), exist_ok=True)


# ---------------------------------------------------------------------------
# 任务运行时模型(runtime 包装;引擎协议只收 request,见 pipeline.py 契约)
# ---------------------------------------------------------------------------


class WikiTask(BaseModel):
    """单个仓库生成任务的进程内运行时状态(状态/进度/运行时 asyncio.Task 引用)。

    request 为纯 dict(引擎零 Request 概念);出网契约由 app 响应模型
    (schemas.WikiTaskStatus/Summary)校验序列化。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)  # 允许 asyncio.Task 字段

    status: TaskStatus = TaskStatus.PENDING
    error: str | None = None
    submitted_at: int = Field(default_factory=lambda: int(time.time() * 1000))
    task: asyncio.Task | None = Field(default=None, repr=False)

    request: dict[str, Any]
    pages_done: int = 0
    current_page_ids: list[str] = Field(default_factory=list)
    generated_pages: dict[str, WikiPage] = Field(default_factory=dict)  # 完成页就地累积(续跑=已生成页)
    wiki_structure: WikiStructureModel | None = None
    default_branch: str = "main"  # 结构确定时记录(进度/展示;URL 单源见 PreparedRepo)

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
        """注册表去重键 = repo 键 + target 判等摘要:同一仓库/语言下
        不同 target 的任务可并发并存(隔离生成产物与续跑状态)。"""
        return f"{self.repo_key}@{_generator_digest(self.request['target'])}"

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


# ---------------------------------------------------------------------------
# 通用异步任务注册表(原 utils.TaskRegistry 与 TaskSubmitResult 内迁:全仓唯一
# 消费者即本模块,不再留底层抽象;类型钉为 WikiTask)。
# 提交语义:按 key 去重并入(join)/缓存胜/落盘续跑/并发信号量/TTL 迟移除。
# ---------------------------------------------------------------------------


class TaskSubmitResult(BaseModel):
    task_id: str
    status: TaskStatus | str
    created: bool = False
    joined: bool = False
    from_cache: bool = False
    resumed: bool = False  # 从落盘状态续跑(同仓库再提交命中生成状态)

    @field_validator("status", mode="before")
    @classmethod
    def _status_validate(cls, value):
        if isinstance(value, str):
            return TaskStatus(value.lower())
        return value


# HTTP 提交响应模型(仅 server/api 消费;与 TaskSubmitResult 同一形状)
WikiTaskSubmitResult = TaskSubmitResult


class TaskRegistry:
    """通用异步任务注册表:按 key 去重并入(join)/缓存胜/落盘续跑/并发信号量/TTL 迟移除。

    提交语义的业务差异全部经由子类钩子注入(见各钩子默认实现);
    基类默认:无缓存、无续跑、run 须子类实现。
    """

    def __init__(self, max_concurrent: int = 1, ttl_seconds: float = 300):
        self._tasks: dict[str, WikiTask] = {}
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))

    def get(self, id: str) -> WikiTask | None:
        return self._tasks.get(id)

    def active(self) -> list[WikiTask]:
        return [t for t in self._tasks.values() if not t.status.is_terminal()]

    async def remove(self, id: str) -> WikiTask | None:
        async with self._lock:
            return self._tasks.pop(id, None)

    async def submit(self, task: WikiTask) -> TaskSubmitResult:
        key = task.key
        async with self._lock:
            exist_task = self.get(key)
            if exist_task and not exist_task.status.is_terminal():
                return TaskSubmitResult(task_id=key, status=exist_task.status, joined=True)
            if await self.is_cached(task):
                # 缓存胜:清理陈旧落盘状态(成功写缓存后删状态前崩溃的残留)
                await self.on_cache_hit(task)
                return TaskSubmitResult(task_id=key, status=TaskStatus.COMPLETED, from_cache=True)
            resumed = await self.load_resume(task)
            if resumed is not None:
                task = resumed
            task.task = asyncio.create_task(self._run(task))
            self._tasks[key] = task
            return TaskSubmitResult(
                task_id=key, status=task.status, created=True, resumed=resumed is not None
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

        asyncio.create_task(remove())

    # ------------------------------------------------------------------
    # 子类钩子协议
    # ------------------------------------------------------------------

    async def run(self, task: WikiTask) -> None:
        """执行任务本体(子类必须实现;调用时解析,支持模块全局 monkeypatch)。"""
        raise NotImplementedError

    async def is_cached(self, task: WikiTask) -> bool:
        """磁盘上是否有完整成品缓存(命中则快速返回,不再执行)。"""
        return False

    async def on_cache_hit(self, task: WikiTask) -> None:
        """缓存命中时的清理动作(默认无;wiki 用于清除陈旧续跑状态)。"""
        return None

    async def load_resume(self, task: WikiTask) -> WikiTask | None:
        """从落盘状态恢复并返回重建的任务(默认无续跑)。"""
        return None

    def _ttl_seconds(self) -> float:
        """TTL call-time 解析(默认构造参数;子类可读模块全局以支持测试 monkeypatch)。"""
        return self._ttl


class WikiTaskRegistry(TaskRegistry):
    """wiki 专属提交语义(缓存胜/续跑/生成器执行)经钩子注入;TTL 读模块全局供测试 monkeypatch。"""

    async def run(self, task: WikiTask) -> None:
        await generate_repo_wiki(task)  # 调用时经模块全局解析(monkeypatch 生效)

    async def is_cached(self, task: WikiTask) -> bool:
        r = task.request
        cache = await read_wiki_cache(
            r["owner"], r["repo"], r["type"], r["language"], digest=_generator_digest(r["target"])
        )
        if cache is None:
            return False
        # 判等身份与缓存内记录对齐(旧缓存字段缺失/旧契约 → 判不匹配,重新生成)
        if _cache_generator_matches(cache, r["target"]):
            return True
        _log(
            f"成品缓存 target 不匹配({_cache_identity(cache)!r} vs "
            f"{_resolve_generator(r['target'])!r}),忽略并重新生成: {r['owner']}/{r['repo']}"
        )
        return False

    async def on_cache_hit(self, task: WikiTask) -> None:
        r = task.request
        await delete_resume_state(
            r["owner"], r["repo"], r["type"], r["language"], digest=_generator_digest(r["target"])
        )

    async def load_resume(self, task: WikiTask) -> WikiTask | None:
        r = task.request
        state = await read_resume_state(
            r["owner"], r["repo"], r["type"], r["language"], digest=_generator_digest(r["target"])
        )
        if state is None:
            return None
        # 状态文件按 target 摘要隔离(同仓库不同 target 并存);凭证从当前提交合并
        # (落盘状态只存公开三元组,见 _persist_state)
        merged = {**state["request"], "target": _merge_creds(state["request"].get("target"), r["target"])}
        return WikiTask(
            request=merged,  # dict → BaseModel 校验重建
            status=(
                TaskStatus.GENERATING
                if state.get("wiki_structure") is not None
                else TaskStatus.DETERMINING_STRUCTURE
            ),
            pages_done=len(state.get("generated_pages") or {}),
            wiki_structure=deepwiki.wiki_structure_of(state.get("wiki_structure")),
            default_branch=state.get("default_branch", "main"),
            submitted_at=state["submitted_at"],  # 保留原始提交时间
            generated_pages={
                k: deepwiki.WikiPage(**v) for k, v in (state.get("generated_pages") or {}).items()
            },
        )

    def _ttl_seconds(self) -> float:
        # call-time 读模块全局:tests monkeypatch tasks._WIKI_TASK_TTL_SECONDS
        return _WIKI_TASK_TTL_SECONDS


registry = WikiTaskRegistry(
    max_concurrent=_MAX_CONCURRENT_WIKI_TASKS,
    ttl_seconds=_WIKI_TASK_TTL_SECONDS,
)


# ---------------------------------------------------------------------------
# 进度落盘投影(原 cache.py _persist_state 迁入:引擎持久化层只留 IO 原语,
# 从 live 任务组装快照 + 锁串行属任务 runtime 语义)
# ---------------------------------------------------------------------------


async def _persist_state(task: WikiTask) -> None:
    """把任务当前进度落盘(结构/已完成页/状态);并发写由模块锁串行。

    落盘即剥离凭证:request.target 存 _strip_creds(只含判等身份字段),
    续跑合并用户重新提交的凭证(见 WikiTaskRegistry.load_resume)。
    """
    req = dict(task.request)
    req["target"] = _strip_creds(task.request["target"])  # strip → 公开三元组(仅判等身份字段)
    state = {
        "version": 1,
        "request": req,
        "status": task.status,  # str Enum;json.dumps 序列化为字面字符串,读回由 BaseModel 校验转回
        "wiki_structure": dataclasses.asdict(task.wiki_structure) if task.wiki_structure else None,
        "generated_pages": {pid: dataclasses.asdict(pg) for pid, pg in task.generated_pages.items()},
        "default_branch": task.default_branch,
        "submitted_at": task.submitted_at,
        "error": task.error,
    }
    async with _state_write_lock:
        await write_resume_state(
            req["owner"], req["repo"], req["type"], req["language"], state,
            digest=_generator_digest(req["target"]),
        )


# ---------------------------------------------------------------------------
# wiki 主流程(驱动一个任务走完状态机:索引 → 结构 → 页面 → 成品缓存;
# 唯一分派:pipeline 实例在入口取一次,全流程共用)
# ---------------------------------------------------------------------------


@dataclass
class PreparedRepo:
    """一次生成所需的仓库态上下文(单源:分支/文件树/readme 只准备一次,流水线共用)。"""

    repo: Repo
    default_branch: str
    file_tree: list[str]
    readme: str | None


async def _prepare_repo(request: dict, repo: Repo) -> PreparedRepo:
    """仓库态运行时准备(默认分支 + 文件树/README 路径);克隆由 ensure_index 承担。"""
    default_branch = await asyncio.to_thread(detect_default_branch, repo.save_path)
    file_tree, readme = await asyncio.to_thread(
        read_repo_file_tree,
        repo.save_path,
        request.get("included_files") or [],
        request.get("included_dirs") or [],
        request.get("excluded_files") or [],
        request.get("excluded_dirs") or [],
    )
    return PreparedRepo(repo=repo, default_branch=default_branch, file_tree=file_tree, readme=readme)


async def generate_repo_wiki(task: WikiTask) -> None:
    """驱动一个任务走完状态机(索引 → 结构 → 页面 → 缓存),失败置 FAILED。

    进度中途落盘(deepwiki_resume_*):结构确定后与每页完成后各写一次,
    失败/取消也尽力写;同仓库再次提交时从落盘状态续跑(见 TaskRegistry.submit)。
    """
    r = task.request
    try:
        await _persist_state(task)  # 入口即落盘:中断于索引/结构阶段的也能续跑
        pipeline = _wiki_pipeline(r["target"])  # 唯一分派:全流程共用一个实例
        repo = Repo(r["repo_url"], r["type"], access_token=r.get("token"))
        # 索引:只建一次(v1 无增量;已存在即跳过)
        if not _index_ready(repo):
            task.status = TaskStatus.INDEXING
            _log(f"索引中: {task.repo_key}")
            extra_excludes = (
                [*r["excluded_dirs"], *r["excluded_files"]]
                if (r["excluded_dirs"] or r["excluded_files"]) else None
            )
            await ensure_index(repo, extra_excludes=extra_excludes)
        # 仓库态(分支/文件树):结构确定与页面生成共用同一次准备
        prepared = await _prepare_repo(r, repo)

        if task.wiki_structure is None or pipeline.needs_structure_regenerate(
            project_key=task.repo_key, choice=r["target"],
        ):
            # 续跑:结构已落盘(cc 下以交付文件为准,被删则强制重生成)则跳过 agent 调用
            task.status = TaskStatus.DETERMINING_STRUCTURE
            task.wiki_structure = await _determine_structure(task, pipeline, prepared)
            await _persist_state(task)

        task.status = TaskStatus.GENERATING
        task.generated_pages.update(
            await pipeline.hydrate_pages(
                project_key=task.repo_key, choice=r["target"], repo=prepared.repo,
                structure=task.wiki_structure, default_branch=prepared.default_branch,
            )
        )  # cc 以文件为权威覆盖落盘 state 旧文本;llm no-op(返回空快照)
        pages = await _generate_pages(task, pipeline, prepared)

        if not await save_generated_wiki(
            r["owner"], r["repo"], r["type"], r["repo_url"], r["target"],
            task.wiki_structure, pages, language=r["language"],
        ):
            raise RuntimeError("写 wiki 缓存失败")  # 不删状态:再提交仅重试写缓存
        await delete_resume_state(
            r["owner"], r["repo"], r["type"], r["language"], digest=_generator_digest(r["target"])
        )
        task.status = TaskStatus.COMPLETED
        _log(f"wiki 任务完成: {task.repo_key}")
    except asyncio.CancelledError:  # Ctrl+C/停机:尽力持久化一次后重新抛出
        await _persist_state(task)
        raise
    except Exception as e:
        task.status = TaskStatus.FAILED
        task.error = str(e)
        await _persist_state(task)  # FAILED 也落盘,后续提交可续跑
        _log(f"wiki 任务失败: {task.repo_key} - {e}")


async def _determine_structure(
    task: WikiTask, pipeline: WikiPipeline, prepared: PreparedRepo,
) -> WikiStructureModel:
    """确定 wiki 结构(按 target.generator 分派 cc/dsh/codex/llm);失败上抛使任务 FAILED。"""
    r = task.request
    task.default_branch = prepared.default_branch  # 记录(进度/展示;URL 单源见 PreparedRepo)
    return await pipeline.determine_structure(
        choice=r["target"], repo=prepared.repo, owner=r["owner"], repo_name=r["repo"],
        file_tree=prepared.file_tree, readme=prepared.readme,
        comprehensive=r["comprehensive"], language=r["language"], run_id=task.repo_key,
    )


async def _generate_page(
    task: WikiTask, page: WikiPage, pipeline: WikiPipeline, prepared: PreparedRepo,
) -> WikiPage:
    """生成单个页面(编排:取流水线实例与仓库上下文;内容与终态格式化收在 pipeline 内)。"""
    r = task.request
    content = await pipeline.generate_page(
        choice=r["target"], repo=prepared.repo, page=page,
        language=r["language"], default_branch=prepared.default_branch, run_id=task.repo_key,
    )
    return dataclasses.replace(page, content=content)


async def _generate_page_with_retry(
    task: WikiTask, page: WikiPage, pipeline: WikiPipeline, prepared: PreparedRepo,
) -> WikiPage:
    last_error: Exception | None = None
    for attempt in range(_WIKI_PAGE_RETRIES + 1):
        try:
            return await _generate_page(task, page, pipeline, prepared)
        except Exception as e:  # noqa: BLE001 - 瞬时/永久错误统一由重试预算兜底
            last_error = e
            _log(f"页面 {page.id} 生成失败(尝试 {attempt + 1}/{_WIKI_PAGE_RETRIES + 1}): {e}")
    # 重试耗尽:回退错误占位页,保证整个 wiki 仍能完成
    content = f"Error generating content: {last_error}"
    r = task.request
    pipeline.write_error_page(
        project_key=task.repo_key, choice=r["target"], page=page, content=content,
    )
    return dataclasses.replace(page, content=content)


def _pending_pages(structure: WikiStructureModel, done: dict[str, WikiPage]) -> list[WikiPage]:
    """按结构顺序返回尚未生成的页面(done: 已完成页 id → 页)。"""
    return [p for p in structure.pages if p.id not in done]


async def _generate_pages(
    task: WikiTask, pipeline: WikiPipeline, prepared: PreparedRepo,
) -> dict[str, WikiPage]:
    """有界并发 + 每页重试地生成所有页面;续跑跳过已落盘的页,每页完成后立即落盘。"""
    structure = task.wiki_structure
    assert structure is not None
    sema = asyncio.Semaphore(_WIKI_PAGE_CONCURRENCY)
    task.pages_done = len(task.generated_pages)  # 续跑:从恢复的完成数起步
    pending = _pending_pages(structure, task.generated_pages)

    async def one(page: WikiPage) -> None:
        async with sema:
            task.current_page_ids.append(page.id)
            try:
                task.generated_pages[page.id] = await _generate_page_with_retry(
                    task, page, pipeline, prepared
                )
            finally:
                with contextlib.suppress(ValueError):
                    task.current_page_ids.remove(page.id)
                task.pages_done += 1
            await _persist_state(task)  # 每页完成即落盘(锁内串行写)

    await asyncio.gather(*(one(page) for page in pending))
    return task.generated_pages
