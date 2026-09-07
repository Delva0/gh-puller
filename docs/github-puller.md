<details>
<summary>Relevant sources</summary>

- [gh_puller/github/](../gh_puller/github/)
- [scripts/](../scripts/)
- [tests/github/](../tests/github/)
</details>

# GitHub observation archive

`gh_puller.github` records the observable Issue, pull-request, and Git state of one
GitHub repository as an offline source of truth. SQLite holds immutable semantic
observations and resumable writer state. A repository-bound bare Git store holds the
code objects named by those observations.

The archive is a faithful record of what its supported source operations observed.
It is not a claim that GitHub exposes every state transition, nor an attempt to
reproduce the GitHub web application.

## Observation model

The public data unit is a **fact observation**: one semantically closed source read
for a stable `(family, subject_key)` identity. Examples include the comments of
Issue 7, the review threads of PR 12, and the native branch/tag map of the repository.
A fact contains:

| Field | Meaning |
| --- | --- |
| `family`, `subject_key` | Stable semantic identity and current-fact key. |
| `schema_version` | Payload contract used by this observation. |
| `resource_number` | Related repository Issue/PR number, when applicable. |
| `observed_from`, `observed_until` | Real clock interval enclosing the complete source operation. |
| `coverage` | What conclusion that operation supports. |
| `origin` | `api`, `git`, `derived`, or `import`. |
| `payload_digest`, `payload` | Content-addressed JSON evidence and its decoded value. |
| `source_digest` | Digest of the source payload for a derived fact, when applicable. |
| `published_at` | Time the closed observation became durable in SQLite. |

One multi-page collection has one observation window; fields inside the same source
response do not receive invented per-field timestamps. Derived facts inherit the
window of their source. Later observations append rows instead of rewriting history.

`payload["value"]` is the stable operation result. `payload["raw"]` retains the
source-native JSON used to produce it. REST operations therefore retain unprojected
response fields. GraphQL operations retain every selected node and field, but cannot
retain fields absent from their query. `payload["cache"]`, when present, pairs HTTP
validators with the value that they validate; a valid `304` reuses those bytes while
creating a new, honestly timed observation.

Sources: [gh_puller/github/observations.py](../gh_puller/github/observations.py); [gh_puller/github/collector.py](../gh_puller/github/collector.py)

### Families and API requests

A family is a semantic completeness boundary for readers, not an HTTP endpoint,
page, or request. One parent task can publish many independently timed families, and
one family closes only after its transport work reaches a conclusive coverage result.
Already published sibling families remain readable if later work for the same
parent fails.

| Operation shape | Transport work | Published facts |
| --- | --- | --- |
| Issue/PR catalog page | One REST request shared by up to 100 parents | One `issue` observation per selected parent; PR detail remains a separate `pull` family. |
| Complete collection such as comments or reviews | One or more REST or GraphQL pages | One observation for the whole family, never one per page. |
| Batched closing-Issue lookup | A GraphQL request can serve several PRs; nested pagination may add requests | One `pull-closing-issues` observation per PR. |
| Count-proven empty collection or structured derivation | No additional API request | A complete derived observation with the source's time window. |
| Git refs, PR snapshots, or commit reconstruction | Local Git commands and, when needed, Git remote fetches | `git-refs`, `pull-git`, or `commit-object`; no REST/GraphQL request accounting. |

This separation keeps downstream identities stable when routing changes between REST
and GraphQL, preserves one honest completeness result across pagination, and makes
publication and retry granular without presenting an entire Issue/PR as one atomic
snapshot.

Sources: [gh_puller/github/client.py](../gh_puller/github/client.py); [gh_puller/github/collector.py](../gh_puller/github/collector.py)

### Time and offline queries

There is no repository-wide target timestamp or atomic run snapshot. Facts close and
become readable independently, which preserves the finest time precision available
from the source operation.

The three read views answer different questions:

| Reader | Result |
| --- | --- |
| `iter_observations(..., after=N)` | Immutable publication stream after a stable integer cursor. |
| `iter_current_facts(...)` | Latest actual observation for each selected fact identity. |
| `iter_facts_as_of(..., at=T)` | Latest observation per identity with `observed_until <= T`. |

