"""Measure release PTY input-to-frame latency, CPU and RSS without paid providers."""

# Standalone helper for the Cargo project, not a Python package.
# ruff: noqa: INP001

import argparse
import contextlib
import fcntl
import json
import os
import platform
import pty
import selectors
import shutil
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path


def message(role, text):
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


def envelope(kind, data, seq):
    return {"type": kind, "data": data, "seq": seq, "session": "benchmark", "elapsedMs": seq * 5}


def history(path, size):
    with path.open("w") as target:
        target.write(json.dumps(envelope("session/start", {"label": "Agent · performance fixture"}, 0)) + "\n")
        seq = 1
        while seq < size:
            rid = str(seq)
            output = [
                {
                    "type": "reasoning",
                    "content": [{"type": "reasoning_text", "text": "Inspect the source and verify the result."}],
                },
                message(
                    "assistant",
                    "## Result\n\nThe implementation preserves context.\n\n"
                    "| Field | Value |\n|---|---|\n| state | valid |\n\n"
                    "```rust\nassert_eq!(before, after);\n```",
                ),
                {"type": "function_call", "call_id": rid, "name": "read_file", "arguments": '{"path":"src/lib.rs"}'},
            ]
            events = [
                ("turn/start", {}),
                ("context/append/user", {"items": [message("user", "Check context reconstruction and tool results.")]}),
                ("step/start", {}),
                ("model/request", {"requestId": rid}),
                (
                    "model/response",
                    {"requestId": rid, "output": output, "usage": {"input": 300, "output": 45, "cacheRead": 200}},
                ),
                ("context/append/assistant", {"items": output}),
                ("tool/start", {"callId": rid, "name": "read_file", "arguments": {"path": "src/lib.rs"}}),
                ("tool/end", {"callId": rid, "result": "activity result"}),
                (
                    "context/append/tool",
                    {"items": [{"type": "function_call_output", "call_id": rid, "output": "Verified source content."}]},
                ),
                ("step/end", {}),
                ("turn/end", {}),
            ]
            for kind, data in events:
                if seq >= size:
                    break
                target.write(json.dumps(envelope(kind, data, seq)) + "\n")
                seq += 1


