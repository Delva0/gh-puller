"""Read text, images, PDF documents/pages and notebooks in the shared container."""

import base64
import os
import re
from dataclasses import replace

from .read_file_worker import file_size
from .registry import ToolProvider, tool
from .shell_contract import positive_env
from .tool_offload import ToolOutput

# OpenClaude 5cd11336caeaf2023ca1e02e5975c761cfc585fe, src/tools/FileReadTool/prompt.ts.
READ_DESCRIPTION = (
    'Reads a file from the local filesystem. You can access any file directly by using this tool.\n'
    'Assume this tool is able to read all files on the machine. If the User provides a path to a file '
    'assume that path is valid. It is okay to read a file that does not exist; an error will be '
    'returned.\n'
    '\n'
    'Usage:\n'
    '- The file_path parameter must be an absolute path, not a relative path\n'
    '- By default, it reads up to 2000 lines starting from the beginning of the file\n'
    "- You can optionally specify a line offset and limit (especially handy for long files), but it's "
    'recommended to read the whole file by not providing these parameters\n'
    '- Results are returned using cat -n format, with line numbers starting at 1\n'
    '- This tool allows Claude Code to read images (eg PNG, JPG, etc). When reading an image file the '
    'contents are presented visually as Claude Code is a multimodal LLM.\n'
    '- This tool can read PDF files (.pdf). For large PDFs (more than 10 pages), you MUST provide the '
    'pages parameter to read specific page ranges (e.g., pages: "1-5"). Reading a large PDF without the '
    'pages parameter will fail. Maximum 20 pages per request.\n'
    '- This tool can read Jupyter notebooks (.ipynb files) and returns all cells with their outputs, '
    'combining code, text, and visualizations.\n'
    '- This tool can only read files, not directories. To read a directory, use an ls command via the '
    'bash tool.\n'
    '- You will regularly be asked to read screenshots. If the user provides a path to a screenshot, '
    'ALWAYS use this tool to view the file at the path. This tool will work with all temporary file '
    'paths.\n'
    '- If you read a file that exists but has empty contents you will receive a system reminder warning '
    'in place of file contents.'
)

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string", "description": "The absolute path to the file to read"},
        "offset": {"type": "integer", "minimum": 0, "description":
                   "The line number to start reading from. Only provide if the file is too large to read at once"},
        "limit": {"type": "integer", "minimum": 1, "description":
                  "The number of lines to read. Only provide if the file is too large to read at once."},
        "pages": {"type": "string", "description": 'Page range for PDF files (e.g., "1-5", "3", "10-20"). '
                  "Only applicable to PDF files. Maximum 20 pages per request."},
    },
    "required": ["file_path"],
    "additionalProperties": False,
}
FILE_UNCHANGED = (
    "File unchanged since last read. The content from the earlier read_file tool_result in this conversation "
    "is still current — refer to that instead of re-reading."
)
CYBER_REMINDER = (
    "\n\n<system-reminder>\nWhenever you read a file, you should consider whether it would be considered malware. "
    "You CAN and SHOULD provide analysis of malware, what it is doing. But you MUST refuse to improve or "
    "augment the code. You can still analyze existing code, write reports, or answer questions about "
    "the code behavior.\n</system-reminder>\n"
)


def read_settings(model=""):
    return {"max_bytes": 256 * 1024, "max_tokens": positive_env("CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS", 25000),
            "pdf_supported": "claude-3-haiku" not in model.lower(),
            "include_reminder": os.getenv("OPENCLAUDE_DISABLE_TOOL_REMINDERS", "").strip().lower()
            not in {"1", "true", "yes", "on"} and not re.search(r"claude-opus-4[-.][678](?:\D|$)", model)}


