import os
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path

import pytest

from gh_puller.codebase.cbm.binary import CBMBinary
from gh_puller.codebase.cbm.transports.cli import CLITransport
from gh_puller.codebase.cbm.transports.mcp import MCPTransport, capabilities_from_tools_list
from gh_puller.codebase.cbm.transports.utils import CBMTransportError


class FakeMonitor:
    def __init__(self):
        self.children = set()
        self.exceeded = False
        self.samples = 0

    def add_child(self, pid):
        self.children.add(pid)

    def remove_child(self, pid):
        self.children.discard(pid)

    def sample(self):
        self.samples += 1


def binary_identity(path: Path) -> CBMBinary:
    status = path.stat()
    return CBMBinary(
        path=path,
        sha256=sha256(path.read_bytes()).hexdigest(),
        size=status.st_size,
        version="codebase-memory-mcp test",
        source="test",
        _device=status.st_dev,
        _inode=status.st_ino,
        _mtime_ns=status.st_mtime_ns,
    )


def write_fake_cbm(
    path: Path,
    *,
    delay: float = 0,
    confirm_force_full: bool = True,
    confirm_incremental_controls: bool = True,
) -> None:
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import os
import sys
import time

delay = {delay!r}
confirm_force_full = {confirm_force_full!r}
confirm_incremental_controls = {confirm_incremental_controls!r}
delta_flags = [
    "--delta-closure-overflow",
    "--delta-closure-cost-percent",
    "--delta-dependent-scope",
    "--delta-new-surface",
    "--delta-reference-fanout-cap",
    "--delta-pair-outputs",
    "--delta-pair-refresh-budget",
    "--delta-pair-input-missing",
]

def payload(arguments):
    if delay:
        time.sleep(delay)
    requested = bool(arguments.get("force_full"))
    route = "full" if requested and confirm_force_full else "closure_repair"
    execution = {{
        "route": route,
        "pid": os.getpid(),
        "requested_force_full": requested,
    }}
    if marker := os.environ.get("CBM_TEST_MARKER"):
        execution["marker"] = marker
    body = {{"index_execution": execution}}
    controls = {{key.removeprefix("delta_"): value for key, value in arguments.items() if key.startswith("delta_")}}
    if controls and confirm_incremental_controls:
        body["incremental_controls"] = controls
    return {{
        "content": [{{"type": "text", "text": json.dumps(body)}}],
        "structuredContent": body,
        "isError": False,
    }}

if sys.argv[1:] == ["--version"]:
    print("codebase-memory-mcp test")
    raise SystemExit(0)

if sys.argv[1:] == ["cli", "index_repository", "--help"]:
    print("--force-full", *delta_flags)
    raise SystemExit(0)

if "--help" in sys.argv[1:]:
    raise SystemExit(2)

if len(sys.argv) > 1:
    print(json.dumps(payload(json.loads(sys.argv[-1]))), flush=True)
    raise SystemExit(0)

properties = {{"force_full": {{}}}}
properties.update({{flag[2:].replace("-", "_"): {{}} for flag in delta_flags}})
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {{"protocolVersion": "2024-11-05", "capabilities": {{}}, "serverInfo": {{}}}}
    elif request["method"] == "tools/list":
        result = {{"tools": [{{
            "name": "index_repository",
            "inputSchema": {{"properties": properties}},
        }}]}}
    elif request["method"] == "tools/call":
        result = payload(request["params"].get("arguments", {{}}))
    else:
        result = {{}}
    print(json.dumps({{"jsonrpc": "2.0", "id": request["id"], "result": result}}), flush=True)
