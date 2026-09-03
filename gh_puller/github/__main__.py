"""提供 GitHub 归档的一次性与固定间隔调度命令。

本模块只负责 CLI、调度恢复和结构化运行结果；拉取契约见 ``gh_puller.github``，
算法与运维说明见 ``docs/github-puller.md``。
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

from .progress import ConsoleProgress
from .puller import GitHubPullConfig, GitHubPuller, PullResult
from .store import schedule_state

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

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
    parser = argparse.ArgumentParser(prog="python -m gh_puller.github")
    commands = parser.add_subparsers(dest="command", required=True)

    once = commands.add_parser("once", help="pull once to an optional RFC 3339 target")
    _add_common_arguments(once)
    once.add_argument("--target", type=_parse_time)

    schedule = commands.add_parser("schedule", help="pull on UTC-aligned fixed intervals")
    _add_common_arguments(schedule)
    schedule.add_argument(
        "--interval",
        type=_parse_interval,
        default=_DEFAULT_INTERVAL,
        metavar="DURATION",
        help="UTC-aligned cadence such as 30m, 1h, or 1d (default: 1h)",
    )
    return parser


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("repository", help="GitHub owner/repo")
    parser.add_argument("destination", type=Path, help="SQLite raw-fact database")
    parser.add_argument("--api-url", default="https://api.github.com")
    parser.add_argument("--graphql-url")
    parser.add_argument("--api-version", default="2022-11-28")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--transient-retries", type=int, default=5)
    parser.add_argument("--overlap-seconds", type=int, default=2)
    parser.add_argument("--no-progress", action="store_true", help="disable progress on stderr")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("target must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("target must include a timezone")
    return parsed.astimezone(UTC)


def _parse_interval(value: str) -> timedelta:
    match = _INTERVAL.fullmatch(value)
    if match is None:
        raise argparse.ArgumentTypeError("interval must be a positive integer followed by s, m, h, or d")
    amount, unit = match.groups()
    return int(amount) * _INTERVAL_UNITS[unit]


def _config(args: argparse.Namespace) -> GitHubPullConfig:
    return GitHubPullConfig(
        repository=args.repository,
        destination=args.destination,
        api_url=args.api_url,
        graphql_url=args.graphql_url,
        api_version=args.api_version,
        concurrency=args.concurrency,
        request_timeout=args.request_timeout,
        transient_retries=args.transient_retries,
        overlap_seconds=args.overlap_seconds,
    )


async def _dispatch(args: argparse.Namespace) -> None:
    observer = None if args.no_progress else ConsoleProgress()
    puller = GitHubPuller(_config(args), observer=observer)
    if args.command == "once":
        _emit(await puller.pull(args.target))
        return
    with _schedule_lock(args.destination):
        await _run_schedule(puller, args.destination, args.interval)


async def _run_schedule(
    puller: GitHubPuller,
    destination: Path,
    interval: timedelta,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    emit: Callable[[PullResult], None] | None = None,
    max_runs: int | None = None,
) -> None:
    if interval <= timedelta(0):
        raise ValueError("interval must be positive")
    sink = _emit if emit is None else emit
    runs = 0
    if max_runs is not None and runs >= max_runs:
        return
    state = await schedule_state(destination)
    committed = None if state.committed_target is None else _parse_time(state.committed_target)
    if state.pending_target is not None:
        pending = _parse_time(state.pending_target)
        sink(await puller.pull(pending))
        runs += 1
        committed = pending if committed is None else max(committed, pending)
        if max_runs is not None and runs >= max_runs:
            return
    while max_runs is None or runs < max_runs:
        target = _scheduled_target(committed, _utc(now()), interval)
        result = await puller.pull(target)
        sink(result)
        committed = target
        runs += 1


@contextmanager
def _schedule_lock(destination: Path) -> Iterator[None]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    path = destination.parent / f".{destination.name}.gh-puller-schedule.lock"
    file = path.open("a+")
    try:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"scheduled puller already runs for {destination}") from exc
        yield
    finally:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        file.close()


def _scheduled_target(
    committed: datetime | None,
    now: datetime,
    interval: timedelta,
) -> datetime:
    due = _floor_interval(now, interval)
    if committed is None:
        return due
    following = _floor_interval(committed, interval) + interval
    return max(due, following)


def _floor_interval(value: datetime, interval: timedelta) -> datetime:
    elapsed = _utc(value) - _EPOCH
    return _EPOCH + (elapsed // interval) * interval


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must be timezone-aware")
    return value.astimezone(UTC)


def _emit(result: PullResult) -> None:
    payload: dict[str, Any] = {
        "catalog_items": result.catalog_items,
        "changed_items": result.changed_items,
        "completed_at": result.completed_at.isoformat().replace("+00:00", "Z"),
        "lag_seconds": result.lag_seconds,
        "requests": result.requests,
        "run_id": result.run_id,
        "target_at": result.target_at.isoformat().replace("+00:00", "Z"),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


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
    """运行 GitHub 归档命令。

    Args:
        argv: 不含程序名的参数；None 使用当前进程参数。

    Returns:
        成功为 0；SIGINT 为 130；SIGTERM 为 143。
    """
    load_dotenv()
    args = _parser().parse_args(argv)
    return asyncio.run(_run_with_signals(args))


if __name__ == "__main__":
    raise SystemExit(main())
