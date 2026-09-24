"""Replay actual offline adapter test events against Rust and Python at every prefix."""

# Standalone helper for the Cargo project, not a Python package.
# ruff: noqa: INP001

import argparse
import json
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

from gh_puller.agent.events import DELTA_TYPES, EventBus, fold_state


class Capture:
    def __init__(self, directory):
        self.directory = directory
        self.events = []
        self.paths = []
        self.original = EventBus.publish

    def pytest_configure(self):
        def publish(bus, event):
            self.events.append(event)
            return self.original(bus, event)

        EventBus.publish = publish

    def pytest_runtest_setup(self, item):
        self.events = []

    def pytest_runtest_logfinish(self, nodeid, location):
        sessions = defaultdict(list)
        for event in self.events:
            sessions[event.get("session", "")].append(event)
        for index, events in enumerate(sessions.values()):
            path = self.directory / f"{Path(location[0]).stem}-{location[2]}-{index}.jsonl"
            path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))
            self.paths.append(path)

    def pytest_unconfigure(self):
        EventBus.publish = self.original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    plugin = Capture(args.output)
    repository = Path(__file__).resolve().parents[3]
    result = pytest.main([str(repository / "tests/agent/adapters"), "-q"], plugins=[plugin])
    if result:
        raise SystemExit(result)
    count = 0
    summaries = []
    for path in plugin.paths:
        events = [json.loads(line) for line in path.read_text().splitlines()]
        process = subprocess.run(
            [str(args.binary), "--prefixes", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        states = [json.loads(line) for line in process.stdout.splitlines()]
        if len(states) != len(events):
            raise AssertionError(f"{path}: prefix count mismatch")
        for index, state in enumerate(states, 1):
            expected = fold_state(events[:index])
            if state != expected:
                raise AssertionError(f"{path}:{index}: fold differs")
        compact = path.with_suffix(".compact.jsonl")
        compact.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events if e["type"] not in DELTA_TYPES),
        )
        replay = subprocess.run([str(args.binary), "--fold", str(compact)], check=True, capture_output=True, text=True)
        if json.loads(replay.stdout) != fold_state(events):
            raise AssertionError(f"{path}: compact fold differs")
        count += len(events)
        summaries.append({"file": path.name, "events": len(events)})
    report = {"sessions": len(plugin.paths), "prefixes": count, "fixtures": summaries}
    (args.output / "parity.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"sessions": len(plugin.paths), "prefixes": count}))


if __name__ == "__main__":
    main()
