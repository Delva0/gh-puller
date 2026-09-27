"""Synchronize release snapshots and prebuild their local CBM indexes once.

Every remote PEP 440 version tag is synchronized, including prereleases.
Missing snapshots are added and existing snapshots are checked against their
exact remote tag's peeled commit. Successful build receipts live beside the
source, outside the indexed tree; interrupted builds never advance them.
Optional inclusive version floors prune older sources and their CBM indexes.
An unchanged receipt only permits skipping when the published cache DB exists
and CBM reports the expected source binding.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from packaging.version import InvalidVersion, Version

from vllm_kb_adapter.config import Settings
from vllm_kb_adapter.prebuild import PrebuildError, audit_indexes, ensure_indexes, indexed_projects, prebuild_all
from vllm_kb_adapter.snapshots import RegistryError, Snapshot, SnapshotRegistry, _discover_root
from vllm_kb_adapter.upstream import UpstreamError, tool_error_text

if TYPE_CHECKING:
    from collections.abc import Callable

GIB = 1 << 30
RECEIPT = ".vllm-kb-build.json"


class UpdateError(RuntimeError):
    """Snapshot synchronization cannot safely continue."""


@dataclass(frozen=True, slots=True)
class Tag:
    name: str
    version: Version
    commit: str


@dataclass(frozen=True, slots=True)
class Checkout:
    snapshot: Snapshot
    tag: Tag
    remote: str


def _git(*arguments: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *arguments],
        cwd=cwd,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if result.returncode:
        raise UpdateError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _parse_tags(output: str) -> dict[str, Tag]:
    refs = dict(line.split()[::-1] for line in output.splitlines() if line.strip())
    tags = {}
    for ref, commit in refs.items():
        if not ref.startswith("refs/tags/") or ref.endswith("^{}"):
            continue
        name = ref.removeprefix("refs/tags/")
        try:
            version = Version(name)
        except InvalidVersion:
            continue
        tags[name] = Tag(name, version, refs.get(f"{ref}^{{}}", commit))
    if not tags:
        raise UpdateError("remote has no release version tags")
    return tags


def _read_receipt(snapshot: Snapshot) -> dict[str, Any]:
    path = snapshot.path.parent / RECEIPT
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise UpdateError(f"invalid build receipt: {path}")
    return data


def _tag_for_version(tags: dict[str, Tag], version: Version) -> Tag:
    candidates = [tag for tag in tags.values() if tag.version == version]
    for name in (f"v{version}", str(version)):
        if name in tags:
            return tags[name]
    if len(candidates) != 1:
        raise UpdateError(f"missing or ambiguous remote tag for version {version}")
    return candidates[0]


def _local_snapshots(repo: str, root: Path) -> tuple[Snapshot, ...]:
    if not root.is_dir() or not any(path.is_dir() for path in root.iterdir()):
        return ()
    return SnapshotRegistry(_discover_root(root, f"vllm-project/{repo}", repo, repo)).snapshots


def _snapshot(repo: str, root: Path, parsed: Version) -> Snapshot:
    version = str(parsed)
    outer = f"v{version}" if repo == "vllm-ascend" else version
    return Snapshot(
        logical_project=f"vllm-project/{repo}",
        repo=repo,
        version=version,
        parsed_version=parsed,
        path=root / outer / f"{repo}-{version}",
        index_name=f"vllm-kb-{repo}-{version}",
    )


def _plan(repo: str, root: Path, remote: str, from_version: Version | None = None) -> list[Checkout]:
    tags = _parse_tags(_git("ls-remote", "--tags", remote))
    versions = {tag.version for tag in tags.values()}
    selected = {version for version in versions if from_version is None or version >= from_version}
    if not selected:
        raise UpdateError(f"{repo} has no remote versions at or after {from_version}")
    print(f"versions: {repo} remote={len(versions)} selected={len(selected)}", flush=True)
    existing = [
        snapshot for snapshot in _local_snapshots(repo, root)
        if from_version is None or snapshot.parsed_version >= from_version
    ]
    present = {snapshot.parsed_version for snapshot in existing}
    existing.extend(_snapshot(repo, root, parsed) for parsed in sorted(selected - present))
    plan = []
    for snapshot in sorted(existing, key=lambda item: item.parsed_version):
        receipt = _read_receipt(snapshot)
        tag_name = receipt.get("tag")
        if tag_name is not None:
            if tag_name not in tags or tags[tag_name].version != snapshot.parsed_version:
                raise UpdateError(f"tracked tag disappeared or changed version: {repo} {tag_name}")
            tag = tags[tag_name]
        else:
            tag = _tag_for_version(tags, snapshot.parsed_version)
        plan.append(Checkout(snapshot, tag, remote))
    return plan


def _check_checkout(path: Path, remote: str) -> str | None:
    if not path.exists():
        return None
    if not (path / ".git").is_dir() or _git("rev-parse", "--show-toplevel", cwd=path) != str(path):
        raise UpdateError(f"snapshot must be an independent Git checkout: {path}")
    if _git("status", "--porcelain", "--untracked-files=all", cwd=path):
        raise UpdateError(f"snapshot has local changes: {path}")
    origin = _git("remote", "get-url", "origin", cwd=path)
    if origin.removesuffix(".git").rstrip("/") != remote.removesuffix(".git").rstrip("/"):
        raise UpdateError(f"snapshot origin differs from configured repository: {path}")
    return _git("rev-parse", "HEAD", cwd=path)


def _fetch(checkout: Checkout, path: Path) -> None:
    tag = checkout.tag
    _git("fetch", "--quiet", "--depth=1", "--no-tags", "origin", f"refs/tags/{tag.name}", cwd=path)
    actual = _git("rev-parse", "FETCH_HEAD^{commit}", cwd=path)
    if actual != tag.commit:
        raise UpdateError(f"tag moved during fetch: {tag.name}; rerun to discover its new commit")
    _git("checkout", "--quiet", "--detach", tag.commit, cwd=path)


def _sync(checkout: Checkout, head: str | None) -> None:
    path = checkout.snapshot.path
    if head == checkout.tag.commit:
        return
    print(f"fetch: {checkout.snapshot.repo} {checkout.tag.name} {checkout.tag.commit} -> {path}", flush=True)
    if head is not None:
        _fetch(checkout, path)
        return
    # Stage outside the registry so a failed clone cannot publish a partial version.
    with tempfile.TemporaryDirectory(prefix=f".{checkout.snapshot.repo}-", dir=path.parent.parent.parent) as work:
        version_dir = Path(work) / path.parent.name
        staged = version_dir / path.name
        _git("init", "--quiet", str(staged))
        _git("remote", "add", "origin", checkout.remote, cwd=staged)
        _fetch(checkout, staged)
        version_dir.rename(path.parent)


def _available_memory() -> int:
    values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    available = int(values["MemAvailable"].split()[0]) * 1024
    cgroup = Path("/sys/fs/cgroup")
    if (cgroup / "memory.max").exists():
        limit = (cgroup / "memory.max").read_text().strip()
        if limit != "max":
            available = min(available, int(limit) - int((cgroup / "memory.current").read_text()))
    return available


def _disk_path(path: Path) -> Path:
    while not path.exists():
        path = path.parent
    return path


def _check_resources(paths: tuple[Path, ...], min_disk_gib: float, min_memory_gib: float) -> None:
    for path in dict.fromkeys(paths):
        free = shutil.disk_usage(_disk_path(path)).free / GIB
        if free < min_disk_gib:
            raise UpdateError(f"only {free:.1f} GiB free at {path}; require {min_disk_gib:g} GiB")
    available = _available_memory() / GIB
    if available < min_memory_gib:
        raise UpdateError(f"only {available:.1f} GiB memory available; require {min_memory_gib:g} GiB")


class LocalCBM:
    """Use the CBM one-shot CLI with the same tool-result contract as MCP."""

    def __init__(
        self,
        binary: str,
        cache: Path,
        memory_mb: int,
        workers: int,
        resource_check: Callable[[], None],
    ) -> None:
        self.binary = binary
        self.resource_check = resource_check
        self.env = {
            **os.environ,
            "CBM_CACHE_DIR": str(cache),
            "CBM_MEM_BUDGET_MB": str(memory_mb),
            "CBM_WORKERS": str(workers),
        }

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Wait for one native tool and return its MCP result, including errors.

        Args:
            name: Native CBM tool name.
            arguments: Native tool arguments, sent over stdin without shell parsing.
        """
        process = await asyncio.create_subprocess_exec(
            self.binary,
            "cli",
            "--json",
            name,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=self.env,
        )
        exchange = asyncio.create_task(process.communicate(json.dumps(arguments).encode()))
        try:
            while not (await asyncio.wait({exchange}, timeout=1))[0]:
                self.resource_check()
            stdout, _ = exchange.result()
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            await asyncio.gather(exchange, return_exceptions=True)
        if process.returncode:
            raise UpdateError(f"CBM {name} exited {process.returncode}: {stdout.decode(errors='replace')[-2000:]}")
        result = json.loads(stdout)
        if not isinstance(result, dict):
            raise UpdateError(f"CBM {name} returned no result object")
        return result


