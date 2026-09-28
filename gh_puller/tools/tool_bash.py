"""Host bash execution and persistent Docker bash tasks with separate tool contracts."""

import asyncio
import copy
import json
import os
import re
import shutil
import signal
import time
import uuid
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from xml.sax.saxutils import escape

from .registry import ToolInputError, ToolProvider, tool, tool_definitions
from .sandbox_worker import TERMINAL
from .shell_contract import BASH_SCHEMA as SHELL_BASH_SCHEMA
from .shell_contract import (
    OUTPUT_SCHEMA,
    STOP_SCHEMA,
    auto_background_allowed,
    command_result,
    effective_timeout,
    normalize_arguments,
    shell_settings,
)
from .storage import ToolStorage
from .tool_offload import ToolOutput
from .tool_read_file import media_output

CONTRACT = "openclaude-bash-v3"
PREVIOUS_CONTRACT = "openclaude-bash-v2"
LEGACY_CONTRACT = "openclaude-bash-v1"

BASH_DESCRIPTION = (
    "Execute a bash command. Each call starts a new non-interactive shell; files in the working directory persist "
    "across calls. Independent calls in the same response run concurrently. Timeout or cancellation terminates "
    "the command and its child processes. Returns exit_code, stdout and stderr; long output is explicitly marked "
    "as truncated, with paths to the complete output files relative to the working directory."
)
BASH_SCHEMA = {"type": "object", "properties": {
        "command": {"type": "string", "minLength": 1},
        "timeout": {"type": "number", "minimum": 0.1, "maximum": 120, "default": 60,
                    "description": "Timeout in seconds, including all child processes."},
    }, "required": ["command"], "additionalProperties": False}


SANDBOX_DESCRIPTION = (
    "Run commands in the connected persistent Linux container. action defaults to start, taking command and optional "
    "timeout in seconds (no execution time limit by default); each command starts an independent bash shell in the "
    "same working directory. wait defaults to 1 second; a still-running command returns task_id. "
    "action=poll takes task_id, cursor (byte offset, initially 0), and wait (0–60 seconds), returning new output, "
    "the next cursor, status and exit_code. action=stop stops only that task's process group. "
    "Tasks and files survive client exit and conversation clearing. Cancelling a wait does not stop a task. "
    "When truncated, output_file is the full container path; use another bash command to read selected portions. "
    "After a container restart, unfinished tasks are interrupted with unknown exit_code."
)
SANDBOX_SCHEMA = {"type": "object", "properties": {
    "action": {"type": "string", "enum": ["start", "poll", "stop"], "default": "start"},
    "command": {"type": "string", "minLength": 1},
    "task_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
    "cursor": {"type": "integer", "minimum": 0, "default": 0},
    "wait": {"type": "number", "minimum": 0, "maximum": 60, "default": 1},
    "timeout": {"type": "number", "exclusiveMinimum": 0},
}, "additionalProperties": False,
    "allOf": [{"if": {"properties": {"action": {"const": "start"}}},
               "then": {"required": ["command"], "not": {"required": ["task_id"]}},
               "else": {"required": ["task_id"], "not": {"anyOf": [
                   {"required": ["command"]}, {"required": ["timeout"]}]}}}]}

# Probe and install through stdin: no host paths, environment variables or credentials
# are mounted/copied into the container. Only this versioned helper source is sent.
SANDBOX_INSTALL = """
import hashlib, json, os, pathlib, shutil, sys, tempfile
args = json.load(sys.stdin)
assert sys.platform == "linux", "A Linux container is required"
assert shutil.which("bash"), "bash is required in the container"
workdir = pathlib.Path(args["workdir"]).resolve(strict=True)
assert workdir.is_dir(), "workdir must be a directory"
assert os.access(str(workdir), os.R_OK | os.W_OK | os.X_OK), "workdir must be readable and writable"
root = pathlib.Path("/tmp") / ("graphub-code-" + str(os.getuid()))
root.mkdir(mode=0o700, exist_ok=True)
source = args["source"].encode()
helper = root / ("worker-" + hashlib.sha256(source).hexdigest() + ".py")
with tempfile.NamedTemporaryFile(dir=root, delete=False) as output:
    pending = pathlib.Path(output.name)
    output.write(source)
try:
    try:
        os.link(str(pending), str(helper))
    except FileExistsError:
        assert helper.read_bytes() == source, "Installed helper does not match its source hash"
finally:
    pending.unlink()
read_source = args["read_source"].encode()
read_helper = root / ("read-" + hashlib.sha256(read_source).hexdigest() + ".py")
with tempfile.NamedTemporaryFile(dir=root, delete=False) as output:
    pending = pathlib.Path(output.name)
    output.write(read_source)
try:
    try:
        os.link(str(pending), str(read_helper))
    except FileExistsError:
        assert read_helper.read_bytes() == read_source, "Installed read helper does not match its source hash"
finally:
    pending.unlink()
print(json.dumps({"task_root": str(root), "helper": str(helper), "read_helper": str(read_helper),
                  "real_workdir": str(workdir)}))
"""