An as-of result is therefore a time-bounded collection of independently observed
facts, not proof that all returned facts coexisted at one instant. Missing facts mean
“not observed by that boundary,” not an empty GitHub value.

```python
from datetime import UTC, datetime
from pathlib import Path

from gh_puller.github import iter_facts_as_of


async def issue_state_at(database: Path, at: datetime):
    return [
        fact
        async for fact in iter_facts_as_of(
            database,
            at.astimezone(UTC),
            subject_key="issue:7",
        )
    ]
```

### Coverage

Coverage describes one attempted source operation:

| Value | Supported conclusion |
| --- | --- |
| `complete` | The operation closed all declared pages or Git evidence; an empty value may be complete. |
| `null` | The source explicitly returned null for the declared contract. |
| `partial` | The payload identifies a known incomplete subset. |
| `forbidden` | The active GitHub identity could not read the source. |
| `unavailable` | The checked API or Git paths could not supply the source. |

Transport errors and detectably malformed or truncated responses do not produce a
fact. They leave a retryable task instead. By contrast, a conclusive `403` or `404`
can produce a `forbidden` or `unavailable` observation. Current and as-of readers
return the latest conclusion, including a non-complete one; consumers that require
complete data must filter `coverage` explicitly.

## Archived facts

When normal discovery selects an Issue or PR, the writer observes every applicable
supported family rather than only the signal that selected it:

| Families | Archived evidence |
| --- | --- |
| `issue` | Issue/PR root fields, actors, labels, milestone, state, counts, and unknown REST fields. |
| `issue-comments`, `issue-events`, `issue-timeline` | Complete supported conversation and event collections. |
| `issue-reactions`, `issue-comment-reactions` | Parent and per-comment reactions. |
| `issue-relations` | Parent, sub-Issues, blocked-by, and blocking GraphQL relations for an Issue. |
| `pull` | PR detail including base/head repositories, branches, SHAs, merge state, and change counts. |
| `pull-reviews`, `pull-review-threads`, `pull-review-comments` | Reviews plus native thread membership, resolution, position, and comments. |
| `pull-review-comment-reactions`, `pull-requested-reviewers` | Review-comment reactions and requested users/teams. |
| `pull-commits`, `pull-closing-issues` | Ordered PR commits and Issues that GitHub says the PR closes. |
| `pull-git` | Retained base/head/comparison/landing Git evidence for one PR observation. |
| `commit-references`, `commit-object` | Exact structured commit-field paths, acquisition attempts, retention refs, and layered Git reconstruction checks. |
| `git-refs` | Native branch/tag map, default branch, and symbolic `HEAD`. |

`catalog-item` may occur in explicitly imported archives to preserve a source catalog
record that was distinct from its parent detail. It is not a separate live discovery
promise.

Structured commit extraction follows only contract-defined commit fields in PR
commits, reviews, review comments and threads, timeline entries, and events. It does
not guess commit identities from prose, URLs, or arbitrary hexadecimal strings.

### Reconstruction boundary

| Downstream question | What the archive provides | Boundary |
| --- | --- | --- |
| Rebuild an observed Issue/PR discussion | Root, comments, reviews, threads, events, reactions, and relations | Only supported fields and observed states. |
| Find core developers or bug/WIP work | Actor, association, label, review, event, and merge evidence | The mining definition belongs to the downstream job. |
| Relate Issues and PRs | Explicit Issue relations, closing-Issue references, timeline/events, and structured commit links | Unsupported reverse references are not inferred. |
| Recover a PR's source and target | Base/head repository, branch and SHA plus pinned Git evidence | A source object may be explicitly unavailable. |
| Inspect changed code | Normal Git `show`, `diff`, `log`, `merge-base`, and object plumbing | Git LFS bytes, submodule contents, attachments, and unreachable objects are outside the promise. |
| Reproduce a GitHub page exactly | No | Rendering, permission-dependent controls, external assets, and live widgets are not archived. |

## Discovery and synchronization

`sync()` is asynchronous but does not return until one operational cycle completes.
The cycle exists only to recover discovery and work; it is not a public version or a
publication barrier.