def proc_stats(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().split()
    ticks = os.sysconf("SC_CLK_TCK")
    return {
        "cpu_s": (int(fields[13]) + int(fields[14])) / ticks,
        "rss_mib": int(fields[23]) * os.sysconf("SC_PAGE_SIZE") / 1024**2,
    }


def percentile(samples, quantile):
    return sorted(samples)[min(len(samples) - 1, round((len(samples) - 1) * quantile))] if samples else None


def run(args, windows):
    root = args.output / str(windows)
    root.mkdir()
    processes = []
    masters = []
    traces = []
    files = []
    stop = threading.Event()
    selector = selectors.DefaultSelector()
    for index in range(windows):
        path = root / f"events-{index}.jsonl"
        shutil.copyfile(args.output / "history.jsonl", path)
        files.append(path)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 42, 120, 0, 0))
        trace = root / f"input-{index}.jsonl"
        process = subprocess.Popen(
            [str(args.binary), str(path), "--trace-input", str(trace)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={**os.environ, "TERM": "xterm-256color"},
            start_new_session=True,
        )
        os.close(slave)
        os.set_blocking(master, False)
        processes.append(process)
        masters.append(master)
        traces.append(trace)
        selector.register(master, selectors.EVENT_READ)

    def drain():
        while not stop.is_set():
            for key, _ in selector.select(timeout=0.02):
                with contextlib.suppress(OSError, BlockingIOError):
                    os.read(key.fd, 262144)

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    sent = [[] for _ in masters]
    startup = time.monotonic()
    loaded = False
    try:
        while time.monotonic() - startup < 180:
            if any(p.poll() is not None for p in processes):
                raise RuntimeError("TUI exited before loading history")
            for master in masters:
                os.write(master, b"t")
            time.sleep(0.15)
            latest = [
                [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else [] for path in traces
            ]
            if all(records and records[-1]["events"] >= args.events for records in latest):
                loaded = True
                break
        if not loaded:
            raise RuntimeError("history not loaded within 180 seconds")
        startup_s = time.monotonic() - startup
        idle_before = [proc_stats(p.pid) for p in processes]
        idle_start = time.monotonic()
        time.sleep(1)
        idle_duration = time.monotonic() - idle_start
        idle_cpu = [
            100 * (proc_stats(p.pid)["cpu_s"] - baseline["cpu_s"]) / idle_duration
            for p, baseline in zip(processes, idle_before, strict=True)
        ]
        initial_lines = [len(path.read_text().splitlines()) for path in traces]
        before = [proc_stats(p.pid) for p in processes]
        peak = [s["rss_mib"] for s in before]
        begin = time.monotonic()
        tick = begin
        streams = [p.open("a") for p in files]
        seq = args.events
        for stream in streams:
            stream.write(json.dumps(envelope("model/request", {"requestId": "live"}, seq)) + "\n")
            stream.flush()
        seq += 1
        next_input = begin
        while time.monotonic() - begin < args.seconds:
            now = time.monotonic()
            if now >= tick:
                event = envelope("model/delta/text", {"requestId": "live", "index": 0, "text": "word "}, seq)
                for stream in streams:
                    stream.write(json.dumps(event) + "\n")
                    stream.flush()
                tick += 1 / args.rate
                seq += 1
            if now >= next_input:
                for index, master in enumerate(masters):
                    sent[index].append(time.time_ns())
                    os.write(master, b"t")
                    peak[index] = max(peak[index], proc_stats(processes[index].pid)["rss_mib"])
                next_input += 0.1
            time.sleep(max(0, min(tick - time.monotonic(), 0.001)))
        duration = time.monotonic() - begin
        after = [proc_stats(p.pid) for p in processes]
        time.sleep(0.2)
        reports = []
        for index, trace in enumerate(traces):
            rows = [json.loads(line) for line in trace.read_text().splitlines()][initial_lines[index] :]
            external = [(row["frame_unix_ns"] - stamp) / 1e6 for row, stamp in zip(rows, sent[index], strict=False)]
            internal = [row["input_to_frame_ms"] for row in rows]
            reports.append(
                {
                    "samples": len(rows),
                    "sent": len(sent[index]),
                    "input_to_frame_p95_ms": percentile(external, 0.95),
                    "input_to_frame_max_ms": max(external, default=0),
                    "handler_to_frame_p95_ms": percentile(internal, 0.95),
                    "cpu_percent_one_core": 100 * (after[index]["cpu_s"] - before[index]["cpu_s"]) / duration,
                    "idle_cpu_percent_one_core": idle_cpu[index],
                    "peak_rss_mib": peak[index],
                    "events_consumed": rows[-1]["events"] if rows else 0,
                    "all_single_input_frames": all(row["inputs"] == 1 for row in rows),
                },
            )
        for stream in streams:
            stream.close()
        return {
            "windows": windows,
            "startup_s": startup_s,
            "seconds": duration,
            "target_rate_per_window": args.rate,
            "appended_per_window": seq - args.events,
            "events_per_file": seq,
            "processes": reports,
        }
    finally:
        for master in masters:
            with contextlib.suppress(OSError):
                os.write(master, b"q")
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        stop.set()
        thread.join(timeout=1)
        selector.close()
        for master in masters:
            os.close(master)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--events", type=int, default=100_000)
    parser.add_argument("--rate", type=int, default=200)
    parser.add_argument("--seconds", type=float, default=10)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    history(args.output / "history.jsonl", args.events)
    report = {
        "machine": platform.platform(),
        "cpu_count": os.cpu_count(),
        "history_events": args.events,
        "binary": str(args.binary),
        "runs": [],
    }
    for windows in (1, 4):
        result = run(args, windows)
        report["runs"].append(result)
        (args.output / "performance.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
