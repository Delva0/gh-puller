"""Standalone Linux/Python 3 task supervisor, copied into an operator-owned container.

The client never signals a saved PID. Only the live supervisor signals its own
unreaped child process group; stop requests are files in that task's directory.
"""

import codecs
import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path

TERMINAL = {"completed", "stopped", "timed_out", "interrupted", "failed"}
OUTPUT_BYTES = 65536
DISK_LIMIT = 5 * 1024 ** 3
PERSIST_LIMIT = 64 * 1024 ** 2


def proc_stat(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None


def runtime_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + proc_stat(1)[19]


@contextmanager
def locked(task):
    with (task / "lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def write_state(task, state):
    pending = task / (f"state-{os.getpid()}.tmp")
    pending.write_text(json.dumps(state, ensure_ascii=False))
    pending.replace(task / "state.json")


def snapshot(task):
    with locked(task):
        state = json.loads((task / "state.json").read_text())
        if state["status"] not in TERMINAL:
            current = proc_stat(state.get("supervisor_pid", -1))
            stale = state["runtime"] != runtime_id()
            stale |= not current or current[0] == "Z" or current[19] != state["supervisor_start"]
            if stale:
                state.update(status="interrupted", exit_code=None,
                             reason="Container restarted or task supervisor disappeared; final result unknown.")
                write_state(task, state)
        return state


def live_group(pgid):
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            stat = proc_stat(entry.name)
            if stat and stat[0] != "Z" and int(stat[2]) == pgid:
                return True
    return False


def supervise(task):
    process = None
    try:
        with locked(task):
            state = json.loads((task / "state.json").read_text())
            if state["runtime"] != runtime_id() or state["status"] != "queued":
                return
            with (task / "output.log").open("ab", buffering=0) as output:
                process = subprocess.Popen(
                    ["bash", "--noprofile", "--norc", "-o", "pipefail", "-c", state["command"]],
                    cwd=state["workdir"], stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True)
            state.update(status="running", supervisor_pid=os.getpid(), supervisor_start=proc_stat(os.getpid())[19])
            write_state(task, state)
        started, stopping, reason = time.monotonic(), None, None
        # Keep the shell unreaped until its entire group finishes, so its PID cannot
        # be reused while stop/timeout may still signal that group.
        while live_group(process.pid):
            now = time.monotonic()
            if stopping is None:
                if (task / "stop").exists():
                    reason = "stopped"
                elif state.get("timeout") is not None and now - started >= state["timeout"]:
                    reason = "timed_out"
                if reason:
                    stopping = now
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
            elif now - stopping >= 0.2:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            time.sleep(0.05)
        state.update(status=reason or "completed", exit_code=process.wait(), finished_at=time.time())
        with locked(task):
            write_state(task, state)
    except BaseException as exc:
        if process is not None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        with locked(task):
            state = json.loads((task / "state.json").read_text())
            state.update(status="failed", exit_code=None, reason=f"{type(exc).__name__}: {exc}")
            write_state(task, state)


def observe(task, cursor, wait):
    until = time.monotonic() + wait
    while True:
        state = snapshot(task)
        if state["status"] in TERMINAL or time.monotonic() >= until:
            break
        time.sleep(min(0.05, max(0, until - time.monotonic())))
    output = task / "output.log"
    size = output.stat().st_size
    if cursor > size:
        raise ValueError("cursor exceeds output size")
    with output.open("rb") as stream:
        stream.seek(cursor)
        raw = stream.read(OUTPUT_BYTES)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    text = decoder.decode(raw, final=state["status"] in TERMINAL and cursor + len(raw) >= size)
    next_cursor = cursor + len(raw) - len(decoder.getstate()[0])
    return {"task_id": task.name, "status": state["status"], "exit_code": state.get("exit_code"),
            "output": text, "cursor": next_cursor, "output_bytes": size, "truncated": next_cursor < size,
            "output_file": str(output), "stop_requested": (task / "stop").exists(),
            **({"reason": state["reason"]} if "reason" in state else {})}


def request(args):
    if args["action"].startswith("shell_"):
        return shell_request(args)
    if not re.fullmatch(r"[0-9a-f]{32}", args["task_id"]):
        raise ValueError("Invalid task ID")
    root = Path(args["task_root"])
    task = root / args["task_id"]
    action = args["action"]
    if action == "start":
        task.mkdir(mode=0o700)
        (task / "command.sh").write_text(args["command"])
        (task / "output.log").touch()
        # Publish the supervisor identity under the same lock it acquires before
        # launching bash. Even a slow start has no default execution deadline.
        with locked(task):
            state = {"task_id": task.name, "command": args["command"], "workdir": args["workdir"],
                     "timeout": args.get("timeout"), "runtime": runtime_id(), "status": "queued",
                     "exit_code": None, "created_at": time.time()}
            write_state(task, state)
            supervisor = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker", str(task)],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
            stat = proc_stat(supervisor.pid)
            state.update(supervisor_pid=supervisor.pid, supervisor_start=stat[19] if stat else None)
            write_state(task, state)
    elif not (task / "state.json").is_file():
        return {"task_id": task.name, "status": "unknown", "exit_code": None,
                "error": {"type": "UnknownTask", "message": "No task was recorded with this ID in this container."}}
    elif action == "stop":
        if snapshot(task)["status"] not in TERMINAL:
            (task / "stop").touch()
    elif action != "poll":
        raise ValueError("action must be start, poll or stop")
    return observe(task, args.get("cursor", 0), min(60, max(0, args.get("wait", 1))))


def task_path(root, task_id):
    if not re.fullmatch(r"[0-9a-f]{32}", task_id):
        raise ValueError("Invalid task ID")
    task = Path(root) / task_id
    if not (task / "state.json").is_file():
        raise ValueError(f"No task found with ID: {task_id}")
    return task


def kill_owned_tree(process):
    """Only the live supervisor signals descendants of its unreaped shell."""
    descendants = {process.pid}
    processes = {int(p.name): proc_stat(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()}
    while True:
        found = {pid for pid, stat in processes.items() if stat and int(stat[1]) in descendants}
        if found <= descendants:
            break
        descendants |= found
    for pid in sorted(descendants - {process.pid}):
        with suppress(ProcessLookupError):
            fd = os.pidfd_open(pid)
            try:
                current = proc_stat(pid)
                if current and current[19] == processes[pid][19]:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def supervise_shell(task):
    process = None
    try:
        with locked(task):
            state = json.loads((task / "state.json").read_text())
            if state["runtime"] != runtime_id() or state["status"] != "queued":
                return
            # A separate login shell initializes from the CONTAINER profile. eval
            # handles multiline/heredoc commands; only successful foreground calls
            # can publish cwd. Neither environment nor shell variables flow back.
            command = ("eval " + shlex.quote(state["command"]) + " < /dev/null && pwd -P >| "
                       + shlex.quote(str(task / "cwd")))
            with (task / "output.log").open("ab", buffering=0) as output:
                process = subprocess.Popen(["bash", "-lc", command], cwd=state["workdir"],
                                           stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                           start_new_session=True, close_fds=True)
            state.update(status="running", started_at=time.time())
            write_state(task, state)
        started = time.monotonic()
        reason = None
        while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
            with locked(task):
                state = json.loads((task / "state.json").read_text())
                if (task / "stop").exists():
                    reason = "stopped"
                elif (task / "output.log").stat().st_size > DISK_LIMIT:
                    reason = "failed"
                    state["reason"] = "Task exceeded the 5 GiB output limit."
                elif not state["background"] and time.monotonic() - started >= state["timeout"]:
                    if state["auto_background"]:
                        state.update(background=True, background_reason="timeout", backgrounded_at=time.time())
                    else:
                        reason = "timed_out"
                if reason:
                    kill_owned_tree(process)
                write_state(task, state)
            if reason:
                break
            time.sleep(0.05)
        code = process.wait()
        with locked(task):
            state = json.loads((task / "state.json").read_text())
            exit_code = 143 if reason == "timed_out" else 128 - code if code < 0 else code
            state.update(status=reason or "completed", exit_code=exit_code,
                         process_exit_code=code, finished_at=time.time())
            cwd_file = task / "cwd"
            if code == 0 and not state["background"] and cwd_file.is_file():
                state["cwd"] = cwd_file.read_text().rstrip("\n")
            write_state(task, state)
    except BaseException as exc:
        if process is not None:
            if process.returncode is None:
                kill_owned_tree(process)
            process.wait()
        with locked(task):
            state = json.loads((task / "state.json").read_text())
            state.update(status="failed", exit_code=None, reason=f"{type(exc).__name__}: {exc}")
            write_state(task, state)


def shell_observe(task, wait=0, *, foreground=False, output_limit=30000):
    until = time.monotonic() + wait
    while True:
        state = snapshot(task)
        if state["status"] in TERMINAL or (foreground and state["background"]) or time.monotonic() >= until:
            break
        time.sleep(min(0.05, max(0, until - time.monotonic())))
    output = task / "output.log"
    size = output.stat().st_size
    # The preview is bounded even for huge logs; the complete live file always
    # remains readable. A stable result copy has the same 64 MiB cap as upstream.
    with output.open("rb") as stream:
        raw = stream.read(output_limit)
    text = codecs.getincrementaldecoder("utf-8")("replace").decode(raw, final=size <= len(raw))
    truncated = size > len(raw)
    result = {**state, "output_file": str(output), "output_bytes": size,
              "output": text[:output_limit], "truncated": truncated}
    with output.open("rb") as stream:
        stream.seek(max(0, size - 1024))
        result["tail"] = stream.read(1024).decode("utf-8", errors="replace")
    if truncated and state["status"] in TERMINAL:
        saved = task / "result.txt"
        with locked(task):
            if not saved.exists():
                with output.open("rb") as source, (task / "result.pending").open("wb") as dest:
                    remaining = PERSIST_LIMIT
                    while remaining and (chunk := source.read(min(65536, remaining))):
                        dest.write(chunk)
                        remaining -= len(chunk)
                (task / "result.pending").replace(saved)
        result.update(persisted_output=str(saved), persisted_truncated=size > PERSIST_LIMIT)
        with output.open("rb") as stream:
            head = stream.read(1000).decode("utf-8", errors="replace")
            stream.seek(max(0, size - 1000))
            tail = stream.read(1000).decode("utf-8", errors="replace")
        result["preview"] = head + "\n…\n" + tail
    return result


def read_text(args):
    path = Path(args["file_path"]).expanduser()
    if not path.is_absolute():
        path = Path(args["cwd"]) / path
    offset, limit = max(1, args.get("offset", 1)), args.get("limit", 2000)
    lines, total, size, more = [], 0, 0, False
    with path.open(encoding="utf-8", errors="replace") as stream:
        while chunk := stream.readline(65536):
            total += 1
            value = chunk.rstrip("\n")
            long_line = len(value) > 2000
            value = value[:2000]
            while chunk and not chunk.endswith("\n"):
                chunk = stream.readline(65536)
                long_line |= bool(chunk)
            if offset <= total < offset + limit:
                if "\x00" in value:
                    raise ValueError("Only text files are supported; use bash for binary data.")
                rendered = f"{total:6}\t{value}" + (" [line truncated]" if long_line else "")
                size += len(rendered)
                if size > 100000:
                    raise ValueError("File exceeds the read limit. Specify a smaller offset/limit range.")
                lines.append(rendered)
            if total >= offset + limit - 1:
                more = bool(stream.read(1))
                break
    return {"file_path": str(path), "content": "\n".join(lines), "total_lines": None if more else total,
            "has_more": more,
            "offset": offset, "lines": len(lines)}


def shell_request(args):
    action, root = args["action"], Path(args["task_root"])
    if action == "shell_capabilities":
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise ValueError("The Bash contract requires Python 3.9+ with Linux pidfd support.")
        fd = os.pidfd_open(os.getpid())
        os.close(fd)
        return {"python": sys.version, "pidfd": True, "bash": True, "platform": sys.platform}
    if action == "shell_read":
        return read_text(args)
    if action == "shell_status":
        return {"tasks": [shell_observe(task_path(root, tid), output_limit=2000) for tid in args["task_ids"]]}
    if action == "shell_start":
        if not re.fullmatch(r"[0-9a-f]{32}", args["task_id"]):
            raise ValueError("Invalid task ID")
        task = root / args["task_id"]
        task.mkdir(mode=0o700)
        (task / "output.log").touch()
        (task / "command.sh").write_text(args["command"])
        with locked(task):
            state = {"task_id": task.name, "command": args["command"], "description": args["description"],
                     "workdir": args["cwd"], "timeout": args["timeout"], "background": args["background"],
                     "auto_background": args["auto_background"], "protocol": "shell-v1",
                     "runtime": runtime_id(), "status": "queued", "exit_code": None, "created_at": time.time()}
            write_state(task, state)
            supervisor = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "shell-worker", str(task)],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
            stat = proc_stat(supervisor.pid)
            state.update(supervisor_pid=supervisor.pid, supervisor_start=stat[19] if stat else None)
            write_state(task, state)
    else:
        task = task_path(root, args["task_id"])
        if action == "shell_stop":
            if snapshot(task)["status"] not in TERMINAL:
                (task / "stop").touch()
        elif action != "shell_poll":
            raise ValueError(f"Unknown shell operation: {action}")
    return shell_observe(task, args.get("wait", 0), foreground=action == "shell_start",
                         output_limit=args.get("output_limit", 30000))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        supervise(Path(sys.argv[2]))
    elif len(sys.argv) > 1 and sys.argv[1] == "shell-worker":
        supervise_shell(Path(sys.argv[2]))
    else:
        try:
            result = request(json.load(sys.stdin))
        except Exception as error:
            result = {"error": {"type": type(error).__name__, "message": str(error)}}
        print(json.dumps(result, ensure_ascii=False))