```mermaid
flowchart TD
    Call["sync() freezes cycle start S"] --> Resume{"Active cycle?"}
    Resume -- "yes" --> Durable["Resume cursor and pending tasks"]
    Resume -- "no" --> Previous{"Checkpoint W exists?"}
    Previous -- "no: cold start" --> Cold["All Issue/PR roots"]
    Previous -- "yes: warm sync" --> Signals["Changed roots and comments since W - overlap"]
    Cold --> Order["Traverse by immutable creation time"]
    Signals --> Order
    Order --> Page["Persist one catalog page and its tasks"]
    Page --> Consume["Observe selected parents concurrently"]
    Consume --> Publish["Publish each closed fact immediately"]
    Publish --> More{"More pages or tasks?"}
    More -- "yes" --> Page
    More -- "no" --> Checkpoint["Advance discovery checkpoint W to S"]
    Durable --> Consume
```

### Cold start

With no committed discovery checkpoint, the writer traverses GitHub's combined
Issue/PR catalog in ascending creation order. Each page and its parent tasks are
committed before consumption. The current producer/consumer unit is one page: up to
100 catalog entries become durable, their parent operations run with bounded
concurrency, and then discovery proceeds to the next page.

If the process stops, the next call resumes the stored next-page URL and unfinished
tasks. It does not restart the catalog at page one. Facts whose source operations
already closed remain public and are not fetched again merely because the cycle is
incomplete.

### Warm synchronization

Let `W` be the last completed cycle's discovery checkpoint. A new cycle combines:

- Issue/PR roots last updated since `W - overlap`;
- Issue conversation comments returned since the same boundary;
- PR review comments returned since the same boundary.

GitHub's [repository-Issue endpoint](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#list-repository-issues)
applies the `since` filter to last-update time independently of its `sort` parameter.
Cold start omits `since`; warm synchronization sets `since=W-overlap`, which selects
roots by `updated_at`. Both use `sort=created&direction=asc`, so `created_at` controls
only their stable page order. Updating an existing result cannot move it across page
offsets; candidates entering after cycle start may be observed immediately or in the
next cycle. Comment feeds follow the same stable creation order.

Comment feeds are discovery signals: the writer reobserves the complete supported
parent, not just the returned comment. Timestamp overlap makes equal and
second-resolution boundary values harmless; task and publication identities remove
duplicates within the cycle.

The cycle start `S` is fixed before network work. The checkpoint advances from `W`
to `S` only after discovery reaches its terminal page and every task is complete.
Facts read while a long cycle runs keep their actual later observation windows. The
next cycle still starts from `S`, so work occurring during the preceding cycle is not
discarded by advancing to its completion time.

Sources: [gh_puller/github/syncer.py](../gh_puller/github/syncer.py); [tests/github/test_syncer.py](../tests/github/test_syncer.py)

### Discovery limits

GitHub does not provide a repository-wide change feed for every child collection.
The writer therefore uses a deliberate best-effort boundary:

- a silently deleted Issue or PR may remain at its last observation;
- deleted or edited comments, reactions, reviews, review threads, timeline events,
  requested reviewers, and Issue relations may remain stale when no supported signal
  selects their parent;
- states that appear and disappear between source reads were never observed;
- permission-hidden or API-unsupported fields cannot be reconstructed;
- a durable page cursor records completed work but does not turn GitHub's live listing
  into a repository snapshot.

The writer does not fall back to an expensive full repository scan when counts look
suspicious. Once a parent is selected by a supported signal, however, every promised
family is reobserved and detectable pagination inconsistencies fail the task instead
of silently truncating it.

### Failure, idempotency, and rate limits

A catalog page, task definition, fact batch, and task completion are each committed
at their own safe boundary. Publication keys make a retried source operation return
the original rows or reject a conflicting definition. The archive lock permits one
writer per canonical database; unrelated databases may be written concurrently and
consume independent shares of the same GitHub account quota.

