"""Run the production index prebuild or the online adapter service."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import uvicorn

from vllm_kb_adapter.adapter import CHECKLIST_TOOLS
from vllm_kb_adapter.app import create_app
from vllm_kb_adapter.config import Settings
from vllm_kb_adapter.indexing import IndexCoordinator
from vllm_kb_adapter.prebuild import (
    IndexAudit,
    PrebuildError,
    audit_indexes,
    ensure_bindings,
    prebuild_all,
)
from vllm_kb_adapter.snapshots import RegistryError, Snapshot, SnapshotRegistry
from vllm_kb_adapter.upstream import MCPUpstream, UpstreamError


def _parser(settings: Settings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vllm-kb-adapter")
    parser.add_argument("--upstream-url", default=settings.upstream_url)
    parser.add_argument("--vllm-root", type=Path, default=settings.vllm_root)
    parser.add_argument("--vllm-ascend-root", type=Path, default=settings.vllm_ascend_root)
    commands = parser.add_subparsers(dest="command", required=True)

    prebuild = commands.add_parser("prebuild", help="build every versioned snapshot index")
    prebuild.add_argument("--mode", choices=("full", "moderate", "fast"), default="full")
    prebuild.add_argument("--refresh", action="store_true")

    serve = commands.add_parser("serve", help="audit index bindings, then serve the adapter")
    serve.add_argument("--host", default=settings.host)
    serve.add_argument("--port", type=int, default=settings.port)
    serve.add_argument("--path", default=settings.path)
    serve.add_argument("--upstream-timeout", type=float, default=settings.upstream_timeout)
    return parser


async def _prebuild(args, registry: SnapshotRegistry) -> None:
    upstream = MCPUpstream(args.upstream_url, timeout=None)
    try:
        report = await prebuild_all(
            registry,
            upstream,
            mode=args.mode,
            refresh=args.refresh,
            progress=_progress,
        )
    finally:
        await upstream.aclose()
    print(f"prebuild complete: built={len(report.built)} skipped={len(report.skipped)}")


async def _audit(args, registry: SnapshotRegistry) -> IndexAudit:
    upstream = MCPUpstream(args.upstream_url, timeout=args.upstream_timeout)
    try:
        audit = await audit_indexes(registry, upstream)
        tools_result = await upstream.request("tools/list", {})
    finally:
        await upstream.aclose()
    ensure_bindings(audit)
    tools = tools_result.get("tools")
    if not isinstance(tools, list):
        raise PrebuildError("upstream tools/list returned no tool list")
    names = {tool["name"] for tool in tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)}
    required_tools = (*CHECKLIST_TOOLS, "list_projects", "index_repository")
    missing_tools = [name for name in required_tools if name not in names]
    if missing_tools:
        raise PrebuildError(f"upstream is missing required tools: {', '.join(missing_tools)}")
    return audit


def _progress(action: str, snapshot: Snapshot) -> None:
    print(f"{action}: {snapshot.index_name} <- {snapshot.path}", flush=True)


def main(argv: list[str] | None = None) -> int:
    settings = Settings.from_env()
    parser = _parser(settings)
    args = parser.parse_args(argv)
    try:
        registry = SnapshotRegistry.discover(args.vllm_root, args.vllm_ascend_root)
        if args.command == "prebuild":
            asyncio.run(_prebuild(args, registry))
            return 0
        audit = asyncio.run(_audit(args, registry))
    except (PrebuildError, RegistryError, UpstreamError) as exc:
        print(f"vllm-kb-adapter: {exc}", file=sys.stderr)
        return 1

    upstream = MCPUpstream(args.upstream_url, timeout=args.upstream_timeout)
    index_upstream = MCPUpstream(args.upstream_url, timeout=None)
    ready = tuple(snapshot for snapshot in registry.snapshots if snapshot not in audit.missing)
    indexes = IndexCoordinator(index_upstream, ready)
    uvicorn.run(
        create_app(registry, upstream, indexes=indexes, path=args.path),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
