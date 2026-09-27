"""Programmatic tool calling (PTC) through the existing, observable async tools."""

import asyncio
import json
import os
import shutil
import signal
from contextlib import nullcontext, suppress
from dataclasses import replace
from pathlib import Path

from .registry import ToolProvider, ToolRegistry, tool
from .tool_offload import ToolOutput

MAX_OUTPUT_BYTES = 256_000
MAX_CALLS = 128
WORKER = Path(__file__).with_name("ptc_worker.mjs")
PTC_PROMPTS = {
    "A": """Use run_code to write an async JavaScript body or TypeScript with erasable type annotations.
Call the SDK below with await tools.name({...}). SDK tools are callable only inside the program
and return parsed JSON objects.

Run independent tasks concurrently with Promise.all or Promise.allSettled; await dependent tasks in order.
Use Promise.allSettled when you need to retain successful results from independent tasks if others fail.

Select and organize tool results as the task requires, retaining relevant original text, sources, errors
and uncertainty. Preserve pagination and continuation information when further reading is needed.
Prefer the tools' query, find, pagination and read-range parameters to control how much material is retrieved.

Use return or console.log to output content that should enter the conversation; both accept objects directly.
Intermediate tool results stay in logs. Images from completed tool calls are attached to the result automatically.

Check top-level error and results[].error in batch results. Argument or execution errors throw ToolCallError;
toolName and result provide details. Catch and handle errors as needed.

Await all calls you need. Unfinished calls are cancelled when the program returns, fails or is cancelled.

Each execution has fresh variables. Available globals are tools, console.log and standard JavaScript built-ins.
Use the SDK for external access; module imports, filesystem access and direct network access are unavailable.

Each execution allows at most 128 tool calls and 256,000 UTF-8 bytes across logs and the return value.
The timeout includes time spent waiting for tools.""",
    "B": """Write async JavaScript in run_code and call the SDK below with await tools.name({...}).
Independent tasks may run concurrently.

By default, return tool results directly. Use console.log for separate outputs; it accepts objects directly.
Only return values and console.log output enter the conversation.

Each execution has fresh variables and exposes tools, console.log and standard JavaScript built-ins.
Ending the program cancels unfinished calls.""",
}


def normalize_ptc(value: str | bool | None) -> str | bool:
    """Resolve legacy boolean settings to a named prompt variant, or disabled."""
    if value is None or value is False:
        return False
    if value is True:
        return "A"
    if value not in PTC_PROMPTS:
        raise ValueError(f"Unknown PTC prompt variant: {value!r}; expected A or B")
    return value


