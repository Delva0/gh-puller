"""持久化 PR 代码对象并生成可离线解析的 Git 快照引用。

本模块管理与 SQLite 事实库一一对应的 bare Git 对象库。GitHub 讨论语义由
puller 拉取；本模块只保存目标仓库及 PR refs 可达的 commit、tree 与 blob。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
_HEARTBEAT_SECONDS = 2.0
_FETCH_RETRY_CEILING = 30.0
_TRANSIENT_FETCH_STATUS = re.compile(
    r"\bcurl (?:5|6|7|18|28|35|52|55|56|92)\b"
    r"|(?:returned error|http code)[: ]+(?:408|429|5\d\d)\b",
    re.IGNORECASE,
)
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
_LOG = logging.getLogger(__name__)


class GitStoreError(RuntimeError):
    """持久化 Git 对象库无法建立或验证所需快照。"""


class TransientGitStoreError(GitStoreError):
    """单次 Git fetch 因可重试的传输错误失败。"""


class GitObjectStore:
    """管理一个仓库专属的 bare Git 对象库。

    Args:
        path: 与 SQLite 事实库配套的 bare Git 目录。
        repository: 固定绑定的 GitHub ``owner/repo``。
        remote_url: Git fetch 使用的远端地址。
        token: HTTPS 远端的 GitHub token；不会写入 Git 配置。
        sleep: 瞬时 Git 传输错误的可取消退避等待器。
    """

    def __init__(
        self,
        path: Path,
        repository: str,
        remote_url: str,
        *,
        token: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.path = Path(path)
        self.repository = repository
        self.remote_url = remote_url
        self._token = token
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._ready = False
        self._branches_fetched = False

    async def prefetch(
        self,
        numbers: Sequence[int],
        *,
        heartbeat: Callable[[], None] | None = None,
        retry: Callable[[float], None] | None = None,
        retry_transient: bool = True,
    ) -> None:
        """批量取得随后将被固定的 PR refs。

        Args:
            numbers: 当前 API 消费批次中的 PR numbers。
            heartbeat: Git 网络操作未结束时周期调用的带外观察器。
            retry: 瞬时 Git 传输错误发生时接收退避秒数的观察器。
            retry_transient: 为 False 时将 PR ref 的瞬时失败交还调用方拆批。
        """
        selected = sorted(set(numbers))
        if not selected:
            return
        async with self._lock:
            await self._prepare()
            if not self._branches_fetched:
                await self._git(
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "origin",
                    "+refs/heads/*:refs/gh-puller/remotes/heads/*",
                    heartbeat=heartbeat,
                    retry=retry,
                )
                self._branches_fetched = True
            refspecs = [
                f"+refs/pull/{number}/head:refs/gh-puller/remotes/pulls/{number}/head"
                for number in selected
            ]
            await self._git(
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                "origin",
                *refspecs,
                heartbeat=heartbeat,
                retry=retry,
                retry_transient=retry_transient,
            )

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
            merge commit 仅在对象已可达时一同固定。

        Raises:
            GitStoreError: SHA 非法、可达历史存在多个 merge-base，或引用无法持久化。
        """
        base_sha = _nested_sha(pull, "base", number)
        head_sha = _nested_sha(pull, "head", number)
        value = pull.get("merge_commit_sha")
        merge_sha = value if pull.get("merged") is True and isinstance(value, str) and _SHA.fullmatch(value) else None
        prefix = f"refs/gh-puller/snapshots/pulls/{number}"
        async with self._lock:
            await self._prepare()
            pinnable_merge_sha = await self._available_commit(merge_sha)
            try:
                return await self._pin_snapshot(
                    number,
                    prefix,
                    base_sha,
                    head_sha,
                    pinnable_merge_sha,
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
                    pinnable_merge_sha = await self._available_commit(merge_sha)
                    if missing:
                        return await self._pin_partial_snapshot(
                            prefix,
                            base_sha,
                            head_sha,
                            pinnable_merge_sha,
                            missing,
                        )
                    return await self._pin_snapshot(
                        number,
                        prefix,
                        base_sha,
                        head_sha,
                        pinnable_merge_sha,
                    )
                except GitStoreError as exc:
                    failure = exc
            raise GitStoreError(
                f"pull #{number} Git objects do not match its API snapshot: {failure}",
            ) from failure

    async def _pin_snapshot(
        self,
        number: int,
        prefix: str,
        base_sha: str,
        head_sha: str,
        merge_sha: str | None,
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
        base_ref = f"{prefix}/base/{base_sha}"
        head_ref = f"{prefix}/head/{head_sha}"
        comparison_ref = f"{prefix}/comparison/{comparison_sha}"
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
        if merge_sha is not None:
            merge_ref = f"{prefix}/merge/{merge_sha}"
            refs.append((merge_ref, merge_sha))
            result |= {"merge_commit_ref": merge_ref, "merge_commit_sha": merge_sha}
        commands = "".join(f"update {ref} {sha}\n" for ref, sha in refs)
        await self._git("update-ref", "--stdin", input_text=commands)
        return result

    async def _pin_partial_snapshot(
        self,
        prefix: str,
        base_sha: str,
        head_sha: str,
        merge_sha: str | None,
        missing: set[str],
    ) -> dict[str, Any]:
        refs = []
        result: dict[str, Any] = {
            "base_sha": base_sha,
            "comparison_kind": "unavailable",
            "head_sha": head_sha,
            "unavailable_commits": sorted(missing),
        }
        for side, sha in (("base", base_sha), ("head", head_sha)):
            if sha in missing:
                continue
            ref = f"{prefix}/{side}/{sha}"
            refs.append((ref, sha))
            result[f"{side}_ref"] = ref
        if merge_sha is not None:
            merge_ref = f"{prefix}/merge/{merge_sha}"
            refs.append((merge_ref, merge_sha))
            result |= {"merge_commit_ref": merge_ref, "merge_commit_sha": merge_sha}
        commands = "".join(f"update {ref} {sha}\n" for ref, sha in refs)
        if commands:
            await self._git("update-ref", "--stdin", input_text=commands)
        return result

    async def _missing_commits(self, shas: Sequence[str]) -> set[str]:
        missing = set()
        for sha in shas:
            kind = await self._git("cat-file", "-t", f"{sha}^{{commit}}", ok=(0, 1, 128))
            if kind.strip() != "commit":
                missing.add(sha)
        return missing

    async def _available_commit(self, sha: str | None) -> str | None:
        if sha is None:
            return None
        return None if await self._missing_commits((sha,)) else sha

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
        if base_sha in missing and not self._branches_fetched:
            refspecs.append("+refs/heads/*:refs/gh-puller/remotes/heads/*")
        if head_sha in missing:
            refspecs.append(f"+refs/pull/{number}/head:refs/gh-puller/remotes/pulls/{number}/head")
        if not refspecs:
            return
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
        if refspecs and refspecs[0].startswith("+refs/heads/"):
            self._branches_fetched = True

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
        bound = await self._git("config", "--get", "gh-puller.repository", ok=(0, 1))
        if bound.strip() and bound.strip() != self.repository:
            raise GitStoreError(f"Git store belongs to {bound.strip()}, not {self.repository}")
        if not bound.strip():
            await self._git("config", "gh-puller.repository", self.repository)
        remote = await self._git("remote", "get-url", "origin", ok=(0, 2))
        if remote.strip() and remote.strip() != self.remote_url:
            raise GitStoreError(f"Git store origin is {remote.strip()}, not {self.remote_url}")
        if not remote.strip():
            await self._git("remote", "add", "origin", self.remote_url)
        await self._git("config", "gc.auto", "0")
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
                if arguments[0] != "fetch":
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
                "GIT_CONFIG_KEY_1": "http.extraHeader",
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


def _is_transient_fetch_failure(error: GitStoreError) -> bool:
    detail = str(error).casefold()
    return any(marker in detail for marker in _TRANSIENT_FETCH_MARKERS) or bool(
        _TRANSIENT_FETCH_STATUS.search(detail),
    )


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