def media_output(storage, call_id, content, result):
    """Persist actual bytes and project ordered content into native request parts."""
    parts, observations = [], []
    for block in result.get("blocks", []):
        if block["type"] == "text":
            parts.append(block)
            observations.append({"type": "input_text", "text": block["text"]})
            continue
        source = block["source"]
        media = source["media_type"]
        raw = base64.b64decode(source["data"], validate=True)
        suffix = ".pdf" if block["type"] == "document" else "." + media.split("/")[-1]
        artifact = storage.write(storage.allocate("read-attachment") + suffix, raw, call_id=call_id)
        url = f"data:{media};base64,{source['data']}"
        if block["type"] == "document":
            filename = block["filename"]
            parts.append({"type": "file", "file": {"filename": filename, "file_data": url}})
            observations.append({"type": "input_file", "file_path": artifact,
                                 "filename": filename, "media_type": media})
        else:
            parts.append({"type": "image_url", "image_url": {"url": url, "detail": "auto"}})
            observations.append({"type": "input_image", "image_url": artifact, "media_type": media})
    # Text-only notebook output belongs in the paired tool result. With media,
    # preserve interleaved cells/outputs in one supplemental native message.
    if parts and all(part["type"] == "text" for part in parts):
        return ToolOutput("\n".join(part["text"] for part in parts), result=result)
    return ToolOutput(content, result=result, images=parts, observations=observations)


class ReadFileTool(ToolProvider):
    def __init__(self, session, *, legacy_name=False, model="", settings=None, supports_vision=None):
        self.session, self.legacy_name = session, legacy_name
        self.settings = settings or read_settings(model)
        self.supports_vision = supports_vision
        self.state = {}
        if legacy_name:
            self.tool_specs = tuple(
                replace(spec, description=spec.description.replace("bash", "Bash"))
                for spec in self.tool_specs
            )

    def normalize_arguments(self, name, args):
        if not isinstance(args, dict):
            return args
        args = dict(args)
        for key in ("offset", "limit"):
            value = args.get(key)
            if isinstance(value, str) and re.fullmatch(r"-?\d+(\.\d+)?", value):
                args[key] = float(value) if "." in value else int(value)
            if isinstance(args.get(key), float) and args[key].is_integer():
                args[key] = int(args[key])
        return args

    def clear_context(self):
        self.state.clear()

    def restore(self, state, returned_calls):
        # Never dedup a read whose result is absent from the recovered conversation.
        self.state = {path: item for path, item in state.items() if item["call_id"] in returned_calls}

    @tool(description=READ_DESCRIPTION, parameters=READ_SCHEMA)
    async def read_file(self, call_id, file_path, offset=1, limit=None, pages=None):
        async with self.session.limit:
            result = await self.session.request(
                {"action": "read_file", "file_path": file_path, "cwd": self.session.cwd,
                 "offset": offset, "limit": limit, "pages": pages, "read_state": self.state,
                 "bash_name": "Bash" if self.legacy_name else "bash", "supports_vision": self.supports_vision,
                 **self.settings, "wait": 130}, call_id)
        if state := result.get("read_state"):
            self.state[state["path"]] = {**state, "call_id": call_id}
        kind, data = result["type"], result["file"]
        if kind == "file_unchanged":
            content = FILE_UNCHANGED.replace("read_file", "Read") if self.legacy_name else FILE_UNCHANGED
        elif kind == "text":
            if data["content"]:
                content = "\n".join(f"{number:6d}→{line}" for number, line in
                                    enumerate(data["content"].split("\n"), data["startLine"]))
                if self.settings["include_reminder"]:
                    content += CYBER_REMINDER
            elif not data["totalLines"]:
                content = "<system-reminder>Warning: the file exists but the contents are empty.</system-reminder>"
            else:
                content = ("<system-reminder>Warning: the file exists but is shorter than the provided offset "
                           f"({data['startLine']}). The file has {data['totalLines']} lines.</system-reminder>")
        elif kind == "image":
            content = f"Image read: {file_path}"
            dims = data["dimensions"]
            width, height = dims["originalWidth"], dims["originalHeight"]
            shown_width, shown_height = dims["displayWidth"], dims["displayHeight"]
            if (width, height) != (shown_width, shown_height):
                note = (f"[Image: original {width}x{height}, displayed at {shown_width}x{shown_height}. "
                        f"Multiply coordinates by {width / shown_width:.2f} to map to original image.]")
                result["blocks"].append({"type": "text", "text": note})
        elif kind == "notebook":
            content = f"Notebook read: {file_path} ({len(data['cells'])} cells)"
        elif kind == "pdf":
            content = f"PDF file read: {data['filePath']} ({file_size(data['originalSize'])})"
        else:
            content = (f"PDF pages extracted: {data['count']} page(s) from {data['filePath']} "
                       f"({file_size(data['originalSize'])})")
        return media_output(self.session.storage, call_id, content, result)
