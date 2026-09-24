"""Measure one release observer with one or eight files and one or four visible panes."""

# Standalone helper for the Cargo project, not a Python package.
# ruff: noqa: INP001

import argparse
import contextlib
import fcntl
import hashlib
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

from benchmark import envelope, history, percentile, proc_stats


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.endswith("}")] if path.exists() else []


def run(args, files, visible):
    root = args.output / f"{files}-files-{visible}-panes"
    root.mkdir()
    paths = [root / f"events-{index}.jsonl" for index in range(files)]
    for path in paths:
        shutil.copyfile(args.output / "history.jsonl", path)
    trace = root / "input.jsonl"
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", args.height, args.width, 0, 0))
    command = [str(args.binary), *map(str, paths), "--trace-input", str(trace)]
    process = subprocess.Popen(
        command,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "TERM": "xterm-256color"},
        start_new_session=True,
    )
    os.close(slave)
    os.set_blocking(master, False)
    selector = selectors.DefaultSelector()
    selector.register(master, selectors.EVENT_READ)
    stop = threading.Event()

    def drain():
        while not stop.is_set():
            for key, _ in selector.select(timeout=0.02):
                with contextlib.suppress(OSError, BlockingIOError):
                    os.read(key.fd, 262144)

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    startup = time.monotonic()
    streams = []
    try:
        while time.monotonic() - startup < 180:
            if process.poll() is not None:
                raise RuntimeError("TUI exited while loading history")
            os.write(master, b"t")
            time.sleep(0.15)
            rows = records(trace)
            if rows and len(rows[-1].get("files", [])) == files and min(rows[-1]["files"]) >= args.events:
                break
        else:
            raise RuntimeError("History did not load within 180 seconds")
        startup_s = time.monotonic() - startup
        if visible == 4:
            for command_bytes in [
                b"\x17v",
                b"events-1.jsonl\r",
                b"\x17s",
                b"events-2.jsonl\r",
                b"\x17h",
                b"\x17s",
                b"events-3.jsonl\r",
            ]:
                os.write(master, command_bytes)
                time.sleep(0.08)
        time.sleep(0.3)
        idle_before = proc_stats(process.pid)
        idle_start = time.monotonic()
        time.sleep(1)
        idle_cpu = 100 * (proc_stats(process.pid)["cpu_s"] - idle_before["cpu_s"]) / (time.monotonic() - idle_start)
        initial_lines = len(records(trace))
        before = proc_stats(process.pid)
        peak = before["rss_mib"]
        begin = time.monotonic()
        streams = [p.open("a") for p in paths]
        seq = args.events
        for stream in streams:
            stream.write(json.dumps(envelope("model/request", {"requestId": "live"}, seq)) + "\n")
            stream.flush()
        seq += 1
        tick = begin
        next_input = begin
        sent = []
        while time.monotonic() - begin < args.seconds:
            now = time.monotonic()
            if now >= tick:
                event = envelope("model/delta/text", {"requestId": "live", "index": 0, "text": "word "}, seq)
                for stream in streams:
                    stream.write(json.dumps(event) + "\n")
                    stream.flush()
                seq += 1
                tick += 1 / args.rate
            if now >= next_input:
                sent.append({"unix_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns()})
                os.write(master, b"t")
                peak = max(peak, proc_stats(process.pid)["rss_mib"])
                next_input += 0.1
            time.sleep(max(0, min(tick - time.monotonic(), 0.001)))
        duration = time.monotonic() - begin
        after = proc_stats(process.pid)
        time.sleep(0.3)
        rows = records(trace)[initial_lines:]
        paired = len(rows) == len(sent) and all(row["inputs"] == 1 for row in rows)
        external = (
            [(row["frame_monotonic_ns"] - stamp["monotonic_ns"]) / 1e6 for row, stamp in zip(rows, sent, strict=False)]
            if paired
            else []
        )
        (root / "samples.jsonl").write_text(
            "".join(
                json.dumps({"sent": stamp, "frame": row, "latency_ms": latency}) + "\n"
                for row, stamp, latency in zip(rows, sent, external, strict=False)
            ),
        )
        report = {
            "clock": "CLOCK_MONOTONIC",
            "command": command,
            "files": files,
            "visible_panes": visible,
            "terminal": [args.width, args.height],
            "startup_s": startup_s,
            "seconds": duration,
            "target_rate_per_file": args.rate,
            "appended_per_file": seq - args.events,
            "samples": len(rows),
            "sent": len(sent),
            "all_single_input_frames": paired,
            "input_to_frame_p95_ms": percentile(external, 0.95),
            "input_to_frame_max_ms": max(external, default=None),
            "handler_to_frame_p95_ms": percentile([row["input_to_frame_ms"] for row in rows], 0.95),
            "cpu_percent_one_core": 100 * (after["cpu_s"] - before["cpu_s"]) / duration,
            "idle_cpu_percent_one_core": idle_cpu,
            "peak_rss_mib": peak,
            "events_at_last_input": rows[-1]["files"] if rows else [],
            "visible_files": rows[-1]["visible_files"] if rows else [],
        }
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            os.write(master, b"t")
            time.sleep(0.1)
            latest = records(trace)[-1]
            if min(latest["files"]) >= seq:
                break
        report["final_events"] = latest["files"]
        report["all_files_caught_up"] = all(n == seq for n in latest["files"])
        return report
    finally:
        for stream in streams:
            stream.close()
        with contextlib.suppress(OSError):
            os.write(master, b"\x03")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        stop.set()
        thread.join(timeout=1)
        selector.close()
        os.close(master)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--events", type=int, default=100_000)
    parser.add_argument("--rate", type=int, default=200)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--width", type=int, default=120)
    parser.add_argument("--height", type=int, default=42)
    args = parser.parse_args()
    args.binary = args.binary.resolve()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    history(args.output / "history.jsonl", args.events)
    report = {
        "machine": platform.platform(),
        "cpu_count": os.cpu_count(),
        "history_events_per_file": args.events,
        "binary_sha256": hashlib.sha256(args.binary.read_bytes()).hexdigest(),
        "runs": [],
    }
    for files, visible in [(1, 1), (8, 1), (8, 4)]:
        result = run(args, files, visible)
        report["runs"].append(result)
        (args.output / "performance.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
