"""提供 GitHub 归档的一次性与整点小时调度命令。

本模块只负责 CLI、调度恢复和结构化运行结果；拉取契约见 ``gh_puller.github``，
算法与运维说明见 ``docs/github-puller.md``。
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import signal
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv

from .puller import GitHubPullConfig, GitHubPuller, PullResult

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

_STATE_NAME = ".gh-puller-state.json"
_HOUR = timedelta(hours=1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m gh_puller.github")
    commands = parser.add_subparsers(dest="command", required=True)

    once = commands.add_parser("once", help="pull once to an optional RFC 3339 target")
    _add_common_arguments(once)
    once.add_argument("--target", type=_parse_time)

    hourly = commands.add_parser("hourly", help="pull every UTC clock hour without skipping targets")
    _add_common_arguments(hourly)
    return parser


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("repository", help="GitHub owner/repo")
    parser.add_argument("destination", type=Path, help="dedicated archive Git worktree")
    parser.add_argument("--api-url", default="https://api.github.com")
    parser.add_argument("--graphql-url")
    parser.add_argument("--api-version", default="2022-11-28")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--transient-retries", type=int, default=5)
    parser.add_argument("--overlap-seconds", type=int, default=2)
    parser.add_argument("--catalog-mode", choices=("certified", "exhaustive"), default="certified")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("target must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("target must include a timezone")
    return parsed.astimezone(UTC)


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
        catalog_mode=args.catalog_mode,
    )


async def _dispatch(args: argparse.Namespace) -> None:
    puller = GitHubPuller(_config(args))
    if args.command == "once":
        _emit(await puller.pull(args.target))
        return
    with _hourly_lock(args.destination):
        await _run_hourly(puller, args.destination)


async def _run_hourly(
    puller: GitHubPuller,
    destination: Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    emit: Callable[[PullResult], None] | None = None,
    max_runs: int | None = None,
) -> None:
    target = await _hourly_start(destination, _utc(now()))
    sink = _emit if emit is None else emit
    runs = 0
    while max_runs is None or runs < max_runs:
        result = await puller.pull(target)
        sink(result)
        target = _next_hour(target)
        runs += 1


async def _hourly_start(destination: Path, now: datetime) -> datetime:
    committed = await _latest_committed_target(destination)
    pending = _pending_target(destination)
    if pending is not None and (committed is None or pending > committed):
        return pending
    if committed is not None:
        return _next_hour(committed)
    return _floor_hour(now)


async def _latest_committed_target(destination: Path) -> datetime | None:
    if not (destination / ".git").exists():
        return None
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(destination),
        "log",
        "-1",
        "--format=%s",
        "--",
        _STATE_NAME,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    if process.returncode:
        return None
    subject = stdout.decode().strip()
    if not subject:
        return None
    try:
        return _parse_time(subject)
    except argparse.ArgumentTypeError:
        return None


def _pending_target(destination: Path) -> datetime | None:
    path = destination / _STATE_NAME
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    last_pull = state.get("last_pull")
    if not isinstance(last_pull, dict) or not isinstance(last_pull.get("target_at"), str):
        return None
    return _parse_time(last_pull["target_at"])


@contextmanager
def _hourly_lock(destination: Path) -> Iterator[None]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    path = destination.parent / f".{destination.name}.gh-puller-hourly.lock"
    file = path.open("a+")
    try:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"hourly puller already runs for {destination}") from exc
        yield
    finally:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        file.close()


def _floor_hour(value: datetime) -> datetime:
    return _utc(value).replace(minute=0, second=0, microsecond=0)


def _next_hour(value: datetime) -> datetime:
    return _floor_hour(value) + _HOUR


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must be timezone-aware")
    return value.astimezone(UTC)


def _emit(result: PullResult) -> None:
    payload: dict[str, Any] = {
        "catalog_items": result.catalog_items,
        "changed_items": result.changed_items,
        "commit": result.commit,
        "completed_at": result.completed_at.isoformat().replace("+00:00", "Z"),
        "lag_seconds": result.lag_seconds,
        "requests": result.requests,
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
