"""Run recoverable targeted observations without advancing discovery state.

Refresh callers select semantic fact families while the durable plan owns source
dependencies and structured-commit consequences. Jobs share the normal syncer's
source operations and immutable fact stream. A structured-commit baseline freezes
raw source observations, closes missing derived scans, then verifies their unique Git
objects. Retryable transport errors remain task-attempt state rather than false source
observations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .collector import (
    GitHubFactCollector,
    IncompleteGitHubDataError,
    _error_text,
    _utc,
)
from .commit_references import (
    COMMIT_REFERENCE_SOURCE_FAMILIES,
    commit_reference_payload,
    observation_commit_references,
)
from .observations import (
    Coverage,
    FactObservation,
    MaintenanceJob,
    MaintenanceTask,
    ObservationArchive,
    TaskDraft,
)
from .progress import _SyncProgressTracker
from .runtime import GitHubRuntime, archive_lock

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from .progress import ProgressObserver
    from .syncer import GitHubSyncConfig

_COMMIT_TASK_SIZE = 256
_REFERENCE_SCAN_TASK_SIZE = 256
_COMMON_PARENT_FAMILIES = frozenset(
    {
        "commit-object",
        "commit-references",
        "issue",
        "issue-comment-reactions",
        "issue-comments",
        "issue-events",
        "issue-reactions",
        "issue-timeline",
    },
)
ISSUE_FAMILIES = _COMMON_PARENT_FAMILIES | {"issue-relations"}
PULL_FAMILIES = _COMMON_PARENT_FAMILIES | {
    "pull",
    "pull-closing-issues",
    "pull-commits",
    "pull-git",
    "pull-requested-reviewers",
    "pull-review-comment-reactions",
    "pull-review-comments",
    "pull-review-threads",
    "pull-reviews",
}
REFRESH_FAMILIES = tuple(sorted(ISSUE_FAMILIES | PULL_FAMILIES | {"git-refs"}))
_FAMILY_DEPENDENCIES = {
    "commit-object": frozenset({"commit-references"}),
    "issue-comment-reactions": frozenset({"issue-comments"}),
    "pull-commits": frozenset({"pull"}),
    "pull-git": frozenset({"pull"}),
    "pull-requested-reviewers": frozenset({"pull"}),
    "pull-review-comment-reactions": frozenset({"pull-review-comments"}),
    "pull-review-comments": frozenset({"pull", "pull-review-threads"}),
}


@dataclass(frozen=True, slots=True)
class FamilyPlan:
    """Hold one target kind's requested and dependency-closed fact families."""

    requested: tuple[str, ...]
    effective: tuple[str, ...]
    reference_sources: tuple[str, ...]

    def payload(self) -> dict[str, list[str]]:
        """Return the stable representation stored in job and task scopes."""
        return {
            "requested_families": list(self.requested),
            "effective_families": list(self.effective),
            "reference_source_families": list(self.reference_sources),
        }


def refresh_request(
    pulls: Iterable[int],
    issues: Iterable[int],
    commits: Iterable[str],
    families: Iterable[str] | None,
) -> dict[str, Any]:
    """Normalize one caller selection into its durable request representation."""
    pull_numbers = _numbers(pulls)
    issue_numbers = _numbers(issues)
    shas = tuple(sorted({validate_commit_sha(value) for value in commits}))
    if set(pull_numbers) & set(issue_numbers):
        raise ValueError("one parent cannot be selected as both Issue and PR")
    if families is None:
        selected = set()
        if pull_numbers:
            selected.update(PULL_FAMILIES)
        if issue_numbers:
            selected.update(ISSUE_FAMILIES)
        if shas:
            selected.add("commit-object")
    else:
        selected = set(families)
        unknown = selected - set(REFRESH_FAMILIES)
        if unknown:
            raise ValueError(f"unknown refresh families: {', '.join(sorted(unknown))}")
    if not pull_numbers and not issue_numbers and not shas and "git-refs" not in selected:
        raise ValueError("refresh requires an Issue, PR, commit, or git-refs target")
    return {
        "commits": list(shas),
        "families": sorted(selected),
        "issues": list(issue_numbers),
        "pulls": list(pull_numbers),
    }


