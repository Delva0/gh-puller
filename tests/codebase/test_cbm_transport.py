import os
from pathlib import Path

import pytest

from gh_puller.codebase.cbm_transport import (
    CBMTransportError,
    CLITransport,
    PersistentMCPTransport,
    capabilities_from_tools_list,
    make_transport,
)


class FakeMonitor:
    def __init__(self):
        self.child_pid = None
        self.exceeded = False
        self.samples = 0

    def sample(self):
        self.samples += 1


def write_fake_cbm(
    path: Path,
    *,
    hang_on_tool: bool = False,
    confirm_force_full: bool = True,
    confirm_incremental_controls: bool = True,
) -> None:
    delay = "time.sleep(60)" if hang_on_tool else ""
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import os
import sys
import time

confirm_force_full = {confirm_force_full!r}
confirm_incremental_controls = {confirm_incremental_controls!r}

def make_payload(arguments):
    requested_force_full = bool(arguments.get("force_full"))
    route = "full" if requested_force_full and confirm_force_full else "closure_repair"
    execution = {{
        "route": route,
        "pid": os.getpid(),
        "requested_force_full": requested_force_full,
    }}
    if marker := os.environ.get("CBM_TEST_MARKER"):
        execution["marker"] = marker
    body = {{"index_execution": execution}}
    controls = {{key.removeprefix("delta_"): value for key, value in arguments.items() if key.startswith("delta_")}}
    if controls and confirm_incremental_controls:
        body["incremental_controls"] = controls
    return {{
        "content": [{{"type": "text", "text": json.dumps(body)}}],
        "isError": False,
    }}

if len(sys.argv) > 1:
    print(json.dumps(make_payload(json.loads(sys.argv[-1]))), flush=True)
    raise SystemExit(0)

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {{"protocolVersion": "2024-11-05", "capabilities": {{}}, "serverInfo": {{}}}}
    elif request["method"] == "tools/call":
        {delay}
        result = make_payload(request["params"].get("arguments", {{}}))
    else:
        result = {{}}
    print(json.dumps({{"jsonrpc": "2.0", "id": request["id"], "result": result}}), flush=True)