def _build_receipt(checkout: Checkout, cache: Path, mode: str, binary_version: str) -> dict[str, Any]:
    return {
        "tag": checkout.tag.name,
        "commit": checkout.tag.commit,
        "source": str(checkout.snapshot.path),
        "cache": str(cache),
        "mode": mode,
        "cbm_version": binary_version,
    }


def _write_receipt(snapshot: Snapshot, receipt: dict[str, Any]) -> None:
    destination = snapshot.path.parent / RECEIPT
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(receipt, indent=2) + "\n")
    temporary.replace(destination)


def _progress(action: str, snapshot: Snapshot) -> None:
    print(f"{action}: {snapshot.index_name} <- {snapshot.path}", flush=True)


def _binary_version(binary: str) -> str:
    return subprocess.run(
        [binary, "--version"], capture_output=True, text=True, check=True, timeout=30,
    ).stdout.strip()


def _artifact_project(path: Path) -> str | None:
    name, separator, suffix = path.name.partition(".db")
    if separator and (suffix in {"", "-wal", "-shm", "-journal"} or suffix.startswith(".stage.")):
        return name
    return None


def _prune_plan(repo: str, root: Path, cache: Path, floor: Version | None, projects: dict[str, Path]) -> list[Snapshot]:
    if floor is None:
        return []
    obsolete = {
        snapshot.index_name: snapshot for snapshot in _local_snapshots(repo, root)
        if snapshot.parsed_version < floor
    }
    names = set(projects)
    if cache.is_dir():
        names.update(name for path in cache.iterdir() if (name := _artifact_project(path)) is not None)
    prefix = f"vllm-kb-{repo}-"
    for name in names:
        if not name.startswith(prefix):
            continue
        try:
            parsed = Version(name.removeprefix(prefix))
        except InvalidVersion:
            continue
        if parsed < floor:
            snapshot = _snapshot(repo, root, parsed)
            if snapshot.index_name == name:
                obsolete.setdefault(name, snapshot)
    for snapshot in obsolete.values():
        expected = _snapshot(repo, root, snapshot.parsed_version).path
        if snapshot.path != expected or snapshot.path.parent.is_symlink() or snapshot.path.is_symlink():
            raise UpdateError(f"refusing to prune a snapshot outside its version directory: {snapshot.path}")
        bound = projects.get(snapshot.index_name)
        if bound is not None and bound != snapshot.path:
            raise UpdateError(f"index {snapshot.index_name} points to {bound}, expected {snapshot.path}")
    return sorted(obsolete.values(), key=lambda snapshot: snapshot.parsed_version)


