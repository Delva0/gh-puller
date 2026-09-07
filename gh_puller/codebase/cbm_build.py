"""Build a persistent Merkle archive from a Git repository through CBM."""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, fields
from hashlib import sha256
from pathlib import Path

from .archive import (
    FORMAT_VERSION,
    GRAPH_FIDELITY_VERSION,
    Archive,
    ArchiveError,
    ArchiveWriter,
    RadixTree,
    TreeRef,
    graph_digest,
)
from .binary import CBMBinaryError, resolve_cbm_binary
from .cbm_transport import CBMTransportError, make_transport
from .generation_diff import ChangeSet, PinnedGeneration
from .git_tree import TreeError, changed_paths, materialize_full
from .incremental_config import (
    IncrementalConfig,
    IncrementalConfigError,
    add_incremental_arguments,
)
from .store import ExtractionError, GraphRows, iter_edges, iter_nodes, validate_rows

SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


class BuildError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BuildOptions:
    """Configuration for one archive build or resume operation."""

    repo: str | Path
    build_dir: str | Path
    binary: str | Path | None = None
    cbm_manifest: str | Path | None = None
    cbm_registry: str | Path | None = None
    out_dir: str | Path | None = None
    project_name: str | None = None
    mode: str = "full"
    force_full: bool = False
    incremental: IncrementalConfig = field(default_factory=IncrementalConfig)
    compression_level: int = 1
    max_commits: int | None = None
    memory_limit: int | None = None
    timeout: int = 3600
    cbm_transport: str = "persistent-mcp"
    allow_cbm_upgrade: bool = False


def build_archive(options: BuildOptions) -> int:
    """Build or resume one persistent code graph archive.

    Args:
        options: Repository, destination, CBM identity, and independent indexing
            controls for this operation.

    Returns:
        Zero after the final archive and summary pass verification.
    """
    options.incremental.validate()
    values = {item.name: getattr(options, item.name) for item in fields(options) if item.name != "incremental"}
    values.update({f"delta_{key}": value for key, value in options.incremental.to_dict().items()})
    args = argparse.Namespace(**values)
    args.build_dir = str(prepare_output_dir(args.build_dir, args.out_dir))
    return build(args)


def _run(command, *, env=None, timeout=120):
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        env=env,
        timeout=timeout,
        check=False,
    )


def enumerate_commits(repo: Path) -> list[tuple[str, tuple[str, ...]]]:
    result = _run(["git", "-C", str(repo), "rev-list", "--topo-order", "--parents", "HEAD"])
    if result.returncode:
        raise BuildError(result.stderr.strip())
    commits = []
    for line in reversed(result.stdout.splitlines()):
        fields = line.split()
        if not fields or not SHA_RE.fullmatch(fields[0]):
            raise BuildError(f"invalid commit line: {line!r}")
        commits.append((fields[0], tuple(fields[1:])))
    return commits


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-") or "repo"


def _duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class Progress:
    def __init__(self, total: int, initial: int):
        self.total = total
        self.initial = initial
        self.completed = initial
        self.started = time.monotonic()
        self.interactive = sys.stderr.isatty()
        self.width = 0
        self.render("ready")

    def stage(self, sha: str, stage: str) -> None:
        if self.interactive:
            self.render(f"{stage} {sha[:10]}")

    def advance(self, sha: str, detail: str) -> None:
        self.completed += 1
        self.render(f"{sha[:10]} {detail}")

    def render(self, label: str) -> None:
        fraction = self.completed / self.total
        filled = int(24 * fraction)
        elapsed = time.monotonic() - self.started
        done = self.completed - self.initial
        eta = elapsed / done * (self.total - self.completed) if done and self.completed < self.total else 0
        eta_text = _duration(eta) if done else "--:--:--"
        line = (
            f"[{('#' * filled).ljust(24, '-')}] {self.completed}/{self.total} {fraction:6.2%} "
            f"elapsed {_duration(elapsed)} ETA {eta_text}  {label}"
        )
        if self.interactive:
            sys.stderr.write(f"\r{line}{' ' * max(0, self.width - len(line))}")
            self.width = len(line)
        else:
            sys.stderr.write(f"{line}\n")
        sys.stderr.flush()

    def close(self) -> None:
        if self.interactive:
            sys.stderr.write("\n")
            sys.stderr.flush()


