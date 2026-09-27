"""Independent Python implementation of the pinned OpenClaude Bash wire behavior.

The reference and platform boundaries are recorded in docs/code-bash-contract.md.
Runtime process behavior is implemented in Python; tool descriptions live in tool_bash.py.
"""

import math
import os
import re
import shlex
from pathlib import PurePosixPath

BASH_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "The command to execute"},
        "timeout": {
            "type": "number",
            "description": "Optional timeout in milliseconds (max 600000)",
        },
        "description": {"type": "string", "description":
            'Clear, concise description of what this command does in active voice. Never use words like "complex" '
            'or "risk" in the description - just describe what it does.\n\n'
            'For simple commands (git, npm, standard CLI tools), keep it brief (5-10 words):\n'
            '- ls → "List files in current directory"\n'
            '- git status → "Show working tree status"\n'
            '- npm install → "Install package dependencies"\n\n'
            'For commands that are harder to parse at a glance (piped commands, obscure flags, etc.), add enough '
            'context to clarify what it does:\n'
            '- find . -name "*.tmp" -exec rm {} \\; → "Find and delete all .tmp files recursively"\n'
            '- git reset --hard origin/main → "Discard all local changes and match remote main"\n'
            "- curl -s url | jq '.data[]' → \"Fetch JSON from URL and extract data array elements\""},
        "run_in_background": {"type": "boolean", "description":
                              "Set to true to run this command in the background. "
                              "Use bash to read the output file later."},
    },
    "required": ["command"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "block": {"type": "boolean", "default": True},
        "timeout": {
            "type": "number",
            "minimum": 0,
            "maximum": 600000,
            "default": 30000,
            "description": "Maximum wait in milliseconds; does not stop the command.",
        },
    },
    "required": ["task_id"],
    "additionalProperties": False,
}
STOP_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "Background task to stop."},
        "shell_id": {"type": "string", "description": "Deprecated alias for task_id."},
    },
    "additionalProperties": False,
}


def normalize_arguments(name, args):
    if not isinstance(args, dict):
        return args
    args = dict(args)
    numbers = {"bash": ("timeout",)}.get(name, ())
    booleans = {"bash": ("run_in_background",), "task_output": ("block",)}.get(name, ())
    for key in numbers:
        value = args.get(key)
        if isinstance(value, str) and re.fullmatch(r"-?\d+(\.\d+)?", value):
            args[key] = float(value) if "." in value else int(value)
    for key in booleans:
        if isinstance(args.get(key), str) and args[key] in {"true", "false"}:
            args[key] = args[key] == "true"
    return args


def positive_env(name, fallback):
    match = re.match(r"\s*\+?(\d+)", os.getenv(name, ""))
    return int(match[1]) if match and int(match[1]) > 0 else fallback


def shell_settings():
    default = min(positive_env("BASH_DEFAULT_TIMEOUT_MS", 120000), 1800000)
    maximum = min(max(default, positive_env("BASH_MAX_TIMEOUT_MS", 600000)), 1800000)
    return {
        "default_timeout_ms": default,
        "max_timeout_ms": maximum,
        "max_output_bytes": min(positive_env("BASH_MAX_OUTPUT_LENGTH", 30000), 150000),
        "max_task_output_chars": min(positive_env("TASK_MAX_OUTPUT_LENGTH", 32000), 160000),
        "background_enabled": os.getenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "").lower().strip()
        not in {"1", "true", "yes", "on"},
    }


def effective_timeout(value, settings):
    if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)):
        raise ValueError("timeout must be a finite number of milliseconds")
    return min(value if value is not None and value > 0 else settings["default_timeout_ms"], settings["max_timeout_ms"])


def command_parts(command):
    """Only a result-label heuristic, never a shell parser or authorization check."""
    segments, current, operators = [], [], []
    quote, escaped, comment, operator_index = None, False, False, -2
    for index, char in enumerate(command):
        if comment:
            if char != "\n":
                continue
            comment = False
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and quote != "'":
            current.append(char)
            escaped = True
        elif quote:
            current.append(char)
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            current.append(char)
            quote = char
        elif char == "#" and (not current or current[-1].isspace()):
            comment = True
        elif char == "&" and (command[max(0, index - 1):index] in {">", "<"}
                              or command[index + 1:index + 2] == ">"):
            current.append(char)
        elif char in ";&|()\n":
            if current:
                segments.append("".join(current))
                current = []
            if index == operator_index + 1:
                operators[-1] += char
            else:
                operators.append(char)
            operator_index = index
        else:
            current.append(char)
    if current:
        segments.append("".join(current))
    try:
        parts = [tokens for segment in segments if (tokens := shlex.split(segment))]
    except ValueError:
        return [command.split()], []
    else:
        return parts, operators


def auto_background_allowed(command):
    parts, _ = command_parts(command)
    # The reference compares its first entire command segment to 'sleep', not
    # the first executable word. Preserve that observable behavior (sleep 1 is allowed).
    return not parts or parts[0] != ["sleep"]