Transient HTTP and Git failures retry with bounded exponential backoff. Primary and
secondary rate limits wait inside the active async call and recheck periodically, so
the caller remains blocked until completion or cancellation. When an atomic client
operation has equivalent REST and GraphQL implementations, the client chooses using
their latest known relative capacity and tries the other transport when appropriate.
Transport-specific operations remain on their required quota.

## Explicit refresh and backfill

Maintenance jobs read declared sources without changing discovery checkpoint `W`.
They are useful when a research sample must be current despite having no discovery
signal, or when a finite published history needs a new verification baseline.

```mermaid
flowchart TD
    Request["Declare targets and desired families"] --> Plan["Expand source dependencies"]
    Plan --> Scope["Persist requested and effective scope"]
    Scope --> Work["Claim pending tasks"]
    Work --> Read["Read API or verify Git"]
    Read --> Publish["Atomically append closed facts"]
    Publish --> Done{"All tasks have outcomes?"}
    Done -- "no" --> Work
    Done -- "yes" --> Close["Close job; leave W unchanged"]
    Read -- "retryable failure" --> Error["Persist attempt and error"]
    Error --> Work
```

Only one maintenance job is active per archive. Its scope, task population, attempts,
errors, outcomes, and request count are durable. A process restart resumes pending
tasks. A fact published before interruption is recognized by its publication key and
is not reread; a retryable failure publishes no substitute fact and therefore cannot
overwrite the last successful observation.

An optional caller idempotency key names one exact request. Reusing it resumes or
returns that job. Without a key, a matching interrupted job resumes, while every call
after completion creates a fresh observation. Maintenance facts use the same global
observation IDs and `iter_observations(after=N)` stream as sync facts. Their
`maintenance_job_id` and `maintenance_task_id` identify the owning work.

Sources: [gh_puller/github/maintenance.py](../gh_puller/github/maintenance.py); [gh_puller/github/observations.py](../gh_puller/github/observations.py)

### Targeted refresh

With no `--family`, each numbered target refreshes every applicable live family. An
explicit `--family` narrows the desired result; the caller selects semantics, while
the maintainer expands and executes the dependencies needed to support them.

| Target | Applicable families |
| --- | --- |
| Archived Issue number | `issue`, `issue-comments`, `issue-events`, `issue-timeline`, `issue-reactions`, `issue-comment-reactions`, `issue-relations`, `commit-references`, and `commit-object`. |
| Archived PR number | The Issue families except `issue-relations`, plus `pull`, `pull-reviews`, `pull-review-threads`, `pull-review-comments`, `pull-review-comment-reactions`, `pull-commits`, `pull-requested-reviewers`, `pull-closing-issues`, and `pull-git`. |
| Commit ID | `commit-object`; repeats provenance-backed acquisition and reconstruction checks for that exact ID. |
| Repository | `git-refs`, selected explicitly without a numbered target. |

PR and Issue roots must have at least one complete archived observation. A newer
`forbidden` or `unavailable` root does not disable an explicit retry. Repeat
`--pull`, `--issue`, or `--commit` to select several targets. Repeated `--family`
values form one desired family set: every supplied target kind must match at least
one value, and every value must match a supplied target.

Every numbered refresh first rereads its `issue` root to confirm current existence
and Issue/PR kind. Other dependency examples are comments before per-comment
reactions, PR detail and review threads before review-comment fallback, and all
applicable structured sources—two for an Issue and six for a PR—before a requested
`commit-references` or parent-scoped `commit-object` result. A newly observed complete
structured source is always scanned for its contract-defined commit fields and those
commits are checked in the Git store. Thus a narrow source refresh does not leave its
derived reference evidence pending.

The durable job scope records `requested_families`, `effective_families`, and the raw
families used for structured-reference scans. An interrupted retry reuses each
already published source operation within that plan. `catalog-item` remains an
import-only family and cannot be actively refreshed. A numbered refresh does not
implicitly refresh repository Git refs.

```bash
uv run -m gh_puller.github refresh \
  vllm-project/vllm archives/vllm.sqlite3 \
  --pull 24324 \
  --family pull-review-threads

uv run -m gh_puller.github refresh \
  vllm-project/vllm archives/vllm.sqlite3 \
  --pull 24324 --issue 4395

uv run -m gh_puller.github refresh \
  vllm-project/vllm archives/vllm.sqlite3 \
  --commit COMMIT_SHA --idempotency-key research-sample-1

uv run -m gh_puller.github refresh \
  vllm-project/vllm archives/vllm.sqlite3 \
  --family git-refs
```