def _total_memory() -> int:
    candidates = []
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            candidates.append(int(line.split()[1]) * 1024)
            break
    cgroup = Path("/sys/fs/cgroup/memory.max")
    if cgroup.exists() and (value := cgroup.read_text().strip()) != "max":
        candidates.append(int(value))
    return min(candidates)


def _size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return 0


def _rss(pid: int | None) -> int:
    if pid is None:
        return 0
    total, pending, seen = 0, [pid], set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            for line in Path(f"/proc/{current}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1]) * 1024
                    break
            pending.extend(int(child) for child in Path(f"/proc/{current}/task/{current}/children").read_text().split())
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    return total


class ResourceMonitor:
    def __init__(self, paths: list[Path], memory_limit: int):
        self.paths = paths
        self.memory_limit = memory_limit
        self.peak_scratch = 0
        self.peak_rss = 0
        self.child_pid = None
        self.exceeded = False
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()

    def sample(self):
        self.peak_scratch = max(self.peak_scratch, sum(_size(path) for path in self.paths))
        own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        self.peak_rss = max(self.peak_rss, own + _rss(self.child_pid))
        self.exceeded |= self.peak_rss > self.memory_limit

    def _loop(self):
        while not self.stop_event.wait(0.1):
            self.sample()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)
        self.sample()


def run_index(
    binary: Path,
    tree: Path,
    project: str,
    mode: str,
    cache_root: Path,
    timeout: int,
    monitor: ResourceMonitor,
    *,
    force_full: bool = False,
    incremental_controls: dict[str, str | int] | None = None,
) -> dict:
    transport = make_transport("cli", binary, cache_root, timeout, monitor)
    try:
        return transport.index(
            tree,
            project,
            mode,
            force_full=force_full,
            incremental_controls=incremental_controls,
        )
    except CBMTransportError as exc:
        raise BuildError(str(exc)) from exc
    finally:
        transport.close()


def cleanup_project(binary: Path, project: str, cache_root: Path) -> tuple[bool, str]:
    monitor = ResourceMonitor([], 1 << 62)
    transport = make_transport("cli", binary, cache_root, 120, monitor)
    try:
        return transport.delete_project(project)
    finally:
        transport.close()


