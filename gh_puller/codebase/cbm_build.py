"""Run the repository-wide pipeline over the single-commit build operation."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from .archive import FORMAT_VERSION, GRAPH_FIDELITY_VERSION, Archive, ArchiveError
from .binary import CBMBinaryError, resolve_cbm_binary
from .build_plan import BuildPlan
from .cbm_runner import CBMRunner
from .cbm_transport import CBMTransportError
from .commit_build import CommitTarget, build_commit
from .errors import BuildError
from .git_tree import TreeError
from .incremental_config import IncrementalConfig, IncrementalConfigError, add_incremental_arguments
from .kga_recorder import KGARecorder

PlanSelector = BuildPlan | Callable[[CommitTarget], BuildPlan]
SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


@dataclass(frozen=True, slots=True)
class BuildOptions:
    """Configuration for one repository-wide archive pipeline."""

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


def _legacy_plan(options: BuildOptions) -> BuildPlan:
    try:
        return BuildPlan(
            analysis_mode=options.mode,
            route="full" if options.force_full else "delta",
            incremental=options.incremental,
        )
    except (IncrementalConfigError, ValueError) as exc:
        raise BuildError(f"invalid build plan: {exc}") from exc


def build_archive(options: BuildOptions) -> int:
    """Build all selected repository commits with one constant plan.

    Args:
        options: Repository, destination, CBM identity, and legacy constant plan.

    Returns:
        Zero after the final archive and summary pass verification.
    """
    return build_repository(options, _legacy_plan(options))


def build_repository(options: BuildOptions, plans: PlanSelector) -> int:
    """Build or resume a repository by applying one plan to each selected commit.

    Args:
        options: Repository-wide paths, resource limits, and CBM process settings.
        plans: Constant plan or selector called once for every unarchived commit.

    Returns:
        Zero after every selected commit is durable and the final index verifies.
    """
    build_dir = prepare_output_dir(options.build_dir, options.out_dir)
    return _build_repository(options, build_dir, plans)


def _run(command, *, timeout=120):
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def enumerate_commits(repo: Path) -> list[tuple[str, tuple[str, ...]]]:
    result = _run(["git", "-C", str(repo), "rev-list", "--topo-order", "--parents", "HEAD"])
    if result.returncode:
        raise BuildError(result.stderr.strip())
    commits = []
    for line in reversed(result.stdout.splitlines()):
        values = line.split()
        if not values or not SHA_RE.fullmatch(values[0]):
            raise BuildError(f"invalid commit line: {line!r}")
        commits.append((values[0], tuple(values[1:])))
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


def _incremental_metadata(config: IncrementalConfig) -> dict:
    return BuildPlan(incremental=config).metadata()["cbm_incremental"]


def _validate_resume_binary(last_item: dict | None, digest: str, allow_upgrade: bool) -> bool:
    if last_item is None:
        return False
    recorded = last_item.get("cbm_binary_sha256")
    if recorded == digest:
        return False
    if allow_upgrade:
        return True
    if recorded is None:
        raise BuildError("archive predates CBM binary recording; resume once with --allow-cbm-upgrade")
    raise BuildError(
        "CBM binary differs from the archive: "
        f"recorded={recorded}, requested={digest}; use --allow-cbm-upgrade to accept a semantic boundary",
    )


def _validate_resume_fidelity(last_item: dict | None, project: str) -> None:
    if last_item is None:
        return
    if last_item.get("graph_fidelity_version") != GRAPH_FIDELITY_VERSION:
        raise BuildError("archive has not passed the one-time graph fidelity migration")
    recorded = last_item.get("cbm_project")
    if recorded != project:
        raise BuildError(
            f"CBM project differs from the archive: recorded={recorded!r}, requested={project!r}",
        )


def _repository(path: str | Path) -> Path:
    repo = Path(path).resolve()
    top = _run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"], timeout=30)
    if top.returncode:
        raise BuildError(f"not a git repository: {repo}")
    return Path(top.stdout.strip())


def _selected_commits(repo: Path, maximum: int | None) -> tuple[list[tuple[str, tuple[str, ...]]], int, str]:
    commits = enumerate_commits(repo)
    source_count, source_head = len(commits), commits[-1][0]
    if maximum is not None:
        if maximum <= 0:
            raise BuildError("--max-commits must be positive")
        commits = commits[:maximum]
    return commits, source_count, source_head


def _archive_prefix(path: Path) -> tuple[tuple[str, ...], bool, dict | None]:
    if not path.exists():
        return (), False, None
    with Archive(path, allow_incomplete=True) as archive:
        commits = archive.commit_ids()
        return commits, archive.complete, archive.manifest() if commits else None


def _select_plan(plans: PlanSelector, target: CommitTarget) -> BuildPlan:
    plan = plans(target) if callable(plans) else plans
    if not isinstance(plan, BuildPlan):
        raise BuildError(f"build plan selector returned {type(plan).__name__}, expected BuildPlan")
    return plan


def _constant_plan_metadata(plans: PlanSelector) -> dict:
    if not isinstance(plans, BuildPlan):
        return {"cbm_build_plan": "per-commit"}
    return {
        "mode": plans.analysis_mode,
        "cbm_incremental": plans.metadata()["cbm_incremental"],
        "cbm_force_full": plans.force_full,
    }


def _build_repository(options: BuildOptions, build_dir: Path, plans: PlanSelector) -> int:
    try:
        cbm_binary = resolve_cbm_binary(
            options.binary,
            manifest=options.cbm_manifest,
            registry=options.cbm_registry,
        )
    except CBMBinaryError as exc:
        raise BuildError(str(exc)) from exc
    repo = _repository(options.repo)
    commits, source_count, source_head = _selected_commits(repo, options.max_commits)
    build_dir.mkdir(parents=True, exist_ok=True)
    archive_path = build_dir / "archive.kga"
    archived, archive_complete, last_item = _archive_prefix(archive_path)
    if len(archived) > len(commits):
        raise BuildError(f"archive has {len(archived)} commits, requested {len(commits)}")
    if archived != tuple(sha for sha, _parents in commits[: len(archived)]):
        raise BuildError("archive is not the selected repository topo prefix")
    if archive_complete and len(archived) == len(commits):
        with Archive(archive_path) as archive:
            archive.verify()
            archive.verify_snapshot()
        print(json.dumps({"archive": str(archive_path), "commits": len(archived), "verified": True}))
        return 0

    identity = sha256(f"gh-puller-codebase:{repo}:{build_dir}".encode()).hexdigest()[:12]
    project = options.project_name or f"cbm-archive-{_slug(repo.name)}-{identity}"
    _validate_resume_fidelity(last_item, project)
    binary_upgrade = _validate_resume_binary(last_item, cbm_binary.sha256, options.allow_cbm_upgrade)
    cache_root = Path(os.environ.get("CBM_CACHE_DIR") or Path.home() / ".cache" / "codebase-memory-mcp")
    work_dir = Path(tempfile.gettempdir()) / f"{project}-work"
    progress = Progress(len(commits), len(archived))
    runner = None
    recorder = None
    timings = []
    plan_counts: dict[str, int] = {}
    try:
        progress.render("starting-cbm")
        runner = CBMRunner(
            cbm_binary,
            project,
            cache_root,
            work_dir,
            timeout=options.timeout,
            memory_limit=options.memory_limit,
            transport=options.cbm_transport,
        )
        recorder = KGARecorder(archive_path, compression_level=options.compression_level)
        resume_aligned = recorder.latest_commit is not None and runner.current_commit == recorder.latest_commit

        for ordinal in range(len(archived), len(commits)):
            sha, parents = commits[ordinal]
            previous_sha = commits[ordinal - 1][0] if ordinal else None
            target = CommitTarget(ordinal, sha, parents, previous_sha)
            plan = _select_plan(plans, target)
            plan_key = f"{plan.analysis_mode}:{plan.route}:{plan.incremental.digest()[:12]}"
            plan_counts[plan_key] = plan_counts.get(plan_key, 0) + 1
            result = build_commit(
                target,
                plan,
                recorder,
                repo=repo,
                runner=runner,
                force_snapshot=binary_upgrade,
                on_stage=lambda stage, commit_sha=sha: progress.stage(commit_sha, stage),
            )
            binary_upgrade = False
            timings.append(
                {
                    "sha": sha,
                    **result.stage_seconds,
                    "build_plan": plan.metadata(),
                    "cbm_index_execution": result.index_execution,
                },
            )
            manifest = result.manifest
            progress.advance(
                sha,
                f"requested={plan.route} actual={result.index_execution['route']} "
                f"files={manifest['changed_files'] if manifest['changed_files'] is not None else 'full'} "
                f"rows={manifest['changed_rows']} pages={manifest['pages_written']} "
                f"cbm={result.stage_seconds['cbm_seconds']:.1f}s "
                f"diff={result.stage_seconds['generation_diff_seconds']:.1f}s "
                f"merkle={result.stage_seconds['merkle_seconds']:.1f}s",
            )

        progress.render("finalizing")
        plan_metadata = _constant_plan_metadata(plans)
        recorder.finalize(
            {
                "repo": str(repo),
                "head": commits[-1][0],
                "selection": "root-first topo prefix",
                **plan_metadata,
                "cbm_binary": cbm_binary.provenance(),
            },
        )
        with Archive(archive_path) as archive:
            archive.verify_index()
            archive.verify_snapshot()
            archive_count = len(archive)
        progress.render("cleanup")
        cleanup_ok, cleanup_detail = runner.delete_project()
        runner.cleanup_work_dir()
        resources = runner.resources
        capabilities = sorted(runner.capabilities)
        startup_seconds = runner.startup_seconds
        runner.close()
        runner = None
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
            **plan_metadata,
            "cbm_plan_counts": plan_counts,
            "cbm_binary": cbm_binary.provenance(),
            "cbm_capabilities_detected": capabilities,
            "cbm_transport": options.cbm_transport,
            "cbm_transport_startup_seconds": startup_seconds,
            "cbm_resume_aligned": resume_aligned,
            "archive": {"path": str(archive_path), "bytes": archive_path.stat().st_size, "commits": archive_count},
            "resources": {"peak_scratch_bytes": resources.scratch_bytes, "peak_rss_bytes": resources.rss_bytes},
            "new_commit_timings": timings,
            "cleanup": {"project_deleted": cleanup_ok, "detail": cleanup_detail},
            "verified": True,
        }
        (build_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        progress.close()
        print(json.dumps({"archive": summary["archive"], "resources": summary["resources"]}, indent=2))
        return 0  # noqa: TRY300 - failure cleanup is centralized for all build stages.
    except Exception:
        if recorder is not None:
            recorder.close_incomplete()
        if runner is not None:
            runner.close()
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
    parser.add_argument(
        "--mode",
        choices=("full", "moderate", "fast", "cross-repo-intelligence"),
        default="full",
    )
    parser.add_argument(
        "--force-full",
        action="store_true",
        help="ask CBM to bypass noop/delta routing for every selected commit",
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


def _options_from_namespace(args: argparse.Namespace) -> BuildOptions:
    try:
        incremental = IncrementalConfig.from_namespace(args)
    except IncrementalConfigError as exc:
        raise BuildError(f"invalid delta controls: {exc}") from exc
    return BuildOptions(
        repo=args.repo,
        build_dir=args.build_dir,
        binary=args.binary,
        cbm_manifest=args.cbm_manifest,
        cbm_registry=args.cbm_registry,
        out_dir=args.out_dir,
        project_name=args.project_name,
        mode=args.mode,
        force_full=bool(args.force_full),
        incremental=incremental,
        compression_level=args.compression_level,
        max_commits=args.max_commits,
        memory_limit=args.memory_limit,
        timeout=args.timeout,
        cbm_transport=args.cbm_transport,
        allow_cbm_upgrade=bool(args.allow_cbm_upgrade),
    )


def build(args: argparse.Namespace) -> int:
    """Run the legacy namespace entry point through the repository pipeline.

    Args:
        args: Namespace populated by :func:`add_build_arguments`.

    Returns:
        Zero after a verified build.
    """
    return build_archive(_options_from_namespace(args))


def run_from_namespace(args: argparse.Namespace) -> int:
    """Run a parsed CLI build request and render expected user-facing failures.

    Args:
        args: Namespace populated by :func:`add_build_arguments`.
    """
    try:
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