""",
    )
    path.chmod(0o755)


def test_persistent_mcp_reuses_one_process(tmp_path):
    binary = tmp_path / "fake-cbm"
    write_fake_cbm(binary)
    monitor = FakeMonitor()

    transport = PersistentMCPTransport(binary, tmp_path / "cache", 5, monitor)
    process_pid = transport.process.pid
    try:
        first = transport.index(tmp_path / "tree", "project", "full")
        second = transport.index(tmp_path / "tree", "project", "full")
        queried = transport.call_tool("search_graph", {"project": "project"})
        deleted, _ = transport.delete_project("project")
        assert (
            first
            == second
            == {
                "route": "closure_repair",
                "pid": process_pid,
                "requested_force_full": False,
            }
        )
        assert queried["content"]
        assert deleted
        assert monitor.child_pid == process_pid
        assert monitor.samples >= 5  # initialize, two indexes, query, and delete
    finally:
        transport.close()

    assert monitor.child_pid is None
    assert transport.process.returncode == 0


def test_cli_transport_remains_available_as_control(tmp_path):
    binary = tmp_path / "fake-cbm"
    write_fake_cbm(binary)
    monitor = FakeMonitor()
    transport = CLITransport(binary, tmp_path / "cache", 5, monitor)

    execution = transport.index(tmp_path / "tree", "project", "full")
    queried = transport.call_tool("search_graph", {"project": "project"})

    assert execution["route"] == "closure_repair"
    assert queried["content"]
    assert execution["pid"] != os.getpid()
    assert monitor.child_pid is None
    assert monitor.samples == 2


def test_persistent_mcp_timeout_terminates_process(tmp_path):
    binary = tmp_path / "fake-cbm"
    write_fake_cbm(binary, hang_on_tool=True)
    monitor = FakeMonitor()
    transport = PersistentMCPTransport(binary, tmp_path / "cache", 1, monitor)

    with pytest.raises(CBMTransportError, match="timed out"):
        transport.index(tmp_path / "tree", "project", "full")

    assert transport.process.poll() is not None
    assert monitor.child_pid is None
    transport.close()


def test_transport_factory_rejects_unknown_name(tmp_path):
    with pytest.raises(CBMTransportError, match="unknown CBM transport"):
        make_transport("unknown", tmp_path / "cbm", tmp_path / "cache", 1, FakeMonitor())


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
        {"force-full-route", "granular-delta-controls", "persistent-mcp"},
    )
    properties.pop("delta_pair_input_missing")
    assert "granular-delta-controls" not in capabilities_from_tools_list(result)


@pytest.mark.parametrize("transport_name", ["cli", "persistent-mcp"])
def test_transport_passes_environment_overrides(tmp_path, transport_name):
    binary = tmp_path / "fake-cbm"
    write_fake_cbm(binary)
    monitor = FakeMonitor()
    transport = make_transport(
        transport_name,
        binary,
        tmp_path / "cache",
        5,
        monitor,
        {"CBM_TEST_MARKER": "granular-delta"},
    )
    try:
        execution = transport.index(tmp_path / "tree", "project", "full")
    finally:
        transport.close()

    assert execution["marker"] == "granular-delta"


@pytest.mark.parametrize("transport_name", ["cli", "persistent-mcp"])
def test_transport_passes_and_requires_force_full_confirmation(tmp_path, transport_name):
    binary = tmp_path / "fake-cbm"
    write_fake_cbm(binary)
    transport = make_transport(
        transport_name,
        binary,
        tmp_path / "cache",
        5,
        FakeMonitor(),
    )
    try:
        execution = transport.index(tmp_path / "tree", "project", "full", force_full=True)
    finally:
        transport.close()

    assert execution["route"] == "full"
    assert execution["requested_force_full"] is True


@pytest.mark.parametrize("transport_name", ["cli", "persistent-mcp"])
def test_transport_rejects_cbm_without_force_full_support(tmp_path, transport_name):
    binary = tmp_path / "old-cbm"
    write_fake_cbm(binary, confirm_force_full=False)
    transport = make_transport(
        transport_name,
        binary,
        tmp_path / "cache",
        5,
        FakeMonitor(),
    )
    try:
        with pytest.raises(CBMTransportError, match="did not confirm"):
            transport.index(tmp_path / "tree", "project", "full", force_full=True)
    finally:
        transport.close()


@pytest.mark.parametrize("transport_name", ["cli", "persistent-mcp"])
def test_transport_passes_and_requires_incremental_controls_confirmation(tmp_path, transport_name):
    binary = tmp_path / "fake-cbm"
    write_fake_cbm(binary)
    transport = make_transport(
        transport_name,
        binary,
        tmp_path / "cache",
        5,
        FakeMonitor(),
    )
    controls = {
        "closure_overflow": "repair",
        "closure_cost_percent": 20,
        "dependent_scope": "symbol",
        "new_surface": "bounded",
        "reference_fanout_cap": 64,
        "pair_outputs": "lazy",
        "pair_refresh_budget": 10000,
        "pair_input_missing": "skip",
    }
    try:
        execution = transport.index(
            tmp_path / "tree",
            "project",
            "full",
            incremental_controls=controls,
        )
    finally:
        transport.close()

    assert execution["route"] == "closure_repair"


@pytest.mark.parametrize("transport_name", ["cli", "persistent-mcp"])
def test_transport_rejects_cbm_without_incremental_controls_support(tmp_path, transport_name):
    binary = tmp_path / "old-cbm"
    write_fake_cbm(binary, confirm_incremental_controls=False)
    transport = make_transport(
        transport_name,
        binary,
        tmp_path / "cache",
        5,
        FakeMonitor(),
    )
    try:
        with pytest.raises(CBMTransportError, match="did not confirm"):
            transport.index(
                tmp_path / "tree",
                "project",
                "full",
                incremental_controls={"closure_overflow": "full"},
            )
    finally:
        transport.close()