def refresh_plan(request: dict[str, Any]) -> dict[str, FamilyPlan]:
    """Build dependency-closed plans for every selected target kind."""
    selected = set(request["families"])
    matched = set()
    plan = {}
    for group, kind, applicable in (
        ("issues", "issue", ISSUE_FAMILIES),
        ("pulls", "pull", PULL_FAMILIES),
    ):
        if not request[group]:
            continue
        requested = selected & applicable
        if not requested:
            raise ValueError(f"selected families do not apply to {kind} targets")
        plan[group] = parent_family_plan(kind, requested)
        matched.update(requested)
    if request["commits"]:
        if "commit-object" not in selected:
            raise ValueError("commit targets require the commit-object family")
        plan["commits"] = _singular_plan("commit-object")
        matched.add("commit-object")
    if "git-refs" in selected:
        plan["repository"] = _singular_plan("git-refs")
        matched.add("git-refs")
    unmatched = selected - matched
    if unmatched:
        raise ValueError(
            f"selected families have no compatible target: {', '.join(sorted(unmatched))}",
        )
    if not plan:
        raise ValueError("refresh has no applicable target")
    return plan


def parent_family_plan(kind: str, requested: set[str]) -> FamilyPlan:
    """Close one Issue or PR family selection over its source dependencies."""
    applicable = ISSUE_FAMILIES if kind == "issue" else PULL_FAMILIES
    source_families = COMMIT_REFERENCE_SOURCE_FAMILIES & applicable
    effective = set(requested) | {"issue"}
    while True:
        dependencies = {
            dependency
            for family in effective
            for dependency in (
                source_families
                if family == "commit-references"
                else _FAMILY_DEPENDENCIES.get(family, ())
            )
        }
        expanded = effective | dependencies
        if expanded == effective:
            break
        effective = expanded
    reference_sources = effective & source_families
    if reference_sources:
        effective.update({"commit-object", "commit-references"})
    return FamilyPlan(
        tuple(sorted(requested)),
        tuple(sorted(effective)),
        tuple(sorted(reference_sources)),
    )


def parent_refresh_scope(
    task: MaintenanceTask,
) -> tuple[str, set[str], tuple[str, ...]]:
    """Validate and decode a persisted parent refresh plan."""
    kind = task.payload.get("kind")
    number = task.payload.get("number")
    requested = task.payload.get("requested_families")
    if (
        kind not in {"issue", "pull"}
        or type(number) is not int
        or number < 1
        or number != task.resource_number
        or not isinstance(requested, list)
        or not requested
        or any(not isinstance(family, str) for family in requested)
    ):
        raise RuntimeError(f"maintenance task {task.task_key} has invalid parent scope")
    expected = parent_family_plan(kind, set(requested))
    for field, value in expected.payload().items():
        if task.payload.get(field) != value:
            raise RuntimeError(f"maintenance task {task.task_key} has invalid family plan")
    return kind, set(expected.effective), expected.reference_sources


def _singular_plan(family: str) -> FamilyPlan:
    return FamilyPlan((family,), (family,), ())


def _numbers(values: Iterable[int]) -> tuple[int, ...]:
    selected = tuple(sorted(set(values)))
    if any(type(number) is not int or number < 1 for number in selected):
        raise ValueError("Issue and PR numbers must be positive integers")
    return selected