class PTCTool(ToolProvider):
    def __init__(self, storage, registry: ToolRegistry, dispatch, *, concurrency: int = 8, variant: str = "A",
                 immediate_tools: tuple[str, ...] = ()):
        self.storage, self.dispatch = storage, dispatch
        self.bindings = tuple(registry.specs)
        self.limit = asyncio.Semaphore(concurrency)
        self.immediate_tools = frozenset(immediate_tools)
        self.worker = WORKER.read_text()
        sdk = registry.sdk()
        spec = self.tool_specs[0]
        self.tool_specs = (replace(spec, description=PTC_PROMPTS[variant] + "\n\n" + sdk),)

    @tool(description=PTC_PROMPTS["A"], parameters={"type": "object", "properties": {
        "code": {"type": "string", "minLength": 1},
        "timeout": {"type": "number", "minimum": 0.1, "maximum": 600, "default": 120},
    }, "required": ["code"], "additionalProperties": False})
    async def run_code(self, call_id: str, code: str, timeout: float = 120) -> ToolOutput:  # noqa: ASYNC109
        operation = self.storage.allocate("ptc")
        self.storage.event("ptc/start", call_id=call_id, operation=operation, timeout=timeout)
        self.storage.record(f"{operation}.ts", code)
        process = None
        tasks, completed, logs = [], {}, []
        result, error, stderr_task = None, None, None
        seen, output_bytes = set(), 0

        async def send(packet):
            process.stdin.write((json.dumps(packet, ensure_ascii=False) + "\n").encode())
            await process.stdin.drain()

        async def invoke(packet):
            name, sub_id = packet["name"], f"{call_id}:ptc:{packet['id']}"
            try:
                async with nullcontext() if name in self.immediate_tools else self.limit:
                    output = await self.dispatch({"id": sub_id, "function": {
                        "name": name, "arguments": json.dumps(packet.get("args"), ensure_ascii=False),
                    }}, parent_call_id=call_id)
                completed[packet["id"]] = output
                value = json.loads(output.content)
                reply = {"id": packet["id"], "name": name, "result": value}
                if "error" in value:
                    reply["error"] = {"message": json.dumps(value["error"], ensure_ascii=False)}
            except Exception as exc:
                reply = {"id": packet["id"], "name": name, "error": {"message": str(exc)}}
            with suppress(BrokenPipeError, ConnectionResetError):
                await send(reply)

        try:
            async with asyncio.timeout(timeout):
                node = shutil.which("node")
                if not node:
                    raise FileNotFoundError("run_code requires Node.js 22.18 or newer on PATH")
                # Permissions apply to the worker, while credentials and HTTP clients remain in this process.
                process = await asyncio.create_subprocess_exec(
                    node, "--permission", "--max-old-space-size=128", "--disable-warning=ExperimentalWarning",
                    "--input-type=module", "--eval", self.worker,
                    cwd=self.storage.root, env={key: value for key, value in os.environ.items()
                                            if key in {"PATH", "LANG", "LC_ALL", "TZ"}},
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    start_new_session=True, limit=1024 * 1024,
                )
                stderr_task = asyncio.create_task(process.stderr.read(64_000))
                await send({"code": code, "tools": self.bindings,
                            "maxOutputBytes": MAX_OUTPUT_BYTES, "maxCalls": MAX_CALLS})
                while line := await process.stdout.readline():
                    packet = json.loads(line)
                    kind = packet.get("type")
                    if kind == "runtime":
                        self.storage.event("ptc/runtime", call_id=call_id, operation=operation,
                                       engine="node", version=packet["version"])
                    elif kind == "call":
                        number = packet.get("id")
                        if (not isinstance(number, int) or number in seen or len(seen) >= MAX_CALLS
                                or packet.get("name") not in self.bindings):
                            raise ValueError("Invalid PTC tool dispatch")
                        seen.add(number)
                        tasks.append(asyncio.create_task(invoke(packet)))
                    elif kind == "log":
                        text = packet["text"]
                        output_bytes += len(text.encode())
                        if output_bytes > MAX_OUTPUT_BYTES:
                            raise ValueError("PTC output limit exceeded")
                        logs.append(text)
                        self.storage.event("ptc/log", call_id=call_id, text=text)
                    elif kind == "done":
                        result, error = packet.get("result"), packet.get("error")
                        if len(json.dumps(result, ensure_ascii=False).encode()) + output_bytes > MAX_OUTPUT_BYTES:
                            raise ValueError("PTC output limit exceeded")
                        break
                    else:
                        raise ValueError("Invalid PTC worker message")
                else:
                    raise RuntimeError("PTC worker exited without a result")
        except BaseException as exc:
            error = {"type": type(exc).__name__, "message": str(exc) or type(exc).__name__}
            if not isinstance(exc, Exception):
                raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if process is not None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                # Drain unread protocol output so a full pipe cannot block process settlement.
                while await process.stdout.read(65536):
                    pass
                await process.wait()
            await asyncio.gather(*tasks, return_exceptions=True)
            if stderr_task is not None:
                self.storage.record(f"{operation}.stderr.txt", (await stderr_task).decode(errors="replace"))
            body = {"logs": logs, "result": result}
            if error:
                body["error"] = error
            body["cancelled_calls"] = sum(task.cancelled() for task in tasks)
            self.storage.record(f"{operation}.result.json", body)
            self.storage.event("ptc/end", call_id=call_id, operation=operation,
                           status="failed" if error else "completed", subcalls=len(tasks),
                           cancelled_calls=body["cancelled_calls"])
        outputs = [completed[number] for number in sorted(completed)]
        return ToolOutput(json.dumps(body, ensure_ascii=False), fatal=any(output.fatal for output in outputs),
                          images=[part for output in outputs for part in output.images],
                          observations=[part for output in outputs for part in output.observations])