### Structured-commit baseline

`backfill` freezes the current maximum observation ID as a **source cutoff** `N`.
Its baseline consists of every complete raw observation at `id <= N` from PR
commits, reviews, review comments, review threads, and Issue/PR timeline and event
families. It extracts commit fields from those raw payloads directly; the derived
reference index is never used to decide which source observations or commit IDs
exist.

```mermaid
flowchart TD
    Freeze["Freeze raw source cutoff N"] --> Enumerate["Enumerate complete contract sources with id <= N"]
    Enumerate --> Derive["Rebuild missing commit-reference scans"]
    Derive --> Barrier["All frozen sources scanned, including empty results"]
    Barrier --> Verify["Verify unique commit IDs without schema-two outcomes"]
    Verify --> Complete["Close the frozen baseline"]
```

Existing exact `commit-references` facts are reused. A missing scan becomes a
recoverable first-stage task even when the raw payload contains no commit ID; its
published empty result proves that the source was inspected. Git verification starts
only after every such task completes. The second stage deduplicates commit IDs while
retaining every distinct source edge in the resulting provenance.

The boundary applies to the raw source, not to publication time of its derivative.
A source observation with `id <= N` remains in the baseline when its
`commit-references` fact is published at an ID greater than `N`. A raw source first
published after `N` belongs to the next baseline. The frozen job records a digest and
counts for its source population, reference edges, empty observations, reused scans,
and task population, so a zero-target completion is distinguishable from an
unexamined index.

Source enumeration and reference rebuilding read SQLite only and consume no GitHub
API requests. Git-object tasks may still fetch known Git remotes when the managed
store lacks an object. Both stages are durable: interruption resumes the same source
cutoff, reuses already published scans or object results, and never advances normal
discovery checkpoint `W`. Job completion means every unique commit in the frozen
raw-source population has a schema-two outcome; it does not mean every commit was
obtainable.

```bash
uv run -m gh_puller.github backfill \
  vllm-project/vllm archives/vllm.sqlite3 \
  --idempotency-key structured-commits-2026-09
```

`commit_reference_index` is a rebuildable acceleration index used after the frozen
source population is closed. The immutable raw observation defines baseline
membership; the corresponding `commit-references` payload preserves parent, source
fact, field path, and source-object identity. Each `commit-object` result links back
to the supporting reference observation IDs and records the number of source edges
checked.

## SQLite and Git archive pair

For a destination named `DATABASE`, the archive boundary is:

```text
DATABASE       SQLite facts, observation history, and recovery state
DATABASE.git   Repository-bound bare Git object store by default
```

`archive_meta` binds the current archive-format identifier, repository identity, Git
layout, and the absolute Git-store path. Opening a database with another format,
repository, layout, or Git-store path is rejected. Back up both members and restore
them to their original paths. For an archive created with a non-default Git store,
every writer must pass that exact already-bound path with `--git-destination`; the CLI
has no migration or rebind operation.

The durable SQLite relations are grouped by responsibility:

| Relations | Responsibility |
| --- | --- |
| `fact_observations`, `fact_batches`, `payload_blobs` | Immutable facts, atomic publication order, and compressed canonical JSON. |
| `fact_heads`, `current_facts`, `fact_records` | Latest-by-observation-time identities and joined encoded payload records. |
| `sync_cycles`, `discovery_items`, `discovery_signals`, `sync_tasks` | Recoverable writer state; not GitHub facts. |
| `maintenance_jobs`, `maintenance_tasks` | Frozen refresh/backfill scopes, attempts, progress, outcomes, and errors. |
| `commit_reference_index` | Rebuildable acceleration index over immutable structured-reference facts. |
| `fact_schemas`, `archive_meta` | Format registry and archive binding. |

Sources: [gh_puller/github/schema.py](../gh_puller/github/schema.py); [gh_puller/github/observations.py](../gh_puller/github/observations.py)