# OpenClaude 5cd11336caeaf2023ca1e02e5975c761cfc585fe, src/tools/BashTool/prompt.ts.
# Tool-routing hints, commit/PR workflows and common-operation examples are omitted.
SHELL_BASH_DESCRIPTION = (
    'Executes a given bash command and returns its output.\n'
    '\n'
    'The working directory persists between commands, but shell state does not. The shell environment is '
    "initialized from the user's profile (bash or zsh).\n"
    '\n'
    '# Instructions\n'
    ' - If your command will create new directories or files, first use this tool to run `ls` to verify '
    'the parent directory exists and is the correct location.\n'
    ' - Always quote file paths that contain spaces with double quotes in your command (e.g., cd "path '
    'with spaces/file.txt")\n'
    ' - Try to maintain your current working directory throughout the session by using absolute paths and '
    'avoiding usage of `cd`. You may use `cd` if the User explicitly requests it.\n'
    ' - You may specify an optional timeout in milliseconds (up to 600000ms / 10 minutes). By default, '
    'your command will timeout after 120000ms (2 minutes).\n'
    ' - You can use the `run_in_background` parameter to run the command in the background. Only use this '
    "if you don't need the result immediately and are OK being notified when the command completes later. "
    "You do not need to check the output right away - you'll be notified when it finishes. You do not "
    "need to use '&' at the end of the command when using this parameter.\n"
    ' - When issuing multiple commands:\n'
    '  - If the commands are independent and can run in parallel, make multiple bash tool calls in a '
    'single message. Example: if you need to run "git status" and "git diff", send a single message with '
    'two bash tool calls in parallel.\n'
    "  - If the commands depend on each other and must run sequentially, use a single bash call with '&&' "
    'to chain them together.\n'
    "  - Use ';' only when you need to run commands sequentially but don't care if earlier commands "
    'fail.\n'
    '  - DO NOT use newlines to separate commands (newlines are ok in quoted strings).\n'
    ' - For git commands:\n'
    '  - Prefer to create a new commit rather than amending an existing commit.\n'
    '  - Before running destructive operations (e.g., git reset --hard, git push --force, git checkout '
    '--), consider whether there is a safer alternative that achieves the same goal. Only use destructive '
    'operations when they are truly the best approach.\n'
    '  - Never skip hooks (--no-verify) or bypass signing (--no-gpg-sign, -c commit.gpgsign=false) unless '
    'the user has explicitly asked for it. If a hook fails, investigate and fix the underlying issue.\n'
    ' - Avoid unnecessary `sleep` commands:\n'
    '  - Do not sleep between commands that can run immediately — just run them.\n'
    '  - If your command is long running and you would like to be notified when it finishes — use '
    '`run_in_background`. No sleep needed.\n'
    '  - Do not retry failing commands in a sleep loop — diagnose the root cause.\n'
    '  - If waiting for a background task you started with `run_in_background`, you will be notified when '
    'it completes — do not poll.\n'
    '  - If you must poll an external process, use a check command (e.g. `gh run view`) rather than '
    'sleeping first.\n'
    '  - If you must sleep, keep the duration short (1-5 seconds) to avoid blocking the user.\n'
)

BACKGROUND_NOTE = (
    "You can use the `run_in_background` parameter to run the command in the background. Only use this if "
    "you don't need the result immediately and are OK being notified when the command completes later. "
    "You do not need to check the output right away - you'll be notified when it finishes. You do not "
    "need to use '&' at the end of the command when using this parameter."
)


