"""Import one stopped version-nine archive into fine-grained observations.

The importer is an explicit one-time bridge, not a runtime compatibility path. It
projects every stored collection from each selected legacy resource, observes
missing review threads and Issue relations at real current times, verifies
structured commit objects in the managed Git store, then restores unfinished
catalog work or seeds a conservative discovery checkpoint.

Legacy bundle projections use the migration execution time and ``origin=import``.
This deliberately records the operator-approved assumed-unchanged migration policy;
it does not reinterpret the legacy run target as an actual source-read timestamp.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..client import GitHubAPI, GitHubResource
from ..commit_references import CommitReference, observation_commit_references
from ..errors import GitHubAPIError
from ..git_store import GitObjectStore, default_git_url, git_store_path
from ..locking import archive_lock
from ..observations import (
    Coverage,
    DiscoveryItemDraft,
    FactDraft,
    FactObservation,
    ObservationArchive,
    Origin,
    TaskDraft,
    iter_observations,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Sequence

_CORE_BATCH_SIZE = 25
_REFERENCE_BATCH_SIZE = 100
_GIT_BATCH_SIZE = 4096
_PENDING_PAGE_SIZE = 100
_PENDING_CURSOR_PREFIX = "import-v9:pending:"
_FACT_FAMILIES = {
    "commit-object": "commit-object",
    "commit-references": "commit-references",
    "git-refs": "git-refs",
    "issue-relations": "issue-relations",
    "review-threads": "pull-review-threads",
}
_REFERENCE_FAMILIES = {
    "issue-events",
    "issue-timeline",
    "pull-commits",
    "pull-review-comments",
    "pull-review-threads",
    "pull-reviews",
}


@dataclass(frozen=True, slots=True)
class V9ImportConfig:
    """Configure the explicit v9-to-v10 archive bridge."""

    source: Path
    destination: Path
    repository: str
    token: str | None = None
    api_url: str = "https://api.github.com"
    graphql_url: str | None = None
    api_version: str = "2022-11-28"
    request_timeout: float = 30.0
    concurrency: int = 8
    git_url: str | None = None
    git_destination: Path | None = None

    def __post_init__(self) -> None:
        source = Path(self.source)
        destination = Path(self.destination)
        owner, separator, repo = self.repository.partition("/")
        if not separator or not owner or not repo or "/" in repo:
            raise ValueError("repository must be 'owner/repo'")
        if source.resolve() == destination.resolve():
            raise ValueError("migration source and destination must differ")
        if self.concurrency < 1:
            raise ValueError("concurrency must be positive")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "destination", destination)
        if self.git_destination is not None:
            object.__setattr__(self, "git_destination", Path(self.git_destination))


@dataclass(frozen=True, slots=True)
class V9ImportResult:
    """Summarize the idempotent migration stages."""

    resources: int
    supplemental_facts: int
    live_facts: int
    referenced_commits: int
    checkpoint: datetime


@dataclass(frozen=True, slots=True)
class _LegacyResource:
    id: int
    run_id: int
    number: int
    kind: str
    present: bool
    summary_digest: str
    bundle_digest: str | None
    target_at: str


@dataclass(frozen=True, slots=True)
class _LegacyPending:
    number: int
    kind: str
    summary_digest: str


@dataclass(frozen=True, slots=True)
class _LegacyPendingCatalog:
    checkpoint: datetime
    observed_from: datetime
    items: tuple[_LegacyPending, ...]


class V9Importer:
    """Execute the resumable migration while both archive writers are locked.

    Args:
        config: Source v9 archive, new destination, repository, and network policy.
        api: Test or host-provided GitHub reader for R1/R3 backfill.
        git: Test or host-provided Git store for R2/R4 evidence.
        now: Time source for actual import and live observation windows.
    """

    def __init__(
        self,
        config: V9ImportConfig,
        *,
        api: Any | None = None,
        git: Any | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self._api = api
        self._git = git
        self._now = now
        self._owner, self._repo = config.repository.split("/", 1)
        self._store_lock = asyncio.Lock()

    async def migrate(self) -> V9ImportResult:
        """Run every import stage before restoring the discovery position.

        Returns:
            Counts for the selected current resources and completed observation work.
        """
        async with (
            archive_lock(self.config.source, wait=False),
            archive_lock(self.config.destination),
        ):
            source = _open_source(self.config.source, self.config.repository)
            try:
                resources = _current_resources(source)
                checkpoint = _source_checkpoint(source)
                pending = _pending_catalog(source, checkpoint)
                async with ObservationArchive(
                    self.config.destination,
                    self.config.repository,
                    self._git_destination(),
                ) as archive:
                    core = await self.import_core(source, archive, resources)
                    supplemental = await self.import_supplemental(source, archive)
                    live = await self.backfill_live(archive, resources)
                    references = await self.retain_references(archive)
                    await self.observe_refs(archive)
                    await self._restore_pending(archive, source, pending)
            finally:
                source.close()
        return V9ImportResult(core, supplemental, live, references, checkpoint)

    async def _restore_pending(
        self,
        archive: ObservationArchive,
        source: sqlite3.Connection,
        pending: _LegacyPendingCatalog | None,
    ) -> None:
        """Restore a stopped cold catalog as one current recoverable cycle.

        Args:
            archive: Open v10 destination after all imported facts are durable.
            source: Validated read-only v9 connection supplying catalog summaries.
            pending: Validated unfinished catalog, or None when discovery had no work.
        """
        if pending is None:
            await archive.seed_discovery_checkpoint(_source_checkpoint(source))
            return
        committed = await archive.discovery_checkpoint()
        if committed is not None:
            if committed < pending.checkpoint:
                raise RuntimeError("destination checkpoint precedes imported discovery")
            return
        cycle = await archive.start_cycle(pending.checkpoint)
        if cycle.started_at != pending.checkpoint or cycle.checkpoint_from is not None:
            raise RuntimeError("destination has another active sync cycle")
        await archive.enqueue_tasks(
            cycle.id,
            (TaskDraft("git-refs", "git-refs", "repository", {}),),
        )
        cursor = await archive.begin_discovery(cycle.id, _pending_cursor(0))
        if cursor is None:
            return
        page_index = _pending_page(cursor)
        if page_index != cycle.discovery_pages:
            raise RuntimeError("imported discovery cursor conflicts with durable pages")
        pages = tuple(_chunks(pending.items, _PENDING_PAGE_SIZE))
        for index in range(page_index, len(pages)):
            chunk = pages[index]
            observed_until = _utc(self._now())
            items = tuple(
                DiscoveryItemDraft(
                    number=item.number,
                    kind=item.kind,
                    observed_from=pending.observed_from,
                    observed_until=observed_until,
                    summary=_blob(source, item.summary_digest),
                )
                for item in chunk
            )
            tasks = tuple(
                TaskDraft(
                    task_key=f"parent:{item.number}",
                    kind="parent",
                    subject_key=f"issue:{item.number}",
                    resource_number=item.number,
                    payload={"number": item.number},
                )
                for item in chunk
            )
            pulls = [item.number for item in chunk if item.kind == "pull"]
            if pulls:
                tasks += (
                    TaskDraft(
                        task_key=f"closing:import-v9:{index}",
                        kind="closing-issues",
                        subject_key=f"import-v9-page:{index}",
                        payload={"numbers": pulls},
                    ),
                )
            next_cursor = (
                None if index + 1 == len(pages) else _pending_cursor(index + 1)
            )
            cycle = await archive.save_discovery_page(
                cycle.id,
                _pending_cursor(index),
                next_cursor,
                items,
                tasks,
            )

    async def import_core(
        self,
        source: sqlite3.Connection,
        archive: ObservationArchive,
        resources: Sequence[_LegacyResource],
    ) -> int:
        """Project current v9 bundles in bounded atomic batches.

        Args:
            source: Validated read-only v9 SQLite connection.
            archive: Open v10 destination.
            resources: Latest v9 resource version for each observed parent.

        Returns:
            Number of selected Issue/PR resources, including unavailable rows.
        """
        for chunk in _chunks(resources, _CORE_BATCH_SIZE):
            key = _batch_key("v9-core", ((item.id, item.bundle_digest) for item in chunk))
            if await archive.publication(key) is not None:
                continue
            observed_at = _utc(self._now())
            facts = []
            identities: set[tuple[str, str]] = set()
            for item in chunk:
                for fact in _resource_facts(source, item, observed_at):
                    identity = fact.family, fact.subject_key
                    if identity in identities:
                        continue
                    identities.add(identity)
                    facts.append(fact)
            await archive.publish(key, "import", _utc(self._now()), tuple(facts))
        return len(resources)

    async def import_supplemental(
        self,
        source: sqlite3.Connection,
        archive: ObservationArchive,
    ) -> int:
        """Retain conclusive v9 supplemental observations with their real windows.

        Args:
            source: Validated read-only v9 SQLite connection.
            archive: Open v10 destination.

        Returns:
            Number of imported conclusive fact attempts.
        """
        count = 0
        for row in _supplemental_rows(source):
            status = str(row["status"])
            if status == "failed":
                continue
            family = _FACT_FAMILIES.get(str(row["fact_kind"]))
            if family is None:
                continue
            key = f"v9-fact:{row['source_table']}:{row['id']}:{row['payload_digest']}"
            if await archive.publication(key) is not None:
                count += 1
                continue
            payload = _blob(source, str(row["payload_digest"]))
            payload["legacy_import"] = {
                "source_schema": 9,
                "source_table": row["source_table"],
                "source_id": row["id"],
                "source_digest": row["source_digest"],
            }
            if family in {"git-refs", "issue-relations"} and "value" not in payload:
                payload["value"] = payload.get("raw")
            fact = FactDraft(
                family=family,
                schema_version=int(row["schema_version"]),
                subject_key=str(row["subject_key"]),
                resource_number=(
                    None if row["resource_number"] is None else int(row["resource_number"])
                ),
                source_digest=None,
                observed_from=_time(str(row["observed_from"])),
                observed_until=_time(str(row["observed_until"])),
                coverage=Coverage(status),
                origin=Origin.IMPORT,
                payload=payload,
            )
            await archive.publish(key, "import", _utc(self._now()), (fact,))
            count += 1
        return count

    async def backfill_live(
        self,
        archive: ObservationArchive,
        resources: Sequence[_LegacyResource],
    ) -> int:
        """Observe current R1 review threads and R3 Issue relations.

        Args:
            archive: Open v10 destination.
            resources: Imported current parents selecting the live operation.

        Returns:
            Number of parents with an idempotently published R1 or R3 result.
        """
        api, owned = self._make_api()
        try:
            selected = [item for item in resources if item.present and item.bundle_digest]
            completed = 0
            for chunk in _chunks(selected, self.config.concurrency):
                results = await asyncio.gather(
                    *(self._backfill_parent(api, archive, item) for item in chunk),
                    return_exceptions=True,
                )
                failures = [result for result in results if isinstance(result, BaseException)]
                completed += len(results) - len(failures)
                if failures:
                    raise failures[0]
            return completed
        finally:
            if owned:
                await api.close()

    async def retain_references(self, archive: ObservationArchive) -> int:
        """Derive all structured references and verify their Git objects.

        Args:
            archive: Open v10 destination containing imported and live facts.

        Returns:
            Number of distinct structured commit IDs in migration scope.
        """
        shas: set[str] = set()
        async for chunk in _reference_source_batches(self.config.destination):
            key = _batch_key(
                "v9-references",
                ((fact.id, fact.payload_digest) for fact in chunk),
            )
            facts = []
            for source in chunk:
                references = observation_commit_references(source.family, source.payload)
                for reference in references:
                    shas.add(reference.sha)
                facts.append(
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
                                _reference_payload(reference)
                                for reference in references
                            ],
                        },
                    ),
                )
            if await archive.publication(key) is None:
                await archive.publish(key, "import", _utc(self._now()), tuple(facts))
        git = self._make_git()
        selected = sorted(shas)
        pending = []
        for chunk in _chunks(selected, _GIT_BATCH_SIZE):
            key = _batch_key("v9-objects", ((sha,) for sha in chunk))
            if await archive.publication(key) is not None:
                continue
            pending.append((key, chunk))
        if pending:
            observed_from = _utc(self._now())
            results = await git.retain_commits(
                [sha for _, chunk in pending for sha in chunk],
            )
            observed_until = _utc(self._now())
        for key, chunk in pending:
            facts = tuple(
                _commit_object_fact(
                    self.config.repository,
                    sha,
                    results,
                    observed_from,
                    observed_until,
                )
                for sha in chunk
            )
            await archive.publish(key, "import", _utc(self._now()), facts)
        return len(selected)

    async def observe_refs(self, archive: ObservationArchive) -> FactObservation:
        """Record one current native branch/tag mapping after Git migration work.

        Args:
            archive: Open v10 destination.

        Returns:
            The idempotent R4 observation for this migration.
        """
        key = "v9-migration:git-refs"
        existing = await archive.publication(key)
        if existing is not None:
            return _single(existing)
        observed_from = _utc(self._now())
        refs = await self._make_git().sync_upstream()
        observed_until = _utc(self._now())
        facts = await archive.publish(
            key,
            "import",
            _utc(self._now()),
            (
                FactDraft(
                    family="git-refs",
                    subject_key="repository",
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
        )
        return _single(facts)

    async def _backfill_parent(
        self,
        api: Any,
        archive: ObservationArchive,
        item: _LegacyResource,
    ) -> FactObservation:
        family = "pull-review-threads" if item.kind == "pull" else "issue-relations"
        key = f"v9-live:{family}:{item.number}"
        async with self._store_lock:
            existing = await archive.publication(key)
        if existing is not None:
            return _single(existing)
        observed_from = _utc(self._now())
        try:
            resource = (
                await api.pull_review_threads(self._owner, self._repo, item.number)
                if item.kind == "pull"
                else await api.issue_relations(self._owner, self._repo, item.number)
            )
        except GitHubAPIError as exc:
            coverage = _coverage_error(exc)
            if coverage is None:
                raise
            payload = {
                "operation": family,
                "repository": self.config.repository,
                "resource_number": item.number,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "status_code": exc.status_code,
                    "url": exc.url,
                },
            }
        else:
            coverage = Coverage.COMPLETE
            payload = _source_payload(
                "PullReviewThreads" if item.kind == "pull" else "IssueRelations",
                self.config.repository,
                item.number,
                resource,
            )
        fact = FactDraft(
            family=family,
            subject_key=f"{item.kind}:{item.number}",
            resource_number=item.number,
            observed_from=observed_from,
            observed_until=_utc(self._now()),
            coverage=coverage,
            origin=Origin.API,
            payload=payload,
        )
        async with self._store_lock:
            result = await archive.publish(key, "import", _utc(self._now()), (fact,))
        return _single(result)

    def _make_api(self) -> tuple[Any, bool]:
        if self._api is not None:
            return self._api, False
        return (
            GitHubAPI(
                token=_token(self.config.token),
                base_url=self.config.api_url,
                graphql_url=self.config.graphql_url,
                api_version=self.config.api_version,
                timeout=self.config.request_timeout,
            ),
            True,
        )

    def _make_git(self) -> Any:
        if self._git is not None:
            return self._git
        self._git = GitObjectStore(
            self._git_destination(),
            self.config.repository,
            self.config.git_url or default_git_url(self.config.repository),
            token=_token(self.config.token),
        )
        return self._git

    def _git_destination(self) -> Path:
        return self.config.git_destination or git_store_path(self.config.source)


def _open_source(path: Path, repository: str) -> sqlite3.Connection:
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        metadata = {
            str(row["key"]): str(row["value"])
            for row in source.execute("SELECT key, value FROM archive_meta")
        }
    except sqlite3.Error:
        source.close()
        raise
    if metadata.get("schema_version") != "9":
        source.close()
        raise ValueError("source is not a version-nine GitHub archive")
    if metadata.get("repository") != repository:
        source.close()
        raise ValueError("source archive belongs to another repository")
    return source


def _current_resources(source: sqlite3.Connection) -> list[_LegacyResource]:
    rows = source.execute(
        """
        WITH ranked AS (
            SELECT
                v.*,
                r.target_at,
                ROW_NUMBER() OVER (PARTITION BY v.number ORDER BY v.id DESC) AS rank
            FROM resource_versions AS v
            JOIN pull_runs AS r ON r.id = v.run_id
        )
        SELECT * FROM ranked WHERE rank = 1 ORDER BY number
        """,
    )
    return [
        _LegacyResource(
            id=int(row["id"]),
            run_id=int(row["run_id"]),
            number=int(row["number"]),
            kind=str(row["kind"]),
            present=bool(row["present"]),
            summary_digest=str(row["summary_digest"]),
            bundle_digest=(
                None if row["bundle_digest"] is None else str(row["bundle_digest"])
            ),
            target_at=str(row["target_at"]),
        )
        for row in rows
    ]


def _source_checkpoint(source: sqlite3.Connection) -> datetime:
    row = source.execute(
        """
        SELECT MAX(r.target_at)
        FROM pull_runs AS r
        WHERE EXISTS (
            SELECT 1 FROM resource_versions AS v WHERE v.run_id = r.id
        )
        """,
    ).fetchone()
    if row is None or row[0] is None:
        raise ValueError("source archive has no observed resources")
    return _time(str(row[0]))


def _pending_catalog(
    source: sqlite3.Connection,
    checkpoint: datetime,
) -> _LegacyPendingCatalog | None:
    rows = source.execute(
        """
        SELECT run_id, number, kind, summary_digest
        FROM pull_tasks
        WHERE completed = 0
        ORDER BY id
        """,
    ).fetchall()
    if not rows:
        return None
    run_ids = {int(row["run_id"]) for row in rows}
    if len(run_ids) != 1:
        raise ValueError("v9 archive has pending tasks from multiple runs")
    run_id = run_ids.pop()
    pass_row = source.execute(
        """
        SELECT r.started_at, p.cutoff_at, p.catalog_complete
        FROM pull_runs AS r
        JOIN pull_passes AS p ON p.run_id = r.id
        WHERE r.id = ? AND p.name = 'closing'
        """,
        (run_id,),
    ).fetchone()
    if pass_row is None or not bool(pass_row["catalog_complete"]):
        raise ValueError("v9 pending run has no complete closing catalog")
    cutoff = _time(str(pass_row["cutoff_at"]))
    if cutoff != checkpoint:
        raise ValueError("v9 pending catalog has another discovery boundary")
    items = tuple(
        _LegacyPending(
            number=int(row["number"]),
            kind=str(row["kind"]),
            summary_digest=str(row["summary_digest"]),
        )
        for row in rows
    )
    if (
        any(item.kind not in {"issue", "pull"} for item in items)
        or any(not item.summary_digest for item in items)
        or len({item.number for item in items}) != len(items)
    ):
        raise ValueError("v9 pending catalog has invalid task identities")
    return _LegacyPendingCatalog(
        checkpoint=cutoff,
        observed_from=_time(str(pass_row["started_at"])),
        items=items,
    )


def _pending_cursor(page: int) -> str:
    return f"{_PENDING_CURSOR_PREFIX}{page}"


def _pending_page(cursor: str) -> int:
    if not cursor.startswith(_PENDING_CURSOR_PREFIX):
        raise RuntimeError("destination has another discovery cursor")
    value = cursor.removeprefix(_PENDING_CURSOR_PREFIX)
    try:
        page = int(value)
    except ValueError as exc:
        raise RuntimeError("imported discovery cursor is invalid") from exc
    if page < 0:
        raise RuntimeError("imported discovery cursor is invalid")
    return page


async def _reference_source_batches(
    path: Path,
) -> AsyncIterator[tuple[FactObservation, ...]]:
    seen: set[str] = set()
    batch: list[FactObservation] = []
    for family in sorted(_REFERENCE_FAMILIES):
        async for fact in iter_observations(path, family=family):
            if (
                fact.coverage is not Coverage.COMPLETE
                or fact.payload_digest in seen
            ):
                continue
            seen.add(fact.payload_digest)
            batch.append(fact)
            if len(batch) == _REFERENCE_BATCH_SIZE:
                yield tuple(batch)
                batch.clear()
    if batch:
        yield tuple(batch)


def _resource_facts(
    source: sqlite3.Connection,
    item: _LegacyResource,
    observed_at: datetime,
) -> tuple[FactDraft, ...]:
    summary = _blob(source, item.summary_digest)
    legacy = {
        "source_schema": 9,
        "resource_version_id": item.id,
        "run_id": item.run_id,
        "legacy_target_at": item.target_at,
        "migration_policy": "assumed-unchanged",
    }
    facts = [
        _import_fact(
            "catalog-item",
            f"issue:{item.number}",
            item.number,
            observed_at,
            "LegacyCatalogItem",
            summary,
            summary,
            legacy,
        ),
    ]
    if not item.present or item.bundle_digest is None:
        facts.append(
            FactDraft(
                family="issue",
                subject_key=f"issue:{item.number}",
                resource_number=item.number,
                observed_from=observed_at,
                observed_until=observed_at,
                coverage=Coverage.UNAVAILABLE,
                origin=Origin.IMPORT,
                payload={
                    "operation": "LegacyUnavailableParent",
                    "repository": summary.get("repository"),
                    "resource_number": item.number,
                    "legacy_import": legacy,
                },
            ),
        )
        return tuple(facts)

    bundle = _blob(source, item.bundle_digest)
    cache = _bundle_cache(source, item.bundle_digest)
    if bundle.get("number") != item.number or bundle.get("kind") != item.kind:
        raise ValueError(f"legacy bundle identity mismatch for #{item.number}")
    sources = _optional_object(bundle.get("api_sources")) or {}
    facts.extend(
        _import_fact(
            family,
            f"issue:{item.number}",
            item.number,
            observed_at,
            operation,
            bundle.get(field),
            _raw_source(sources, source_key, bundle.get(field)),
            legacy,
            source=_source_name(sources, source_key),
            cache=_optional_object(cache.get(cache_key)),
        )
        for family, field, operation, source_key, cache_key in (
            ("issue", "issue", "Issue", "issue", "issue"),
            (
                "issue-comments",
                "issue_comments",
                "IssueComments",
                "issue_comments",
                "issue_comments",
            ),
            ("issue-timeline", "timeline", "IssueTimeline", "timeline", "timeline"),
            ("issue-events", "events", "IssueEvents", "events", "events"),
            ("issue-reactions", "reactions", "IssueReactions", "reactions", "reactions"),
        )
    )
    facts.extend(
        _reaction_facts(
            item,
            bundle,
            cache,
            sources,
            observed_at,
            legacy,
            "issue_comment_reactions",
            "issue-comment-reactions",
            "issue-comment",
        ),
    )
    pull = bundle.get("pull_request")
    if item.kind == "pull":
        pull = _object(pull, f"legacy pull #{item.number}")
        pull_sources = _optional_object(pull.get("api_sources")) or {}
        facts.extend(
            _import_fact(
                family,
                f"pull:{item.number}",
                item.number,
                observed_at,
                operation,
                pull.get(field),
                _raw_source(pull_sources, source_key, pull.get(field)),
                legacy,
                source=_source_name(pull_sources, source_key),
                cache=_optional_object(
                    (_optional_object(cache.get("pull_request")) or {}).get(cache_key),
                ),
            )
            for family, field, operation, source_key, cache_key in (
                ("pull", "detail", "PullRequest", "detail", "detail"),
                ("pull-reviews", "reviews", "PullReviews", "reviews", "reviews"),
                (
                    "pull-review-comments",
                    "review_comments",
                    "PullReviewComments",
                    "review_comments",
                    "review_comments",
                ),
                ("pull-commits", "commits", "PullCommits", "commits", "commits"),
                (
                    "pull-requested-reviewers",
                    "requested_reviewers",
                    "PullRequestedReviewers",
                    "requested_reviewers",
                    "requested_reviewers",
                ),
                (
                    "pull-closing-issues",
                    "closing_issues_references",
                    "ClosingIssuesReferences",
                    "closing_issues_references",
                    "closing_issues_references",
                ),
            )
        )
        facts.extend(
            _reaction_facts(
                item,
                pull,
                _optional_object(cache.get("pull_request")) or {},
                pull_sources,
                observed_at,
                legacy,
                "review_comment_reactions",
                "pull-review-comment-reactions",
                "pull-review-comment",
            ),
        )
        git = _object(pull.get("git"), f"legacy pull #{item.number} Git snapshot")
        facts.append(
            _import_fact(
                "pull-git",
                f"pull:{item.number}",
                item.number,
                observed_at,
                "PullGitSnapshot",
                git,
                git,
                legacy,
                coverage=(
                    Coverage.PARTIAL
                    if git.get("comparison_kind") == "unavailable"
                    else Coverage.COMPLETE
                ),
                source="git",
            ),
        )
    return tuple(facts)


def _reaction_facts(
    item: _LegacyResource,
    container: dict[str, Any],
    cache: dict[str, Any],
    sources: dict[str, Any],
    observed_at: datetime,
    legacy: dict[str, Any],
    field: str,
    family: str,
    subject_prefix: str,
) -> tuple[FactDraft, ...]:
    values = _object(container.get(field), f"legacy {field}")
    source_map = _optional_object(sources.get(field)) or {}
    cache_map = _optional_object(cache.get(field)) or {}
    facts = []
    for key, value in values.items():
        try:
            comment_id = int(key)
        except ValueError as exc:
            raise ValueError(f"legacy {field} has an invalid comment ID") from exc
        facts.append(
            _import_fact(
                family,
                f"{subject_prefix}:{comment_id}",
                item.number,
                observed_at,
                "CommentReactions",
                value,
                _raw_source(source_map, key, value),
                legacy,
                source=_source_name(source_map, key),
                cache=_optional_object(cache_map.get(key)),
            ),
        )
    return tuple(facts)


def _import_fact(
    family: str,
    subject_key: str,
    resource_number: int,
    observed_at: datetime,
    operation: str,
    value: Any,
    raw: Any,
    legacy: dict[str, Any],
    *,
    coverage: Coverage = Coverage.COMPLETE,
    source: str = "rest",
    cache: dict[str, Any] | None = None,
) -> FactDraft:
    payload = {
        "operation": operation,
        "resource_number": resource_number,
        "source": source,
        "value": value,
        "raw": raw,
        "legacy_import": legacy,
    }
    if cache is not None:
        payload["cache"] = cache
    return FactDraft(
        family=family,
        subject_key=subject_key,
        resource_number=resource_number,
        observed_from=observed_at,
        observed_until=observed_at,
        coverage=coverage,
        origin=Origin.IMPORT,
        payload=payload,
    )


def _supplemental_rows(source: sqlite3.Connection) -> Iterator[sqlite3.Row]:
    yield from source.execute(
        """
        SELECT
            'fact_versions' AS source_table,
            id, fact_kind, schema_version, subject_key, resource_number,
            source_digest, observed_from, observed_until, status, payload_digest
        FROM fact_versions
        UNION ALL
        SELECT
            'pending_fact_versions' AS source_table,
            id, fact_kind, schema_version, subject_key, resource_number,
            source_digest, observed_from, observed_until, status, payload_digest
        FROM pending_fact_versions
        ORDER BY source_table, id
        """,
    )


def _blob(source: sqlite3.Connection, digest: str) -> dict[str, Any]:
    row = source.execute(
        "SELECT codec, raw_size, payload FROM payload_blobs WHERE digest = ?",
        (digest,),
    ).fetchone()
    if row is None:
        raise ValueError(f"missing v9 payload {digest}")
    return _decode(digest, str(row["codec"]), int(row["raw_size"]), bytes(row["payload"]))


def _bundle_cache(source: sqlite3.Connection, bundle_digest: str) -> dict[str, Any]:
    row = source.execute(
        """
        SELECT cache_digest, codec, raw_size, payload
        FROM bundle_http_cache
        WHERE bundle_digest = ?
        """,
        (bundle_digest,),
    ).fetchone()
    if row is None:
        return {}
    return _decode(
        str(row["cache_digest"]),
        str(row["codec"]),
        int(row["raw_size"]),
        bytes(row["payload"]),
    )


def _decode(digest: str, codec: str, raw_size: int, payload: bytes) -> dict[str, Any]:
    if codec != "zlib-json-v1":
        raise ValueError(f"unsupported v9 payload codec {codec}")
    raw = zlib.decompress(payload)
    if len(raw) != raw_size or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"corrupt v9 payload {digest}")
    value = json.loads(raw)
    return _object(value, f"v9 payload {digest}")


def _raw_source(sources: dict[str, Any], key: str, fallback: Any) -> Any:
    source = _optional_object(sources.get(key))
    return fallback if source is None else source.get("raw", fallback)


def _source_name(sources: dict[str, Any], key: str) -> str:
    source = _optional_object(sources.get(key))
    name = None if source is None else source.get("source")
    return name if isinstance(name, str) else "rest"


def _source_payload(
    operation: str,
    repository: str,
    resource_number: int,
    resource: GitHubResource,
) -> dict[str, Any]:
    payload = {
        "operation": operation,
        "repository": repository,
        "resource_number": resource_number,
        "source": resource.source,
        "value": resource.value,
        "raw": resource.raw,
    }
    if resource.cache is not None:
        payload["cache"] = resource.cache
    return payload


def _commit_object_fact(
    repository: str,
    sha: str,
    results: dict[str, dict[str, Any]],
    observed_from: datetime,
    observed_until: datetime,
) -> FactDraft:
    result = results.get(sha)
    if not isinstance(result, dict) or result.get("sha") != sha:
        raise ValueError(f"Git retention omitted commit {sha}")
    status = result.get("status")
    if status not in {"available", "unavailable"}:
        raise ValueError(f"Git retention returned invalid status for {sha}")
    return FactDraft(
        family="commit-object",
        subject_key=f"commit:{sha}",
        observed_from=observed_from,
        observed_until=observed_until,
        coverage=(Coverage.COMPLETE if status == "available" else Coverage.UNAVAILABLE),
        origin=Origin.GIT,
        payload={
            "operation": "GitCommitRetention",
            "repository": repository,
            "sha": sha,
            "value": result,
        },
    )


def _coverage_error(error: GitHubAPIError) -> Coverage | None:
    if error.status_code in {401, 403}:
        return Coverage.FORBIDDEN
    if error.status_code == 404:
        return Coverage.UNAVAILABLE
    if error.status_code is None and "authenticat" in str(error).lower():
        return Coverage.FORBIDDEN
    return None


def _batch_key(prefix: str, identities: Any) -> str:
    raw = json.dumps(
        list(identities),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"{prefix}:{hashlib.sha256(raw).hexdigest()}"


def _chunks(values: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _single(observations: tuple[FactObservation, ...]) -> FactObservation:
    if len(observations) != 1:
        raise RuntimeError("migration publication is not singular")
    return observations[0]


def _reference_payload(reference: CommitReference) -> dict[str, Any]:
    return {
        "sha": reference.sha,
        "field_path": reference.field_path,
        "source_kind": reference.source_kind,
        "source_id": reference.source_id,
    }


def _optional_object(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{context} is not an object")
    return value


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _token(configured: str | None) -> str | None:
    value = configured or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    return value or None
