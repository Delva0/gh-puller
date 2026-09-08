"""Collect and publish repository facts for sync and maintenance workflows.

The collector owns source reads, per-operation publication idempotency, coverage
classification, derived facts, and Git evidence capture. Its callers own discovery,
durable job scheduling, and lifecycle of the injected API, Git, and archive objects.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from .api_contract import GitHubAPIError, GitHubResource
from .commit_references import (
    CommitReference,
    commit_reference_payload,
    commit_reference_scope,
    observation_commit_references,
)
from .git_store import (
    CommitFetchSource,
    GitStoreError,
    TransientGitStoreError,
    default_git_url,
)
from .observations import (
    Coverage,
    DiscoveryItem,
    FactDraft,
    FactObservation,
    MaintenanceTask,
    ObservationArchive,
    Origin,
    SyncTask,
    TaskDraft,
)

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from .progress import _SyncProgressTracker
    from .runtime import GitHubAPIReader, GitObjectWriter

_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
_CATALOG_ACCEPT = "application/vnd.github.raw+json"


class IncompleteGitHubDataError(RuntimeError):
    """A source response cannot prove the promised collection is complete."""


class _CollectorConfig(Protocol):
    repository: str
    git_batch_size: int
    git_url: str | None


class GitHubFactCollector:
    """Read and immediately publish complete semantic fact families.

    Args:
        config: Repository identity and Git collection policy.
        now: Timezone-aware clock delimiting actual source observations.
        progress: Out-of-band receiver for API and Git work.
        store_lock: Serialization lock shared by one workflow's archive operations.
    """

    def __init__(
        self,
        config: _CollectorConfig,
        *,
        now: Callable[[], datetime],
        progress: _SyncProgressTracker,
        store_lock: asyncio.Lock,
    ) -> None:
        self.config = config
        self._now = now
        self._progress = progress
        self._store_lock = store_lock
        self._owner, self._repo = config.repository.split("/", 1)
        self._base = f"/repos/{self._owner}/{self._repo}"

    def bind_progress(self, progress: _SyncProgressTracker) -> None:
        """Bind the disposable progress receiver for the next workflow run."""
        self._progress = progress

    async def run_sync_task(
        self,
        api: GitHubAPIReader,
        git: GitObjectWriter,
        archive: ObservationArchive,
        task: SyncTask,
    ) -> Exception | None:
        try:
            if task.kind == "parent":
                await self._hydrate_parent(api, archive, task)
            elif task.kind == "closing-issues":
                await self.pull_closing_issues(api, archive, task)
            elif task.kind == "git-refs":
                await self.git_refs(git, archive, task)
            else:
                raise RuntimeError(f"unknown sync task kind: {task.kind}")
        except Exception as exc:
            async with self._store_lock:
                await archive.record_task_error(task.id, _error_text(exc))
            return exc
        return None

    async def run_pull_git_batch(
        self,
        git: GitObjectWriter,
        archive: ObservationArchive,
        tasks: list[SyncTask],
    ) -> list[Exception | None]:
        pending = []
        for task in tasks:
            if await self.publication(archive, task, "pull-git") is None:
                pending.append(task)
            else:
                await self._finish_task(archive, task)
        if not pending:
            return []
        self._progress.phase("syncing_git", f"pulls={len(pending)}")
        try:
            await self._prefetch_git(git, pending)
        except Exception as exc:
            for task in pending:
                async with self._store_lock:
                    await archive.record_task_error(task.id, _error_text(exc))
            return [exc]
        results = []
        for task in pending:
            try:
                await self.pull_git(git, archive, task)
            except Exception as exc:
                async with self._store_lock:
                    await archive.record_task_error(task.id, _error_text(exc))
                results.append(exc)
            else:
                results.append(None)
        return results

    async def run_commit_batch(
        self,
        git: GitObjectWriter,
        archive: ObservationArchive,
        tasks: list[SyncTask],
    ) -> list[Exception | None]:
        pending = []
        for task in tasks:
            if await self.publication(archive, task, "commit-object") is None:
                pending.append(task)
            else:
                async with self._store_lock:
                    await archive.complete_task(task.id, _utc(self._now()))
        if not pending:
            return []
        task_shas = [_required_sha(task.payload.get("sha"), task.task_key) for task in pending]
        shas = tuple(dict.fromkeys(task_shas))
        try:
            references = await self.commit_reference_index(archive, set(shas))
            facts = await self.commit_objects(
                git,
                archive,
                {sha: references.get(sha, ()) for sha in shas},
                resource_number=None,
            )
        except Exception as exc:
            for task in pending:
                async with self._store_lock:
                    await archive.record_task_error(task.id, _error_text(exc))
            return [exc]
        failures: list[Exception | None] = []
        facts_by_sha = {
            _required_sha(fact.payload.get("sha"), fact.subject_key): fact
            for fact in facts
        }
        for task, sha in zip(pending, task_shas, strict=True):
            try:
                await self.publish(
                    archive,
                    task,
                    "commit-object",
                    (facts_by_sha[sha],),
                    complete_task=True,
                )
            except Exception as exc:
                async with self._store_lock:
                    await archive.record_task_error(task.id, _error_text(exc))
                failures.append(exc)
            else:
                failures.append(None)
        return failures

    async def publication(
        self,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
        operation: str,
    ) -> tuple[FactObservation, ...] | None:
        async with self._store_lock:
            return await archive.publication(_publication_key(task, operation))

    async def _current(
        self,
        archive: ObservationArchive,
        family: str,
        subject_key: str,
    ) -> FactObservation | None:
        async with self._store_lock:
            return await archive.current_fact(family, subject_key)

    async def publish(
        self,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
        operation: str,
        facts: tuple[FactDraft, ...],
        *,
        complete_task: bool = False,
    ) -> tuple[FactObservation, ...]:
        async with self._store_lock:
            maintenance = isinstance(task, MaintenanceTask)
            return await archive.publish(
                _publication_key(task, operation),
                "refresh" if maintenance else "sync",
                _utc(self._now()),
                facts,
                cycle_id=None if maintenance else task.cycle_id,
                task_id=(task.id if complete_task and not maintenance else None),
                maintenance_task_id=task.id if maintenance else None,
            )

    async def _finish_task(
        self,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
    ) -> None:
        async with self._store_lock:
            if isinstance(task, MaintenanceTask):
                await archive.complete_maintenance_task(
                    task.id,
                    _utc(self._now()),
                    Coverage.COMPLETE,
                )
            else:
                await archive.complete_task(task.id, _utc(self._now()))

    async def _hydrate_parent(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        task: SyncTask,
    ) -> None:
        number = _task_number(task)
        async with self._store_lock:
            item = await archive.discovery_item(task.cycle_id, number)
            issue_signal, pull_signal = await archive.discovery_signals(
                task.cycle_id,
                number,
            )
        issue_fact = await self.issue(api, archive, task, item)
        if issue_fact.coverage is not Coverage.COMPLETE:
            await self._finish_task(archive, task)
            return
        issue = _fact_object(issue_fact, f"issue #{number}")
        kind = "pull" if "pull_request" in issue else "issue"
        if item is not None and item.kind != kind:
            raise IncompleteGitHubDataError(
                f"catalog and root disagree on issue #{number} kind",
            )

        comments = await self.issue_comments(
            api,
            archive,
            task,
            issue_fact,
            force=issue_signal,
        )
        timeline = await self.issue_timeline(api, archive, task)
        events = await self.issue_events(api, archive, task)
        await self.issue_reactions(api, archive, task, issue_fact)
        if comments.coverage is Coverage.COMPLETE:
            await self.comment_reactions(
                api,
                archive,
                task,
                comments,
                endpoint="issues/comments",
                family="issue-comment-reactions",
                subject_prefix="issue-comment",
            )

        sources = [timeline, events]
        if kind == "issue":
            await self.issue_relations(api, archive, task)
        else:
            sources.extend(
                await self._pull_facts(
                    api,
                    archive,
                    task,
                    force_review_comments=pull_signal,
                    cataloged=item is not None,
                ),
            )
        await self.structured_commits(archive, task, sources)
        await self._finish_task(archive, task)

    async def issue(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
        item: DiscoveryItem | None,
    ) -> FactObservation:
        existing = await self.publication(archive, task, "issue")
        if existing is not None:
            return _single(existing, "issue")
        number = _task_number(task)
        subject = f"issue:{number}"
        if item is not None:
            _validate_issue(item.summary, number)
            return _single(
                await self.publish(
                    archive,
                    task,
                    "issue",
                    (
                        FactDraft(
                            family="issue",
                            subject_key=subject,
                            resource_number=number,
                            observed_from=item.observed_from,
                            observed_until=item.observed_until,
                            coverage=Coverage.COMPLETE,
                            origin=Origin.API,
                            payload=_source_payload(
                                "RepositoryIssueCatalog",
                                self.config.repository,
                                number,
                                item.summary,
                                "rest",
                                item.summary,
                                None,
                            ),
                        ),
                    ),
                ),
                "issue",
            )
        previous, cache = await self._previous(archive, "issue", subject)
        observed_from = _utc(self._now())
        path = f"{self._base}/issues/{number}"
        try:
            value, updated = await api.get_json_cached(
                path,
                previous=previous,
                cache=cache,
                accept=_CATALOG_ACCEPT,
            )
        except GitHubAPIError as exc:
            coverage = _coverage_error(exc, unauthenticated=False)
            if coverage is None:
                raise
            return await self._coverage_fact(
                archive,
                task,
                "issue",
                "issue",
                subject,
                number,
                observed_from,
                coverage,
                exc,
            )
        observed_until = _utc(self._now())
        issue = _object(value, f"issue #{number}")
        _validate_issue(issue, number)
        return _single(
            await self.publish(
                archive,
                task,
                "issue",
                (
                    FactDraft(
                        family="issue",
                        subject_key=subject,
                        resource_number=number,
                        observed_from=observed_from,
                        observed_until=observed_until,
                        coverage=Coverage.COMPLETE,
                        origin=Origin.API,
                        payload=_source_payload(
                            "Issue",
                            self.config.repository,
                            number,
                            issue,
                            "rest",
                            issue,
                            updated,
                        ),
                    ),
                ),
            ),
            "issue",
        )

    async def issue_timeline(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
    ) -> FactObservation:
        number = _task_number(task)
        path = f"{self._base}/issues/{number}/timeline"
        return await self._resource_collection(
            archive,
            task,
            "issue-timeline",
            "issue-timeline",
            f"issue:{number}",
            "IssueTimeline",
            number,
            lambda previous, cache: _paginate_resource(
                api,
                path,
                previous,
                cache,
            ),
        )

    async def issue_events(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
    ) -> FactObservation:
        number = _task_number(task)
        path = f"{self._base}/issues/{number}/events"
        return await self._resource_collection(
            archive,
            task,
            "issue-events",
            "issue-events",
            f"issue:{number}",
            "IssueEvents",
            number,
            lambda previous, cache: _paginate_resource(
                api,
                path,
                previous,
                cache,
            ),
        )

    async def issue_comments(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
        issue_fact: FactObservation,
        *,
        force: bool,
    ) -> FactObservation:
        number = _task_number(task)
        issue = _fact_object(issue_fact, f"issue #{number}")
        if not force and _zero(issue.get("comments")):
            return await self._derived(
                archive,
                task,
                "issue-comments",
                "issue-comments",
                f"issue:{number}",
                number,
                [],
                issue_fact,
                "IssueCommentCount",
            )
        return await self._resource_collection(
            archive,
            task,
            "issue-comments",
            "issue-comments",
            f"issue:{number}",
            "IssueComments",
            number,
            lambda previous, cache: api.issue_comments(
                self._owner,
                self._repo,
                number,
                previous=previous,
                cache=cache,
            ),
        )

    async def issue_reactions(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
        issue_fact: FactObservation,
    ) -> FactObservation:
        number = _task_number(task)
        issue = _fact_object(issue_fact, f"issue #{number}")
        if _zero_count(issue.get("reactions")):
            return await self._derived(
                archive,
                task,
                "issue-reactions",
                "issue-reactions",
                f"issue:{number}",
                number,
                [],
                issue_fact,
                "IssueReactionCount",
            )
        return await self._resource_collection(
            archive,
            task,
            "issue-reactions",
            "issue-reactions",
            f"issue:{number}",
            "IssueReactions",
            number,
            lambda previous, cache: api.reactions(
                f"{self._base}/issues/{number}/reactions",
                _optional_string(issue.get("node_id")),
                previous=previous,
                cache=cache,
            ),
        )

    async def comment_reactions(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
        comments_fact: FactObservation,
        *,
        endpoint: str,
        family: str,
        subject_prefix: str,
    ) -> tuple[FactObservation, ...]:
        comments = _fact_list(comments_fact, family)
        facts = []
        for comment in comments:
            comment_id = comment.get("id")
            if type(comment_id) is not int or comment_id < 1:
                raise IncompleteGitHubDataError(f"{family} source has an invalid comment ID")
            operation = f"{family}:{comment_id}"
            subject = f"{subject_prefix}:{comment_id}"
            if _zero_count(comment.get("reactions")):
                facts.append(
                    await self._derived(
                        archive,
                        task,
                        operation,
                        family,
                        subject,
                        _task_number(task),
                        [],
                        comments_fact,
                        "CommentReactionCount",
                    ),
                )
                continue
            facts.append(
                await self._resource_collection(
                    archive,
                    task,
                    operation,
                    family,
                    subject,
                    "CommentReactions",
                    _task_number(task),
                    lambda previous, cache, comment_id=comment_id, comment=comment: api.reactions(
                        f"{self._base}/{endpoint}/{comment_id}/reactions",
                        _optional_string(comment.get("node_id")),
                        previous=previous,
                        cache=cache,
                    ),
                ),
            )
        return tuple(facts)

    async def _pull_facts(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        task: SyncTask,
        *,
        force_review_comments: bool,
        cataloged: bool,
    ) -> list[FactObservation]:
        number = _task_number(task)
        detail = await self.pull(api, archive, task)
        if detail.coverage is not Coverage.COMPLETE:
            return [detail]
        reviews = await self.pull_reviews(api, archive, task)
        threads = await self.pull_review_threads(api, archive, task)
        review_comments = await self.pull_review_comments(
            api,
            archive,
            task,
            detail,
            threads,
            force=force_review_comments,
        )
        commits = await self.pull_commits(api, archive, task, detail)
        await self.pull_requested_reviewers(api, archive, task, detail)
        if review_comments.coverage is Coverage.COMPLETE:
            await self.comment_reactions(
                api,
                archive,
                task,
                review_comments,
                endpoint="pulls/comments",
                family="pull-review-comment-reactions",
                subject_prefix="pull-review-comment",
            )
        await self._enqueue_pull_git(archive, task, detail)
        if not cataloged:
            await self._enqueue_closing_issues(archive, task, number)
        return [reviews, threads, review_comments, commits]

    async def pull(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
    ) -> FactObservation:
        number = _task_number(task)
        return await self._resource_object(
            archive,
            task,
            "pull",
            "pull",
            f"pull:{number}",
            "PullRequest",
            number,
            lambda previous, cache: api.pull_request(
                self._owner,
                self._repo,
                number,
                previous=previous,
                cache=cache,
            ),
        )

    async def pull_reviews(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
    ) -> FactObservation:
        number = _task_number(task)
        return await self._resource_collection(
            archive,
            task,
            "pull-reviews",
            "pull-reviews",
            f"pull:{number}",
            "PullReviews",
            number,
            lambda previous, cache: api.pull_reviews(
                self._owner,
                self._repo,
                number,
                previous=previous,
                cache=cache,
            ),
        )

    async def pull_review_threads(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
    ) -> FactObservation:
        existing = await self.publication(archive, task, "pull-review-threads")
        if existing is not None:
            return _single(existing, "pull-review-threads")
        number = _task_number(task)
        observed_from = _utc(self._now())
        try:
            resource = await api.pull_review_threads(self._owner, self._repo, number)
        except GitHubAPIError as exc:
            coverage = _coverage_error(exc, unauthenticated=True)
            if coverage is None:
                raise
            return await self._coverage_fact(
                archive,
                task,
                "pull-review-threads",
                "pull-review-threads",
                f"pull:{number}",
                number,
                observed_from,
                coverage,
                exc,
            )
        observed_until = _utc(self._now())
        value = _object(resource.value, f"pull #{number} review threads")
        comments = _objects(value.get("review_comments"), "review thread comments")
        threads = _object(value.get("threads"), "review thread connection")
        _objects(threads.get("nodes"), "review threads")
        payload = _source_payload(
            "PullReviewThreads",
            self.config.repository,
            number,
            value,
            resource.source,
            resource.raw,
            resource.cache,
        )
        payload["comment_count"] = len(comments)
        return _single(
            await self.publish(
                archive,
                task,
                "pull-review-threads",
                (
                    FactDraft(
                        family="pull-review-threads",
                        subject_key=f"pull:{number}",
                        resource_number=number,
                        observed_from=observed_from,
                        observed_until=observed_until,
                        coverage=Coverage.COMPLETE,
                        origin=Origin.API,
                        payload=payload,
                    ),
                ),
            ),
            "pull-review-threads",
        )

    async def pull_review_comments(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
        detail: FactObservation,
        threads: FactObservation,
        *,
        force: bool,
    ) -> FactObservation:
        number = _task_number(task)
        if threads.coverage is Coverage.COMPLETE:
            value = _fact_object(threads, "review threads").get("review_comments")
            comments = _objects(value, "review thread comments")
            return await self._derived(
                archive,
                task,
                "pull-review-comments",
                "pull-review-comments",
                f"pull:{number}",
                number,
                comments,
                threads,
                "PullReviewThreads",
            )
        pull = _fact_object(detail, f"pull #{number}")
        if not force and _zero(pull.get("review_comments")):
            return await self._derived(
                archive,
                task,
                "pull-review-comments",
                "pull-review-comments",
                f"pull:{number}",
                number,
                [],
                detail,
                "PullReviewCommentCount",
            )
        return await self._resource_collection(
            archive,
            task,
            "pull-review-comments",
            "pull-review-comments",
            f"pull:{number}",
            "PullReviewComments",
            number,
            lambda previous, cache: api.pull_review_comments(
                self._owner,
                self._repo,
                number,
                previous=previous,
                cache=cache,
            ),
        )

    async def pull_commits(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
        detail: FactObservation,
    ) -> FactObservation:
        number = _task_number(task)
        pull = _fact_object(detail, f"pull #{number}")
        expected = pull.get("commits")
        if _zero(expected):
            return await self._derived(
                archive,
                task,
                "pull-commits",
                "pull-commits",
                f"pull:{number}",
                number,
                [],
                detail,
                "PullCommitCount",
            )
        if type(expected) is not int or expected < 1:
            raise IncompleteGitHubDataError(f"pull #{number} has an invalid commit count")
        base, head = _comparison_shas(pull, number)
        return await self._resource_collection(
            archive,
            task,
            "pull-commits",
            "pull-commits",
            f"pull:{number}",
            "PullCommits",
            number,
            lambda previous, cache: api.pull_commits(
                self._owner,
                self._repo,
                number,
                expected=expected,
                base=base,
                head=head,
                previous=previous,
                cache=cache,
            ),
        )

    async def pull_requested_reviewers(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
        detail: FactObservation,
    ) -> FactObservation:
        number = _task_number(task)
        pull = _fact_object(detail, f"pull #{number}")
        embedded = _embedded_review_requests(pull)
        if embedded is not None:
            return await self._derived(
                archive,
                task,
                "pull-requested-reviewers",
                "pull-requested-reviewers",
                f"pull:{number}",
                number,
                embedded,
                detail,
                "PullRequestDetail",
            )
        path = f"{self._base}/pulls/{number}/requested_reviewers"
        return await self._resource_object(
            archive,
            task,
            "pull-requested-reviewers",
            "pull-requested-reviewers",
            f"pull:{number}",
            "PullRequestedReviewers",
            number,
            lambda previous, cache: _object_resource(
                api,
                path,
                previous,
                cache,
            ),
        )

    async def issue_relations(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
    ) -> FactObservation:
        existing = await self.publication(archive, task, "issue-relations")
        if existing is not None:
            return _single(existing, "issue-relations")
        number = _task_number(task)
        observed_from = _utc(self._now())
        try:
            resource = await api.issue_relations(self._owner, self._repo, number)
        except GitHubAPIError as exc:
            coverage = _coverage_error(exc, unauthenticated=True)
            if coverage is None:
                raise
            return await self._coverage_fact(
                archive,
                task,
                "issue-relations",
                "issue-relations",
                f"issue:{number}",
                number,
                observed_from,
                coverage,
                exc,
            )
        observed_until = _utc(self._now())
        value = _object(resource.value, f"issue #{number} relations")
        return _single(
            await self.publish(
                archive,
                task,
                "issue-relations",
                (
                    FactDraft(
                        family="issue-relations",
                        subject_key=f"issue:{number}",
                        resource_number=number,
                        observed_from=observed_from,
                        observed_until=observed_until,
                        coverage=Coverage.COMPLETE,
                        origin=Origin.API,
                        payload=_source_payload(
                            "IssueRelations",
                            self.config.repository,
                            number,
                            value,
                            resource.source,
                            resource.raw,
                            resource.cache,
                        ),
                    ),
                ),
            ),
            "issue-relations",
        )

    async def _resource_collection(
        self,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
        operation_key: str,
        family: str,
        subject_key: str,
        operation: str,
        resource_number: int,
        load: Callable[
            [list[dict[str, Any]] | None, dict[str, Any] | None],
            Awaitable[GitHubResource],
        ],
    ) -> FactObservation:
        existing = await self.publication(archive, task, operation_key)
        if existing is not None:
            return _single(existing, operation_key)
        previous, cache = await self._previous(archive, family, subject_key)
        previous_list = _optional_objects(previous)
        observed_from = _utc(self._now())
        try:
            resource = await load(previous_list, cache)
        except GitHubAPIError as exc:
            coverage = _coverage_error(exc, unauthenticated=False)
            if coverage is None:
                raise
            return await self._coverage_fact(
                archive,
                task,
                operation_key,
                family,
                subject_key,
                resource_number,
                observed_from,
                coverage,
                exc,
            )
        observed_until = _utc(self._now())
        value = _objects(resource.value, operation)
        return _single(
            await self.publish(
                archive,
                task,
                operation_key,
                (
                    FactDraft(
                        family=family,
                        subject_key=subject_key,
                        resource_number=resource_number,
                        observed_from=observed_from,
                        observed_until=observed_until,
                        coverage=Coverage.COMPLETE,
                        origin=Origin.API,
                        payload=_source_payload(
                            operation,
                            self.config.repository,
                            resource_number,
                            value,
                            resource.source,
                            resource.raw,
                            resource.cache,
                        ),
                    ),
                ),
            ),
            operation_key,
        )

    async def _resource_object(
        self,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
        operation_key: str,
        family: str,
        subject_key: str,
        operation: str,
        resource_number: int,
        load: Callable[
            [dict[str, Any] | None, dict[str, Any] | None],
            Awaitable[GitHubResource],
        ],
    ) -> FactObservation:
        existing = await self.publication(archive, task, operation_key)
        if existing is not None:
            return _single(existing, operation_key)
        previous, cache = await self._previous(archive, family, subject_key)
        previous_object = previous if isinstance(previous, dict) else None
        observed_from = _utc(self._now())
        try:
            resource = await load(previous_object, cache)
        except GitHubAPIError as exc:
            coverage = _coverage_error(exc, unauthenticated=False)
            if coverage is None:
                raise
            return await self._coverage_fact(
                archive,
                task,
                operation_key,
                family,
                subject_key,
                resource_number,
                observed_from,
                coverage,
                exc,
            )
        observed_until = _utc(self._now())
        value = _object(resource.value, operation)
        return _single(
            await self.publish(
                archive,
                task,
                operation_key,
                (
                    FactDraft(
                        family=family,
                        subject_key=subject_key,
                        resource_number=resource_number,
                        observed_from=observed_from,
                        observed_until=observed_until,
                        coverage=Coverage.COMPLETE,
                        origin=Origin.API,
                        payload=_source_payload(
                            operation,
                            self.config.repository,
                            resource_number,
                            value,
                            resource.source,
                            resource.raw,
                            resource.cache,
                        ),
                    ),
                ),
            ),
            operation_key,
        )

    async def _derived(
        self,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
        operation_key: str,
        family: str,
        subject_key: str,
        resource_number: int,
        value: Any,
        source: FactObservation,
        evidence: str,
    ) -> FactObservation:
        existing = await self.publication(archive, task, operation_key)
        if existing is not None:
            return _single(existing, operation_key)
        return _single(
            await self.publish(
                archive,
                task,
                operation_key,
                (
                    FactDraft(
                        family=family,
                        subject_key=subject_key,
                        resource_number=resource_number,
                        source_digest=source.payload_digest,
                        observed_from=source.observed_from,
                        observed_until=source.observed_until,
                        coverage=Coverage.COMPLETE,
                        origin=Origin.DERIVED,
                        payload={
                            "operation": evidence,
                            "repository": self.config.repository,
                            "resource_number": resource_number,
                            "source_observation_id": source.id,
                            "source_payload_digest": source.payload_digest,
                            "value": value,
                        },
                    ),
                ),
            ),
            operation_key,
        )

    async def _coverage_fact(
        self,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
        operation_key: str,
        family: str,
        subject_key: str,
        resource_number: int,
        observed_from: datetime,
        coverage: Coverage,
        error: GitHubAPIError,
    ) -> FactObservation:
        return _single(
            await self.publish(
                archive,
                task,
                operation_key,
                (
                    FactDraft(
                        family=family,
                        subject_key=subject_key,
                        resource_number=resource_number,
                        observed_from=observed_from,
                        observed_until=_utc(self._now()),
                        coverage=coverage,
                        origin=Origin.API,
                        payload={
                            "operation": operation_key,
                            "repository": self.config.repository,
                            "resource_number": resource_number,
                            "error": {
                                "type": type(error).__name__,
                                "message": str(error),
                                "status_code": error.status_code,
                                "url": error.url,
                            },
                        },
                    ),
                ),
            ),
            operation_key,
        )

    async def _previous(
        self,
        archive: ObservationArchive,
        family: str,
        subject_key: str,
    ) -> tuple[Any | None, dict[str, Any] | None]:
        previous = await self._current(archive, family, subject_key)
        if previous is None or previous.coverage is not Coverage.COMPLETE:
            return None, None
        value = previous.payload.get("value")
        cache = previous.payload.get("cache")
        return value, cache if isinstance(cache, dict) else None

    async def pull_closing_issues(
        self,
        api: GitHubAPIReader,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
        numbers: Sequence[int] | None = None,
    ) -> tuple[FactObservation, ...]:
        existing = await self.publication(archive, task, "closing-issues")
        if existing is not None:
            if isinstance(task, SyncTask):
                await self._finish_task(archive, task)
            return existing
        value = task.payload.get("numbers") if numbers is None else list(numbers)
        if (
            not isinstance(value, list)
            or not value
            or len(value) > 100
            or any(type(number) is not int or number < 1 for number in value)
            or len(set(value)) != len(value)
        ):
            raise RuntimeError("closing-issues task has invalid PR numbers")
        numbers = list(value)
        observed_from = _utc(self._now())
        try:
            results = await api.closing_issue_references(
                self._owner,
                self._repo,
                numbers,
            )
        except GitHubAPIError as exc:
            coverage = _coverage_error(exc, unauthenticated=True)
            if coverage is None:
                raise
            observed_until = _utc(self._now())
            facts = tuple(
                FactDraft(
                    family="pull-closing-issues",
                    subject_key=f"pull:{number}",
                    resource_number=number,
                    observed_from=observed_from,
                    observed_until=observed_until,
                    coverage=coverage,
                    origin=Origin.API,
                    payload={
                        "operation": "ClosingIssuesReferences",
                        "repository": self.config.repository,
                        "resource_number": number,
                        "error": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                            "status_code": exc.status_code,
                            "url": exc.url,
                        },
                    },
                )
                for number in numbers
            )
        else:
            observed_until = _utc(self._now())
            if set(results) != set(numbers):
                raise IncompleteGitHubDataError(
                    "closing issue response does not match its requested PRs",
                )
            facts = tuple(
                FactDraft(
                    family="pull-closing-issues",
                    subject_key=f"pull:{number}",
                    resource_number=number,
                    observed_from=observed_from,
                    observed_until=observed_until,
                    coverage=Coverage.COMPLETE,
                    origin=Origin.API,
                    payload=_source_payload(
                        "ClosingIssuesReferences",
                        self.config.repository,
                        number,
                        _objects(results[number], f"pull #{number} closing issues"),
                        "graphql",
                        results[number],
                        None,
                    ),
                )
                for number in numbers
            )
        return await self.publish(
            archive,
            task,
            "closing-issues",
            facts,
            complete_task=True,
        )

    async def git_refs(
        self,
        git: GitObjectWriter,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
    ) -> FactObservation:
        existing = await self.publication(archive, task, "git-refs")
        if existing is not None:
            if isinstance(task, SyncTask):
                await self._finish_task(archive, task)
            return _single(existing, "git-refs")
        self._progress.phase("syncing_git", "upstream")
        observed_from = _utc(self._now())
        refs = await git.sync_upstream(
            heartbeat=self._progress.git_heartbeat,
            retry=self._progress.git_retry,
        )
        observed_until = _utc(self._now())
        return _single(
            await self.publish(
                archive,
                task,
                "git-refs",
                (
                    FactDraft(
                        family="git-refs",
                        subject_key="repository",
                        resource_number=None,
                        observed_from=observed_from,
                        observed_until=observed_until,
                        coverage=Coverage.COMPLETE,
                        origin=Origin.GIT,
                        payload={
                            "operation": "GitRefObservation",
                            "repository": self.config.repository,
                            "value": _object(refs, "Git ref observation"),
                        },
                    ),
                ),
                complete_task=True,
            ),
            "git-refs",
        )

    async def _enqueue_pull_git(
        self,
        archive: ObservationArchive,
        task: SyncTask,
        detail: FactObservation,
    ) -> None:
        number = _task_number(task)
        pull = _fact_object(detail, f"pull #{number}")
        async with self._store_lock:
            await archive.enqueue_tasks(
                task.cycle_id,
                (
                    TaskDraft(
                        task_key=f"pull-git:{number}",
                        kind="pull-git",
                        subject_key=f"pull:{number}",
                        resource_number=number,
                        payload={"number": number, "pull": pull},
                    ),
                ),
            )

    async def _enqueue_closing_issues(
        self,
        archive: ObservationArchive,
        task: SyncTask,
        number: int,
    ) -> None:
        async with self._store_lock:
            await archive.enqueue_tasks(
                task.cycle_id,
                (
                    TaskDraft(
                        task_key=f"closing:single:{number}",
                        kind="closing-issues",
                        subject_key=f"pull:{number}",
                        resource_number=number,
                        payload={"numbers": [number]},
                    ),
                ),
            )

    async def _prefetch_git(
        self,
        git: GitObjectWriter,
        tasks: list[SyncTask],
    ) -> None:
        for offset in range(0, len(tasks), self.config.git_batch_size):
            await self._prefetch_git_group(
                git,
                tasks[offset : offset + self.config.git_batch_size],
            )

    async def _prefetch_git_group(
        self,
        git: GitObjectWriter,
        tasks: list[SyncTask],
    ) -> None:
        pulls = {_task_number(task): _object(task.payload.get("pull"), task.task_key) for task in tasks}
        try:
            await git.prefetch(
                pulls,
                heartbeat=self._progress.git_heartbeat,
                retry=self._progress.git_retry,
                retry_transient=len(tasks) == 1,
            )
        except TransientGitStoreError:
            if len(tasks) == 1:
                raise
            middle = len(tasks) // 2
            await self._prefetch_git_group(git, tasks[:middle])
            await self._prefetch_git_group(git, tasks[middle:])

    async def pull_git(
        self,
        git: GitObjectWriter,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
        pull: dict[str, Any] | None = None,
    ) -> FactObservation:
        existing = await self.publication(archive, task, "pull-git")
        if existing is not None:
            if isinstance(task, SyncTask):
                await self._finish_task(archive, task)
            return _single(existing, "pull-git")
        number = _task_number(task)
        pull = _object(task.payload.get("pull"), task.task_key) if pull is None else pull
        observed_from = _utc(self._now())
        snapshot = await git.capture(
            number,
            pull,
            heartbeat=self._progress.git_heartbeat,
            retry=self._progress.git_retry,
        )
        observed_until = _utc(self._now())
        return _single(
            await self.publish(
                archive,
                task,
                "pull-git",
                (
                    FactDraft(
                        family="pull-git",
                        subject_key=f"pull:{number}",
                        resource_number=number,
                        observed_from=observed_from,
                        observed_until=observed_until,
                        coverage=(
                            Coverage.PARTIAL
                            if snapshot.get("comparison_kind") == "unavailable"
                            else Coverage.COMPLETE
                        ),
                        origin=Origin.GIT,
                        payload={
                            "operation": "PullGitSnapshot",
                            "repository": self.config.repository,
                            "resource_number": number,
                            "value": _object(snapshot, f"pull #{number} Git snapshot"),
                        },
                    ),
                ),
                complete_task=True,
            ),
            "pull-git",
        )

    async def commit_fetch_sources(
        self,
        archive: ObservationArchive,
        references: Mapping[str, Sequence[dict[str, Any]]],
    ) -> dict[str, tuple[CommitFetchSource, ...]]:
        numbers = sorted(
            {
                number
                for selected in references.values()
                for item in selected
                if type(number := item.get("resource_number")) is int and number > 0
            },
        )
        pulls = {}
        for number in numbers:
            current = await self._current(archive, "pull", f"pull:{number}")
            if current is not None and current.coverage is Coverage.COMPLETE:
                pulls[number] = _fact_object(current, f"pull #{number}")
        result: dict[str, list[CommitFetchSource]] = {}
        for value, selected in references.items():
            sha = _required_sha(value, "commit source")
            sources = result.setdefault(sha, [])
            for number in sorted(
                {value for item in selected if type(value := item.get("resource_number")) is int and value in pulls},
            ):
                sources.append(
                    CommitFetchSource(
                        "pull-ref",
                        self.config.git_url or default_git_url(self.config.repository),
                        f"refs/pull/{number}/head",
                        self.config.repository,
                        number,
                    ),
                )
                pull = pulls[number]
                head = pull.get("head")
                repository = head.get("repo") if isinstance(head, dict) else None
                full_name = repository.get("full_name") if isinstance(repository, dict) else None
                ref = head.get("ref") if isinstance(head, dict) else None
                if (
                    isinstance(full_name, str)
                    and "/" in full_name
                    and full_name != self.config.repository
                    and isinstance(ref, str)
                    and ref
                ):
                    sources.append(
                        CommitFetchSource(
                            "repository-ref",
                            _repository_git_url(repository, full_name),
                            f"refs/heads/{ref}",
                            full_name,
                            number,
                        ),
                    )
        return {sha: tuple(dict.fromkeys(sources)) for sha, sources in result.items()}

    async def commit_objects(
        self,
        git: GitObjectWriter,
        archive: ObservationArchive,
        references: Mapping[str, Sequence[dict[str, Any]]],
        *,
        resource_number: int | None,
    ) -> tuple[FactDraft, ...]:
        """Acquire commit objects and construct one observation draft per SHA."""
        shas = tuple(_required_sha(sha, "commit target") for sha in references)
        if not shas:
            return ()
        self._progress.phase("syncing_git", f"commits={len(shas)}")
        observed_from = _utc(self._now())
        results = await git.retain_commits(
            shas,
            sources=await self.commit_fetch_sources(archive, references),
            heartbeat=self._progress.git_heartbeat,
            retry=self._progress.git_retry,
        )
        observed_until = _utc(self._now())
        return tuple(
            _commit_object_fact(
                self.config.repository,
                sha,
                references[sha],
                results.get(sha),
                resource_number,
                observed_from,
                observed_until,
            )
            for sha in shas
        )

    async def commit_reference_index(
        self,
        archive: ObservationArchive,
        shas: set[str],
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        cutoff = await archive.observation_cutoff()
        references: dict[str, list[dict[str, Any]]] = {}
        async for item in archive.iter_commit_references(cutoff, shas):
            sha = _required_sha(item.get("sha"), "commit reference")
            references.setdefault(sha, []).append(item)
        return {sha: tuple(items) for sha, items in references.items()}

    async def structured_commits(
        self,
        archive: ObservationArchive,
        task: SyncTask | MaintenanceTask,
        sources: list[FactObservation],
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        references: dict[int, list[CommitReference]] = {}
        source_by_id = {}
        reference_by_id = {}
        for source in sources:
            if source.coverage is not Coverage.COMPLETE:
                continue
            selected = list(
                observation_commit_references(source.family, source.payload),
            )
            references[source.id] = selected
            source_by_id[source.id] = source
            operation = f"commit-references:{source.id}"
            publication = await self.publication(archive, task, operation)
            if publication is None:
                publication = await self.publish(
                    archive,
                    task,
                    operation,
                    (
                        FactDraft(
                            family="commit-references",
                            subject_key=f"payload:{source.payload_digest}",
                            resource_number=source.resource_number,
                            source_digest=source.payload_digest,
                            observed_from=source.observed_from,
                            observed_until=source.observed_until,
                            coverage=Coverage.COMPLETE,
                            origin=Origin.DERIVED,
                            payload={
                                "operation": "StructuredCommitReferenceScan",
                                "repository": self.config.repository,
                                "source_family": source.family,
                                "source_observation_id": source.id,
                                "source_payload_digest": source.payload_digest,
                                "references": [
                                    commit_reference_payload(reference)
                                    for reference in selected
                                ],
                            },
                        ),
                    ),
                )
            reference_by_id[source.id] = _single(
                publication,
                "commit-references",
            )
        by_sha: dict[str, list[dict[str, Any]]] = {}
        for source_id, selected in references.items():
            source = source_by_id[source_id]
            reference_fact = reference_by_id[source_id]
            for reference in selected:
                by_sha.setdefault(reference.sha, []).append(
                    {
                        "reference_observation_id": reference_fact.id,
                        "source_observation_id": source.id,
                        "source_payload_digest": source.payload_digest,
                        "source_family": source.family,
                        "resource_number": source.resource_number,
                        **commit_reference_payload(reference),
                    },
                )
        frozen = {sha: tuple(items) for sha, items in sorted(by_sha.items())}
        if not frozen or isinstance(task, MaintenanceTask):
            return frozen
        async with self._store_lock:
            await archive.enqueue_tasks(
                task.cycle_id,
                tuple(
                    TaskDraft(
                        task_key=f"commit-object:{sha}:parent-task:{task.id}",
                        kind="commit-object",
                        subject_key=f"commit:{sha}",
                        resource_number=None,
                        payload={"sha": sha},
                    )
                    for sha in frozen
                ),
            )
        return frozen


async def _paginate_resource(
    api: GitHubAPIReader,
    path: str,
    previous: list[dict[str, Any]] | None,
    cache: dict[str, Any] | None,
) -> GitHubResource:
    value, updated = await api.paginate_cached(
        path,
        previous=previous,
        cache=cache,
    )
    return GitHubResource(value, "rest", value, updated)


async def _object_resource(
    api: GitHubAPIReader,
    path: str,
    previous: dict[str, Any] | None,
    cache: dict[str, Any] | None,
) -> GitHubResource:
    value, updated = await api.get_json_cached(
        path,
        previous=previous,
        cache=cache,
    )
    return GitHubResource(value, "rest", value, updated)


def _publication_key(task: SyncTask | MaintenanceTask, operation: str) -> str:
    owner = f"maintenance:{task.job_id}" if isinstance(task, MaintenanceTask) else f"sync:{task.cycle_id}"
    return f"{owner}:task:{task.id}:{operation}"


def _task_number(task: SyncTask | MaintenanceTask) -> int:
    value = task.payload.get("number", task.resource_number)
    if type(value) is not int or value < 1:
        raise RuntimeError(f"{task.task_key} has no valid resource number")
    return value


def _validate_issue(value: dict[str, Any], number: int) -> None:
    if value.get("number") != number:
        raise IncompleteGitHubDataError(f"GitHub returned another parent for #{number}")
    created_at = value.get("created_at")
    updated_at = value.get("updated_at")
    if not isinstance(created_at, str) or not isinstance(updated_at, str):
        raise IncompleteGitHubDataError(f"issue #{number} has invalid timestamps")
    _stored_time(created_at)
    _stored_time(updated_at)


def _single(
    observations: tuple[FactObservation, ...],
    operation: str,
) -> FactObservation:
    if len(observations) != 1:
        raise RuntimeError(f"{operation} publication is not singular")
    return observations[0]


def _fact_object(fact: FactObservation, context: str) -> dict[str, Any]:
    if fact.coverage is not Coverage.COMPLETE:
        raise IncompleteGitHubDataError(f"{context} has no complete observation")
    return _object(fact.payload.get("value"), context)


def _fact_list(fact: FactObservation, context: str) -> list[dict[str, Any]]:
    if fact.coverage is not Coverage.COMPLETE:
        raise IncompleteGitHubDataError(f"{context} has no complete observation")
    return _objects(fact.payload.get("value"), context)


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IncompleteGitHubDataError(f"{context} is not an object")
    return value


def _objects(value: Any, context: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise IncompleteGitHubDataError(f"{context} is not an object collection")
    return value


def _optional_objects(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        return None
    return value


def _source_payload(
    operation: str,
    repository: str,
    resource_number: int,
    value: Any,
    source: str,
    raw: Any,
    cache: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = {
        "operation": operation,
        "repository": repository,
        "resource_number": resource_number,
        "source": source,
        "value": value,
        "raw": raw,
    }
    if cache is not None:
        payload["cache"] = cache
    return payload


def _commit_object_fact(
    repository: str,
    sha: str,
    references: Sequence[dict[str, Any]],
    result: Any,
    resource_number: int | None,
    observed_from: datetime,
    observed_until: datetime,
) -> FactDraft:
    if not isinstance(result, dict):
        raise GitStoreError(f"Git retention returned no result for {sha}")
    coverage = {
        "available": Coverage.COMPLETE,
        "partial": Coverage.PARTIAL,
        "unavailable": Coverage.UNAVAILABLE,
    }.get(result.get("status"))
    if coverage is None:
        raise GitStoreError(f"Git retention returned invalid status for {sha}")
    return FactDraft(
        family="commit-object",
        schema_version=2,
        subject_key=f"commit:{sha}",
        resource_number=resource_number,
        observed_from=observed_from,
        observed_until=observed_until,
        coverage=coverage,
        origin=Origin.GIT,
        payload={
            "operation": "GitCommitReconstruction",
            "repository": repository,
            "reference_scope": commit_reference_scope(references),
            "sha": sha,
            "value": result,
        },
    )


def _coverage_error(
    error: GitHubAPIError,
    *,
    unauthenticated: bool,
) -> Coverage | None:
    if error.status_code in {401, 403}:
        return Coverage.FORBIDDEN
    if error.status_code == 404:
        return Coverage.UNAVAILABLE
    message = str(error).lower()
    if unauthenticated and "authenticat" in message and "require" in message:
        return Coverage.FORBIDDEN
    return None


def _comparison_shas(pull: dict[str, Any], number: int) -> tuple[str, str]:
    base = _object(pull.get("base"), f"pull #{number} base")
    head = _object(pull.get("head"), f"pull #{number} head")
    return (
        _required_sha(base.get("sha"), f"pull #{number} base"),
        _required_sha(head.get("sha"), f"pull #{number} head"),
    )


def _embedded_review_requests(pull: dict[str, Any]) -> dict[str, Any] | None:
    users = pull.get("requested_reviewers")
    teams = pull.get("requested_teams")
    if (
        not isinstance(users, list)
        or any(not isinstance(user, dict) for user in users)
        or not isinstance(teams, list)
        or teams
    ):
        return None
    return {"users": users, "teams": []}


def _zero(value: Any) -> bool:
    return type(value) is int and value == 0


def _zero_count(value: Any) -> bool:
    return isinstance(value, dict) and _zero(value.get("total_count"))


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _required_sha(value: Any, context: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise IncompleteGitHubDataError(f"{context} has no valid commit ID")
    return value


def _repository_git_url(repository: dict[str, Any], full_name: str) -> str:
    html_url = repository.get("html_url")
    if isinstance(html_url, str) and html_url.startswith(("http://", "https://")):
        return f"{html_url.rstrip('/')}.git"
    return default_git_url(full_name)


def _error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _stored_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("GitHub timestamp has no timezone")
    return parsed.astimezone(UTC)