def bash_prompt(settings):
    prompt = SHELL_BASH_DESCRIPTION
    if not settings["background_enabled"]:
        prompt = prompt.replace(" - " + BACKGROUND_NOTE + "\n", "")
    maximum, default = settings["max_timeout_ms"], settings["default_timeout_ms"]
    return prompt.replace("600000ms / 10 minutes", f"{maximum}ms / {maximum / 60000:g} minutes").replace(
        "120000ms (2 minutes)", f"{default}ms ({default / 60000:g} minutes)")


class BashTool(ToolProvider):
    """Execute the supplied command without rewriting it or caching its results."""

    def __init__(self, storage: ToolStorage, *, token: str, concurrency: int = 8):
        if not shutil.which("gh"):
            raise FileNotFoundError("GitHub CLI (gh) is required for the bash backend")
        self.storage, self.token = storage, token
        self.limit = asyncio.Semaphore(concurrency)
        self.cwd = storage.root / "work/initial"
        self.cwd.mkdir(parents=True)
        self.env = {key: value for key, value in os.environ.items() if key in {
            "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
            "https_proxy", "http_proxy", "all_proxy", "no_proxy",
        }}
        self.env.update(GH_TOKEN=token, GH_CONFIG_DIR=str(storage.root / "work/gh-config"), GH_PROMPT_DISABLED="1",
                        GH_PAGER="cat", PAGER="cat", NO_COLOR="1", GH_NO_UPDATE_NOTIFIER="1",
                        GH_NO_EXTENSION_UPDATE_NOTIFIER="1", GH_TELEMETRY="false")
        self.unavailable = None

    def clear_context(self) -> None:
        """Start a new scratch directory when the caller explicitly clears context."""
        self.cwd = self.storage.root / "work" / self.storage.allocate("work")
        self.cwd.mkdir()

    @tool(description=BASH_DESCRIPTION, parameters=BASH_SCHEMA,
          returns={"type": "object", "properties": {
              "exit_code": {"type": ["integer", "null"]}, "stdout": {"type": "string"},
              "stderr": {"type": "string"}, "error": {"type": "object"},
          }, "required": ["exit_code", "stdout", "stderr"]})
    async def bash(self, call_id: str, command: str, timeout: float = 60) -> dict:  # noqa: ASYNC109
        operation = self.storage.allocate("bash")
        self.storage.event("io/queued", operation=operation, call_id=call_id, kind_name="bash",
                       arguments={"command": command, "timeout": timeout})
        self.storage.record(f"{operation}.command.sh", command)
        process = None
        readers = []
        outputs = {"stdout": bytearray(), "stderr": bytearray()}
        error = None

        async def collect(stream, output):
            while chunk := await stream.read(65536):
                output.extend(chunk)

        async def stop():
            if process is None:
                return
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

        try:
            async with self.limit:
                self.storage.event("io/start", operation=operation, call_id=call_id)
                process = await asyncio.create_subprocess_exec(
                    "bash", "--noprofile", "--norc", "-o", "pipefail", "-c", command,
                    cwd=self.cwd, env=self.env, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
                )
                readers = [asyncio.create_task(collect(getattr(process, name), output))
                           for name, output in outputs.items()]
                async with asyncio.timeout(timeout):
                    await process.wait()
                    await asyncio.shield(asyncio.gather(*readers))
        except BaseException as exc:
            error = {"type": type(exc).__name__, "message": str(exc) or type(exc).__name__}
            if not isinstance(exc, Exception):
                raise
        finally:
            # Kill the whole group on timeout/cancellation, including background gh jobs.
            await stop()
            if readers:
                await asyncio.gather(*readers)
            result = {"exit_code": process.returncode if process else None}
            for name, body in outputs.items():
                raw = bytes(body).replace(self.token.encode(), b"[REDACTED]") if self.token else bytes(body)
                content = raw.decode("utf-8", errors="replace")
                path = self.storage.write(f"{operation}.{name}.txt", raw)
                result.update({name: content[:40000], f"{name}_truncated": len(content) > 40000,
                               f"{name}_file": (self.storage.root / path).relative_to(
                                   self.cwd, walk_up=True).as_posix()})
            if error:
                result["error"] = error
            elif result["exit_code"] != 0:
                result["error"] = {"type": "CommandFailed", "message": f"Exit code {result['exit_code']}"}
            self.storage.record(f"{operation}.result.json", result)
            self.storage.event("io/end", operation=operation, call_id=call_id,
                           status="failed" if "error" in result else "completed")
        return result


