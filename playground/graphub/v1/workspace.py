"""Provide ordinary coding tools inside a networked disposable Docker workspace.

Bash accepts arbitrary commands. Only explicitly supplied directories are mounted;
facts are read-only and working files are writable. This is host-data isolation,
not a static-analysis command filter. Shell programs and credentials are runtime
configuration, not extra model tools. The script entry point handles file edits
inside the container without importing the agent or its credentials.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from uuid import uuid4


async def process(arguments, *, data=b"", environment=None, capture_bytes=2 << 20):
    child = await asyncio.create_subprocess_exec(
        *arguments, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=environment,
    )

    async def collect(stream):
        saved, total = bytearray(), 0
        while chunk := await stream.read(65536):
            saved.extend(chunk[:max(0, capture_bytes - len(saved))])
            total += len(chunk)
        return {"text": saved.decode(errors="replace"), "bytes": total, "truncated": total > len(saved)}

    async def feed():
        try:
            child.stdin.write(data)
            await child.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # A command may exit without consuming its input.
        finally:
            child.stdin.close()

    try:
        async with asyncio.TaskGroup() as tasks:
            stdout = tasks.create_task(collect(child.stdout))
            stderr = tasks.create_task(collect(child.stderr))
            tasks.create_task(feed())
            await child.wait()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    return {"exit_code": child.returncode, "stdout": stdout.result(), "stderr": stderr.result()}


class Workspace:
    def __init__(self, directory: Path, *, image: str, mounts: dict[str, Path] | None = None,
                 environment: dict[str, str] | None = None):
        """Own one networked shell environment for an experimental agent session.

        Args:
            directory: Writable workspace dedicated to this run, without host secrets.
            image: Explicit local image tag or immutable image ID containing shell tools.
            mounts: Container absolute paths mapped to read-only fact directories.
            environment: Explicit container environment, independent of host defaults.
                Credential values are removed from returned command output.
        """
        self.directory = directory.resolve()
        self.image = image
        self.mounts = mounts or {}
        self.environment = dict(environment or {})
        self.script = Path(__file__).resolve()
        self.uv = Path(shutil.which("uv")).resolve()
        self.name = "graphub-v1-" + uuid4().hex
        self.opened = False

    async def __aenter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        arguments = [
            "docker", "run", "--detach", "--init", "--name", self.name, "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "512",
            "--memory", "4g", "--cpus", "2", "--tmpfs", "/tmp:rw,size=1g",  # noqa: S108 - Private container tmpfs.
            "--user", f"{os.getuid()}:{os.getgid()}", "--workdir", "/work", "--entrypoint", "/bin/sleep",
            "--mount", f"type=bind,src={self.directory},dst=/work",
            "--mount", f"type=bind,src={self.script},dst=/runner/workspace.py,readonly",
            "--mount", f"type=bind,src={self.uv},dst=/usr/local/bin/uv,readonly",
            "--env", "UV_CACHE_DIR=/work/.uv-cache", "--env", "GIT_CONFIG_GLOBAL=/work/.gitconfig",
            "--env", "GH_CONFIG_DIR=/work/.gh", "--env", "UV_PYTHON_DOWNLOADS=never",
        ]
        for target, source in self.mounts.items():
            arguments.extend(["--mount", f"type=bind,src={source.resolve()},dst={target},readonly"])
        for key in self.environment:
            arguments.extend(["--env", key])
        arguments.extend([self.image, "infinity"])
        result = await process(arguments, environment=os.environ | self.environment)
        if result["exit_code"]:
            raise RuntimeError(result["stderr"]["text"])
        self.opened = True
        return self

    async def __aexit__(self, *_exc):
        if self.opened:
            await process(["docker", "rm", "--force", self.name])
            self.opened = False

    async def execute(self, arguments: list[str], *, data: str = "", cwd: str = "/work", seconds: int = 120) -> str:
        """Execute a command without a program whitelist and return bounded output.

        Args:
            arguments: Exact executable and argv inside the container.
            data: UTF-8 stdin, separate from command arguments.
            cwd: Container working directory; no host path resolution is performed.
            seconds: Command deadline. Background descendants remain owned by the
                container and are removed at session close or cancellation.
        """
        try:
            result = await process([
                "docker", "exec", "--interactive", "--workdir", cwd, self.name,
                "timeout", "--kill-after=2", str(seconds), *arguments,
            ], data=data.encode())
        except asyncio.CancelledError:
            # Cancelling docker exec alone does not terminate the process in its container.
            await self.__aexit__()
            raise
        for key, secret in self.environment.items():
            if secret and any(part in key.upper() for part in ("TOKEN", "KEY", "SECRET", "PASSWORD")):
                for stream in ("stdout", "stderr"):
                    result[stream]["text"] = result[stream]["text"].replace(secret, "<redacted>")
        return json.dumps(result, ensure_ascii=False)

    def tools(self):
        """Return the common filesystem and shell tool collection."""
        from .agent import Tool

        async def bash(args):
            return await self.execute(["bash", "-c", args["command"]], cwd=args.get("cwd", "/work"),
                                      seconds=args.get("timeout_seconds", 120))

        async def grep(args):
            argv = ["rg", "--line-number", "--with-filename", "--hidden", "--no-ignore", "--color=never"]
            if args.get("ignore_case"):
                argv.append("--ignore-case")
            if args.get("glob"):
                argv.extend(["--glob", args["glob"]])
            argv.extend(["--", args["pattern"], args.get("path", "/work")])
            return await self.execute(argv)

        async def glob(args):
            return await self.execute(["rg", "--files", "--hidden", "--no-ignore", "--glob", args["pattern"],
                                       "--", args.get("path", "/work")])

        def file_handler(operation):
            async def invoke(args):
                return await self.execute(["uv", "run", "--offline", "--no-project", "python", "-I",
                                           "/runner/workspace.py", operation], data=json.dumps(args))
            return invoke

        def make(name, description, properties, required, invoke):
            return Tool(name, description, {"type": "object", "properties": properties,
                                           "required": required, "additionalProperties": False}, invoke)

        string = {"type": "string"}
        return [
            make("Bash", "Execute a shell command in the working environment; returns exit code, stdout and stderr.",
                 {"command": string, "cwd": string,
                  "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 1800}}, ["command"], bash),
            make("Grep", "Search file contents with a regular expression; return paths and line numbers.",
                 {"pattern": string, "path": string, "glob": string, "ignore_case": {"type": "boolean"}},
                 ["pattern"], grep),
            make("Glob", "Find file paths matching a glob pattern beneath a directory.",
                 {"pattern": string, "path": string}, ["pattern"], glob),
            make("Read", "Read a text file with one-based line numbers. Offset defaults to 1 and limit to 200 lines.",
                 {"path": string, "offset": {"type": "integer", "minimum": 1},
                  "limit": {"type": "integer", "minimum": 1}}, ["path"], file_handler("read")),
            make("Write", "Create or overwrite a UTF-8 file. Parent directories are created as needed.",
                 {"path": string, "content": string}, ["path", "content"], file_handler("write")),
            make("Edit", "Replace an exact text occurrence. Ambiguous matches require replace_all=true.",
                 {"path": string, "old_string": string, "new_string": string, "replace_all": {"type": "boolean"}},
                 ["path", "old_string", "new_string"], file_handler("edit")),
        ]


def file_operation(operation, arguments):
    path = Path(arguments["path"])
    if operation == "read":
        offset, limit = arguments.get("offset", 1), arguments.get("limit", 200)
        with path.open(encoding="utf-8", newline="") as source:
            for number, line in enumerate(source, 1):
                if offset <= number < offset + limit:
                    print(f"{number}: {line}", end="")
                if number >= offset + limit:
                    break
        return
    if operation == "write":
        path.parent.mkdir(parents=True, exist_ok=True)
        content = arguments["content"]
    elif operation == "edit":
        with path.open(encoding="utf-8", newline="") as source:
            content = source.read()
        old = arguments["old_string"]
        count = content.count(old)
        if not old or not count or (count > 1 and not arguments.get("replace_all")):
            raise ValueError(f"Expected a nonempty unambiguous match; found {count}")
        content = content.replace(old, arguments["new_string"], -1 if arguments.get("replace_all") else 1)
    else:
        raise ValueError("Unknown file operation")
    path.write_text(content)
    print(json.dumps({"path": str(path), "bytes": len(content.encode())}))


if __name__ == "__main__":
    file_operation(sys.argv[1], json.load(sys.stdin))
