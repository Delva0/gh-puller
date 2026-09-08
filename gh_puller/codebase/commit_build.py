"""Build one selected commit into one exact KGA generation.

The function in this module is the foundational build operation. Repository-wide
pipelines supply ordering and lifecycle policy but call the same operation for
every commit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import BuildError
from .git_tree import changed_paths, materialize_full
from .kga_recorder import KGACommit

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .build_plan import BuildPlan
    from .cbm_runner import CBMRunner
    from .graph_reader import GraphReader
    from .kga_recorder import KGARecorder


@dataclass(frozen=True, slots=True)
class CommitTarget:
    """One Git commit selected for the next archive position."""

    ordinal: int
    sha: str
    parents: tuple[str, ...]
    previous_sha: str | None


@dataclass(frozen=True, slots=True)
class CommitBuildResult:
    """Execution evidence returned after one durable KGA checkpoint."""

    manifest: dict
    stage_seconds: dict[str, float]
    index_execution: dict


def _stage(callback: Callable[[str], None] | None, name: str) -> None:
    if callback is not None:
        callback(name)


def _recover_committed_pending(runner: CBMRunner, recorder: KGARecorder) -> None:
    pending = runner.pending_commit
    if pending is not None and recorder.latest_commit == pending:
        runner.mark_archived(pending)


def build_commit(
    repo: str | Path,
    target: CommitTarget,
    plan: BuildPlan,
    runner: CBMRunner,
    reader: GraphReader,
    recorder: KGARecorder,
    *,
    force_snapshot: bool = False,
    metadata: Mapping[str, object] | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> CommitBuildResult:
    """Build one selected Git commit into a specified KGA.

    Args:
        repo: Source Git repository containing ``target``.
        target: Commit identity, archive position, and preceding selected commit.
        plan: CBM coverage and full/delta policy for this commit only.
        runner: Reusable CBM process and build state owner.
        reader: Exact reader bound to the runner's published project database.
        recorder: KGA transaction receiving this generation.
        force_snapshot: Disable the generation-diff optimization for this commit.
        metadata: Additional immutable CBM provenance stored in the manifest.
        on_stage: Optional callback receiving materialize, index, capture, and record stages.

    Returns:
        Durable manifest, stage timings, and CBM execution evidence.

    Raises:
        BuildError: Components disagree about the project, archive position, or
            an interrupted commit.
    """
    if reader.db_path != runner.db_path or reader.project != runner.project:
        raise BuildError("graph reader is not bound to the CBM runner")
    if target.ordinal != len(recorder.commits):
        raise BuildError(
            f"commit ordinal {target.ordinal} does not match KGA position {len(recorder.commits)}",
        )
    if target.previous_sha != recorder.latest_commit:
        raise BuildError(
            f"commit predecessor {target.previous_sha!r} does not match KGA head {recorder.latest_commit!r}",
        )
    _recover_committed_pending(runner, recorder)
    pending = runner.pending_commit
    if pending is not None and pending != target.sha:
        raise BuildError(f"interrupted commit {pending} must be recovered before {target.sha}")

    repo_path = Path(repo).resolve()
    timings = {}
    started = time.monotonic()
    _stage(on_stage, "materialize")
    changed_files = (
        len(changed_paths(repo_path, target.previous_sha, target.sha))
        if target.previous_sha is not None
        else None
    )
    materialize_full(repo_path, target.sha, runner.tree)
    timings["materialize_seconds"] = time.monotonic() - started

    aligned = (
        pending is None
        and recorder.latest_commit is not None
        and runner.current_commit == recorder.latest_commit
    )
    previous = reader.pin() if aligned and not force_snapshot else None
    runner.begin_commit(target.sha)
    try:
        started = time.monotonic()
        _stage(on_stage, "cbm-index")
        execution = runner.index(plan)
        timings["cbm_seconds"] = time.monotonic() - started

        started = time.monotonic()
        _stage(on_stage, "graph-capture")
        capture = reader.capture(
            previous,
            force_snapshot=force_snapshot,
            unchanged=execution.get("route") == "noop",
        )
        timings["generation_diff_seconds"] = time.monotonic() - started
    finally:
        if previous is not None:
            previous.close()

    started = time.monotonic()
    _stage(on_stage, "kga-record")
    provenance = {
        **plan.metadata(),
        **dict(metadata or {}),
        "cbm_index_execution": execution,
        "cbm_force_full": plan.force_full,
        "cbm_binary_sha256": runner.binary.sha256,
    }
    manifest = recorder.append(
        KGACommit(target.ordinal, target.sha, target.parents, changed_files),
        capture,
        project=runner.project,
        metadata=provenance,
    )
    runner.mark_archived(target.sha)
    timings["merkle_seconds"] = time.monotonic() - started
    return CommitBuildResult(
        manifest,
        {key: round(value, 3) for key, value in timings.items()},
        execution,
    )
