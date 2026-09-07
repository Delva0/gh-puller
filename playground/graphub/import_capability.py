"""Probe native CBM include extraction with controlled full-index fixtures.

The fixtures test indexing separately from path traversal and archive projection.
Expected targets describe static include possibilities, not active preprocessor
branches. Results expose missing relationships without repairing source graphs.
"""  # noqa: INP001 - Standalone native capability reproduction.

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from gh_puller.codebase import resolve_cbm_binary
from gh_puller.codebase.cbm_transport import PersistentMCPTransport, index_execution_from_envelope

from .import_paths import query_paths

DISPATCHERS = {
    "top_level": '#include "a.hpp"\n#include "b.hpp"\n',
    "include_guard": '#ifndef DISPATCH_HPP\n#define DISPATCH_HPP\n#include "a.hpp"\n#include "b.hpp"\n#endif\n',
    "conditional": '#if defined(PLATFORM_A)\n#include "a.hpp"\n#else\n#include "b.hpp"\n#endif\n',
}


def probe(binary, dispatcher):
    sources = {"entry.cpp": '#include "dispatch.hpp"\nint main() { return 0; }\n',
               "dispatch.hpp": dispatcher, "a.hpp": "struct Alpha {};\n", "b.hpp": "struct Beta {};\n"}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="graphub-include-capability-") as scratch:
        root = Path(scratch)
        tree, cache = root / "source", root / "cache"
        tree.mkdir()
        cache.mkdir(mode=0o700)
        for name, content in sources.items():
            (tree / name).write_text(content)
        monitor = SimpleNamespace(child_pid=None, exceeded=False, sample=lambda: None)
        transport = PersistentMCPTransport(binary.path, cache, 60, monitor,
                                           extra_environment={"CBM_RUNTIME_DIR": str(cache)})
        try:
            indexed = transport.call_tool("index_repository", {"repo_path": str(tree), "name": "graphub-probe",
                                                               "mode": "full", "force_full": True,
                                                               "persistence": False})
            execution = index_execution_from_envelope(indexed)
            if execution is None or execution["route"] != "full":
                raise ValueError("The fixture was not fully reindexed")
            queries = {depth: query_paths(transport, ["entry.cpp"], depth) for depth in (1, 2)}
        finally:
            transport.close()
    observed = {path["nodes"][-1]["file"] for path in queries[2]["paths"]}
    # Temporary storage and log paths are telemetry, not fixture identity.
    normalized_index = json.loads(json.dumps(indexed).replace(str(root), "<fixture>"))
    return {"sources": sources, "index_response": normalized_index, "queries": queries,
            "expected_two_hop_targets": ["a.hpp", "b.hpp"], "observed_two_hop_targets": sorted(observed),
            "missing": sorted({"a.hpp", "b.hpp"} - observed), "unexpected": sorted(observed - {"a.hpp", "b.hpp"}),
            "seconds": time.monotonic() - started}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    binary = resolve_cbm_binary()
    cases = {}
    for name, dispatcher in DISPATCHERS.items():
        cases[name] = probe(binary, dispatcher)
        print(json.dumps({"fixture": name, "missing": cases[name]["missing"],
                          "unexpected": cases[name]["unexpected"], "seconds": cases[name]["seconds"]}), flush=True)
    result = {"cbm_sha256": binary.sha256, "fixtures": cases}
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if args.require_complete and any(case["missing"] or case["unexpected"] for case in cases.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