def _remove_pruned_files(snapshot: Snapshot, cache: Path) -> None:
    # Native deletion owns the published DB; only abandoned sidecars remain here.
    for path in cache.iterdir():
        if _artifact_project(path) == snapshot.index_name:
            path.unlink()
    if snapshot.path.parent.exists():
        shutil.rmtree(snapshot.path.parent)


async def _prune(snapshots: list[Snapshot], cache: Path, projects: dict[str, Path], upstream: LocalCBM) -> None:
    for snapshot in snapshots:
        print(f"remove: {snapshot.index_name} <- {snapshot.path.parent}", flush=True)
        if snapshot.index_name in projects or (cache / f"{snapshot.index_name}.db").exists():
            result = await upstream.call_tool("delete_project", {"project": snapshot.index_name})
            if result.get("isError"):
                raise UpdateError(f"cannot remove {snapshot.index_name}: {tool_error_text(result)}")
    remaining = await indexed_projects(upstream) if snapshots else {}
    for snapshot in snapshots:
        if snapshot.index_name in remaining or (cache / f"{snapshot.index_name}.db").exists():
            raise UpdateError(f"CBM did not remove index {snapshot.index_name}")
        _remove_pruned_files(snapshot, cache)


async def _update(args) -> None:
    roots = (args.vllm_root, args.vllm_ascend_root)
    repositories = (
        ("vllm", roots[0], args.vllm_remote, args.vllm_from_version),
        ("vllm-ascend", roots[1], args.vllm_ascend_remote, args.vllm_ascend_from_version),
    )
    plan = [
        checkout for repo, root, remote, floor in repositories
        for checkout in _plan(repo, root, remote, floor)
    ]
    heads = {checkout.snapshot.path: _check_checkout(checkout.snapshot.path, checkout.remote) for checkout in plan}
    missing = sum(heads[checkout.snapshot.path] is None for checkout in plan)
    changed = sum(
        heads[checkout.snapshot.path] is not None and heads[checkout.snapshot.path] != checkout.tag.commit
        for checkout in plan
    )
    print(f"source plan: versions={len(plan)} missing={missing} changed={changed}", flush=True)
    if args.dry_run:
        for repo, root, remote, floor in repositories:
            for snapshot in _prune_plan(repo, root, args.cache_dir, floor, {}):
                _check_checkout(snapshot.path, remote)
                print(f"remove: {snapshot.index_name} <- {snapshot.path.parent}")
        for checkout in plan:
            action = "keep" if heads[checkout.snapshot.path] == checkout.tag.commit else "fetch"
            print(
                f"{action}: {checkout.snapshot.repo} {checkout.tag.name} "
                f"{checkout.tag.commit} -> {checkout.snapshot.path}",
            )
        print("dry run: index freshness is checked only during a real run")
        return
    binary_version = _binary_version(args.cbm_binary)
    upstream = LocalCBM(
        args.cbm_binary, args.cache_dir, args.cbm_memory_mb, args.workers,
        lambda: _check_resources(
            (*roots, args.cache_dir), min(2, args.min_free_disk_gib), min(2, args.min_available_memory_gib),
        ),
    )
    projects = await indexed_projects(upstream)
    for checkout in plan:
        snapshot = checkout.snapshot
        bound = projects.get(snapshot.index_name)
        if bound is not None and bound != snapshot.path:
            raise UpdateError(f"index {snapshot.index_name} points to {bound}, expected {snapshot.path}")
    obsolete = []
    for repo, root, remote, floor in repositories:
        for snapshot in _prune_plan(repo, root, args.cache_dir, floor, projects):
            _check_checkout(snapshot.path, remote)
            obsolete.append(snapshot)
    await _prune(obsolete, args.cache_dir, projects, upstream)
    built = 0
    for checkout in plan:
        snapshot = checkout.snapshot
        receipt = _build_receipt(checkout, args.cache_dir, args.mode, binary_version)
        if (
            heads[snapshot.path] == checkout.tag.commit
            and _read_receipt(snapshot) == receipt
            and projects.get(snapshot.index_name) == snapshot.path
            and (args.cache_dir / f"{snapshot.index_name}.db").is_file()
        ):
            print(f"skip: {snapshot.index_name} {checkout.tag.commit}", flush=True)
            continue
        _check_resources((*roots, args.cache_dir), args.min_free_disk_gib, args.min_available_memory_gib)
        _sync(checkout, heads[snapshot.path])
        _check_resources((*roots, args.cache_dir), args.min_free_disk_gib, args.min_available_memory_gib)
        await prebuild_all(
            SnapshotRegistry([snapshot]), upstream, mode=args.mode, refresh=True, progress=_progress,
        )
        if not (args.cache_dir / f"{snapshot.index_name}.db").is_file():
            raise UpdateError(f"CBM did not publish a cache DB for {snapshot.index_name}")
        if _check_checkout(snapshot.path, checkout.remote) != checkout.tag.commit:
            raise UpdateError(f"snapshot changed during indexing: {snapshot.path}")
        _write_receipt(snapshot, receipt)
        built += 1
    registry = SnapshotRegistry.discover(*roots)
    ensure_indexes(await audit_indexes(registry, upstream))
    print(
        f"update complete: built={built} skipped={len(plan) - built} removed={len(obsolete)}; cache={args.cache_dir}",
        flush=True,
    )


