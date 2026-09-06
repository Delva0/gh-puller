"""提供 GitHub 观测归档的同步、调度与维护命令。

调度只决定何时调用同步器；维护任务不推进发现水位。事实时间始终来自实际
source read，不会被调度边界或维护请求时间改写。
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import re
import signal
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv

from .maintenance import REFRESH_FAMILIES, GitHubMaintainer, MaintenanceResult
from .progress import ConsoleProgress
from .syncer import GitHubSyncConfig, GitHubSyncer, SyncResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Sequence

_DEFAULT_INTERVAL = timedelta(hours=1)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_INTERVAL = re.compile(r"([1-9][0-9]*)([smhd])\Z")
_INTERVAL_UNITS = {
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uv run -m gh_puller.github")
    commands = parser.add_subparsers(dest="command", required=True)

    once = commands.add_parser("once", help="run or resume one sync cycle")
    _add_sync_arguments(once)

    schedule = commands.add_parser("schedule", help="sync on fixed UTC intervals")
    _add_sync_arguments(schedule)
    schedule.add_argument(
        "--interval",
        type=_parse_interval,
        default=_DEFAULT_INTERVAL,
        metavar="DURATION",
        help="UTC-aligned cadence such as 30m, 1h, or 1d (default: 1h)",
    )

    refresh = commands.add_parser(
        "refresh",
        help="actively observe selected Issues, PRs, commits, or Git refs",
    )
    _add_sync_arguments(refresh)
    refresh.add_argument("--pull", type=_positive_int, action="append", default=[])
    refresh.add_argument("--issue", type=_positive_int, action="append", default=[])
    refresh.add_argument("--commit", action="append", default=[])
    refresh.add_argument(
        "--family",
        dest="families",
        choices=REFRESH_FAMILIES,
        action="append",
        help="refresh this family; repeat to narrow the goal (dependencies are automatic)",
    )
    refresh.add_argument("--idempotency-key")

    backfill = commands.add_parser(
        "backfill",
        help="verify structured commits over a frozen raw-source range",
    )
    _add_sync_arguments(backfill)
    backfill.add_argument("--idempotency-key")

    return parser


def _add_sync_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("repository", help="GitHub owner/repo")
    parser.add_argument("destination", type=Path, help="SQLite observation archive")
    _add_source_arguments(parser)
    parser.add_argument("--git-batch-size", type=_positive_int, default=8)
    parser.add_argument("--overlap-seconds", type=_positive_int, default=2)
    parser.add_argument("--no-progress", action="store_true", help="disable progress on stderr")


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-url", default="https://api.github.com")
    parser.add_argument("--graphql-url")
    parser.add_argument("--api-version", default="2022-11-28")
    parser.add_argument("--git-url", help="Git remote URL")
    parser.add_argument("--git-destination", type=Path, help="bare Git object store")
    parser.add_argument("--concurrency", type=_positive_int, default=8)
    parser.add_argument("--request-timeout", type=float, default=30.0)


def _parse_interval(value: str) -> timedelta:
    match = _INTERVAL.fullmatch(value)
    if match is None:
        raise argparse.ArgumentTypeError(
            "interval must be a positive integer followed by s, m, h, or d",
        )
    amount, unit = match.groups()
    return int(amount) * _INTERVAL_UNITS[unit]


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _config(args: argparse.Namespace) -> GitHubSyncConfig:
    return GitHubSyncConfig(
        repository=args.repository,
        destination=args.destination,
        api_url=args.api_url,
        graphql_url=args.graphql_url,
        api_version=args.api_version,
        git_url=args.git_url,
        git_destination=args.git_destination,
        concurrency=args.concurrency,
        git_batch_size=args.git_batch_size,
        request_timeout=args.request_timeout,
        overlap_seconds=args.overlap_seconds,
    )


async def _dispatch(args: argparse.Namespace) -> None:
    observer = None if args.no_progress else ConsoleProgress()
    if args.command in {"backfill", "refresh"}:
        maintainer = GitHubMaintainer(_config(args), observer=observer)
        result = (
            await maintainer.backfill(idempotency_key=args.idempotency_key)
            if args.command == "backfill"
            else await maintainer.refresh(
                pulls=args.pull,
                issues=args.issue,
                commits=args.commit,
                families=args.families,
                idempotency_key=args.idempotency_key,
            )
        )
        _emit_maintenance(result)
        return
    syncer = GitHubSyncer(_config(args), observer=observer)
    if args.command == "once":
        _emit(await syncer.sync())
        return
    with _schedule_lock(args.destination):
        await _run_schedule(syncer, args.interval)


async def _run_schedule(
    syncer: GitHubSyncer,
    interval: timedelta,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    emit: Callable[[SyncResult], None] | None = None,
    max_cycles: int | None = None,
) -> None:
    """Synchronize immediately, then at each following UTC interval boundary.

    Args:
        syncer: Repository-bound synchronization operation.
        interval: Positive UTC-aligned invocation cadence.
        now: Scheduler clock; it does not timestamp facts.
        sleep: Interruptible boundary wait.
        emit: Completed-cycle sink; None writes CLI JSON.
        max_cycles: Test boundary; None schedules forever.
    """
    if interval <= timedelta(0):
        raise ValueError("interval must be positive")
    sink = _emit if emit is None else emit
    completed = 0
    while max_cycles is None or completed < max_cycles:
        sink(await syncer.sync())
        completed += 1
        if max_cycles is not None and completed >= max_cycles:
            return
        current = _utc(now())
        await sleep(max((_next_boundary(current, interval) - current).total_seconds(), 0.0))


@contextmanager
def _schedule_lock(destination: Path) -> Iterator[None]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    path = destination.parent / f".{destination.name}.gh-puller-schedule.lock"
    file = path.open("a+")
    try:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"scheduled sync already runs for {destination}") from exc
        yield
    finally:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        file.close()


def _next_boundary(value: datetime, interval: timedelta) -> datetime:
    elapsed = _utc(value) - _EPOCH
    return _EPOCH + (elapsed // interval + 1) * interval


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must include a timezone")
    return value.astimezone(UTC)


def _emit(result: SyncResult) -> None:
    payload: dict[str, Any] = {
        "checkpoint_from": _optional_time(result.checkpoint_from),
        "completed_at": _time(result.completed_at),
        "cycle_id": result.cycle_id,
        "discovered_items": result.discovered_items,
        "requests": result.requests,
        "started_at": _time(result.started_at),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _emit_maintenance(result: MaintenanceResult) -> None:
    print(
        json.dumps(
            {
                "completed_at": _time(result.completed_at),
                "completed_tasks": result.completed_tasks,
                "job_id": result.job_id,
                "job_key": result.job_key,
                "kind": result.kind,
                "requested_at": _time(result.requested_at),
                "requests": result.requests,
                "total_tasks": result.total_tasks,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )


def _optional_time(value: datetime | None) -> str | None:
    return None if value is None else _time(value)


def _time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


async def _run_with_signals(args: argparse.Namespace) -> int:
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(_dispatch(args))
    exit_code: int | None = None

    def stop(code: int) -> None:
        nonlocal exit_code
        exit_code = code
        task.cancel()

    installed: list[signal.Signals] = []
    for sig, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
        try:
            loop.add_signal_handler(sig, stop, code)
        except NotImplementedError:
            continue
        installed.append(sig)
    try:
        await task
    except asyncio.CancelledError:
        if exit_code is None:
            raise
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
    return exit_code or 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run a GitHub archive command.

    Args:
        argv: Arguments without the program name; None uses process arguments.

    Returns:
        Zero on completion, 130 for SIGINT, or 143 for SIGTERM.
    """
    load_dotenv()
    return asyncio.run(_run_with_signals(_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
