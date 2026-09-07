"""Validate typed import paths in the actual CBM engine using a small adversarial graph."""
# ruff: noqa: INP001, S101 - Standalone research tests use pytest assertions.

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from gh_puller.codebase import resolve_cbm_binary
from gh_puller.codebase.cbm_transport import PersistentMCPTransport
from gh_puller.codebase.store import GraphRows

from .import_paths import bind, query_paths
from .stack_search import restore


@pytest.fixture(scope="module")
def native_graph():
    files = {"entry": "src/unit.cc", "dispatch": "include/dispatch.hpp", "a": "include/a.hpp",
             "b": "include/b.hpp", "end": "include/end.hpp", "inbound": "tools/consumer.cc", "call": "ignore/call.hpp"}
    nodes = {name: {"label": "File", "name": Path(file).name, "file_path": file,
                    "start_line": 0, "end_line": 0, "properties": {}} for name, file in files.items()}
    relations = [("entry", "dispatch", "IMPORTS", "left"), ("entry", "dispatch", "IMPORTS", "right"),
                 ("dispatch", "a", "IMPORTS", ""), ("dispatch", "b", "IMPORTS", ""),
                 ("a", "end", "IMPORTS", ""), ("inbound", "entry", "IMPORTS", ""),
                 ("entry", "call", "CALLS", "")]
    edges = {key: {"properties": {"local_name": key[3]} if key[2] == "IMPORTS" else {}} for key in relations}
    with tempfile.TemporaryDirectory(prefix="graphub-import-test-") as scratch:
        root = Path(scratch)
        restore(GraphRows(nodes, edges), root, "graphub-probe")
        monitor = SimpleNamespace(child_pid=None, exceeded=False, sample=lambda: None)
        transport = PersistentMCPTransport(
            resolve_cbm_binary().path, root, 60, monitor, extra_environment={"CBM_RUNTIME_DIR": str(root)},
        )
        try:
            yield transport, set(files.values())
        finally:
            transport.close()


def test_native_one_hop_keeps_parallel_aliases_and_excludes_other_edge_types(native_graph):
    transport, _ = native_graph
    result = query_paths(transport, ["src/unit.cc"], 1)
    assert result["truncated"] is False
    assert len(result["paths"]) == 2
    assert {path["nodes"][-1]["file"] for path in result["paths"]} == {"include/dispatch.hpp"}
    assert {path["edges"][0]["local_name"] for path in result["paths"]} == {"left", "right"}


def test_native_two_hops_preserve_the_intermediate_file_and_stop_at_the_bound(native_graph):
    transport, _ = native_graph
    result = query_paths(transport, ["src/unit.cc"], 2)
    assert len(result["paths"]) == 4
    assert {path["nodes"][-1]["file"] for path in result["paths"]} == {"include/a.hpp", "include/b.hpp"}
    assert all(path["nodes"][1]["file"] == "include/dispatch.hpp" for path in result["paths"])
    assert all(len(path["edges"]) == 2 for path in result["paths"])


def test_native_limit_reports_truncation_with_a_sentinel(native_graph):
    transport, _ = native_graph
    result = query_paths(transport, ["src/unit.cc"], 2, limit=1)
    assert result["truncated"] is True and len(result["paths"]) == 1
    assert result["call"]["response"]["total"] == 2


def test_empty_coordinates_do_not_call_cbm():
    assert query_paths(None, [], 1) == {"paths": [], "truncated": False, "call": None}


@pytest.mark.parametrize("depth", [0, 3])
def test_unregistered_depths_fail_before_native_execution(depth):
    with pytest.raises(ValueError, match="one or two"):
        query_paths(None, ["src/unit.cc"], depth)


def test_binding_uses_cumulative_depth_controls_and_retains_alternative_witnesses(native_graph):
    transport, files = native_graph
    queries = {depth: query_paths(transport, ["src/unit.cc"], depth) for depth in (1, 2)}
    result = bind([{"file": "src/unit.cc", "line": 4}], queries, files)
    assert len(result) == 4
    entry = next(item for item in result if item["node"]["file"] == "src/unit.cc")
    assert entry["roles"] == ["depth_0", "depth_1", "depth_2"]
    leaf = next(item for item in result if item["node"]["file"] == "include/a.hpp")
    assert leaf["roles"] == ["depth_2"] and len(leaf["additional_anchors"]) == 1