def _version(value: str) -> Version:
    try:
        return Version(value)
    except InvalidVersion as exc:
        raise argparse.ArgumentTypeError(f"invalid version: {value}") from exc


def _positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _parser() -> argparse.ArgumentParser:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--vllm-root", type=Path, default=settings.vllm_root)
    parser.add_argument("--vllm-ascend-root", type=Path, default=settings.vllm_ascend_root)
    parser.add_argument("--vllm-remote", default="https://github.com/vllm-project/vllm.git")
    parser.add_argument("--vllm-ascend-remote", default="https://github.com/vllm-project/vllm-ascend.git")
    parser.add_argument(
        "--vllm-from-version", type=_version,
        help="include this vLLM version and newer; delete older snapshots and indexes",
    )
    parser.add_argument(
        "--vllm-ascend-from-version", type=_version,
        help="include this Ascend version and newer; delete older snapshots and indexes",
    )
    parser.add_argument("--cache-dir", type=Path, default=Path(os.environ.get(
        "CBM_CACHE_DIR", str(Path.home() / ".cache" / "codebase-memory-mcp"),
    )))
    parser.add_argument("--cbm-binary", default="codebase-memory-mcp")
    parser.add_argument("--mode", choices=("full", "moderate", "fast"), default="full")
    parser.add_argument("--min-free-disk-gib", type=_positive, default=10)
    parser.add_argument("--min-available-memory-gib", type=_positive, default=10)
    parser.add_argument("--cbm-memory-mb", type=_positive_int, default=4096)
    parser.add_argument("--workers", type=_positive_int, default=2)
    parser.add_argument("--dry-run", action="store_true", help="check remote commits and local trees without writing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.vllm_root = args.vllm_root.expanduser().resolve()
    args.vllm_ascend_root = args.vllm_ascend_root.expanduser().resolve()
    args.cache_dir = args.cache_dir.expanduser().resolve()
    roots = (args.vllm_root, args.vllm_ascend_root)
    try:
        if any(left == right or left in right.parents or right in left.parents
               for left, right in ((roots[0], roots[1]), (roots[0], args.cache_dir), (roots[1], args.cache_dir))):
            raise UpdateError("snapshot roots and CBM cache must be separate directories")
        with ExitStack() as stack:
            if not args.dry_run:
                _check_resources((*roots, args.cache_dir), args.min_free_disk_gib, args.min_available_memory_gib)
                for root in sorted((*roots, args.cache_dir)):
                    root.mkdir(parents=True, exist_ok=True, mode=0o700)
                    lock = stack.enter_context((root / ".update.lock").open("a"))
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        raise UpdateError(f"another updater holds {root}") from exc
            asyncio.run(_update(args))
    except (
        UpdateError, PrebuildError, RegistryError, UpstreamError, OSError, ValueError, subprocess.SubprocessError,
    ) as exc:
        print(f"update-vllm-snapshots: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("update-vllm-snapshots: interrupted; rerun to finish pending builds", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