DIAGNOSTICS = {"ruff", "eslint", "flake8", "biome", "mypy", "pyright", "prettier", "black", "pytest", "jest", "vitest"}
INFORMATIONAL = {
    "grep": "No matches found",
    "rg": "No matches found",
    "diff": "Files differ",
    "find": "Some directories were inaccessible",
    "test": "Condition is false",
    "[": "Condition is false",
}
WRAPPERS = {"uvx", "npx", "npm", "bunx", "pipx", "python", "python3", "py", "pnpm", "yarn", "bun"}
SCRIPT_NAMES = {
    "lint": "eslint",
    "lint:fix": "eslint",
    "test": "jest",
    "test:unit": "jest",
    "test:watch": "jest",
    "typecheck": "tsc",
    "type-check": "tsc",
}
VALUE_FLAGS = {
    "-p",
    "--package",
    "--from",
    "--with",
    "--spec",
    "--python",
    "--env-file",
    "--cache-dir",
    "--workspace",
    "-w",
    "--filter",
    "-F",
    "--cwd",
    "--dir",
    "-C",
}


def runnable(tokens):
    tokens = list(tokens)
    while tokens:
        if re.match(r"[a-zA-Z_]\w*=", tokens[0]):
            tokens.pop(0)
        elif PurePosixPath(tokens[0]).name == "env":
            tokens.pop(0)
            while tokens and tokens[0].startswith("-"):
                flag = tokens.pop(0)
                if flag.split("=", 1)[0] in {"-S", "--split-string"}:
                    payload = flag.split("=", 1)[1] if "=" in flag else tokens.pop(0) if tokens else ""
                    return runnable(shlex.split(payload))
                if flag in {"-u", "--unset", "-C", "-P"} and tokens:
                    tokens.pop(0)
        else:
            break
    return tokens


def resolved_command(tokens):
    tokens = runnable(tokens)
    if not tokens:
        return "", False
    name = PurePosixPath(tokens.pop(0)).name
    if name not in WRAPPERS:
        return name, False
    if name in {"python", "python3", "py", "pipx", "bun"}:
        allowed = {"python": {"-m"}, "python3": {"-m"}, "py": {"-m"}, "pipx": {"run"}, "bun": {"exec", "x"}}[name]
        if not tokens or tokens.pop(0) not in allowed:
            return name, False
    if name in {"npm", "pnpm", "yarn"}:
        while tokens and (tokens[0].startswith("-") or tokens[0] == "workspace"):
            flag = tokens.pop(0)
            if (flag in VALUE_FLAGS or flag == "workspace") and tokens:
                tokens.pop(0)
        if tokens and tokens[0] in {"run", "run-script", "exec", "x"}:
            tokens.pop(0)
        elif name == "npm" and (not tokens or tokens[0] != "test"):
            return name, False
    while tokens and tokens[0].startswith("-"):
        flag = tokens.pop(0)
        if flag in VALUE_FLAGS and tokens:
            tokens.pop(0)
    wrapped = PurePosixPath(tokens[0]).name if tokens else name
    if name in {"npm", "pnpm", "yarn"}:
        wrapped = SCRIPT_NAMES.get(wrapped, wrapped)
    return wrapped, True


def command_result(command, code, output):
    """Retain the real exit code while separating diagnostics from launch failures."""
    if code == 0:
        return False, ""
    parts, operators = command_parts(command)
    name, wrapped = resolved_command(parts[-1] if parts else [])
    message = ""
    failed = True
    if code is not None and code > 0:
        if name in INFORMATIONAL or name in DIAGNOSTICS:
            failed = code >= 2
            message = INFORMATIONAL.get(name, "violations or test failures reported") if code == 1 else ""
        elif name == "pylint":
            failed = bool(code & 32)
            message = "lint diagnostics reported" if not failed else ""
        elif name == "tsc":
            usage = re.search(
                r"error TS(?:5023|5024|5025|5029|5057|6053|6054):|Unknown compiler option|"
                r"Compiler option .* requires a value|File .* not found",
                output,
                re.IGNORECASE,
            )
            failed = bool(usage) or not (code == 2 or (code == 1 and re.search(r"error TS\d+", output, re.IGNORECASE)))
            message = "type errors reported" if not failed else ""
    if not failed:
        previous = [resolved_command(p)[0] for p in parts[:-1]]
        setup_failure = any(
            re.search(
                rf"(?:^|\n).*\b{re.escape(p)}:.*(?:no such file|not found|permission denied|does not exist)",
                output,
                re.IGNORECASE,
            )
            for p in previous
        )
        wrapper_failure = wrapped and re.search(
            r"(?:^|\n)\s*(?:npm (?:ERR!|error) code (?!ELIFECYCLE\b)|pnpm ERR! (?!Command failed with exit code)|"
            r"yarn (?:error|ERR!)|bunx? (?:error|ERR!)|pipx.*error|Fatal error from pip|"
            r"error: failed to (?:download|install|fetch)|failed to (?:download|install)|"
            r"No matching distribution found|Could not find a version that satisfies)",
            output,
            re.IGNORECASE,
        )
        skipped = (
            not output.strip()
            and any(op in {"&&", "|"} for op in operators)
            and name in DIAGNOSTICS | {"pylint", "tsc"}
            and bool(set(previous) & {"false", "test", "[", "cd", "pushd"})
        )
        failed = bool(setup_failure or wrapper_failure or skipped)
    return failed, f"Exit code {code}" if failed else message