class DockerSandbox:
    """Pin the effective Docker endpoint and full container ID; never manage its lifecycle."""

    def __init__(self, container, workdir, *, identity=None):
        self.container, self.workdir = container, workdir
        self.expected = identity
        self.env = dict(os.environ)
        self.identity = None
        self.prefix = []

    async def docker(self, *args, body=None, wait=30):
        process = await asyncio.create_subprocess_exec(
            "docker", *self.prefix, *args, env=self.env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            async with asyncio.timeout(wait):
                output, error = await process.communicate(body)
        except BaseException:
            # Only the local Docker client is reaped. A launched container task is
            # owned by its detached supervisor and is never stopped here.
            with suppress(ProcessLookupError):
                process.kill()
            await process.communicate()
            raise
        if process.returncode:
            raise RuntimeError(error.decode(errors="replace").strip() or "Docker command failed")
        return output

    async def context_endpoint(self, context):
        data = json.loads(await self.docker("context", "inspect", context))[0]
        endpoint = data["Endpoints"]["docker"]
        return {"endpoint": endpoint["Host"], "skip_tls_verify": endpoint.get("SkipTLSVerify", False)}

    async def connect(self):
        if self.expected:
            connection = self.expected["docker"]
        else:
            context = (await self.docker("context", "show")).decode().strip()
            host = self.env.get("DOCKER_HOST") if not self.env.get("DOCKER_CONTEXT") else None
            connection = ({"context": context, "mode": "host", "endpoint": host,
                           "tls_verify": self.env.get("DOCKER_TLS_VERIFY", ""),
                           "tls": self.env.get("DOCKER_TLS", ""),
                           "cert_path": self.env.get("DOCKER_CERT_PATH", "")}
                          if host else {"context": context, "mode": "context",
                                        **await self.context_endpoint(context)})
        self.env.pop("DOCKER_CONTEXT", None)
        self.env.pop("DOCKER_HOST", None)
        if connection["mode"] == "host":
            self.prefix = ["--host", connection["endpoint"]]
            for name, key in (("DOCKER_TLS_VERIFY", "tls_verify"), ("DOCKER_TLS", "tls"),
                              ("DOCKER_CERT_PATH", "cert_path")):
                if connection.get(key):
                    self.env[name] = connection[key]
                else:
                    self.env.pop(name, None)
        else:
            self.prefix = ["--context", connection["context"]]
        self.identity = {"docker": connection}
        inspected = await self.inspect(self.container)
        if self.expected and inspected["Id"] != self.expected["container_id"]:
            raise ValueError("Saved container is missing or its name now refers to a replacement container")
        installed = json.loads(await self.docker(
            "exec", "-i", inspected["Id"], "python3", "-c", SANDBOX_INSTALL,
            body=json.dumps({"workdir": self.workdir,
                             "source": Path(__file__).with_name("sandbox_worker.py").read_text(),
                             "read_source": Path(__file__).with_name("read_file_worker.py").read_text()}).encode()))
        self.identity.update(container=self.container, container_id=inspected["Id"], image_id=inspected["Image"],
                             image=inspected["Config"]["Image"], workdir=self.workdir, **installed)
        if self.expected:
            for key in ("container_id", "image_id", "workdir", "real_workdir", "task_root"):
                if self.identity[key] != self.expected[key]:
                    raise ValueError(f"Saved sandbox {key} has changed")
        return self

    async def inspect(self, target):
        connection = self.identity["docker"]
        if connection["mode"] == "context":
            actual = await self.context_endpoint(connection["context"])
            if any(actual[key] != connection[key] for key in actual):
                raise ValueError("Docker context endpoint has changed; refusing to connect to a different daemon")
        inspected = json.loads(await self.docker("inspect", "--type", "container", target))[0]
        if not inspected["State"]["Running"]:
            raise ValueError("The selected container is not running; start it outside the agent")
        return inspected

    async def request(self, args):
        await self.inspect(self.identity["container_id"])
        body = {**args, "task_root": self.identity["task_root"], "workdir": self.identity["real_workdir"]}
        helper = "read_helper" if args["action"] in {"read_file", "shell_image"} else "helper"
        return json.loads(await self.docker(
            "exec", "-i", self.identity["container_id"], "python3", self.identity[helper],
            body=json.dumps(body, allow_nan=False).encode(), wait=args.get("wait", 1) + 30))


class SandboxBashTool(ToolProvider):
    def __init__(self, storage, sandbox, *, concurrency=8):
        self.storage, self.sandbox = storage, sandbox
        self.limit = asyncio.Semaphore(concurrency)
        self.observed, self.calls = {}, {}

    def clear_context(self):
        """Conversation clearing never changes the sandbox or its tasks."""

    def cancelled_output(self, call_id):
        if call_id not in self.calls:
            return {"status": "not_started", "exit_code": None,
                    "error": {"type": "CancelledError", "message": "Cancelled before a task was allocated."}}
        return {"task_id": self.calls.get(call_id), "status": "unknown", "exit_code": None,
                "error": {"type": "CancelledError", "message": "Wait cancelled; poll task_id to observe its state."}}

    @tool(description=SANDBOX_DESCRIPTION, parameters=SANDBOX_SCHEMA)
    async def bash(self, call_id, action="start", command=None, task_id=None, cursor=0, wait=1, timeout=None):  # noqa: ASYNC109
        task_id = uuid.uuid4().hex if action == "start" else task_id
        self.calls[call_id] = task_id
        args = {"action": action, "task_id": task_id, "cursor": cursor, "wait": wait}
        if action == "start":
            args.update(command=command, timeout=timeout)
        # Reject non-standard JSON floats before starting a subprocess.
        try:
            json.dumps(args, allow_nan=False)
        except ValueError as exc:
            raise ToolInputError("wait and timeout must be finite") from exc
        operation = self.storage.allocate("sandbox-bash")
        self.storage.event("io/queued", operation=operation, call_id=call_id, kind_name="sandbox-bash", arguments=args)
        self.storage.event("sandbox/task_request", call_id=call_id, task_id=task_id, action=action)
        self.storage.record(f"{operation}.request.json", args)
        try:
            async with self.limit:
                self.storage.event("io/start", operation=operation, call_id=call_id)
                result = {"task_id": task_id, "status": "unknown", "exit_code": None,
                          **await self.sandbox.request(args)}
        except BaseException as exc:
            result = {"task_id": task_id, "status": "unknown", "exit_code": None,
                      "error": {"type": type(exc).__name__, "message": str(exc) or "Wait interrupted; poll task_id."}}
            if not isinstance(exc, Exception):
                raise
        finally:
            # Store only what this client actually observed; never invent the final
            # result of a task that continues after the client exits.
            self.observed[task_id] = result
            self.storage.record(f"{operation}.result.json", result)
            self.storage.event("sandbox/task_observed", call_id=call_id, result=result)
            self.storage.event("io/end", operation=operation, call_id=call_id,
                           status="failed" if "error" in result else "completed")
        return result


def task_status(result):
    if result["status"] in {"queued", "running"}:
        return "running"
    if result["status"] == "stopped":
        return "killed"
    return "completed" if result["status"] == "completed" and result["exit_code"] == 0 else "failed"


class DockerBashTools(ToolProvider):
    def __init__(self, storage, sandbox, *, concurrency=8, settings=None, legacy_names=False):
        self.storage, self.sandbox = storage, sandbox
        self.legacy_names = legacy_names
        self.settings = settings or shell_settings()
        self.limit = asyncio.Semaphore(concurrency)
        self.cwd = sandbox.identity["real_workdir"]
        self.observed, self.calls, self.background = {}, {}, {}
        self.ready = asyncio.Event()
        self.refresh_lock = asyncio.Lock()
        self.watcher = None
        self.transport_error = None
        self.unresolved = {}
        spec = self.tool_specs[0]
        schema = copy.deepcopy(spec.parameters)
        schema["properties"]["timeout"]["description"] = (
            f"Optional timeout in milliseconds (max {self.settings['max_timeout_ms']})")
        if not self.settings["background_enabled"]:
            schema["properties"].pop("run_in_background")
        self.tool_specs = (replace(spec, description=bash_prompt(self.settings), parameters=schema),
                           *self.tool_specs[1:])

        if legacy_names:
            self.tool_specs = tuple(
                replace(spec, description=spec.description.replace("read_file", "Read"))
                for spec in self.tool_specs
            )

    def normalize_arguments(self, name, args):
        return normalize_arguments(name, args)

    def clear_context(self):
        """Clearing the conversation preserves the container, cwd and owned tasks."""
        for item in self.background.values():
            if item["notified"]:
                item["acknowledged"] = True
        self.checkpoint()

    def save(self):
        return {"cwd": self.cwd, "tasks": self.observed, "calls": self.calls, "background": self.background}

    def restore(self, state, returned_calls=None):
        self.cwd = state.get("cwd", self.cwd)
        self.observed.update(state.get("tasks", {}))
        self.calls.update(state.get("calls", {}))
        self.background.update(state.get("background", {}))
        if returned_calls is None:
            returned_calls = {call for call, tid in self.calls.items()
                              if self.observed.get(tid, {}).get("status") in TERMINAL}
        self.unresolved = {tid: call for call, tid in self.calls.items()
                           if tid not in self.background and call not in returned_calls}
        self.start_watcher()

    def record(self, result, call_id=None):
        if "task_id" in result:
            self.observed[result["task_id"]] = result
            self.storage.event("sandbox/task_observed", call_id=call_id, result=result)

    def checkpoint(self):
        self.storage.event("sandbox/shell_state", **self.save())

    async def request(self, args, call_id=None):
        operation = self.storage.allocate("sandbox-shell")
        self.storage.event("io/queued", operation=operation, call_id=call_id, kind_name="sandbox-shell", arguments=args)
        self.storage.record(f"{operation}.request.json", args)
        self.storage.event("io/start", operation=operation, call_id=call_id)
        try:
            result = await self.sandbox.request(args)
            self.storage.record(f"{operation}.result.json", result)
            self.record(result, call_id)
            if "error" in result:
                raise ToolInputError(result["error"]["message"])
        except BaseException as exc:
            self.storage.event(
                "io/end",
                operation=operation,
                call_id=call_id,
                status="failed",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            raise
        self.storage.event("io/end", operation=operation, call_id=call_id, status="completed")
        return result

    def track(self, task_id, call_id):
        self.background.setdefault(
            task_id,
            {
                "call_id": call_id,
                "notified": False,
                "last_size": 0,
                "last_growth": time.time(),
                "stall_notified": False,
            },
        )
        self.checkpoint()
        self.start_watcher()

    def start_watcher(self):
        if (self.background or self.unresolved) and (self.watcher is None or self.watcher.done()):
            self.watcher = asyncio.create_task(self.watch())

    async def watch(self):
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                self.transport_error = exc
                self.ready.set()
                self.storage.event("sandbox/watch_error", error={"type": type(exc).__name__, "message": str(exc)})
            await asyncio.sleep(1)

    async def aclose(self):
        if self.watcher:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)

    async def refresh(self):
        async with self.refresh_lock:
            for tid, call in list(self.unresolved.items()):
                result = await self.sandbox.request({"action": "shell_poll", "task_id": tid, "wait": 0})
                if "error" in result:
                    self.storage.event("sandbox/task_missing", task_id=tid, call_id=call, error=result["error"])
                else:
                    self.record(result, call)
                    self.track(tid, call)
                del self.unresolved[tid]
            ids = []
            for tid, item in self.background.items():
                if not item["notified"]:
                    if self.observed.get(tid, {}).get("status") in TERMINAL:
                        self.ready.set()
                    else:
                        ids.append(tid)
            if not ids:
                return
            response = await self.request({"action": "shell_status", "task_ids": ids, "wait": 0})
            self.transport_error = None
            for result in response["tasks"]:
                self.record(result)
                item = self.background[result["task_id"]]
                if item["notified"]:
                    continue
                if result["status"] in TERMINAL:
                    self.ready.set()
                elif result["output_bytes"] != item["last_size"]:
                    item.update(last_size=result["output_bytes"], last_growth=time.time())
                elif not item["stall_notified"] and time.time() - item["last_growth"] >= 45:
                    tail = result.get("tail", "").rstrip().split("\n")[-1]
                    if re.search(
                        r"\([yY]/[nN]\)|\[[yY]/[nN]\]|\(yes/no\)|Press (any key|Enter)|"
                        r"Continue\?|Overwrite\?|\b(?:Do you|Would you|Shall I|Are you sure|Ready to).*\?\s*$",
                        tail,
                        re.IGNORECASE,
                    ):
                        item["stall_pending"] = True
                        self.ready.set()
                    else:
                        item["last_growth"] = time.time()

    async def take_notifications(self):
        await self.refresh()
        output = []
        for tid, item in self.background.items():
            result = self.observed.get(tid, {})
            if item["notified"] or not result:
                continue
            terminal = result["status"] in TERMINAL
            if not terminal and not item.get("stall_pending"):
                continue
            status = task_status(result)
            summary = (
                f'Background command "{result["description"]}" {status} (exit code {result["exit_code"]})'
                if terminal
                else f'Background command "{result["description"]}" appears to be waiting for interactive input'
            )
            if terminal and result.get("reason"):
                summary += ": " + result["reason"]
            fields = {"task-id": tid, "tool-use-id": item["call_id"], "output-file": result["output_file"]}
            if terminal:
                fields["status"] = status
            fields["summary"] = summary
            message = "<task-notification>\n" + "\n".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in fields.items())
            message += "\n</task-notification>"
            if not terminal:
                message += (
                    "\nLast output:\n"
                    + result.get("tail", "")
                    + "\nStop the task and retry with piped input or a non-interactive flag."
                )
                item.update(stall_pending=False, stall_notified=True)
            else:
                item["notified"] = True
            output.append(message)
            self.storage.event("sandbox/notification", task_id=tid, content=message, terminal=terminal)
        self.ready.clear()
        if output:
            self.checkpoint()
        return output

    async def wait_notifications(self):
        self.start_watcher()
        await self.ready.wait()
        if self.transport_error:
            raise self.transport_error

    async def pending(self):
        await self.refresh()
        return any(not item["notified"] for item in self.background.values())

    def cancelled_output(self, call_id):
        return {
            "task_id": self.calls.get(call_id),
            "error": {
                "type": "CancelledError",
                "message": "Bash wait cancelled. See recorded task state for stop/background status.",
            },
        }

    @tool(description=SHELL_BASH_DESCRIPTION, parameters=SHELL_BASH_SCHEMA)
    async def bash(self, call_id, command, timeout=None, description=None, run_in_background=False):  # noqa: ASYNC109
        timeout_ms = effective_timeout(timeout, self.settings)
        async with self.limit:
            tid = uuid.uuid4().hex
            self.calls[call_id] = tid
            self.storage.event("sandbox/task_request", call_id=call_id, task_id=tid, action="shell_start")
            args = {
                "action": "shell_start",
                "task_id": tid,
                "command": command,
                "description": description or command,
                "cwd": self.cwd,
                "timeout": timeout_ms / 1000,
                "background": run_in_background,
                "auto_background": self.settings["background_enabled"] and auto_background_allowed(command),
                "wait": 0 if run_in_background else timeout_ms / 1000 + 1,
                "output_limit": self.settings["max_output_bytes"],
            }
            try:
                request = asyncio.create_task(self.request(args, call_id))
                try:
                    async with asyncio.timeout(2):
                        result = await asyncio.shield(request)
                except TimeoutError:
                    self.storage.event("sandbox/progress", task_id=tid, call_id=call_id, foreground=True)
                    result = await request
                # Boundary scheduling must not expose an unfinished foreground result.
                while result["status"] not in TERMINAL and not result["background"]:
                    result = await self.request(
                        {"action": "shell_poll", "task_id": tid, "wait": 1,
                         "output_limit": self.settings["max_output_bytes"]}, call_id)
            except asyncio.CancelledError:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
                with suppress(Exception):
                    result = await self.request({"action": "shell_poll", "task_id": tid, "wait": 0}, call_id)
                    if result["background"]:
                        self.track(tid, call_id)
                    elif result["status"] not in TERMINAL:
                        await self.request({"action": "shell_stop", "task_id": tid, "wait": 2}, call_id)
                raise
            if result["background"] and (run_in_background or result["status"] not in TERMINAL):
                self.track(tid, call_id)
                content = (
                    f"Command running in background with ID: {tid}. Output is being written to: {result['output_file']}"
                )
                return ToolOutput(content, result=result)
            if result.get("cwd"):
                self.cwd = result["cwd"]
            self.checkpoint()
            failed, explanation = command_result(command, result["exit_code"], result["output"])
            if result["status"] != "completed":
                failed = True
                explanation = result.get("reason") or f"Command {result['status']} before completion"
            content = re.sub(r"^(\s*\n)+", "", result["output"]).rstrip()
            if result.get("persisted_output"):
                content = (
                    f"<persisted-output>\nOutput ({result['output_bytes']} bytes) saved to: "
                    f"{result['persisted_output']}\n"
                    + (
                        f"Saved copy capped at 64 MiB; live log: {result['output_file']}\n"
                        if result["persisted_truncated"]
                        else ""
                    )
                    + "Preview (head and tail):\n"
                    + result["preview"]
                    + "\n</persisted-output>"
                )
            if explanation:
                content = explanation + ("\n" + content if content else "")
            if failed:
                result = {**result, "error": {"type": "CommandFailed", "message": explanation}}
            if re.match(r"^\s*data:image/[a-z0-9.+_-]+;base64,", result["output"], re.IGNORECASE):
                picture = await self.request({"action": "shell_image", "file_path": result["output_file"],
                                              "wait": 0}, call_id)
                if picture.get("blocks"):
                    return media_output(self.storage, call_id, explanation or "Image output from bash", {
                        **result, "isImage": True, "blocks": picture["blocks"], "image": picture["file"]})
            return ToolOutput(content, result=result)

    def require_background(self, tid):
        if not tid:
            raise ToolInputError("Provide task_id (or the deprecated shell_id).")
        if tid not in self.background:
            raise ToolInputError(f"No background task found with ID: {tid}")

    def mark_notified(self, tid):
        self.background[tid]["notified"] = True
        self.background[tid]["acknowledged"] = True
        self.checkpoint()
        if all(item["notified"] for item in self.background.values()):
            self.ready.clear()

    @tool(
        description="Deprecated compatibility tool for background task output. Prefer bash to read its output file. "
        "Completion notifications arrive automatically. block=true waits up to timeout milliseconds (default 30000).",
        parameters=OUTPUT_SCHEMA,
    )
    async def task_output(self, call_id, task_id, block=True, timeout=30000):  # noqa: ASYNC109
        self.require_background(task_id)
        async with self.limit:
            result = await self.request(
                {
                    "action": "shell_poll",
                    "task_id": task_id,
                    "wait": timeout / 1000 if block else 0,
                    "output_limit": 8 * 1024 * 1024,
                },
                call_id,
            )
        done = result["status"] in TERMINAL
        if done:
            self.mark_notified(task_id)
        output = result["output"]
        if result["truncated"]:
            output += f"\n[Output truncated; read {result['output_file']} for remaining content.]"
        data = {
            "retrieval_status": "success" if done else "timeout" if block else "not_ready",
            "task": {
                "task_id": task_id,
                "task_type": "local_bash",
                "status": task_status(result),
                "description": result["description"],
                "output": output,
                "exitCode": result["exit_code"],
            },
        }
        parts = [f"<retrieval_status>{data['retrieval_status']}</retrieval_status>",
                 f"<task_id>{task_id}</task_id>", "<task_type>local_bash</task_type>",
                 f"<status>{task_status(result)}</status>"]
        if result["exit_code"] is not None:
            parts.append(f"<exit_code>{result['exit_code']}</exit_code>")
        if output.strip():
            limit = self.settings["max_task_output_chars"]
            if len(output) > limit:
                header = f"[Truncated. Full output: {result['output_file']}]\n\n"
                available = max(0, limit - len(header))
                output = header + (output[-available:] if available else "")
            parts.append("<output>\n" + output.rstrip() + "\n</output>")
        if result.get("reason"):
            data["task"]["error"] = result["reason"]
            parts.append(f"<error>{escape(result['reason'])}</error>")
        return ToolOutput("\n\n".join(parts), result=data)

    @tool(
        description="Stop a running background task and its child processes by task_id.",
        parameters=STOP_SCHEMA,
    )
    async def task_stop(self, call_id, task_id=None, shell_id=None):
        tid = task_id or shell_id
        self.require_background(tid)
        async with self.limit:
            current = await self.request({"action": "shell_poll", "task_id": tid, "wait": 0}, call_id)
            if current["status"] in TERMINAL:
                raise ToolInputError(f"Task {tid} is not running (status: {task_status(current)}).")
            result = await self.request({"action": "shell_stop", "task_id": tid, "wait": 5}, call_id)
        if result["status"] not in TERMINAL:
            raise RuntimeError(f"Task {tid} did not stop; its status is still unknown")
        self.mark_notified(tid)
        return {
            "message": f"Successfully stopped task: {tid} ({result['command']})",
            "task_id": tid,
            "task_type": "local_bash",
            "command": result["command"],
        }


BASH_DEFINITIONS = tool_definitions(BashTool)