def _project_exists(db_path: Path, project: str) -> bool:
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            return connection.execute("SELECT 1 FROM projects WHERE name=?", (project,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _write_state(path: Path, sha: str) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(sha)
    os.replace(temporary, path)


def prepare_output_dir(build_dir: str | Path, out_dir: str | Path | None) -> Path:
    source = Path(build_dir).resolve()
    if out_dir is None or Path(out_dir).resolve() == source:
        return source
    destination = Path(out_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    allowed = {"archive.kga", "summary.json", ".archive.kga.copying", ".summary.json.copying"}
    for temporary in (destination / ".archive.kga.copying", destination / ".summary.json.copying"):
        temporary.unlink(missing_ok=True)
    unrelated = [item.name for item in destination.iterdir() if item.name not in allowed]
    if unrelated:
        raise BuildError(f"--out-dir contains unrelated files: {unrelated[:3]}")
    if (destination / "archive.kga").exists():
        return destination
    source_archive = source / "archive.kga"
    if not source_archive.exists():
        raise BuildError(f"source archive does not exist: {source_archive}")
    Archive(source_archive).verify()
    for name in ("archive.kga", "summary.json"):
        source_path, destination_path = source / name, destination / name
        if source_path.exists():
            temporary = destination / f".{name}.copying"
            shutil.copy2(source_path, temporary)
            os.replace(temporary, destination_path)
    return destination


def _root(item: dict | None, name: str) -> TreeRef | None:
    return TreeRef.from_json(item.get(name)) if item else None


def _incremental_metadata(config: IncrementalConfig) -> dict:
    return {
        "version": 1,
        "digest": config.digest(),
        "options": config.to_dict(),
    }


def _validate_resume_incremental_config(last_item: dict | None, config: IncrementalConfig) -> None:
    if last_item is None:
        return
    recorded = last_item.get("cbm_incremental")
    if recorded is None:
        if config != IncrementalConfig():
            raise BuildError(
                "archive predates incremental-config recording and can only resume with "
                "the strict default delta controls",
            )
        return
    expected = _incremental_metadata(config)
    if recorded != expected:
        recorded_digest = recorded.get("digest", "invalid") if isinstance(recorded, dict) else "invalid"
        raise BuildError(
            "requested delta controls do not match the archive: "
            f"recorded={recorded_digest}, requested={expected['digest']}",
        )


def _validate_resume_force_full(last_item: dict | None, force_full: bool) -> None:
    if last_item is None:
        return
    recorded = last_item.get("cbm_force_full", False)
    if not isinstance(recorded, bool) or recorded != force_full:
        raise BuildError(
            f"requested force_full setting does not match the archive: recorded={recorded!r}, requested={force_full!r}",
        )


def _validate_resume_binary(last_item: dict | None, digest: str, allow_upgrade: bool) -> None:
    if last_item is None:
        return
    recorded = last_item.get("cbm_binary_sha256")
    if recorded == digest:
        return
    if allow_upgrade:
        return
    if recorded is None:
        raise BuildError("archive predates CBM binary recording; resume once with --allow-cbm-upgrade")
    raise BuildError(
        "CBM binary differs from the archive: "
        f"recorded={recorded}, requested={digest}; use --allow-cbm-upgrade to accept a semantic boundary",
    )


def _requires_full_snapshot_anchor(last_item: dict | None, binary_digest: str, project: str) -> bool:
    if last_item is None:
        return True
    return (
        last_item.get("graph_fidelity_version") != GRAPH_FIDELITY_VERSION
        or last_item.get("cbm_binary_sha256") != binary_digest
        or last_item.get("cbm_project") != project
    )


def build(args) -> int:
    try:
        incremental_config = IncrementalConfig.from_namespace(args)
    except IncrementalConfigError as exc:
        raise BuildError(f"invalid delta controls: {exc}") from exc
    incremental_metadata = _incremental_metadata(incremental_config)
    incremental_controls = incremental_config.to_dict()
    force_full = bool(getattr(args, "force_full", False))
    try:
        cbm_binary = resolve_cbm_binary(
            getattr(args, "binary", None),
            manifest=getattr(args, "cbm_manifest", None),
            registry=getattr(args, "cbm_registry", None),
        )
    except CBMBinaryError as exc:
        raise BuildError(str(exc)) from exc
    repo = Path(args.repo).resolve()
    top = _run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"], timeout=30)
    if top.returncode:
        raise BuildError(f"not a git repository: {repo}")
    repo = Path(top.stdout.strip())
    binary = cbm_binary.path
    commits = enumerate_commits(repo)
    source_count, source_head = len(commits), commits[-1][0]
    if args.max_commits is not None:
        if args.max_commits <= 0:
            raise BuildError("--max-commits must be positive")
        commits = commits[: args.max_commits]

    build_dir = Path(args.build_dir).resolve()
    build_dir.mkdir(parents=True, exist_ok=True)
    archive_path = build_dir / "archive.kga"
    existing = Archive(archive_path, allow_incomplete=True) if archive_path.exists() else None
    existing_count = len(existing) if existing else 0
    if existing_count > len(commits):
        raise BuildError(f"archive has {existing_count} commits, requested {len(commits)}")
    if existing and [item["sha"] for item in existing._commits] != [sha for sha, _ in commits[:existing_count]]:
        raise BuildError("archive is not the selected repository topo prefix")
    last_existing_item = existing._commits[-1] if existing_count else None
    _validate_resume_incremental_config(last_existing_item, incremental_config)
    _validate_resume_force_full(last_existing_item, force_full)
    if existing and existing.complete and existing_count == len(commits):
        existing.verify()
        existing.verify_snapshot()
        print(json.dumps({"archive": str(archive_path), "commits": existing_count, "verified": True}))
        return 0
    _validate_resume_binary(
        last_existing_item,
        cbm_binary.sha256,
        bool(getattr(args, "allow_cbm_upgrade", False)),
    )

    cache_root = Path(os.environ.get("CBM_CACHE_DIR") or Path.home() / ".cache" / "codebase-memory-mcp")
    identity = sha256(f"gh-puller-codebase:{repo}:{build_dir}".encode()).hexdigest()[:12]
    project = args.project_name or f"cbm-archive-{_slug(repo.name)}-{identity}"
    scratch = Path(tempfile.gettempdir()) / f"{project}-work"
    tree, state_path = scratch / "tree", scratch / "current-sha"
    db_path = cache_root / f"{project}.db"
    memory_limit = args.memory_limit or max(1 << 30, _total_memory() - (1 << 30))
    monitor = ResourceMonitor([scratch, db_path, db_path.with_suffix(".db-wal")], memory_limit)
    monitor.start()
    progress = Progress(len(commits), existing_count)
    writer = None
    transport = None
    transport_startup_seconds = None
    bootstrap_seconds = None
    timings = []
    try:
        progress.render("starting-cbm")
        cbm_binary.verify_unchanged()
        started = time.monotonic()
        transport = make_transport(
            args.cbm_transport,
            binary,
            cache_root,
            args.timeout,
            monitor,
            incremental_config.environment(),
        )
        detected_capabilities = transport.capabilities()
        required_capabilities = {"granular-delta-controls"}
        if args.cbm_transport == "persistent-mcp":
            required_capabilities.add("persistent-mcp")
        if force_full:
            required_capabilities.add("force-full-route")
        missing_capabilities = required_capabilities - detected_capabilities
        if missing_capabilities:
            raise BuildError(f"CBM binary lacks required capabilities: {sorted(missing_capabilities)}")
        transport_startup_seconds = round(time.monotonic() - started, 3)
        writer = ArchiveWriter(
            archive_path,
            compression_level=args.compression_level,
            extend_complete=bool(existing and existing.complete),
        )
        last_item = writer.commits[-1] if writer.commits else None
        node_root, edge_root = _root(last_item, "node_root"), _root(last_item, "edge_root")
        full_snapshot_anchor = _requires_full_snapshot_anchor(last_item, cbm_binary.sha256, project)

        if last_item:
            last_sha = last_item["sha"]
            # The archive commit is the transaction boundary. A prior process may
            # have published the next CBM generation and crashed before committing
            # its archive frame, so filesystem state alone cannot prove alignment.
            progress.stage(last_sha, "bootstrap-cbm")
            started = time.monotonic()
            materialize_full(repo, last_sha, tree)
            transport.index(
                tree,
                project,
                args.mode,
                force_full=force_full,
                incremental_controls=incremental_controls,
            )
            _write_state(state_path, last_sha)
            bootstrap_seconds = round(time.monotonic() - started, 3)

        for ordinal in range(existing_count, len(commits)):
            sha, parents = commits[ordinal]
            previous_sha = commits[ordinal - 1][0] if ordinal else None
            stage_times = {}
            started = time.monotonic()
            progress.stage(sha, "materialize")
            changed_files = len(changed_paths(repo, previous_sha, sha)) if previous_sha else None
            # A full materialization gives CBM a coherent commit snapshot. The
            # fork's manifest planner still selects noop, closure repair, or a
            # conservative full fallback within index_repository.
            materialize_full(repo, sha, tree)
            stage_times["materialize_seconds"] = time.monotonic() - started

            progress.stage(sha, "cbm-index")
            previous_generation = PinnedGeneration(db_path, project) if _project_exists(db_path, project) else None
            try:
                started = time.monotonic()
                index_execution = transport.index(
                    tree,
                    project,
                    args.mode,
                    force_full=force_full,
                    incremental_controls=incremental_controls,
                )
                stage_times["cbm_seconds"] = time.monotonic() - started
                started = time.monotonic()
                if full_snapshot_anchor:
                    changes = None
                    generation_diff_source = "full_snapshot_anchor"
                elif previous_generation and index_execution["route"] == "noop":
                    changes = ChangeSet({}, {})
                    generation_diff_source = "noop"
                elif previous_generation:
                    changes = previous_generation.changes_after_publish()
                    generation_diff_source = "full_generation"
                else:
                    changes = None
                    generation_diff_source = "full_snapshot"
                stage_times["generation_diff_seconds"] = time.monotonic() - started
            finally:
                if previous_generation:
                    previous_generation.close()

            progress.stage(sha, "merkle-update")
            started = time.monotonic()
            if changes is not None:
                node_changes, edge_changes = changes.nodes, changes.edges
            else:
                node_root, edge_root = None, None
                node_changes = dict(iter_nodes(db_path, project))
                edge_changes = dict(iter_edges(db_path, project))
                try:
                    validate_rows(GraphRows(node_changes, edge_changes), project)
                except ExtractionError as exc:
                    raise BuildError(f"CBM produced an unrestorable graph: {exc}") from exc
            node_tree, edge_tree = RadixTree(writer, "nodes"), RadixTree(writer, "edges")
            node_root = node_tree.apply(node_root, node_changes)
            edge_root = edge_tree.apply(edge_root, edge_changes)
            manifest = {
                "ordinal": ordinal,
                "sha": sha,
                "parents": list(parents),
                "node_root": node_root.to_json() if node_root else None,
                "edge_root": edge_root.to_json() if edge_root else None,
                "nodes": node_root.count if node_root else 0,
                "edges": edge_root.count if edge_root else 0,
                "graph_digest": graph_digest(node_root, edge_root),
                "changed_files": changed_files,
                "changed_rows": len(node_changes) + len(edge_changes),
                "pages_read": node_tree.pages_read + edge_tree.pages_read,
                "pages_written": node_tree.pages_written + edge_tree.pages_written,
                "generation_diff_source": generation_diff_source,
                "graph_fidelity_version": GRAPH_FIDELITY_VERSION,
                "cbm_index_execution": index_execution,
                "cbm_incremental": incremental_metadata,
                "cbm_force_full": force_full,
                "cbm_binary_sha256": cbm_binary.sha256,
                "cbm_project": project,
            }
            writer.commit(manifest)
            full_snapshot_anchor = False
            _write_state(state_path, sha)
            stage_times["merkle_seconds"] = time.monotonic() - started
            stage_times = {key: round(value, 3) for key, value in stage_times.items()}
            timings.append({"sha": sha, **stage_times, "cbm_index_execution": index_execution})
            progress.advance(
                sha,
                f"route={index_execution['route']} "
                f"files={changed_files if changed_files is not None else 'full'} "
                f"rows={manifest['changed_rows']} pages={manifest['pages_written']} "
                f"cbm={stage_times['cbm_seconds']:.1f}s "
                f"diff={stage_times['generation_diff_seconds']:.1f}s "
                f"merkle={stage_times['merkle_seconds']:.1f}s",
            )

        progress.render("finalizing")
        writer.finalize(
            {
                "repo": str(repo),
                "head": commits[-1][0],
                "selection": "root-first topo prefix",
                "cbm_incremental": incremental_metadata,
                "cbm_force_full": force_full,
                "cbm_binary": cbm_binary.provenance(),
            },
        )
        writer.verify_appended()
        archive = Archive(archive_path)
        archive.verify_index()
        archive.verify_snapshot()
        progress.render("cleanup")
        cleanup_ok, cleanup_detail = transport.delete_project(project)
        transport.close()
        transport = None
        if scratch.exists():
            shutil.rmtree(scratch)
        monitor.stop()
        summary = {
            "archive_format_version": FORMAT_VERSION,
            "format": "merkle-module-shards-v1",
            "repo": str(repo),
            "head": commits[-1][0],
            "source_head": source_head,
            "source_commit_count": source_count,
            "selected_commit_count": len(commits),
            "selection": "root-first topo prefix",
            "project": project,
            "mode": args.mode,
            "cbm_incremental": incremental_metadata,
            "cbm_force_full": force_full,
            "cbm_binary": cbm_binary.provenance(),
            "cbm_capabilities_detected": sorted(detected_capabilities),
            "cbm_transport": args.cbm_transport,
            "cbm_transport_startup_seconds": transport_startup_seconds,
            "cbm_bootstrap_seconds": bootstrap_seconds,
            "archive": {"path": str(archive_path), "bytes": archive_path.stat().st_size, "commits": len(archive)},
            "resources": {"peak_scratch_bytes": monitor.peak_scratch, "peak_rss_bytes": monitor.peak_rss},
            "new_commit_timings": timings,
            "cleanup": {
                "project_deleted": cleanup_ok,
                "detail": cleanup_detail,
            },
            "verified": True,
        }
        (build_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        progress.close()
        print(json.dumps({"archive": summary["archive"], "resources": summary["resources"]}, indent=2))
        return 0  # noqa: TRY300 - failure cleanup is centralized for all build stages.
    except Exception:
        if writer is not None:
            writer.close_incomplete()
        if transport is not None:
            transport.close()
        monitor.stop()
        progress.close()
        raise


def add_build_arguments(parser: argparse.ArgumentParser) -> None:
    """Add archive-build options to an argument parser.

    Args:
        parser: Parser or subparser that owns the build command.
    """
    parser.add_argument("--repo", required=True)
    parser.add_argument("--binary")
    parser.add_argument("--cbm-manifest")
    parser.add_argument("--cbm-registry")
    parser.add_argument("--build-dir", required=True)
    parser.add_argument("--out-dir")
    parser.add_argument("--project-name")
    parser.add_argument("--mode", choices=("full", "moderate", "fast", "cross-repo-intelligence"), default="full")
    parser.add_argument(
        "--force-full",
        action="store_true",
        help="ask CBM to bypass noop/delta routing and rebuild every selected commit in full",
    )
    parser.add_argument("--compression-level", type=int, default=1)
    parser.add_argument("--max-commits", type=int)
    parser.add_argument("--memory-limit", type=int)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--cbm-transport", choices=("cli", "persistent-mcp"), default="persistent-mcp")
    parser.add_argument(
        "--allow-cbm-upgrade",
        action="store_true",
        help="resume across a changed or previously unrecorded CBM binary identity",
    )
    add_incremental_arguments(parser)


def run_from_namespace(args: argparse.Namespace) -> int:
    """Run a parsed CLI build request and render expected user-facing failures.

    Args:
        args: Namespace populated by :func:`add_build_arguments`.
    """
    try:
        args.build_dir = str(prepare_output_dir(args.build_dir, args.out_dir))
        return build(args)
    except (BuildError, CBMBinaryError, CBMTransportError, TreeError, ArchiveError, OSError, sqlite3.Error) as exc:
        print(f"codebase build: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a persistent Merkle archive through CBM")
    add_build_arguments(parser)
    return run_from_namespace(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
