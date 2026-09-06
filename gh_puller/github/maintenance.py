"""Run recoverable targeted observations without advancing discovery state.

Maintenance jobs share the normal syncer's source operations and immutable fact
stream. Their request scope and tasks are durable, while retryable transport errors
remain task-attempt state rather than false source observations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .commit_references import commit_reference_scope
from .git_store import GitStoreError
from .locking import archive_lock
from .observations import (
    Coverage,
    FactDraft,
    MaintenanceJob,
    MaintenanceTask,
    ObservationArchive,
    Origin,
    TaskDraft,
)
from .progress import _SyncProgressTracker
from .syncer import (
    GitHubSyncConfig,
    GitHubSyncer,
    _error_text,
    _utc,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from .progress import ProgressObserver

REFRESH_FAMILIES = (
    "commit-object",
    "git-refs",
    "issue-relations",
    "pull-review-threads",
)
_COMMIT_TASK_SIZE = 256


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    """Summarize one completed explicit observation job."""

    job_id: int
    job_key: str
    kind: str
    requested_at: datetime
    completed_at: datetime
    total_tasks: int
    completed_tasks: int
    requests: int


class GitHubMaintainer:
    """Execute explicit, discovery-independent source observations.

    Args:
        config: Repository, archive destinations, and request policy.
        api: Test or host-provided GitHub reader.
        git: Test or host-provided Git object store.
        now: Timezone-aware clock for actual attempts and observations.
        observer: Disposable out-of-band progress receiver.
    """

    def __init__(
        self,
        config: GitHubSyncConfig,
        *,
        api: Any | None = None,
        git: Any | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        observer: ProgressObserver | None = None,
    ) -> None:
        self.config = config
        self._now = now
        self._syncer = GitHubSyncer(
            config,
            api=api,
            git=git,
            now=now,
            observer=observer,
        )
        self._observer = observer

    async def refresh(
        self,
        *,
        pulls: Iterable[int] = (),
        issues: Iterable[int] = (),
        commits: Iterable[str] = (),
        families: Iterable[str] | None = None,
        idempotency_key: str | None = None,
    ) -> MaintenanceResult:
        """Observe selected research objects even without discovery signals.

        Args:
            pulls: Archived PR numbers whose selected facts are reread.
            issues: Archived Issue numbers whose selected facts are reread.
            commits: Exact commit IDs to reacquire and verify.
            families: Selected R1-R4 fact families; None selects every applicable
                family for the supplied targets.
            idempotency_key: Optional caller identity. Reuse returns or resumes the
                same job; omission resumes a matching active job but creates a fresh
                observation after prior completion.

        Returns:
            Completed durable job metadata.

        Raises:
            KeyError: A selected Issue or PR has not been archived.
            RuntimeError: Another maintenance request remains active.
            ValueError: Targets and selected families are inconsistent.
        """
        request = _refresh_request(pulls, issues, commits, families)
        requested_at = _utc(self._now())
        async with (
            archive_lock(self.config.destination),
            ObservationArchive(
                self.config.destination,
                self.config.repository,
                self._syncer._git_destination(),
            ) as archive,
        ):
            active = await archive.active_maintenance_job()
            if active is not None:
                if active.kind != "refresh" or active.scope.get("request") != request:
                    raise RuntimeError(f"maintenance job {active.id} must finish first")
                if idempotency_key is not None and active.job_key != _caller_key(
                    "refresh",
                    idempotency_key,
                ):
                    raise RuntimeError(f"maintenance job {active.id} has another idempotency key")
                job = active
            else:
                job_key = (
                    _caller_key("refresh", idempotency_key)
                    if idempotency_key is not None
                    else f"refresh:{_timestamp(requested_at)}:{uuid.uuid4().hex}"
                )
                existing = await archive.maintenance_job_by_key(job_key)
                if existing is not None:
                    if existing.kind != "refresh" or existing.scope.get("request") != request:
                        raise ValueError("idempotency key belongs to another refresh request")
                    return _result(existing)
                tasks = await self._refresh_tasks(archive, request)
                fact_schemas = {
                    family: 2 if family == "commit-object" else 1
                    for family in request["families"]
                }
                if "pull-review-threads" in request["families"]:
                    fact_schemas |= {"commit-references": 1, "commit-object": 2}
                scope = {
                    "operation": "TargetedFactRefresh",
                    "repository": self.config.repository,
                    "request": request,
                    "fact_schemas": fact_schemas,
                    "population": {
                        "digest": _task_digest(tasks),
                        "total": len(tasks),
                    },
                }
                job = await archive.start_maintenance_job(
                    job_key,
                    "refresh",
                    requested_at,
                    scope,
                    tasks,
                )
            return await self._run(archive, job)

    async def backfill(
        self,
        *,
        idempotency_key: str | None = None,
    ) -> MaintenanceResult:
        """Verify every structured commit in a frozen published fact range.

        Args:
            idempotency_key: Optional caller identity. Reuse returns or resumes the
                same frozen job; omission resumes the active backfill or creates a
                new baseline at the current observation cutoff.

        Returns:
            Completed baseline metadata. Terminal unavailable and partial results
            count as checked tasks but remain distinguishable facts.

        Raises:
            RuntimeError: A different maintenance job remains active.
            ValueError: A caller key belongs to another immutable scope.
        """
        requested_at = _utc(self._now())
        request = {"families": ["commit-object"]}
        async with (
            archive_lock(self.config.destination),
            ObservationArchive(
                self.config.destination,
                self.config.repository,
                self._syncer._git_destination(),
            ) as archive,
        ):
            active = await archive.active_maintenance_job()
            if active is not None:
                if active.kind != "backfill" or active.scope.get("request") != request:
                    raise RuntimeError(f"maintenance job {active.id} must finish first")
                if idempotency_key is not None and active.job_key != _caller_key(
                    "backfill",
                    idempotency_key,
                ):
                    raise RuntimeError(f"maintenance job {active.id} has another idempotency key")
                job = active
            else:
                if idempotency_key is not None:
                    existing = await archive.maintenance_job_by_key(
                        _caller_key("backfill", idempotency_key),
                    )
                    if existing is not None:
                        if existing.kind != "backfill" or existing.scope.get("request") != request:
                            raise ValueError("idempotency key belongs to another backfill request")
                        return _result(existing)
                cutoff = await archive.observation_cutoff()
                ordered_references = await archive.referenced_commits(cutoff)
                referenced = set(ordered_references)
                covered = await archive.checked_commits(cutoff)
                pending = tuple(sha for sha in ordered_references if sha not in covered)
                tasks = _commit_tasks(pending, cutoff)
                scope = {
                    "operation": "StructuredCommitBackfill",
                    "repository": self.config.repository,
                    "request": request,
                    "observation_cutoff": cutoff,
                    "source_schema": {"commit-references": 1},
                    "output_schema": {"commit-object": 2},
                    "referenced_commits": len(referenced),
                    "preexisting_results": len(referenced & covered),
                    "population": {
                        "digest": _task_digest(tasks),
                        "subjects": len(pending),
                        "total": len(tasks),
                    },
                }
                job_key = (
                    _caller_key("backfill", idempotency_key)
                    if idempotency_key is not None
                    else f"backfill:{cutoff}:{_json_digest(scope)}"
                )
                existing = await archive.maintenance_job_by_key(job_key)
                if existing is not None:
                    return _result(existing)
                job = await archive.start_maintenance_job(
                    job_key,
                    "backfill",
                    requested_at,
                    scope,
                    tasks,
                )
            return await self._run(archive, job)

    async def _refresh_tasks(
        self,
        archive: ObservationArchive,
        request: dict[str, Any],
    ) -> tuple[TaskDraft, ...]:
        families = set(request["families"])
        tasks = []
        for number in request["pulls"]:
            await _require_parent(archive, number, "pull")
            if "pull-review-threads" in families:
                tasks.append(
                    TaskDraft(
                        f"pull-review-threads:{number}",
                        "pull-review-threads",
                        f"pull:{number}",
                        {"number": number},
                        number,
                    ),
                )
        for number in request["issues"]:
            await _require_parent(archive, number, "issue")
            if "issue-relations" in families:
                tasks.append(
                    TaskDraft(
                        f"issue-relations:{number}",
                        "issue-relations",
                        f"issue:{number}",
                        {"number": number},
                        number,
                    ),
                )
        if "commit-object" in families:
            cutoff = await archive.observation_cutoff()
            tasks.extend(
                _commit_tasks(request["commits"], cutoff),
            )
        if "git-refs" in families:
            tasks.append(TaskDraft("git-refs", "git-refs", "repository", {}))
        if not tasks:
            raise ValueError("selected fact families do not apply to any target")
        return tuple(tasks)

    async def _reference_index(
        self,
        archive: ObservationArchive,
        cutoff: int,
        selected_shas: set[str] | None = None,
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        references: dict[str, list[dict[str, Any]]] = {}
        async for item in archive.iter_commit_references(cutoff, selected_shas):
            sha = _sha(item.get("sha"))
            references.setdefault(sha, []).append(item)
        return {
            sha: tuple(_distinct_objects(items))
            for sha, items in sorted(references.items())
        }

    async def _run(
        self,
        archive: ObservationArchive,
        job: MaintenanceJob,
    ) -> MaintenanceResult:
        if job.status == "complete":
            return _result(job)
        progress = _SyncProgressTracker(self._observer, self._now)
        self._syncer._progress = progress
        progress.start()
        api, owned = self._syncer._make_api()
        git = self._syncer._make_git()
        request_start = api.request_count
        accounted_at = request_start
        progress.bind_maintenance(job.id, job.request_count, request_start)
        try:
            while job.status == "active":
                started_at = _utc(self._now())
                tasks = await archive.take_maintenance_tasks(
                    job.id,
                    self.config.concurrency,
                    started_at,
                )
                if not tasks:
                    raise RuntimeError(f"maintenance job {job.id} has no pending task")
                progress.phase(
                    f"{job.kind}_facts",
                    f"job={job.id} completed={job.completed_tasks}/{job.total_tasks}",
                )
                errors = await asyncio.gather(
                    *(self._guard_task(api, git, archive, task) for task in tasks),
                )
                await archive.add_maintenance_requests(
                    job.id,
                    api.request_count - accounted_at,
                )
                accounted_at = api.request_count
                failures = [error for error in errors if error is not None]
                if failures:
                    raise failures[0]
                job = await archive.maintenance_job(job.id)
            progress.done(job.request_count)
            return _result(job)
        except Exception as exc:
            progress.error(exc)
            raise
        finally:
            if api.request_count != accounted_at:
                await archive.add_maintenance_requests(
                    job.id,
                    api.request_count - accounted_at,
                )
            if owned:
                await api.close()

    async def _guard_task(
        self,
        api: Any,
        git: Any,
        archive: ObservationArchive,
        task: MaintenanceTask,
    ) -> Exception | None:
        try:
            outcome = await self._execute_task(api, git, archive, task)
            async with self._syncer._store_lock:
                await archive.complete_maintenance_task(task.id, _utc(self._now()), outcome)
        except Exception as exc:
            async with self._syncer._store_lock:
                await archive.record_maintenance_task_error(
                    task.id,
                    _utc(self._now()),
                    _error_text(exc),
                )
            return exc
        return None

    async def _execute_task(
        self,
        api: Any,
        git: Any,
        archive: ObservationArchive,
        task: MaintenanceTask,
    ) -> Coverage:
        if task.kind == "pull-review-threads":
            fact = await self._syncer._review_threads(api, archive, task)
            if fact.coverage is not Coverage.COMPLETE:
                return fact.coverage
            references = await self._syncer._structured_commits(archive, task, [fact])
            return await self._retain_commits(git, archive, task, references)
        if task.kind == "issue-relations":
            return (await self._syncer._issue_relations(api, archive, task)).coverage
        if task.kind == "git-refs":
            await self._syncer._git_refs(git, archive, task)
            return Coverage.COMPLETE
        if task.kind == "commit-object-batch":
            cutoff, shas = _commit_task_scope(task)
            references = await self._reference_index(archive, cutoff, set(shas))
            return await self._retain_commits(
                git,
                archive,
                task,
                {sha: references.get(sha, ()) for sha in shas},
            )
        raise RuntimeError(f"unknown maintenance task kind: {task.kind}")

    async def _retain_commits(
        self,
        git: Any,
        archive: ObservationArchive,
        task: MaintenanceTask,
        references: dict[str, tuple[dict[str, Any], ...]],
    ) -> Coverage:
        shas = tuple(references)
        publication = await self._syncer._publication(archive, task, "commit-objects")
        if publication is not None:
            return _aggregate_coverage([fact.coverage for fact in publication])
        if not shas:
            return Coverage.COMPLETE
        self._syncer._progress.phase("syncing_git", f"commits={len(shas)}")
        observed_from = _utc(self._now())
        source_tasks = [
            replace(
                task,
                task_key=f"{task.task_key}:{sha}",
                payload={"sha": sha, "references": list(references[sha])},
            )
            for sha in shas
        ]
        results = await git.retain_commits(
            shas,
            sources=await self._syncer._commit_fetch_sources(archive, source_tasks),
            heartbeat=self._syncer._progress.git_heartbeat,
            retry=self._syncer._progress.git_retry,
        )
        observed_until = _utc(self._now())
        facts = []
        for sha in shas:
            result = results.get(sha)
            if not isinstance(result, dict):
                raise GitStoreError(f"Git retention returned no result for {sha}")
            status = result.get("status")
            if status == "available":
                coverage = Coverage.COMPLETE
            elif status == "partial":
                coverage = Coverage.PARTIAL
            elif status == "unavailable":
                coverage = Coverage.UNAVAILABLE
            else:
                raise GitStoreError(f"Git retention returned invalid status for {sha}")
            facts.append(
                FactDraft(
                    family="commit-object",
                    schema_version=2,
                    subject_key=f"commit:{sha}",
                    resource_number=task.resource_number,
                    observed_from=observed_from,
                    observed_until=observed_until,
                    coverage=coverage,
                    origin=Origin.GIT,
                    payload={
                        "operation": "GitCommitReconstruction",
                        "repository": self.config.repository,
                        "reference_scope": commit_reference_scope(references[sha]),
                        "sha": sha,
                        "value": result,
                    },
                ),
            )
        publication = await self._syncer._publish(
            archive,
            task,
            "commit-objects",
            tuple(facts),
        )
        return _aggregate_coverage([fact.coverage for fact in publication])


async def refresh(
    config: GitHubSyncConfig,
    *,
    pulls: Iterable[int] = (),
    issues: Iterable[int] = (),
    commits: Iterable[str] = (),
    families: Iterable[str] | None = None,
    idempotency_key: str | None = None,
    observer: ProgressObserver | None = None,
) -> MaintenanceResult:
    """Run or resume one targeted observation job.

    Args:
        config: Repository, archive destinations, and request policy.
        pulls: Archived PR numbers to refresh.
        issues: Archived Issue numbers to refresh.
        commits: Commit IDs to retry.
        families: Selected fact families; None selects applicable defaults.
        idempotency_key: Optional stable caller identity.
        observer: Disposable out-of-band progress receiver.
    """
    return await GitHubMaintainer(config, observer=observer).refresh(
        pulls=pulls,
        issues=issues,
        commits=commits,
        families=families,
        idempotency_key=idempotency_key,
    )


async def backfill(
    config: GitHubSyncConfig,
    *,
    idempotency_key: str | None = None,
    observer: ProgressObserver | None = None,
) -> MaintenanceResult:
    """Run or resume a frozen structured-commit baseline.

    Args:
        config: Repository, archive destinations, and request policy.
        idempotency_key: Optional stable caller identity.
        observer: Disposable out-of-band progress receiver.
    """
    return await GitHubMaintainer(config, observer=observer).backfill(
        idempotency_key=idempotency_key,
    )


async def _require_parent(
    archive: ObservationArchive,
    number: int,
    expected: str,
) -> None:
    fact = await archive.current_fact("issue", f"issue:{number}")
    if fact is None or fact.coverage is not Coverage.COMPLETE:
        raise KeyError(f"#{number} has no complete archived root")
    value = fact.payload.get("value")
    actual = "pull" if isinstance(value, dict) and "pull_request" in value else "issue"
    if actual != expected:
        raise ValueError(f"#{number} is an {actual}, not a {expected}")


def _refresh_request(
    pulls: Iterable[int],
    issues: Iterable[int],
    commits: Iterable[str],
    families: Iterable[str] | None,
) -> dict[str, Any]:
    pull_numbers = _numbers(pulls)
    issue_numbers = _numbers(issues)
    shas = tuple(sorted({_sha(value) for value in commits}))
    if set(pull_numbers) & set(issue_numbers):
        raise ValueError("one parent cannot be selected as both Issue and PR")
    if families is None:
        selected = set()
        if pull_numbers:
            selected.add("pull-review-threads")
        if issue_numbers:
            selected.add("issue-relations")
        if shas:
            selected.add("commit-object")
    else:
        selected = set(families)
        unknown = selected - set(REFRESH_FAMILIES)
        if unknown:
            raise ValueError(f"unknown refresh families: {', '.join(sorted(unknown))}")
    if not pull_numbers and not issue_numbers and not shas and "git-refs" not in selected:
        raise ValueError("refresh requires an Issue, PR, commit, or git-refs")
    return {
        "commits": list(shas),
        "families": sorted(selected),
        "issues": list(issue_numbers),
        "pulls": list(pull_numbers),
    }


def _numbers(values: Iterable[int]) -> tuple[int, ...]:
    selected = tuple(sorted(set(values)))
    if any(type(number) is not int or number < 1 for number in selected):
        raise ValueError("Issue and PR numbers must be positive integers")
    return selected


def _sha(value: object) -> str:
    if not isinstance(value, str) or len(value) not in {40, 64}:
        raise ValueError("commit IDs must contain 40 or 64 lowercase hexadecimal digits")
    if any(character not in "0123456789abcdef" for character in value):
        raise ValueError("commit IDs must contain 40 or 64 lowercase hexadecimal digits")
    return value


def _caller_key(kind: str, value: str) -> str:
    if not value or len(value) > 200:
        raise ValueError("idempotency key must contain 1 to 200 characters")
    return f"{kind}:caller:{value}"


def _commit_tasks(
    shas: Sequence[str],
    cutoff: int,
) -> tuple[TaskDraft, ...]:
    if cutoff < 0:
        raise ValueError("observation cutoff cannot be negative")
    ordered = tuple(_sha(sha) for sha in shas)
    if len(ordered) != len(set(ordered)):
        raise ValueError("commit task population must be unique")
    tasks = []
    for offset in range(0, len(ordered), _COMMIT_TASK_SIZE):
        payload = {
            "observation_cutoff": cutoff,
            "shas": list(ordered[offset : offset + _COMMIT_TASK_SIZE]),
        }
        digest = _json_digest(payload)
        tasks.append(
            TaskDraft(
                f"commit-object-batch:{offset // _COMMIT_TASK_SIZE:08d}:{digest}",
                "commit-object-batch",
                f"commits:{digest}",
                payload,
            ),
        )
    return tuple(tasks)


def _commit_task_scope(
    task: MaintenanceTask,
) -> tuple[int, tuple[str, ...]]:
    cutoff = task.payload.get("observation_cutoff")
    values = task.payload.get("shas")
    if type(cutoff) is not int or cutoff < 0 or not isinstance(values, list):
        raise TypeError(f"maintenance task {task.task_key} has invalid commit scope")
    shas = tuple(_sha(value) for value in values)
    if not shas or len(shas) != len(set(shas)):
        raise ValueError(f"maintenance task {task.task_key} has invalid commit population")
    return cutoff, shas


def _task_digest(tasks: Sequence[TaskDraft]) -> str:
    return _json_digest(
        [
            [task.task_key, task.kind, task.subject_key, task.resource_number, task.payload]
            for task in tasks
        ],
    )


def _json_digest(value: object) -> str:
    raw = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _distinct_objects(values: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result = {}
    for value in values:
        result.setdefault(_json_digest(value), value)
    return list(result.values())


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _aggregate_coverage(values: Sequence[Coverage]) -> Coverage:
    if not values:
        return Coverage.COMPLETE
    rank = {
        Coverage.COMPLETE: 0,
        Coverage.NULL: 0,
        Coverage.PARTIAL: 1,
        Coverage.FORBIDDEN: 2,
        Coverage.UNAVAILABLE: 3,
    }
    return max(values, key=rank.__getitem__)


def _result(job: MaintenanceJob) -> MaintenanceResult:
    if job.status != "complete" or job.completed_at is None:
        raise RuntimeError(f"maintenance job {job.id} is incomplete")
    return MaintenanceResult(
        job_id=job.id,
        job_key=job.job_key,
        kind=job.kind,
        requested_at=job.requested_at,
        completed_at=job.completed_at,
        total_tasks=job.total_tasks,
        completed_tasks=job.completed_tasks,
        requests=job.request_count,
    )