""",
    )
    path.chmod(0o755)


def make_transport(kind, path, cache, monitor, **options):
    identity = binary_identity(path)
    if kind == "mcp":
        return MCPTransport(identity, cache, 5, monitor, {}, **options)
    return CLITransport(identity, cache, 5, monitor, {})


def test_mcp_transport_reuses_an_idle_frontend(tmp_path):
    binary = tmp_path / "fake-cbm"
    write_fake_cbm(binary)
    monitor = FakeMonitor()
    transport = make_transport("mcp", binary, tmp_path / "cache", monitor)

    first = transport.index_repository(tmp_path, tmp_path / "unused.db", "project", "full")
    second = transport.index_repository(tmp_path, tmp_path / "unused.db", "project", "full")

    assert first == second
    assert len(transport.frontend_pids) == 1
    assert monitor.children == set(transport.frontend_pids)
    transport.close()
    assert not monitor.children


def test_mcp_transport_expands_to_multiple_frontends_for_concurrency(tmp_path):
    binary = tmp_path / "fake-cbm"
    write_fake_cbm(binary, delay=0.1)
    monitor = FakeMonitor()
    transport = make_transport(
        "mcp",
        binary,
        tmp_path / "cache",
        monitor,
        max_frontends=4,
    )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: transport.call_tool(None, "echo", {}), range(4)))

    assert len({result["index_execution"]["pid"] for result in results}) == 4
    assert len(transport.frontend_pids) == 4
    transport.close()
    assert not monitor.children


def test_cli_transport_uses_one_process_per_call(tmp_path):
    binary = tmp_path / "fake-cbm"
    write_fake_cbm(binary)
    monitor = FakeMonitor()
    transport = make_transport("cli", binary, tmp_path / "cache", monitor)

    first = transport.index_repository(tmp_path, tmp_path / "unused.db", "project", "full")
    second = transport.call_tool(None, "search_graph", {"project": "project"})

    assert first["pid"] != os.getpid()
    assert second["index_execution"]["pid"] != os.getpid()
    assert not monitor.children
    assert monitor.samples == 2


def test_cli_capabilities_use_cli_without_mcp_frontend(tmp_path):
    binary = tmp_path / "fake-cbm"
    write_fake_cbm(binary)
    monitor = FakeMonitor()
    transport = make_transport("cli", binary, tmp_path / "cache", monitor)

    assert transport.capabilities() == frozenset(
        {"repository-index", "force-full-route", "granular-delta-controls"},
    )
    assert not monitor.children


@pytest.mark.parametrize("kind", ["mcp", "cli"])
def test_frontend_transport_discovers_missing_operations(tmp_path, kind):
    binary = tmp_path / "fake-cbm"
    write_fake_cbm(binary)
    transport = make_transport(kind, binary, tmp_path / "cache", FakeMonitor())
    try:
        assert transport.supports("index_repository")
        assert not transport.supports("future_sdk_operation")
    finally:
        transport.close()


def test_capability_probe_requires_every_delta_control():
    properties = {
        "force_full": {},
        "delta_closure_overflow": {},
        "delta_closure_cost_percent": {},
        "delta_dependent_scope": {},
        "delta_new_surface": {},
        "delta_reference_fanout_cap": {},
        "delta_pair_outputs": {},
        "delta_pair_refresh_budget": {},
        "delta_pair_input_missing": {},
    }
    result = {
        "tools": [
            {
                "name": "index_repository",
                "inputSchema": {"properties": properties},
            },
        ],
    }

    assert capabilities_from_tools_list(result) == frozenset(
        {"repository-index", "force-full-route", "granular-delta-controls"},
    )
    properties.pop("delta_pair_input_missing")
    assert "granular-delta-controls" not in capabilities_from_tools_list(result)


@pytest.mark.parametrize("kind", ["mcp", "cli"])
def test_frontend_transports_pass_environment_overrides(tmp_path, kind):
    binary = tmp_path / "fake-cbm"
    write_fake_cbm(binary)
    transport_type = MCPTransport if kind == "mcp" else CLITransport
    transport = transport_type(
        binary_identity(binary),
        tmp_path / "cache",
        5,
        FakeMonitor(),
        {"CBM_TEST_MARKER": "route-marker"},
    )
    try:
        result = transport.index_repository(tmp_path, tmp_path / "unused.db", "project", "full")
    finally:
        transport.close()

    assert result["marker"] == "route-marker"


@pytest.mark.parametrize("kind", ["mcp", "cli"])
def test_frontend_transports_require_force_full_confirmation(tmp_path, kind):
    binary = tmp_path / "fake-cbm"
    write_fake_cbm(binary, confirm_force_full=False)
    transport = make_transport(kind, binary, tmp_path / "cache", FakeMonitor())
    try:
        with pytest.raises(CBMTransportError, match="did not confirm"):
            transport.index_repository(
                tmp_path,
                tmp_path / "unused.db",
                "project",
                "full",
                force_full=True,
            )
    finally:
        transport.close()


@pytest.mark.parametrize("kind", ["mcp", "cli"])
def test_frontend_transports_require_delta_confirmation(tmp_path, kind):
    binary = tmp_path / "fake-cbm"
    write_fake_cbm(binary, confirm_incremental_controls=False)
    transport = make_transport(kind, binary, tmp_path / "cache", FakeMonitor())
    try:
        with pytest.raises(CBMTransportError, match="did not confirm"):
            transport.index_repository(
                tmp_path,
                tmp_path / "unused.db",
                "project",
                "full",
                incremental_controls={"closure_overflow": "full"},
            )
    finally:
        transport.close()