def validate_commit_sha(value: object) -> str:
    """Return one normalized full commit ID or reject the refresh input."""
    if not isinstance(value, str) or len(value) not in {40, 64}:
        raise ValueError("commit IDs must contain 40 or 64 lowercase hexadecimal digits")
    if any(character not in "0123456789abcdef" for character in value):
        raise ValueError("commit IDs must contain 40 or 64 lowercase hexadecimal digits")
    return value


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
        self._runtime = GitHubRuntime(
            config,
            api=api,
            git=git,
            now=now,
            sleep=asyncio.sleep,
        )
        self._observer = observer
        self._collector = GitHubFactCollector(
            config,
            now=now,
            progress=_SyncProgressTracker(observer, now),
            store_lock=self._runtime.store_lock,
        )

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
            families: Selected fact families. None selects every applicable family
                for each supplied target. Required source families are inferred.
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
        request = refresh_request(pulls, issues, commits, families)
        plan = refresh_plan(request)
        requested_at = _utc(self._now())
        async with (
            archive_lock(self.config.destination),
            ObservationArchive(
                self.config.destination,
                self.config.repository,
                self._runtime.git_destination,
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
                tasks = await self._refresh_tasks(archive, request, plan)
                planned_families = {
                    family
                    for target in plan.values()
                    for family in target.effective
                }
                fact_schemas = {
                    family: 2 if family == "commit-object" else 1
                    for family in planned_families
                }
                scope = {
                    "operation": "TargetedFactRefresh",
                    "repository": self.config.repository,
                    "request": request,
                    "plan": {
                        target: family_plan.payload()
                        for target, family_plan in plan.items()
                    },
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
        """Verify commits extracted from every raw fact in a frozen source range.

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
                self._runtime.git_destination,
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
                scans = await archive.commit_reference_scans(cutoff)
                source_population = hashlib.sha256()
                source_count = 0
                edge_count = 0
                empty_sources = 0
                missing_scans = []
                reference_order: dict[str, tuple[int, int, str]] = {}
                async for source in archive.iter_structured_commit_sources(cutoff):
                    source_count += 1
                    source_population.update(
                        json.dumps(
                            [source.id, source.family, source.payload_digest],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode(),
                    )
                    source_population.update(b"\n")
                    selected = observation_commit_references(
                        source.family,
                        source.payload,
                    )
                    edge_count += len(selected)
                    if not selected:
                        empty_sources += 1
                    signature = (
                        source.id,
                        source.family,
                        source.payload_digest,
                        _json_digest(
                            [commit_reference_payload(reference) for reference in selected],
                        ),
                    )
                    if signature not in scans:
                        missing_scans.append(source.id)
                    resource = (
                        source.resource_number
                        if source.resource_number is not None
                        else 2**63 - 1
                    )
                    for reference in selected:
                        order = (resource, source.id, reference.sha)
                        previous = reference_order.get(reference.sha)
                        if previous is None or order < previous:
                            reference_order[reference.sha] = order
                ordered_references = tuple(
                    sorted(reference_order, key=reference_order.__getitem__),
                )
                referenced = set(ordered_references)
                covered = await archive.checked_commits(cutoff)
                pending = tuple(sha for sha in ordered_references if sha not in covered)
                reference_tasks = _reference_scan_tasks(missing_scans, cutoff)
                commit_tasks = _commit_tasks(pending, cutoff)
                tasks = (*reference_tasks, *commit_tasks)
                scope = {
                    "operation": "StructuredCommitBackfill",
                    "repository": self.config.repository,
                    "request": request,
                    "source_observation_cutoff": cutoff,
                    "source_schema": dict.fromkeys(
                        sorted(COMMIT_REFERENCE_SOURCE_FAMILIES),
                        1,
                    ),
                    "derived_schema": {"commit-references": 1},
                    "output_schema": {"commit-object": 2},
                    "referenced_commits": len(referenced),
                    "preexisting_results": len(referenced & covered),
                    "source_population": {
                        "digest": source_population.hexdigest(),
                        "digest_algorithm": "sha256-json-lines-v1",
                        "observations": source_count,
                        "reference_edges": edge_count,
                        "empty_observations": empty_sources,
                        "preexisting_scans": source_count - len(missing_scans),
                        "pending_scans": len(missing_scans),
                    },
                    "population": {
                        "digest": _task_digest(tasks),
                        "subjects": len(pending),
                        "reference_scan_tasks": len(reference_tasks),
                        "commit_object_tasks": len(commit_tasks),
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
        plan: dict[str, FamilyPlan],
    ) -> tuple[TaskDraft, ...]:
        tasks = []
        for number in request["pulls"]:
            await _require_parent(archive, number, "pull")
            payload = {"kind": "pull", "number": number} | plan["pulls"].payload()
            tasks.append(
                TaskDraft(
                    f"parent-refresh:pull:{number}",
                    "parent-refresh",
                    f"pull:{number}",
                    payload,
                    number,
                ),
            )
        for number in request["issues"]:
            await _require_parent(archive, number, "issue")
            payload = {"kind": "issue", "number": number} | plan["issues"].payload()
            tasks.append(
                TaskDraft(
                    f"parent-refresh:issue:{number}",
                    "parent-refresh",
                    f"issue:{number}",
                    payload,
                    number,
                ),
            )
        if request["commits"]:
            cutoff = await archive.observation_cutoff()
            tasks.extend(
                _commit_tasks(request["commits"], cutoff),
            )
        if "repository" in plan:
            tasks.append(TaskDraft("git-refs", "git-refs", "repository", {}))
        return tuple(tasks)

    async def _reference_index(
        self,
        archive: ObservationArchive,
        source_cutoff: int,
        selected_shas: set[str] | None = None,
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        references: dict[str, list[dict[str, Any]]] = {}
        if selected_shas is None:
            raise ValueError("source-bounded references require exact commit targets")
        async for item in archive.iter_commit_references_by_source(
            source_cutoff,
            selected_shas,
        ):
            sha = validate_commit_sha(item.get("sha"))
            references.setdefault(sha, []).append(item)
        return {
            sha: tuple(_distinct_objects(items))
            for sha, items in sorted(references.items())
        }

    async def _refresh_parent(
        self,
        api: Any,
        git: Any,
        archive: ObservationArchive,
        task: MaintenanceTask,
    ) -> Coverage:
        kind, effective, reference_sources = parent_refresh_scope(task)
        facts: dict[str, FactObservation] = {}
        outcomes: dict[str, Coverage] = {}

        def record(family: str, fact: FactObservation) -> None:
            facts[family] = fact
            outcomes[family] = fact.coverage

        root = await self._collector.issue(api, archive, task, None)
        record("issue", root)
        if root.coverage is not Coverage.COMPLETE:
            return root.coverage
        value = root.payload.get("value")
        actual = "pull" if isinstance(value, dict) and "pull_request" in value else "issue"
        if actual != kind:
            raise IncompleteGitHubDataError(
                f"archived {kind} #{task.resource_number} is now reported as {actual}",
            )

        if "issue-comments" in effective:
            record(
                "issue-comments",
                await self._collector.issue_comments(
                    api,
                    archive,
                    task,
                    root,
                    force=False,
                ),
            )
        if "issue-timeline" in effective:
            record(
                "issue-timeline",
                await self._collector.issue_timeline(api, archive, task),
            )
        if "issue-events" in effective:
            record(
                "issue-events",
                await self._collector.issue_events(api, archive, task),
            )
        if "issue-reactions" in effective:
            record(
                "issue-reactions",
                await self._collector.issue_reactions(api, archive, task, root),
            )
        if "issue-comment-reactions" in effective:
            comments = facts["issue-comments"]
            if comments.coverage is Coverage.COMPLETE:
                reactions = await self._collector.comment_reactions(
                    api,
                    archive,
                    task,
                    comments,
                    endpoint="issues/comments",
                    family="issue-comment-reactions",
                    subject_prefix="issue-comment",
                )
                outcomes["issue-comment-reactions"] = _aggregate_coverage(
                    [fact.coverage for fact in reactions],
                )
            else:
                outcomes["issue-comment-reactions"] = comments.coverage
        if "issue-relations" in effective:
            record(
                "issue-relations",
                await self._collector.issue_relations(api, archive, task),
            )

        if kind == "pull":
            await self._refresh_pull_families(
                api,
                git,
                archive,
                task,
                effective,
                facts,
                outcomes,
            )

        references: dict[str, tuple[dict[str, Any], ...]] = {}
        if reference_sources:
            selected = [facts[family] for family in reference_sources if family in facts]
            references = await self._collector.structured_commits(archive, task, selected)
            source_coverage = _aggregate_coverage(
                [outcomes[family] for family in reference_sources],
            )
            outcomes["commit-references"] = source_coverage
            retention = await self._retain_commits(git, archive, task, references)
            outcomes["commit-object"] = _aggregate_coverage(
                [source_coverage, retention],
            )

        missing = effective - outcomes.keys()
        if missing:
            raise RuntimeError(
                f"maintenance task {task.task_key} left families unobserved: "
                f"{', '.join(sorted(missing))}",
            )
        return _aggregate_coverage([outcomes[family] for family in effective])

    async def _refresh_pull_families(
        self,
        api: Any,
        git: Any,
        archive: ObservationArchive,
        task: MaintenanceTask,
        effective: set[str],
        facts: dict[str, FactObservation],
        outcomes: dict[str, Coverage],
    ) -> None:
        def record(family: str, fact: FactObservation) -> None:
            facts[family] = fact
            outcomes[family] = fact.coverage

        if "pull" in effective:
            record("pull", await self._collector.pull(api, archive, task))
        if "pull-reviews" in effective:
            record(
                "pull-reviews",
                await self._collector.pull_reviews(api, archive, task),
            )
        if "pull-review-threads" in effective:
            record(
                "pull-review-threads",
                await self._collector.pull_review_threads(api, archive, task),
            )
        if "pull-review-comments" in effective:
            detail = facts["pull"]
            threads = facts["pull-review-threads"]
            if Coverage.COMPLETE in {detail.coverage, threads.coverage}:
                record(
                    "pull-review-comments",
                    await self._collector.pull_review_comments(
                        api,
                        archive,
                        task,
                        detail,
                        threads,
                        force=False,
                    ),
                )
            else:
                outcomes["pull-review-comments"] = _aggregate_coverage(
                    [detail.coverage, threads.coverage],
                )
        if "pull-commits" in effective:
            detail = facts["pull"]
            if detail.coverage is Coverage.COMPLETE:
                record(
                    "pull-commits",
                    await self._collector.pull_commits(api, archive, task, detail),
                )
            else:
                outcomes["pull-commits"] = detail.coverage
        if "pull-requested-reviewers" in effective:
            detail = facts["pull"]
            if detail.coverage is Coverage.COMPLETE:
                record(
                    "pull-requested-reviewers",
                    await self._collector.pull_requested_reviewers(api, archive, task, detail),
                )
            else:
                outcomes["pull-requested-reviewers"] = detail.coverage
        if "pull-review-comment-reactions" in effective:
            comments = facts.get("pull-review-comments")
            if comments is not None and comments.coverage is Coverage.COMPLETE:
                reactions = await self._collector.comment_reactions(
                    api,
                    archive,
                    task,
                    comments,
                    endpoint="pulls/comments",
                    family="pull-review-comment-reactions",
                    subject_prefix="pull-review-comment",
                )
                outcomes["pull-review-comment-reactions"] = _aggregate_coverage(
                    [fact.coverage for fact in reactions],
                )
            else:
                outcomes["pull-review-comment-reactions"] = outcomes[
                    "pull-review-comments"
                ]
        if "pull-closing-issues" in effective:
            closing = await self._collector.pull_closing_issues(
                api,
                archive,
                task,
                [task.payload["number"]],
            )
            outcomes["pull-closing-issues"] = _aggregate_coverage(
                [fact.coverage for fact in closing],
            )
        if "pull-git" in effective:
            detail = facts["pull"]
            if detail.coverage is Coverage.COMPLETE:
                value = detail.payload.get("value")
                if not isinstance(value, dict):
                    raise IncompleteGitHubDataError(
                        f"pull #{task.resource_number} detail is not an object",
                    )
                record(
                    "pull-git",
                    await self._collector.pull_git(git, archive, task, value),
                )
            else:
                outcomes["pull-git"] = detail.coverage

    async def _run(
        self,
        archive: ObservationArchive,
        job: MaintenanceJob,
    ) -> MaintenanceResult:
        if job.status == "complete":
            return _result(job)
        progress = _SyncProgressTracker(self._observer, self._now)
        self._collector.bind_progress(progress)
        progress.start()
        api, owned = self._runtime.make_api(progress.api_progress)
        git = self._runtime.make_git()
        request_start = api.request_count
        accounted_at = request_start
        progress.bind_maintenance(job.id, job.request_count, request_start)
        try:
            while job.status == "active":
                started_at = _utc(self._now())
                tasks = ()
                if job.kind == "backfill":
                    tasks = await archive.take_maintenance_tasks(
                        job.id,
                        self.config.concurrency,
                        started_at,
                        kind="commit-reference-scan-batch",
                    )
                if not tasks:
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
            async with self._runtime.store_lock:
                await archive.complete_maintenance_task(task.id, _utc(self._now()), outcome)
        except Exception as exc:
            async with self._runtime.store_lock:
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
        if task.kind == "parent-refresh":
            return await self._refresh_parent(api, git, archive, task)
        if task.kind == "git-refs":
            return (await self._collector.git_refs(git, archive, task)).coverage
        if task.kind == "commit-reference-scan-batch":
            cutoff, source_ids = _reference_scan_task_scope(task)
            sources = [
                source
                async for source in archive.iter_structured_commit_sources(
                    cutoff,
                    source_ids,
                )
            ]
            if tuple(source.id for source in sources) != source_ids:
                raise RuntimeError(f"maintenance task {task.task_key} lost a source fact")
            await self._collector.structured_commits(archive, task, sources)
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
        publication = await self._collector.publication(archive, task, "commit-objects")
        if publication is not None:
            return _aggregate_coverage([fact.coverage for fact in publication])
        if not shas:
            return Coverage.COMPLETE
        facts = await self._collector.commit_objects(
            git,
            archive,
            references,
            resource_number=task.resource_number,
        )
        publication = await self._collector.publish(
            archive,
            task,
            "commit-objects",
            facts,
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
        families: Selected fact families. None selects every applicable family for
            each target; source dependencies are automatic.
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
    fact = await archive.latest_complete_fact("issue", f"issue:{number}")
    if fact is None:
        raise KeyError(f"#{number} has no complete archived root")
    value = fact.payload.get("value")
    actual = "pull" if isinstance(value, dict) and "pull_request" in value else "issue"
    if actual != expected:
        raise ValueError(f"#{number} is an {actual}, not a {expected}")


def _caller_key(kind: str, value: str) -> str:
    if not value or len(value) > 200:
        raise ValueError("idempotency key must contain 1 to 200 characters")
    return f"{kind}:caller:{value}"


def _commit_tasks(
    shas: Sequence[str],
    source_cutoff: int,
) -> tuple[TaskDraft, ...]:
    if source_cutoff < 0:
        raise ValueError("source observation cutoff cannot be negative")
    ordered = tuple(validate_commit_sha(sha) for sha in shas)
    if len(ordered) != len(set(ordered)):
        raise ValueError("commit task population must be unique")
    tasks = []
    for offset in range(0, len(ordered), _COMMIT_TASK_SIZE):
        payload = {
            "source_observation_cutoff": source_cutoff,
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


def _reference_scan_tasks(
    observation_ids: Sequence[int],
    source_cutoff: int,
) -> tuple[TaskDraft, ...]:
    if source_cutoff < 0:
        raise ValueError("source observation cutoff cannot be negative")
    ordered = tuple(observation_ids)
    if (
        any(type(observation_id) is not int or observation_id < 1 for observation_id in ordered)
        or len(ordered) != len(set(ordered))
        or tuple(sorted(ordered)) != ordered
    ):
        raise ValueError("reference scan population must be ordered unique observations")
    tasks = []
    for offset in range(0, len(ordered), _REFERENCE_SCAN_TASK_SIZE):
        payload = {
            "source_observation_cutoff": source_cutoff,
            "source_observation_ids": list(
                ordered[offset : offset + _REFERENCE_SCAN_TASK_SIZE],
            ),
        }
        digest = _json_digest(payload)
        tasks.append(
            TaskDraft(
                f"commit-reference-scan-batch:{offset // _REFERENCE_SCAN_TASK_SIZE:08d}:{digest}",
                "commit-reference-scan-batch",
                f"sources:{digest}",
                payload,
            ),
        )
    return tuple(tasks)


def _reference_scan_task_scope(
    task: MaintenanceTask,
) -> tuple[int, tuple[int, ...]]:
    cutoff = task.payload.get("source_observation_cutoff")
    values = task.payload.get("source_observation_ids")
    if type(cutoff) is not int or cutoff < 0 or not isinstance(values, list):
        raise TypeError(f"maintenance task {task.task_key} has invalid source scope")
    observation_ids = tuple(values)
    if (
        not observation_ids
        or any(
            type(observation_id) is not int
            or observation_id < 1
            or observation_id > cutoff
            for observation_id in observation_ids
        )
        or len(observation_ids) != len(set(observation_ids))
        or tuple(sorted(observation_ids)) != observation_ids
    ):
        raise ValueError(f"maintenance task {task.task_key} has invalid source population")
    return cutoff, observation_ids


def _commit_task_scope(
    task: MaintenanceTask,
) -> tuple[int, tuple[str, ...]]:
    cutoff = task.payload.get("source_observation_cutoff")
    values = task.payload.get("shas")
    if type(cutoff) is not int or cutoff < 0 or not isinstance(values, list):
        raise TypeError(f"maintenance task {task.task_key} has invalid commit scope")
    shas = tuple(validate_commit_sha(value) for value in values)
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
