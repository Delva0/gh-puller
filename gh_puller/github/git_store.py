"""持久化上游仓库与 PR 代码对象并生成可离线解析的 Git 引用。

本模块管理与 SQLite 事实库一一对应的 bare Git 对象库。GitHub 讨论语义由
syncer 拉取；本模块保存标准上游 refs、不可变历史 pins，以及 PR refs 可达的
commit、tree 与 blob。它不提供工作区或下游派生写入。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .archive_format import (
    commit_ref,
    pull_ref,
    pull_staging_ref,
    source_staging_ref,
    upstream_ref,
)
from .schema import GIT_LAYOUT_VERSION

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
_HEARTBEAT_SECONDS = 2.0
_FETCH_RETRY_CEILING = 30.0
_TRANSIENT_FETCH_STATUS = re.compile(
    r"\bcurl (?:5|6|7|18|28|35|52|55|56|92)\b"
    r"|(?:returned error|http code)[: ]+(?:408|429|5\d\d)\b",
    re.IGNORECASE,
)
_MISSING_PULL_REF = re.compile(r"couldn't find remote ref refs/pull/(\d+)/head", re.IGNORECASE)
_MISSING_REMOTE_REF = re.compile(r"couldn't find remote ref (\S+)", re.IGNORECASE)
_TRANSIENT_FETCH_MARKERS = (
    "connection closed",
    "connection reset",
    "connection timed out",
    "could not resolve host",
    "early eof",
    "empty reply from server",
    "error decoding the received tls packet",
    "failed to connect",
    "gnutls recv error",
    "http/2 stream",
    "http/3 stream",
    "network is unreachable",
    "operation timed out",
    "remote end hung up unexpectedly",
    "rpc failed",
    "send failure",
    "tls connection was non-properly terminated",
    "unexpected disconnect",
)
_INCOMPLETE_CLOSURE_MARKERS = (
    "bad object",
    "bad tree object",
    "could not read",
    "failed to traverse",
    "missing blob object",
    "missing tree object",
    "unable to read tree",
)
_LOG = logging.getLogger(__name__)


class GitStoreError(RuntimeError):
    """持久化 Git 对象库无法建立或验证所需快照。"""


class TransientGitStoreError(GitStoreError):
    """单次 Git fetch 因可重试的传输错误失败。"""


@dataclass(frozen=True, slots=True)
class CommitFetchSource:
    """One provenance-backed remote ref that may reach a structured commit.

    Args:
        kind: ``pull-ref`` or ``repository-ref`` acquisition route.
        remote_url: Credential-free Git transport URL used for this attempt.
        remote_ref: Exact advertised ref to fetch.
        repository: Human-readable owner/repo source identity.
        resource_number: Related PR number when this is a pull ref.
    """

    kind: str
    remote_url: str
    remote_ref: str
    repository: str
    resource_number: int | None = None

    def __post_init__(self) -> None:
        if (
            self.kind not in {"pull-ref", "repository-ref"}
            or not self.remote_url
            or not self.remote_ref.startswith("refs/")
            or not self.repository
            or (self.resource_number is not None and self.resource_number < 1)
        ):
            raise ValueError("invalid commit fetch source")


type _SourceGroup = tuple[CommitFetchSource, tuple[str, ...]]
type _SourceBatch = tuple[_SourceGroup, ...]


@dataclass(frozen=True, slots=True)
class _RemoteRefObservation:
    observed_from: str
    observed_until: str
    sha: str | None
    error: str | None = None


class GitObjectStore:
    """管理一个仓库专属的 bare Git 对象库。

    Args:
        path: 与 SQLite 事实库配套的 bare Git 目录。
        repository: 固定绑定的 GitHub ``owner/repo``。
        remote_url: Git fetch 使用的远端地址。
        upstream_synced: 配套事实库是否已证明当前 cycle 完成上游 refs 同步。
        ref_batch_size: 单次 Git 传输包含的同远端精确 refs 上限。
        token: HTTPS 远端的 GitHub token；不会写入 Git 配置。
        sleep: 瞬时 Git 传输错误的可取消退避等待器。
        now: 记录逐来源获取尝试窗口的时区时钟。
    """

    def __init__(
        self,
        path: Path,
        repository: str,
        remote_url: str,
        *,
        upstream_synced: bool = False,
        ref_batch_size: int = 8,
        token: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if ref_batch_size < 1:
            raise ValueError("ref_batch_size must be positive")
        self.path = Path(path)
        self.repository = repository
        self.remote_url = remote_url
        self._ref_batch_size = ref_batch_size
        self._token = token
        self._sleep = sleep
        self._now = now
        self._lock = asyncio.Lock()
        self._ready = False
        self._upstream_synced = upstream_synced
        self._symbolic_head: str | None = None

    async def sync_upstream(
        self,
        *,
        heartbeat: Callable[[], None] | None = None,
        retry: Callable[[float], None] | None = None,
    ) -> dict[str, Any]:
        """同步并固定当前上游 branches 与 tags。

        Args:
            heartbeat: Git 网络操作未结束时周期调用的带外观察器。
            retry: 瞬时 Git 传输错误发生时接收退避秒数的观察器。
        """
        async with self._lock:
            await self._prepare()
            await self._sync_upstream(heartbeat=heartbeat, retry=retry, force=True)
            return await self._ref_observation()

    async def retain_commits(
        self,
        shas: Sequence[str],
        *,
        sources: Mapping[str, Sequence[CommitFetchSource]] | None = None,
        heartbeat: Callable[[], None] | None = None,
        retry: Callable[[float], None] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Acquire, verify, and pin commits named by structured API fields.

        Args:
            shas: Distinct commit object IDs to verify and retain.
            sources: Provenance-backed refs to try for each SHA before a broad
                upstream refresh. Unknown remotes are never guessed.
            heartbeat: Git network operation progress observer.
            retry: Transient Git transport retry observer.

        Returns:
            Per-SHA acquisition attempts, immutable ref, and endpoint/snapshot/history
            verification. ``available`` means the full reachable Git closure passed.

        Raises:
            GitStoreError: Input is invalid or Git fails without proving unavailability.
        """
        selected = tuple(dict.fromkeys(shas))
        if len(selected) != len(shas) or any(_SHA.fullmatch(sha) is None for sha in selected):
            raise ValueError("shas must contain unique Git object IDs")
        if not selected:
            return {}
        supplied = {} if sources is None else sources
        if set(supplied) - set(selected):
            raise ValueError("commit sources contain an unrequested SHA")
        routes = {sha: tuple(dict.fromkeys(supplied.get(sha, ()))) for sha in selected}
        async with self._lock:
            await self._prepare()
            attempts: dict[str, list[dict[str, Any]]] = {sha: [] for sha in selected}
            obtained = dict.fromkeys(selected, "existing")
            observed_from = self._time()
            verification = await self._reconstruction(selected, heartbeat)
            observed_until = self._time()
            for sha in selected:
                attempts[sha].append(
                    _attempt(
                        "managed-store",
                        self.repository,
                        None,
                        observed_from,
                        observed_until,
                        _reconstruction_outcome(verification.get(sha)),
                        _verification_error(verification.get(sha)),
                    ),
                )
            pending = {sha for sha in selected if _reconstruction_outcome(verification.get(sha)) != "available"}
            source_groups = _source_groups(routes, pending)
            staging = {
                source_staging_ref(_source_identity(source))
                for source, _ in source_groups
            }
            batches = _source_batches(
                source_groups,
                pending,
                self._ref_batch_size,
            )
            repository_observations: dict[CommitFetchSource, _RemoteRefObservation] = {}
            pull_tips: dict[int, tuple[str, str]] = {}
            repository_observed = False
            for planned in batches:
                batch = tuple(
                    (source, targets)
                    for source, targets in planned
                    if any(sha in pending for sha in targets)
                )
                if not batch:
                    continue
                if batch[0][0].kind == "repository-ref" and not repository_observed:
                    repository_sources = tuple(
                        source
                        for source, targets in source_groups
                        if source.kind == "repository-ref"
                        and any(sha in pending for sha in targets)
                    )
                    repository_observations = await self._observe_remote_refs(
                        repository_sources,
                        heartbeat=heartbeat,
                    )
                    pull_tips = await self._pull_source_tips(source_groups)
                    repository_observed = True
                observed_from = self._time()
                errors: dict[CommitFetchSource, str | None] = {}
                evidence_refs: dict[CommitFetchSource, str] = {}
                fetch_sources: list[CommitFetchSource] = []
                for source, _ in batch:
                    observation = repository_observations.get(source)
                    equivalent = _equivalent_pull_ref(source, observation, pull_tips)
                    if observation is not None and observation.error is None and observation.sha is None:
                        errors[source] = f"git ls-remote found no advertised ref {source.remote_ref}"
                    elif equivalent is not None:
                        errors[source] = None
                        evidence_refs[source] = equivalent
                    else:
                        fetch_sources.append(source)
                if fetch_sources:
                    errors |= await self._fetch_sources(
                        tuple(fetch_sources),
                        refetch=any(
                            verification.get(sha) is not None
                            for _, targets in batch
                            for sha in targets
                            if sha in pending
                        ),
                        heartbeat=heartbeat,
                        retry=retry,
                    )
                for source, targets in batch:
                    active = tuple(sha for sha in targets if sha in pending)
                    if not active:
                        continue
                    error = errors[source]
                    candidates = active
                    if error is None and (len(fetch_sources) > 1 or source in evidence_refs):
                        candidates = await self._reachable_commits(
                            evidence_refs.get(
                                source,
                                source_staging_ref(_source_identity(source)),
                            ),
                            active,
                        )
                    checked = (
                        {}
                        if error is not None
                        else await self._reconstruction(candidates, heartbeat)
                    )
                    observed_until = self._time()
                    observation = repository_observations.get(source)
                    for sha in active:
                        state = checked.get(sha)
                        outcome = _reconstruction_outcome(state)
                        attempts[sha].append(
                            _attempt(
                                source.kind,
                                source.repository,
                                source.remote_ref,
                                observed_from,
                                observed_until,
                                outcome,
                                error or _verification_error(state),
                                source.resource_number,
                                _preflight_record(
                                    observation,
                                    source.resource_number if source in evidence_refs else None,
                                ),
                            ),
                        )
                        if state is not None:
                            obtained[sha] = source.kind
                            verification[sha] = state
                        if outcome == "available":
                            pending.remove(sha)
            before_upstream = set(pending)
            if before_upstream:
                observed_from = self._time()
                refetch = any(verification.get(sha) is not None for sha in before_upstream)
                if not self._upstream_synced or refetch:
                    await self._sync_upstream(
                        heartbeat=heartbeat,
                        retry=retry,
                        force=refetch,
                        refetch=refetch,
                    )
                checked = await self._reconstruction(
                    tuple(before_upstream),
                    heartbeat,
                )
                observed_until = self._time()
                for sha in before_upstream:
                    state = checked.get(sha)
                    outcome = _reconstruction_outcome(state)
                    attempts[sha].append(
                        _attempt(
                            "upstream-refs",
                            self.repository,
                            "refs/heads/* + refs/tags/*",
                            observed_from,
                            observed_until,
                            outcome,
                            _verification_error(state),
                        ),
                    )
                    if state is not None:
                        obtained[sha] = "upstream-refs"
                        verification[sha] = state
                    if outcome == "available":
                        pending.remove(sha)
            pinnable = tuple(sha for sha in selected if verification.get(sha) is not None)
            if pinnable:
                await self._git(
                    "update-ref",
                    "--stdin",
                    input_text="".join(f"update {commit_ref(sha)} {sha}\n" for sha in pinnable),
                )
            for ref in staging:
                await self._delete_ref(ref)
            return {
                sha: _retention_result(
                    sha,
                    attempts[sha],
                    obtained[sha],
                    verification.get(sha),
                )
                for sha in selected
            }

    async def prefetch(
        self,
        pulls: Mapping[int, dict[str, Any]],
        *,
        heartbeat: Callable[[], None] | None = None,
        retry: Callable[[float], None] | None = None,
        retry_transient: bool = True,
    ) -> None:
        """批量取得随后将被固定的 PR refs。

        Args:
            pulls: 当前 API 消费批次中 PR number 到 detail 对象的映射。
            heartbeat: Git 网络操作未结束时周期调用的带外观察器。
            retry: 瞬时 Git 传输错误发生时接收退避秒数的观察器。
            retry_transient: 为 False 时将 PR ref 的瞬时失败交还调用方拆批。
        """
        selected = sorted(pulls)
        if not selected:
            return
        async with self._lock:
            await self._prepare()
            await self._sync_upstream(heartbeat=heartbeat, retry=retry)
            missing = await self._missing_commits(
                tuple(_nested_sha(pulls[number], "head", number) for number in selected),
            )
            pending = {
                number: f"+refs/pull/{number}/head:{pull_staging_ref(number)}"
                for number in selected
                if _nested_sha(pulls[number], "head", number) in missing
            }
            while pending:
                try:
                    await self._git(
                        "fetch",
                        "--quiet",
                        "--no-tags",
                        "--no-write-fetch-head",
                        "origin",
                        *pending.values(),
                        heartbeat=heartbeat,
                        retry=retry,
                        retry_transient=retry_transient,
                    )
                except GitStoreError as exc:
                    unavailable = _missing_pull_numbers(exc)
                    if not unavailable.intersection(pending):
                        raise
                    pending = {number: refspec for number, refspec in pending.items() if number not in unavailable}
                else:
                    return

    async def capture(
        self,
        number: int,
        pull: dict[str, Any],
        *,
        heartbeat: Callable[[], None] | None = None,
        retry: Callable[[float], None] | None = None,
    ) -> dict[str, Any]:
        """固定一个 PR 当前可达的精确 Git 对象。

        Args:
            number: Repository-local PR number。
            pull: GitHub PR detail 原始对象。
            heartbeat: 补取 Git 对象未结束时周期调用的带外观察器。
            retry: 瞬时 Git 传输错误发生时接收退避秒数的观察器。

        Returns:
            base/head 都可达时返回可交给 ``git diff`` 的完整快照；
            否则固定仍可达的对象并显式标记不可用的比较。API 声明的
            landing 仅在对象已可达时一同固定。

        Raises:
            GitStoreError: SHA 非法、可达历史存在多个 merge-base，或引用无法持久化。
        """
        base_sha = _nested_sha(pull, "base", number)
        head_sha = _nested_sha(pull, "head", number)
        value = pull.get("merge_commit_sha")
        landing_sha = value if pull.get("merged") is True and isinstance(value, str) and _SHA.fullmatch(value) else None
        async with self._lock:
            await self._prepare()
            await self._sync_upstream(heartbeat=heartbeat, retry=retry)
            pinnable_landing_sha = await self._available_commit(landing_sha)
            try:
                return await self._pin_snapshot(
                    number,
                    base_sha,
                    head_sha,
                    pinnable_landing_sha,
                )
            except GitStoreError as exc:
                failure = exc
            required = (base_sha, head_sha)
            missing = await self._missing_commits(required)
            if missing:
                try:
                    await self._refresh_missing(
                        number,
                        base_sha,
                        head_sha,
                        missing,
                        heartbeat=heartbeat,
                        retry=retry,
                    )
                    missing = await self._missing_commits(required)
                    pinnable_landing_sha = await self._available_commit(landing_sha)
                    if missing:
                        return await self._pin_partial_snapshot(
                            number,
                            base_sha,
                            head_sha,
                            pinnable_landing_sha,
                            missing,
                        )
                    return await self._pin_snapshot(
                        number,
                        base_sha,
                        head_sha,
                        pinnable_landing_sha,
                    )
                except GitStoreError as exc:
                    failure = exc
            raise GitStoreError(
                f"pull #{number} Git objects do not match its API snapshot: {failure}",
            ) from failure

    async def _pin_snapshot(
        self,
        number: int,
        base_sha: str,
        head_sha: str,
        landing_sha: str | None,
    ) -> dict[str, Any]:
        merge_bases = (
            await self._git(
                "merge-base",
                "--all",
                base_sha,
                head_sha,
                ok=(0, 1),
            )
        ).splitlines()
        if len(merge_bases) > 1:
            raise GitStoreError(f"pull #{number} has no unique merge base")
        if merge_bases:
            comparison_kind = "merge_base"
            comparison_sha = merge_bases[0]
        else:
            comparison_kind = "empty_tree"
            comparison_sha = (
                await self._git(
                    "hash-object",
                    "-w",
                    "-t",
                    "tree",
                    "--stdin",
                    input_text="",
                )
            ).strip()
        if _SHA.fullmatch(comparison_sha) is None:
            raise GitStoreError(f"pull #{number} has no valid comparison base")
        base_ref = pull_ref(number, "bases", base_sha)
        head_ref = pull_ref(number, "heads", head_sha)
        comparison_ref = pull_ref(number, "comparisons", comparison_sha)
        refs = [
            (base_ref, base_sha),
            (head_ref, head_sha),
            (comparison_ref, comparison_sha),
        ]
        result: dict[str, Any] = {
            "base_ref": base_ref,
            "base_sha": base_sha,
            "comparison_kind": comparison_kind,
            "comparison_ref": comparison_ref,
            "comparison_sha": comparison_sha,
            "head_ref": head_ref,
            "head_sha": head_sha,
        }
        if landing_sha is not None:
            landing_ref = pull_ref(number, "landings", landing_sha)
            refs.append((landing_ref, landing_sha))
            result |= {
                "history_preserved": await self._is_ancestor(head_sha, landing_sha),
                "landing_ref": landing_ref,
                "landing_sha": landing_sha,
            }
        else:
            result["history_preserved"] = None
        commands = "".join(f"update {ref} {sha}\n" for ref, sha in refs)
        await self._git("update-ref", "--stdin", input_text=commands)
        await self._delete_ref(pull_staging_ref(number))
        return result

    async def _pin_partial_snapshot(
        self,
        number: int,
        base_sha: str,
        head_sha: str,
        landing_sha: str | None,
        missing: set[str],
    ) -> dict[str, Any]:
        refs = []
        result: dict[str, Any] = {
            "base_sha": base_sha,
            "comparison_kind": "unavailable",
            "head_sha": head_sha,
            "unavailable_commits": sorted(missing),
        }
        for side, plural, sha in (("base", "bases", base_sha), ("head", "heads", head_sha)):
            if sha in missing:
                continue
            ref = pull_ref(number, plural, sha)
            refs.append((ref, sha))
            result[f"{side}_ref"] = ref
        if landing_sha is not None:
            landing_ref = pull_ref(number, "landings", landing_sha)
            refs.append((landing_ref, landing_sha))
            result |= {
                "history_preserved": None if head_sha in missing else await self._is_ancestor(head_sha, landing_sha),
                "landing_ref": landing_ref,
                "landing_sha": landing_sha,
            }
        else:
            result["history_preserved"] = None
        commands = "".join(f"update {ref} {sha}\n" for ref, sha in refs)
        if commands:
            await self._git("update-ref", "--stdin", input_text=commands)
        await self._delete_ref(pull_staging_ref(number))
        return result

    async def _missing_commits(self, shas: Sequence[str]) -> set[str]:
        if not shas:
            return set()
        output = await self._git(
            "cat-file",
            "--batch-check=%(objectname) %(objecttype)",
            input_text="".join(f"{sha}^{{commit}}\n{sha}^{{tree}}\n" for sha in shas),
        )
        lines = output.splitlines()
        if len(lines) != 2 * len(shas):
            raise GitStoreError("Git returned an incomplete commit verification batch")
        return {
            sha
            for index, sha in enumerate(shas)
            if not lines[2 * index].endswith(" commit") or not lines[2 * index + 1].endswith(" tree")
        }

    async def _fetch_sources(
        self,
        sources: tuple[CommitFetchSource, ...],
        *,
        refetch: bool,
        heartbeat: Callable[[], None] | None,
        retry: Callable[[float], None] | None,
    ) -> dict[CommitFetchSource, str | None]:
        if not sources or len({source.remote_url for source in sources}) != 1:
            raise ValueError("Git source batches require one shared remote")
        for source in sources:
            await self._delete_ref(source_staging_ref(_source_identity(source)))
        return await self._fetch_source_batch(
            sources,
            refetch=refetch,
            heartbeat=heartbeat,
            retry=retry,
        )

    async def _observe_remote_refs(
        self,
        sources: tuple[CommitFetchSource, ...],
        *,
        heartbeat: Callable[[], None] | None,
    ) -> dict[CommitFetchSource, _RemoteRefObservation]:
        semaphore = asyncio.Semaphore(self._ref_batch_size)

        async def observe(source: CommitFetchSource) -> tuple[CommitFetchSource, _RemoteRefObservation]:
            async with semaphore:
                observed_from = self._time()
                try:
                    output = await self._git(
                        "ls-remote",
                        source.remote_url,
                        source.remote_ref,
                        heartbeat=heartbeat,
                        retry_transient=False,
                    )
                    sha = _remote_ref_sha(output, source.remote_ref)
                except GitStoreError as exc:
                    return source, _RemoteRefObservation(
                        observed_from,
                        self._time(),
                        None,
                        str(exc),
                    )
                return source, _RemoteRefObservation(
                    observed_from,
                    self._time(),
                    sha,
                )

        return dict(await asyncio.gather(*(observe(source) for source in sources)))

    async def _pull_source_tips(
        self,
        groups: Sequence[_SourceGroup],
    ) -> dict[int, tuple[str, str]]:
        sources = {
            source_staging_ref(_source_identity(source)): source
            for source, _ in groups
            if source.kind == "pull-ref" and source.resource_number is not None
        }
        if not sources:
            return {}
        output = await self._git(
            "for-each-ref",
            "--format=%(refname)%09%(objectname)",
            "refs/github-archive/staging/sources",
        )
        result: dict[int, tuple[str, str]] = {}
        for line in output.splitlines():
            ref, separator, sha = line.partition("\t")
            source = sources.get(ref)
            if not separator or source is None or _SHA.fullmatch(sha) is None:
                continue
            number = source.resource_number
            if number is not None:
                result[number] = (sha, ref)
        return result

    async def _fetch_source_batch(
        self,
        sources: tuple[CommitFetchSource, ...],
        *,
        refetch: bool,
        heartbeat: Callable[[], None] | None,
        retry: Callable[[float], None] | None,
    ) -> dict[CommitFetchSource, str | None]:
        remote_url = sources[0].remote_url
        remote = "origin" if remote_url == self.remote_url else remote_url

        async def split() -> dict[CommitFetchSource, str | None]:
            middle = len(sources) // 2
            first = await self._fetch_source_batch(
                sources[:middle],
                refetch=refetch,
                heartbeat=heartbeat,
                retry=retry,
            )
            second = await self._fetch_source_batch(
                sources[middle:],
                refetch=refetch,
                heartbeat=heartbeat,
                retry=retry,
            )
            return first | second

        try:
            await self._git(
                "fetch",
                "--quiet",
                "--atomic",
                "--no-tags",
                "--no-write-fetch-head",
                *(("--refetch",) if refetch else ()),
                remote,
                *(
                    f"+{source.remote_ref}:{source_staging_ref(_source_identity(source))}"
                    for source in sources
                ),
                heartbeat=heartbeat,
                retry=retry,
                retry_transient=len(sources) == 1,
            )
        except TransientGitStoreError:
            if len(sources) == 1:
                raise
            return await split()
        except GitStoreError as exc:
            if not _is_known_source_absence(exc):
                if len(sources) > 1:
                    raise GitStoreError(
                        f"Git source batch from {sources[0].repository} failed: {exc}",
                    ) from exc
                source = sources[0]
                raise GitStoreError(
                    f"{source.kind} {source.repository} {source.remote_ref} failed: {exc}",
                ) from exc
            if len(sources) == 1:
                return {sources[0]: str(exc)}
            return await split()
        return dict.fromkeys(sources)

    async def _reachable_commits(
        self,
        ref: str,
        shas: Sequence[str],
    ) -> tuple[str, ...]:
        """Keep batch-fetched targets attributable to one exact source ref."""
        history = set((await self._git("rev-list", ref)).splitlines())
        return tuple(sha for sha in shas if sha in history)

    async def _verify_commits(
        self,
        shas: Sequence[str],
        *,
        heartbeat: Callable[[], None] | None = None,
    ) -> dict[str, dict[str, Any]]:
        if not shas:
            return {}
        trees = await self._commit_trees(shas)
        history_failures = await self._closure_failures(
            tuple((sha, sha) for sha in shas),
            heartbeat,
        )
        snapshot_targets = tuple((sha, trees[sha]) for sha in shas if sha in history_failures)
        snapshot_failures = await self._closure_failures(snapshot_targets, heartbeat)
        return {
            sha: {
                "method": "git-rev-list-objects-missing-error-v1",
                "endpoint": {"status": "complete"},
                "snapshot": _verification_level(snapshot_failures.get(sha)),
                "history": _verification_level(history_failures.get(sha)),
                "retention": {
                    "status": "complete",
                    "ref": commit_ref(sha),
                },
            }
            for sha in shas
        }

    async def _reconstruction(
        self,
        shas: Sequence[str],
        heartbeat: Callable[[], None] | None,
    ) -> dict[str, dict[str, Any]]:
        missing = await self._missing_commits(shas)
        available = tuple(sha for sha in shas if sha not in missing)
        return await self._verify_commits(available, heartbeat=heartbeat)

    async def _commit_trees(self, shas: Sequence[str]) -> dict[str, str]:
        output = await self._git(
            "cat-file",
            "--batch-check=%(objectname) %(objecttype)",
            input_text="".join(f"{sha}^{{tree}}\n" for sha in shas),
        )
        lines = output.splitlines()
        if len(lines) != len(shas):
            raise GitStoreError("Git returned an incomplete root-tree batch")
        result = {}
        for sha, line in zip(shas, lines, strict=True):
            fields = line.split()
            if len(fields) != 2 or fields[1] != "tree" or _SHA.fullmatch(fields[0]) is None:
                raise GitStoreError(f"commit {sha} has no readable root tree")
            result[sha] = fields[0]
        return result

    async def _closure_failures(
        self,
        targets: Sequence[tuple[str, str]],
        heartbeat: Callable[[], None] | None,
    ) -> dict[str, str]:
        failures: dict[str, str] = {}

        async def verify(selected: Sequence[tuple[str, str]]) -> None:
            if not selected:
                return
            try:
                await self._git(
                    "rev-list",
                    "--objects",
                    "--missing=error",
                    "--quiet",
                    *(root for _, root in selected),
                    heartbeat=heartbeat,
                )
            except GitStoreError as exc:
                if not _is_incomplete_closure(exc):
                    raise
                if len(selected) == 1:
                    failures[selected[0][0]] = str(exc)
                    return
                middle = len(selected) // 2
                await verify(selected[:middle])
                await verify(selected[middle:])

        await verify(targets)
        return failures

    def _time(self) -> str:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Git observation clock must include a timezone")
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

    async def _available_commit(self, sha: str | None) -> str | None:
        if sha is None:
            return None
        return None if await self._missing_commits((sha,)) else sha

    async def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        merge_base = await self._git(
            "merge-base",
            ancestor,
            descendant,
            ok=(0, 1),
        )
        return merge_base.strip() == ancestor

    async def _refresh_missing(
        self,
        number: int,
        base_sha: str,
        head_sha: str,
        missing: set[str],
        *,
        heartbeat: Callable[[], None] | None,
        retry: Callable[[float], None] | None,
    ) -> None:
        refspecs = []
        if head_sha in missing:
            refspecs.append(
                f"+refs/pull/{number}/head:{pull_staging_ref(number)}",
            )
        if not refspecs:
            return
        try:
            await self._git(
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                "origin",
                *refspecs,
                heartbeat=heartbeat,
                retry=retry,
            )
        except GitStoreError as exc:
            if number not in _missing_pull_numbers(exc):
                raise

    async def _sync_upstream(
        self,
        *,
        heartbeat: Callable[[], None] | None,
        retry: Callable[[float], None] | None,
        force: bool = False,
        refetch: bool = False,
    ) -> None:
        if self._upstream_synced and not force:
            return
        await self._pin_upstream_refs()
        await self._git(
            "fetch",
            "--quiet",
            "--atomic",
            "--prune",
            "--prune-tags",
            "--no-write-fetch-head",
            *(("--refetch",) if refetch else ()),
            "origin",
            "+refs/heads/*:refs/heads/*",
            "+refs/tags/*:refs/tags/*",
            heartbeat=heartbeat,
            retry=retry,
        )
        await self._pin_upstream_refs()
        await self._set_head(heartbeat=heartbeat, retry=retry)
        self._upstream_synced = True

    async def _ref_observation(self) -> dict[str, Any]:
        lines = (
            await self._git(
                "for-each-ref",
                "--format=%(refname) %(objectname) %(*objectname)",
                "refs/heads",
                "refs/tags",
            )
        ).splitlines()
        refs = []
        for line in lines:
            fields = line.split()
            if len(fields) not in {2, 3}:
                raise GitStoreError("Git returned an invalid native ref record")
            ref, oid, *peeled = fields
            if _SHA.fullmatch(oid) is None or (peeled and _SHA.fullmatch(peeled[0]) is None):
                raise GitStoreError(f"upstream ref has invalid object ID: {ref}")
            item = {"name": ref, "oid": oid}
            if ref.startswith("refs/tags/"):
                item["peeled_oid"] = peeled[0] if peeled else oid
            refs.append(item)
        symbolic_head = self._symbolic_head
        return {
            "repository": self.repository,
            "symbolic_head": symbolic_head,
            "default_branch": (
                symbolic_head.removeprefix("refs/heads/")
                if symbolic_head is not None and symbolic_head.startswith("refs/heads/")
                else None
            ),
            "refs": refs,
        }

    async def _pin_upstream_refs(self) -> None:
        lines = (
            await self._git(
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "refs/heads",
                "refs/tags",
            )
        ).splitlines()
        updates = {}
        for line in lines:
            ref, sha = line.rsplit(" ", 1)
            if _SHA.fullmatch(sha) is None:
                raise GitStoreError(f"upstream ref has invalid object ID: {ref}")
            kind = "heads" if ref.startswith("refs/heads/") else "tags"
            archive_ref = upstream_ref(kind, sha)
            updates[archive_ref] = sha
        if updates:
            await self._git(
                "update-ref",
                "--stdin",
                input_text="".join(f"update {ref} {sha}\n" for ref, sha in updates.items()),
            )

    async def _set_head(
        self,
        *,
        heartbeat: Callable[[], None] | None,
        retry: Callable[[float], None] | None,
    ) -> None:
        advertised = await self._git(
            "ls-remote",
            "--symref",
            "origin",
            "HEAD",
            heartbeat=heartbeat,
            retry=retry,
        )
        self._symbolic_head = None
        for line in advertised.splitlines():
            if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                ref = line.removeprefix("ref: ").removesuffix("\tHEAD")
                self._symbolic_head = ref
                if not await self._missing_commits((ref,)):
                    await self._git("symbolic-ref", "HEAD", ref)
                return

    async def _delete_ref(self, ref: str) -> None:
        await self._git("update-ref", "-d", ref)

    async def _prepare(self) -> None:
        if self._ready:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            await _command(("git", "init", "--bare", str(self.path)), environment=self._environment())
        _remove_temporary_packs(self.path)
        bare = await self._git("rev-parse", "--is-bare-repository")
        if bare.strip() != "true":
            raise GitStoreError(f"Git store is not bare: {self.path}")
        bound = await self._git("config", "--get", "github-archive.repository", ok=(0, 1))
        if bound.strip() and bound.strip() != self.repository:
            raise GitStoreError(f"Git store belongs to {bound.strip()}, not {self.repository}")
        if not bound.strip():
            await self._git("config", "github-archive.repository", self.repository)
        layout = await self._git("config", "--get", "github-archive.layoutVersion", ok=(0, 1))
        if layout.strip() and layout.strip() != GIT_LAYOUT_VERSION:
            raise GitStoreError(f"unsupported Git archive layout {layout.strip()}")
        if not layout.strip():
            await self._git("config", "github-archive.layoutVersion", GIT_LAYOUT_VERSION)
        if (self.path / "shallow").exists():
            raise GitStoreError(f"Git store is shallow: {self.path}")
        partial = await self._git("config", "--get", "extensions.partialClone", ok=(0, 1))
        if partial.strip():
            raise GitStoreError(f"Git store is partial: {self.path}")
        remote = await self._git("remote", "get-url", "origin", ok=(0, 2))
        if remote.strip() and remote.strip() != self.remote_url:
            raise GitStoreError(f"Git store origin is {remote.strip()}, not {self.remote_url}")
        if not remote.strip():
            await self._git("remote", "add", "origin", self.remote_url)
        await self._git("config", "gc.auto", "0")
        if sum(1 for _ in (self.path / "objects" / "pack").glob("*.idx")) > 1:
            await self._git("multi-pack-index", "write")
        self._ready = True

    async def _git(
        self,
        *arguments: str,
        input_text: str | None = None,
        heartbeat: Callable[[], None] | None = None,
        retry: Callable[[float], None] | None = None,
        retry_transient: bool = True,
        ok: tuple[int, ...] = (0,),
    ) -> str:
        wait = 1.0
        while True:
            try:
                return await _command(
                    ("git", "--git-dir", str(self.path), *arguments),
                    environment=self._environment(),
                    input_text=input_text,
                    heartbeat=heartbeat,
                    ok=ok,
                )
            except GitStoreError as exc:
                if arguments[0] not in {"fetch", "ls-remote"}:
                    raise
                _remove_temporary_packs(self.path)
                if not _is_transient_fetch_failure(exc):
                    raise
                if not retry_transient:
                    raise TransientGitStoreError(str(exc)) from exc
                _LOG.warning("%s; retrying in %.1fs", exc, wait)
                if retry is not None:
                    retry(wait)
                await self._sleep(wait)
                if heartbeat is not None:
                    heartbeat()
                wait = min(wait * 2, _FETCH_RETRY_CEILING)

    def _environment(self) -> dict[str, str]:
        environment = os.environ | {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.version",
            "GIT_CONFIG_VALUE_0": "HTTP/1.1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
        if self._token and self.remote_url.startswith(("http://", "https://")):
            credential = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
            environment |= {
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_1": f"http.{self.remote_url}.extraHeader",
                "GIT_CONFIG_VALUE_1": f"Authorization: Basic {credential}",
            }
        return environment


def git_store_path(database: Path) -> Path:
    """返回一个 SQLite 事实库固定对应的 Git 对象库路径。

    Args:
        database: SQLite 事实库路径。

    Returns:
        在原路径后追加 ``.git`` 的 bare Git 目录。
    """
    return Path(f"{Path(database)}.git")


def default_git_url(repository: str) -> str:
    """返回 GitHub.com 仓库的 HTTPS Git URL。

    Args:
        repository: GitHub ``owner/repo``。

    Returns:
        不含凭据的公开 Git URL。
    """
    return f"https://github.com/{repository}.git"


def _source_groups(
    routes: Mapping[str, Sequence[CommitFetchSource]],
    missing: set[str],
) -> list[_SourceGroup]:
    grouped: dict[CommitFetchSource, set[str]] = {}
    for sha in missing:
        for source in routes[sha]:
            grouped.setdefault(source, set()).add(sha)
    priority = {"pull-ref": 0, "repository-ref": 1}
    return [
        (source, tuple(sorted(grouped[source])))
        for source in sorted(
            grouped,
            key=lambda item: (
                priority[item.kind],
                item.repository,
                item.remote_ref,
                item.resource_number or 0,
            ),
        )
    ]


def _source_batches(
    groups: Sequence[_SourceGroup],
    pending: set[str],
    limit: int,
) -> tuple[_SourceBatch, ...]:
    """Group transport-compatible refs without mixing one target's sources."""
    batches: list[_SourceBatch] = []
    current: list[_SourceGroup] = []
    targets: set[str] = set()
    remote_url: str | None = None
    for source, selected in groups:
        active = set(selected).intersection(pending)
        if not active:
            continue
        if current and (
            len(current) >= limit
            or source.remote_url != remote_url
            or source.kind != current[0][0].kind
            or not targets.isdisjoint(active)
        ):
            batches.append(tuple(current))
            current = []
            targets = set()
        current.append((source, selected))
        targets.update(active)
        remote_url = source.remote_url
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def _source_identity(source: CommitFetchSource) -> str:
    return "\0".join(
        (
            source.kind,
            source.repository,
            source.remote_ref,
            str(source.resource_number or 0),
        ),
    )


def _remote_ref_sha(output: str, expected_ref: str) -> str | None:
    if not output:
        return None
    matches = []
    for line in output.splitlines():
        sha, separator, ref = line.partition("\t")
        if not separator or ref != expected_ref or _SHA.fullmatch(sha) is None:
            raise GitStoreError(f"git ls-remote returned an invalid ref for {expected_ref}")
        matches.append(sha)
    if len(matches) != 1:
        raise GitStoreError(f"git ls-remote returned ambiguous refs for {expected_ref}")
    return matches[0]


def _equivalent_pull_ref(
    source: CommitFetchSource,
    observation: _RemoteRefObservation | None,
    pull_tips: Mapping[int, tuple[str, str]],
) -> str | None:
    if observation is None or observation.error is not None or observation.sha is None:
        return None
    tip = pull_tips.get(source.resource_number or 0)
    return tip[1] if tip is not None and tip[0] == observation.sha else None


def _preflight_record(
    observation: _RemoteRefObservation | None,
    equivalent_pull: int | None,
) -> dict[str, Any] | None:
    if observation is None:
        return None
    outcome = "inconclusive" if observation.error is not None else "absent"
    result: dict[str, Any] = {
        "method": "git-ls-remote-tip-v1",
        "observed_from": observation.observed_from,
        "observed_until": observation.observed_until,
        "outcome": "advertised" if observation.sha is not None else outcome,
    }
    if observation.sha is not None:
        result["advertised_sha"] = observation.sha
    if observation.error is not None:
        result["error"] = observation.error
    if equivalent_pull is not None:
        result["equivalent_source"] = {
            "kind": "pull-ref",
            "ref": f"refs/pull/{equivalent_pull}/head",
            "resource_number": equivalent_pull,
        }
    return result


def _attempt(
    kind: str,
    repository: str,
    ref: str | None,
    observed_from: str,
    observed_until: str,
    outcome: str,
    error: str | None = None,
    resource_number: int | None = None,
    preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": kind,
        "repository": repository,
        "ref": ref,
        "observed_from": observed_from,
        "observed_until": observed_until,
        "outcome": outcome,
    }
    if resource_number is not None:
        result["resource_number"] = resource_number
    if error is not None:
        result["error"] = error
    if preflight is not None:
        result["preflight"] = preflight
    return result


def _verification_level(error: str | None) -> dict[str, str]:
    return {"status": "complete"} if error is None else {"status": "partial", "reason": error}


def _reconstruction_outcome(verification: dict[str, Any] | None) -> str:
    if verification is None:
        return "unavailable"
    history = verification.get("history")
    return "available" if isinstance(history, dict) and history.get("status") == "complete" else "partial"


def _verification_error(verification: dict[str, Any] | None) -> str | None:
    if verification is None:
        return None
    for key in ("history", "snapshot"):
        value = verification.get(key)
        if isinstance(value, dict) and isinstance(value.get("reason"), str):
            return value["reason"]
    return None


def _retention_result(
    sha: str,
    attempts: list[dict[str, Any]],
    obtained: str,
    verification: dict[str, Any] | None,
) -> dict[str, Any]:
    if verification is None:
        return {
            "sha": sha,
            "status": "unavailable",
            "attempts": attempts,
            "reason": "known Git sources did not provide the commit and root tree",
            "verification": {
                "method": "git-rev-list-objects-missing-error-v1",
                "endpoint": {"status": "unavailable"},
                "snapshot": {"status": "not-checked"},
                "history": {"status": "not-checked"},
                "retention": {"status": "not-pinned"},
            },
        }
    history = verification.get("history")
    complete = isinstance(history, dict) and history.get("status") == "complete"
    result = {
        "sha": sha,
        "status": "available" if complete else "partial",
        "attempts": attempts,
        "obtained": obtained,
        "ref": commit_ref(sha),
        "verification": verification,
    }
    if not complete:
        result["reason"] = "reachable Git object closure failed verification"
    return result


def _is_transient_fetch_failure(error: GitStoreError) -> bool:
    detail = str(error).casefold()
    return any(marker in detail for marker in _TRANSIENT_FETCH_MARKERS) or bool(
        _TRANSIENT_FETCH_STATUS.search(detail),
    )


def _is_known_source_absence(error: GitStoreError) -> bool:
    return _MISSING_REMOTE_REF.search(str(error)) is not None


def _is_incomplete_closure(error: GitStoreError) -> bool:
    detail = str(error).casefold()
    return any(marker in detail for marker in _INCOMPLETE_CLOSURE_MARKERS)


def _missing_pull_numbers(error: GitStoreError) -> set[int]:
    return {int(number) for number in _MISSING_PULL_REF.findall(str(error))}


def _remove_temporary_packs(path: Path) -> None:
    for temporary in (path / "objects" / "pack").glob("tmp_pack_*"):
        try:
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            _LOG.warning("Could not remove incomplete Git pack %s: %s", temporary, exc)


def _nested_sha(pull: dict[str, Any], side: str, number: int) -> str:
    value = pull.get(side)
    sha = value.get("sha") if isinstance(value, dict) else None
    if not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
        raise GitStoreError(f"pull #{number} has no valid {side} SHA")
    return sha


async def _command(
    command: Sequence[str],
    *,
    environment: dict[str, str],
    input_text: str | None = None,
    heartbeat: Callable[[], None] | None = None,
    ok: tuple[int, ...] = (0,),
) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise GitStoreError("git executable was not found") from exc
    communication = asyncio.create_task(
        process.communicate(None if input_text is None else input_text.encode()),
    )
    try:
        while not communication.done():
            await asyncio.wait((communication,), timeout=_HEARTBEAT_SECONDS)
            if not communication.done() and heartbeat is not None:
                heartbeat()
        stdout, stderr = await communication
    except BaseException:
        if process.returncode is None:
            process.terminate()
            await process.wait()
        raise
    if process.returncode not in ok:
        detail = stderr.decode(errors="replace").strip() or f"exit status {process.returncode}"
        action = command[3] if len(command) > 3 and command[1] == "--git-dir" else command[1]
        raise GitStoreError(f"git {action} failed: {detail}")
    return stdout.decode(errors="replace")