### Git evidence

Current upstream branches and tags use native refs. Before updating them, the writer
pins their old tips so a force-push or deletion cannot erase previously observed
history:

```text
refs/heads/<branch>
refs/tags/<tag>
refs/github-archive/upstream/heads/<sha>
refs/github-archive/upstream/tags/<sha>
```

PR and structured-commit evidence uses repository-name-independent immutable refs:

```text
refs/github-archive/pulls/<n>/bases/<sha>
refs/github-archive/pulls/<n>/heads/<sha>
refs/github-archive/pulls/<n>/comparisons/<sha>
refs/github-archive/pulls/<n>/landings/<sha>
refs/github-archive/commits/<sha>
```

The upstream repository is synchronized once per cycle. If a PR head is already in
that graph, no separate PR fetch is needed. Otherwise the writer fetches the original
PR head, which preserves open and closed-unmerged histories as well as pre-squash or
pre-rebase commits when GitHub still exposes them. Batched PR fetches start at
`--git-batch-size`; structured-commit sources sharing one remote use the same bound.
Both recursively split on transient transfer failure.

The writer keeps Git lookup cost bounded as evidence accumulates. It packs loose
refs after 256 additions, runs Git's incremental multi-pack maintenance at 64 pack
files, and refreshes the commit graph with either operation. These are derived-index
and physical-layout changes: ref names, object IDs, and SQLite facts do not change.

`comparison_kind=merge_base` names the unique merge base for an offline PR diff.
`empty_tree` represents unrelated histories. `unavailable` records which required
objects could not be obtained without claiming a complete diff. A landing ref is
recorded when GitHub identifies a merged result and that object is available;
`history_preserved` says whether the original head is its ancestor, not which merge
button was used.

The store is ordinary bare Git and uses Git's content-addressed object namespace.
Different PRs that name the same commit share the same object while SQLite preserves
their separate relationships.

Structured commit retention checks sources in a bounded order: the managed object
store, provenance-backed PR or fork branch refs, then the managed upstream branches
and tags. Before transferring a fork branch, the writer observes its advertised tip
with bounded parallelism. A missing branch needs no pack transfer; a tip identical to
the already fetched PR ref reuses that content-addressed history; a different tip is
fetched normally. It does not guess unrelated repositories. Each attempted source
records its repository, ref, real time window, outcome, and error when the ref is
conclusively absent. Authentication and transport failures fall back to the exact ref
fetch and leave the task retryable instead of being published as object unavailability.

A schema-two `commit-object` fact separates four claims:

| Check | Meaning |
| --- | --- |
| `endpoint` | The named object is a commit with a readable root tree. |
| `snapshot` | Every tree and blob reachable from that root tree is locally readable. |
| `history` | The commit, all reachable parents, and their tree/blob closure are locally readable. |
| `retention` | An immutable `refs/github-archive/commits/<sha>` ref pins the object graph against GC. |

The checks use native Git object traversal. Fact coverage is `complete` only when the
reachable history closure is complete, `partial` when the endpoint is available but
the promised closure is not, and `unavailable` when the checked known sources do not
provide the endpoint. Git LFS payloads and submodule repositories are external to
that object closure and remain outside this guarantee. Schema-one historical
`commit-object` facts use the older endpoint/root-tree check; their `complete` value
does not imply a complete history closure. Backfill emits schema-two evidence rather
than reinterpreting them.

Sources: [gh_puller/github/git_store.py](../gh_puller/github/git_store.py); [gh_puller/github/archive_format.py](../gh_puller/github/archive_format.py)

### Direct offline use

The public Python readers decode payloads without network access. SQL and native Git
remain the stable, unrestricted downstream boundary, so specialized mining can build
new databases without a thick project SDK.

```bash
git --git-dir archives/repository.sqlite3.git show COMMIT_SHA
git --git-dir archives/repository.sqlite3.git diff \
  refs/github-archive/pulls/7/comparisons/COMPARISON_SHA \
  refs/github-archive/pulls/7/heads/HEAD_SHA
git --git-dir archives/repository.sqlite3.git rev-list --all
```

