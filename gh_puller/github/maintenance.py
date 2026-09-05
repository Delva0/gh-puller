"""执行不推进普通发现水位的补充事实补采与主动刷新。

本模块冻结已发布资源范围、驱动 store 中的持久任务，并复用 puller 的事实读取与
Git 对象保留逻辑。普通 run 的目录发现、bundle 更新和水位推进不属于这里。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .commit_references import (
    CommitReferenceSource,
    bundle_commit_references,
    review_thread_commit_references,
)
from .git_store import GitStoreError
from .locking import archive_lock
from .progress import _PullProgressTracker
from .puller import GitHubPullConfig, GitHubPuller, _as_utc, _iso
from .store import (
    FactJob,
    FactTask,
    FactTaskSpec,
    SQLiteArchive,
    StagedFact,
    StagedTaskFact,
    json_digest,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Sequence

    from .progress import ProgressObserver

_FACT_GROUPS = {
    "reviews": {"review-threads": 1},
    "relations": {"issue-relations": 1},
    "commits": {"commit-references": 1, "commit-object": 1},
    "refs": {"git-refs": 1},
}
FACT_GROUPS = tuple(_FACT_GROUPS)


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    job_id: int  # Durable fact job identity.
    job_key: str  # Idempotency key derived from the frozen request.
    kind: str  # backfill or refresh.
    target_at: datetime  # Requested observation target.
    completed_at: datetime  # Actual job completion time.
    total_tasks: int  # Frozen missing-work population.
    completed_tasks: int  # Tasks with a published terminal result.


class GitHubFactMaintainer:
    """运行显式补采与刷新操作。

    Args:
        config: 仓库、SQLite 事实库和请求策略。
        api: 测试或宿主提供的 GitHub API 读取对象。
        git: 测试或宿主提供的持久化 Git 对象库。
        now: 冻结默认目标并记录读取窗口的 UTC 时钟。
        sleep: 等待未来目标使用的异步等待函数。
        observer: 同步带外进度观察器；失败不改变事实发布。
    """

    def __init__(
        self,
        config: GitHubPullConfig,
        *,
        api: Any | None = None,
        git: Any | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        observer: ProgressObserver | None = None,
    ) -> None:
        self.config = config
        self._now = now
        self._sleep = sleep
        self._observer = observer
        self._puller = GitHubPuller(
            config,
            api=api,
            git=git,
            now=now,
            sleep=sleep,
        )

    async def backfill(
        self,
        target: datetime | None = None,
        *,
        fact_groups: Iterable[str] = FACT_GROUPS,
        batch_size: int = 8,
    ) -> MaintenanceResult:
        """补齐一次已发布历史截面的缺失事实。

        Args:
            target: 观测目标；None 在进入函数、任何 await 前取当前 UTC 时刻。
            fact_groups: reviews、relations、commits、refs 的任意组合。
            batch_size: 每次原子领取并发布的持久任务上限。

        Returns:
            冻结 scope 对应的已完成作业；重复调用会恢复或复用同一 scope。

        Raises:
            RuntimeError: 普通 pull 尚未发布，或另一事实作业占用写者。
            ValueError: 事实集合、目标或批大小无效。
        """
        requested_at = _as_utc(self._now())
        target_at = requested_at if target is None else _as_utc(target)
        groups = _groups(fact_groups)
        _batch_size(batch_size)
        await self._wait_until(target_at)
        request = {"fact_groups": list(groups)}
        async with (
            archive_lock(self.config.destination),
            SQLiteArchive(self.config.destination, self.config.repository) as archive,
        ):
            pending = await archive.pending_fact_job()
            if pending is not None:
                _match_pending(pending, "backfill", request, target if target is not None else None)
                await self._wait_until(_parse_time(pending.target_at))
                return await self._run_job(archive, pending, batch_size)
            plan = await archive.backfill_plan(_fact_versions(groups))
            scope = plan.scope | {"request": request}
            previous = await archive.latest_fact_job("backfill")
            if (
                not plan.tasks
                and previous is not None
                and previous.status == "complete"
                and previous.scope.get("resource_version_cutoff") == scope["resource_version_cutoff"]
                and previous.scope.get("fact_sets") == scope["fact_sets"]
            ):
                return _result(previous)
            job_key = f"backfill:{json_digest(scope)}"
            existing = await archive.fact_job_by_key(job_key)
            if existing is not None:
                return _result(existing)
            job = await archive.start_fact_job(
                job_key,
                "backfill",
                _iso(target_at),
                _iso(requested_at),
                scope,
                plan.tasks,
            )
            return await self._run_job(archive, job, batch_size)

    async def refresh(
        self,
        target: datetime | None = None,
        *,
        pulls: Iterable[int] = (),
        issues: Iterable[int] = (),
        commits: Iterable[str] = (),
        fact_groups: Iterable[str] = FACT_GROUPS,
        batch_size: int = 8,
    ) -> MaintenanceResult:
        """主动重读指定对象的补充事实。

        Args:
            target: 观测目标；相同目标与选择返回同一作业，新目标创建新观测。
            pulls: 已归档 PR numbers。
            issues: 已归档 Issue numbers。
            commits: 需要再次验证可用性的结构化 commit IDs。
            fact_groups: reviews、relations、commits、refs 的任意组合。
            batch_size: 每次原子领取并发布的持久任务上限。

        Returns:
            已完成的刷新作业。

        Raises:
            KeyError: 指定 parent 尚未进入已发布归档。
            RuntimeError: 普通 pull 尚未发布，或另一事实作业占用写者。
            ValueError: 选择、目标或批大小无效。
        """
        requested_at = _as_utc(self._now())
        target_at = requested_at if target is None else _as_utc(target)
        groups = _groups(fact_groups)
        pull_numbers = _numbers(pulls)
        issue_numbers = _numbers(issues)
        shas = _commits(commits)
        _batch_size(batch_size)
        request = {
            "commits": list(shas),
            "fact_groups": list(groups),
            "issues": list(issue_numbers),
            "pulls": list(pull_numbers),
        }
        if not pull_numbers and not issue_numbers and not shas and "refs" not in groups:
            raise ValueError("refresh requires an Issue, PR, commit, or refs fact set")
        await self._wait_until(target_at)
        identity = {
            "repository": self.config.repository,
            "request": request,
            "target_at": _iso(target_at),
        }
        job_key = f"refresh:{json_digest(identity)}"
        async with (
            archive_lock(self.config.destination),
            SQLiteArchive(self.config.destination, self.config.repository) as archive,
        ):
            pending = await archive.pending_fact_job()
            if pending is not None:
                _match_pending(pending, "refresh", request, target if target is not None else None)
                await self._wait_until(_parse_time(pending.target_at))
                return await self._run_job(archive, pending, batch_size)
            await archive.ensure_maintenance_ready()
            existing = await archive.fact_job_by_key(job_key)
            if existing is not None:
                return _result(existing)
            scope, tasks = await self._refresh_plan(
                archive,
                target_at,
                request,
                pull_numbers,
                issue_numbers,
                shas,
                groups,
            )
            job = await archive.start_fact_job(
                job_key,
                "refresh",
                _iso(target_at),
                _iso(requested_at),
                scope,
                tasks,
            )
            return await self._run_job(archive, job, batch_size)

    async def _refresh_plan(
        self,
        archive: SQLiteArchive,
        target_at: datetime,
        request: dict[str, Any],
        pulls: Sequence[int],
        issues: Sequence[int],
        commits: Sequence[str],
        groups: Sequence[str],
    ) -> tuple[dict[str, Any], tuple[FactTaskSpec, ...]]:
        parents = {number: await archive.fact_parent(number) for number in sorted({*pulls, *issues})}
        for number in pulls:
            if parents[number].kind != "pull":
                raise ValueError(f"#{number} is an Issue, not a PR")
        for number in issues:
            if parents[number].kind != "issue":
                raise ValueError(f"#{number} is a PR, not an Issue")
        tasks: list[FactTaskSpec] = []
        if "reviews" in groups:
            tasks.extend(
                FactTaskSpec(
                    "review-threads",
                    f"pull:{number}",
                    number,
                    parents[number].bundle_digest,
                )
                for number in pulls
            )
        if "relations" in groups:
            tasks.extend(
                FactTaskSpec(
                    "issue-relations",
                    f"issue:{number}",
                    number,
                    parents[number].bundle_digest,
                )
                for number in issues
            )
        if "commits" in groups:
            for number, parent in sorted(parents.items()):
                if parent.bundle_digest is not None:
                    tasks.append(
                        FactTaskSpec(
                            "commit-references",
                            f"bundle:{parent.bundle_digest}",
                            number,
                            parent.bundle_digest,
                        ),
                    )
                if parent.kind == "pull":
                    current = await archive.current_fact_payload(
                        "review-threads",
                        f"pull:{number}",
                    )
                    if current is not None:
                        digest, _ = current
                        tasks.append(
                            FactTaskSpec(
                                "commit-references",
                                f"review-threads:{digest}",
                                number,
                                digest,
                            ),
                        )
            tasks.extend(FactTaskSpec("commit-object", f"commit:{sha}", None, None) for sha in commits)
        if "refs" in groups:
            tasks.append(FactTaskSpec("git-refs", "repository", None, None))
        distinct = {(task.fact_kind, task.subject_key): task for task in tasks}
        frozen = tuple(distinct[key] for key in sorted(distinct))
        if not frozen:
            raise ValueError("selected fact sets do not apply to the requested targets")
        scope = {
            "operation": "SupplementalFactRefresh",
            "repository": self.config.repository,
            "target_at": _iso(target_at),
            "fact_sets": _fact_versions(groups),
            "request": request,
            "population": {
                "digest": json_digest(
                    [
                        [
                            task.fact_kind,
                            task.subject_key,
                            task.resource_number,
                            task.source_digest,
                        ]
                        for task in frozen
                    ],
                ),
                "total": len(frozen),
            },
        }
        return scope, frozen

    async def _run_job(
        self,
        archive: SQLiteArchive,
        job: FactJob,
        batch_size: int,
    ) -> MaintenanceResult:
        if job.status == "complete":
            return _result(job)
        target_at = _parse_time(job.target_at)
        progress = _PullProgressTracker(target_at, self._observer, self._now)
        api, owned = self._puller._make_api(progress)
        git = self._puller._make_git()
        try:
            while job.status == "pending":
                tasks = await archive.take_fact_tasks(job.id, batch_size)
                if not tasks:
                    raise RuntimeError(f"fact job {job.id} has no runnable task")
                progress.phase(
                    f"{job.kind}_facts",
                    detail=f"job={job.id} completed={job.completed_tasks}/{job.total_tasks}",
                )
                try:
                    results = await self._task_results(archive, tasks, api, git, progress)
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    message = f"{type(exc).__name__}: {exc}"
                    for task in tasks:
                        await archive.record_fact_task_error(job.id, task.id, message)
                    progress.error(exc)
                    raise
                job = await archive.publish_fact_batch(job.id, _iso(self._now()), results)
            progress.phase("done", detail=f"fact_job={job.id}")
            return _result(job)
        finally:
            if owned:
                await api.close()

    async def _task_results(
        self,
        archive: SQLiteArchive,
        tasks: Sequence[FactTask],
        api: Any,
        git: Any,
        progress: _PullProgressTracker,
    ) -> tuple[StagedTaskFact, ...]:
        facts: dict[int, list[StagedFact]] = {task.id: [] for task in tasks}
        sources: list[tuple[int, CommitReferenceSource]] = []
        parent_sources: dict[int, CommitReferenceSource] = {}

        async def read_parent(task: FactTask) -> None:
            try:
                if task.fact_kind == "review-threads":
                    fact, _, _ = await self._puller._review_threads_fact(
                        api,
                        _resource_number(task),
                    )
                    fact = replace(fact, source_digest=task.source_digest)
                    facts[task.id].append(fact)
                    if fact.status == "complete":
                        digest = json_digest(fact.payload)
                        parent_sources[task.id] = CommitReferenceSource(
                            "review-threads",
                            digest,
                            task.resource_number,
                            review_thread_commit_references(fact.payload),
                        )
                elif task.fact_kind == "issue-relations":
                    fact = await self._puller._issue_relations_fact(
                        api,
                        _resource_number(task),
                    )
                    facts[task.id].append(replace(fact, source_digest=task.source_digest))
            except Exception as exc:
                facts[task.id].append(_failed_task_fact(task, self._now(), exc))

        parent_tasks = [task for task in tasks if task.fact_kind in {"review-threads", "issue-relations"}]
        await asyncio.gather(*(read_parent(task) for task in parent_tasks))
        for task in tasks:
            if task.id in parent_sources:
                sources.append((task.id, parent_sources[task.id]))
            if task.fact_kind == "commit-references":
                try:
                    sources.append((task.id, await self._source(archive, task)))
                except Exception as exc:
                    facts[task.id].append(_failed_task_fact(task, self._now(), exc))
            elif task.fact_kind == "git-refs":
                try:
                    facts[task.id].append(
                        await self._puller._git_refs_fact(git, progress),
                    )
                except Exception as exc:
                    facts[task.id].append(_failed_task_fact(task, self._now(), exc))
        source_objects: dict[str, StagedFact] = {}
        if sources:
            derived = await self._puller._commit_source_facts(
                git,
                progress,
                [source for _, source in sources],
            )
            owners = {f"{source.kind}:{source.digest}": task_id for task_id, source in sources}
            for fact in derived:
                if fact.fact_kind == "commit-references":
                    facts[owners[fact.subject_key]].append(fact)
                    continue
                source_objects[fact.subject_key] = fact
                reference = fact.payload["referenced_by"][0]
                owner = owners[f"{reference['source_payload_kind']}:{reference['source_payload_digest']}"]
                facts[owner].append(fact)
        direct = [task for task in tasks if task.fact_kind == "commit-object"]
        missing = [
            task.subject_key.removeprefix("commit:") for task in direct if task.subject_key not in source_objects
        ]
        retained: dict[str, dict[str, Any]] = {}
        observed_from = _iso(self._now())
        if missing:
            try:
                retained = await git.retain_commits(
                    missing,
                    heartbeat=progress.git_heartbeat,
                    retry=progress.git_retry,
                )
            except GitStoreError as exc:
                retained = {
                    sha: {
                        "sha": sha,
                        "status": "failed",
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    }
                    for sha in missing
                }
        observed_until = _iso(self._now())
        for task in direct:
            sha = task.subject_key.removeprefix("commit:")
            fact = source_objects.get(task.subject_key)
            if fact is None:
                fact = _commit_object_fact(
                    self.config.repository,
                    sha,
                    retained[sha],
                    observed_from,
                    observed_until,
                )
            facts[task.id].append(
                replace(
                    fact,
                    resource_number=task.resource_number,
                    source_digest=task.source_digest,
                ),
            )
        return tuple(StagedTaskFact(task.id, fact) for task in tasks for fact in facts[task.id])

    async def _source(
        self,
        archive: SQLiteArchive,
        task: FactTask,
    ) -> CommitReferenceSource:
        if task.source_digest is None:
            raise ValueError(f"{task.subject_key} has no source payload")
        kind, digest = task.subject_key.split(":", 1)
        if digest != task.source_digest:
            raise ValueError(f"{task.subject_key} does not match its source payload")
        payload = await archive.fact_payload(digest)
        if kind == "bundle":
            references = bundle_commit_references(payload)
        elif kind == "review-threads":
            references = review_thread_commit_references(payload)
        else:
            raise ValueError(f"unsupported commit source {kind}")
        return CommitReferenceSource(kind, digest, task.resource_number, references)

    async def _wait_until(self, target: datetime) -> None:
        delay = (target - _as_utc(self._now())).total_seconds()
        while delay > 0:
            await self._sleep(delay)
            delay = (target - _as_utc(self._now())).total_seconds()


def _groups(values: Iterable[str]) -> tuple[str, ...]:
    selected = tuple(dict.fromkeys(values))
    unknown = set(selected).difference(_FACT_GROUPS)
    if not selected or unknown:
        suffix = "" if not unknown else f": {', '.join(sorted(unknown))}"
        raise ValueError(f"invalid fact groups{suffix}")
    return tuple(group for group in FACT_GROUPS if group in selected)


def _fact_versions(groups: Iterable[str]) -> dict[str, int]:
    return {kind: version for group in groups for kind, version in _FACT_GROUPS[group].items()}


def _numbers(values: Iterable[int]) -> tuple[int, ...]:
    selected = tuple(sorted(set(values)))
    if any(number < 1 for number in selected):
        raise ValueError("Issue and PR numbers must be positive")
    return selected


def _commits(values: Iterable[str]) -> tuple[str, ...]:
    selected = tuple(sorted(set(values)))
    if any(len(sha) not in {40, 64} or any(char not in "0123456789abcdef" for char in sha) for sha in selected):
        raise ValueError("commit IDs must be 40 or 64 lowercase hexadecimal characters")
    return selected


def _batch_size(value: int) -> None:
    if value < 1:
        raise ValueError("batch_size must be positive")


def _resource_number(task: FactTask) -> int:
    if task.resource_number is None:
        raise ValueError(f"{task.fact_kind} task has no parent number")
    return task.resource_number


def _match_pending(
    job: FactJob,
    kind: str,
    request: dict[str, Any],
    explicit_target: datetime | None,
) -> None:
    if job.kind != kind or job.scope.get("request") != request:
        raise RuntimeError(f"fact job {job.id} must finish first")
    if explicit_target is not None and job.target_at != _iso(_as_utc(explicit_target)):
        raise RuntimeError(f"fact job {job.id} has a different target")


def _failed_task_fact(
    task: FactTask,
    now: datetime,
    error: Exception,
) -> StagedFact:
    observed = _iso(now)
    return StagedFact(
        fact_kind=task.fact_kind,
        schema_version=1,
        subject_key=task.subject_key,
        resource_number=task.resource_number,
        source_digest=task.source_digest,
        observed_from=observed,
        observed_until=observed,
        status="failed",
        payload={
            "operation": "SupplementalFactAttempt",
            "error": {"type": type(error).__name__, "message": str(error)},
        },
    )


def _commit_object_fact(
    repository: str,
    sha: str,
    retention: dict[str, Any],
    observed_from: str,
    observed_until: str,
) -> StagedFact:
    status = "complete" if retention["status"] == "available" else retention["status"]
    return StagedFact(
        fact_kind="commit-object",
        schema_version=1,
        subject_key=f"commit:{sha}",
        resource_number=None,
        source_digest=None,
        observed_from=observed_from,
        observed_until=observed_until,
        status=status,
        payload={
            "operation": "GitCommitRetention",
            "repository": repository,
            "result": retention,
            "referenced_by": [],
        },
    )


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored target must be timezone-aware")
    return parsed.astimezone(UTC)


def _result(job: FactJob) -> MaintenanceResult:
    if job.status != "complete" or job.completed_at is None:
        raise RuntimeError(f"fact job {job.id} is incomplete")
    return MaintenanceResult(
        job_id=job.id,
        job_key=job.job_key,
        kind=job.kind,
        target_at=_parse_time(job.target_at),
        completed_at=_parse_time(job.completed_at),
        total_tasks=job.total_tasks,
        completed_tasks=job.completed_tasks,
    )