Clone the canonical bare store before adding downstream refs or changing Git data:

```bash
git clone --mirror archives/repository.sqlite3.git derived/repository.git
git --git-dir derived/repository.git branch experiment COMMIT_SHA
```

SQLite readers may freely query or copy the database, but only the archive writer
should mutate the canonical pair.

## Running the writer

The CLI loads `.env`, prefers `GH_TOKEN` over `GITHUB_TOKEN`, writes progress to
stderr, and emits completed operation JSON to stdout. Use an authenticated token for
a production archive:

```dotenv
GH_TOKEN=github_pat_your_token
```

Run or resume one cycle:

```bash
uv run -m gh_puller.github once \
  vllm-project/vllm archives/vllm.sqlite3
```

The equivalent library call is asynchronous and blocks the task until the cycle
completes:

```python
import asyncio
from pathlib import Path

from gh_puller.github import GitHubSyncConfig, sync


async def main() -> None:
    await sync(GitHubSyncConfig("vllm-project/vllm", Path("archives/vllm.sqlite3")))


asyncio.run(main())
```

`schedule` invokes the same operation immediately, then after each completion waits
for the next UTC Unix-epoch-aligned interval boundary. The boundary controls only
invocation time; it never changes an observation timestamp. Missed boundaries are
not fabricated as empty cycles.

```bash
uv run -m gh_puller.github schedule \
  vllm-project/vllm archives/vllm.sqlite3 \
  --interval 1h
```

The interval is a positive integer followed by `s`, `m`, `h`, or `d`. Other relevant
controls are `--concurrency`, `--git-batch-size`, `--overlap-seconds`,
`--request-timeout`, `--git-url`, `--git-destination`, and `--no-progress`.

### Managed Linux service

The systemd helper binds one service to the canonical database path. Its 12-character
writer ID is the prefix of the SHA-256 digest of that path; the repository is service
configuration, not part of writer identity. Different databases may have independent
writers, including writers for the same repository. The archive lock prevents two
entry points from writing one database.

`install` is privileged because it creates a system unit and a narrowly scoped polkit
rule. It registers the service disabled and inactive. The bound non-root service user
can then start, stop, or restart it without `sudo`:

```bash
sudo scripts/github-puller-daemon.sh install \
  vllm-project/vllm archives/vllm.sqlite3 \
  --interval 1h --concurrency 8

scripts/github-puller-daemon.sh start archives/vllm.sqlite3
scripts/github-puller-daemon.sh stop archives/vllm.sqlite3
scripts/github-puller-daemon.sh restart archives/vllm.sqlite3
```

`GH_PULLER_SERVICE_USER` selects the owner; under ordinary `sudo`, `SUDO_USER` is the
default. Reinstalling updates the unit but leaves it stopped. It rejects repository,
database, or owner rebinding.

List writers in a narrow table, then inspect one database or writer ID in detail:

```bash
scripts/github-puller-daemon.sh status
scripts/github-puller-daemon.sh status archives/vllm.sqlite3
scripts/github-puller-daemon.sh status 0123456789ab
scripts/github-puller-daemon.sh watch archives/vllm.sqlite3
scripts/github-puller-daemon.sh watch -n 5 0123456789ab
scripts/github-puller-daemon.sh logs archives/vllm.sqlite3
```

The detail view combines systemd and journald with read-only SQLite state. Its quota
values are the latest response headers already observed by the writer; status makes
no GitHub request. `watch` keeps one monitor process alive and reuses archive statistics
until the database or its WAL changes. `core_aux` is local bookkeeping for reaction and requested-reviewer
REST routes whose observed headers use a different effective window; it is not a
promised extra GitHub quota pool. Durable discovery, maintenance, task, fact,
checkpoint, and last-error state remains visible even when no process is running.

`uninstall` removes only the unit and control policy. SQLite, Git objects, `.env`, the
environment, and source tree remain:

```bash
sudo scripts/github-puller-daemon.sh uninstall archives/vllm.sqlite3
```

Sources: [scripts/github-puller-daemon.sh](../scripts/github-puller-daemon.sh); [gh_puller/github/monitor.py](../gh_puller/github/monitor.py)
