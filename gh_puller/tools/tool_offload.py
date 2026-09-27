"""Optional tool-output offload and retrieval without re-executing tools."""

import base64
import json
from dataclasses import asdict, dataclass, field

from .common import ToolStorage
from .registry import BATCH_OUTPUT, ToolProvider, tool, tool_definitions

GET_TOOL_RESULT_DESCRIPTION = (
    "Retrieve the complete saved outputs of earlier tool calls using tool_call_ids from their offload notices. "
    "Returns results in requested order, each with tool_call_id and content or error. "
    "Includes tool-provided images when present. Reads the saved results without rerunning the original tools "
    "or making a network request. Retrieved content is subject to the same context retention policy. "
    "References belong to the current conversation and expire when context is manually cleared."
)
GET_TOOL_RESULT_SCHEMA = {"type": "object", "properties": {
    "tool_call_ids": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
}, "required": ["tool_call_ids"], "additionalProperties": False}


@dataclass
class ToolOutput:
    content: str
    fatal: bool = False
    images: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    result: dict | None = None


@dataclass(frozen=True)
class OffloadPolicy:
    """Agent-selected retention; the originating query/call counts as age one."""

    num_user_query: int = 1
    num_tool_query: int = 16
    preview_lines: int = 10
    preview_chars: int = 2000

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in asdict(self).values()):
            raise ValueError("Tool result retention limits must be positive integers")


@dataclass
class SavedResult:
    artifact: str
    policy: OffloadPolicy


@dataclass
class ResultUse:
    tool_call_id: str
    message: dict
    image_message: dict | None
    user_query: int
    tool_query: int
    seen: bool = False


def readable_result(content: str) -> str:
    """Use the same readable representation for both the line gate and the preview."""
    try:
        return json.dumps(json.loads(content), ensure_ascii=False, indent=2)
    except ValueError:
        return content


def result_preview(content: str, tool_call_id: str, *, preview_lines: int = OffloadPolicy.preview_lines,
                   preview_chars: int = OffloadPolicy.preview_chars) -> str:
    """Index every batch member; ordinary text keeps its bounded leading excerpt."""
    preview = "\n".join(readable_result(content).splitlines()[:preview_lines])[:preview_chars]
    try:
        body = json.loads(content)
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("data"), dict) and "extensions" in body:
        errors = body.get("errors", [])
        summaries = [{"name": name, "status": "error" if value is None and any(
            e.get("path", [None])[0] == name for e in errors) else "partial" if any(
            e.get("path", [None])[0] == name for e in errors) else "ok"} for name, value in body["data"].items()]
        preview = f"Saved GraphQL response: {len(summaries)} roots.\n" + "\n".join(
            json.dumps(item, ensure_ascii=False) for item in summaries)
    elif isinstance(body, dict) and isinstance(body.get("results"), list):
        summaries = []
        for index, item in enumerate(body["results"]):
            if not isinstance(item, dict):
                summaries.append({"index": index, "type": type(item).__name__})
                continue
            summary = {k: item[k] for k in ("name", "result_id", "display_complete") if k in item}
            summary.update(index=index, status="partial" if item.get("error") and item.get("data") is not None
                           else "error" if item.get("error") else "ok")
            if isinstance(item.get("error"), dict):
                summary["error"] = item["error"].get("code", item["error"].get("type"))
            if isinstance(item.get("data"), list):
                summary["items"] = len(item["data"])
            if item.get("pages"):
                summary["lists_complete"] = all(p.get("complete", False) for p in item["pages"])
            summary["continuation"] = bool(item.get("continue_request") or item.get("next_request") or any(
                p.get("next_request") or p.get("previous_request") for p in item.get("pages", [])))
            summaries.append(summary)
        preview = f"Saved batch: {len(summaries)} results.\n" + "\n".join(
            json.dumps(item, ensure_ascii=False) for item in summaries)
    references = json.dumps([tool_call_id], ensure_ascii=False)
    return (f"{preview}\n---\nTool result offloaded. Call get_tool_result(tool_call_ids={references}) "
            "to retrieve the complete result, including any tool-provided images.")


class ToolResultStore(ToolProvider):
    """Disk-backed immutable originals plus independently aging context appearances."""

    def __init__(self, storage: ToolStorage):
        self.storage = storage
        self.user_query = self.tool_query = 0
        self.saved: dict[str, SavedResult] = {}
        self.uses: list[ResultUse] = []

    def clear_context(self) -> None:
        self.saved.clear()
        self.uses.clear()
        self.user_query = self.tool_query = 0

    def begin_user_query(self) -> None:
        self.user_query += 1

    def begin_tool_batch(self, count: int) -> range:
        start = self.tool_query + 1
        self.tool_query += count
        return range(start, self.tool_query + 1)

    def bind(self, output: ToolOutput, message: dict, image_message: dict | None, *, name: str,
             tool_query: int, policy: OffloadPolicy) -> None:
        """Record in model call order, separately from concurrent execution order."""
        readable = readable_result(output.content)
        if len(readable.splitlines()) < policy.preview_lines and len(readable) < policy.preview_chars:
            return
        tool_call_id = message["tool_call_id"]
        artifact = self.storage.write(f"{self.storage.allocate('tool-result')}.json", {
            "call_id": tool_call_id, "name": name,
            "content": output.content, "image_attachments": output.observations, "policy": asdict(policy),
        }, call_id=tool_call_id)
        self.saved[tool_call_id] = SavedResult(artifact, policy)
        self.storage.event("tool_result/saved", call_id=tool_call_id, name=name, artifact=artifact,
                       user_query=self.user_query, tool_query=tool_query)
        self.uses.append(ResultUse(tool_call_id, message, image_message, self.user_query, tool_query))

    @tool(description=GET_TOOL_RESULT_DESCRIPTION, parameters=GET_TOOL_RESULT_SCHEMA, returns=BATCH_OUTPUT)
    async def get_tool_result(self, call_id: str, tool_call_ids: list[str]) -> ToolOutput:
        """Rehydrate only saved tool attachments; native user images never enter this store."""
        results, images, observations = [], [], []
        for tool_call_id in tool_call_ids:
            try:
                if tool_call_id not in self.saved:
                    raise ValueError(
                        f"Unknown tool_call_id {tool_call_id!r} in this context; use an ID from an offload notice",
                    )
                saved = self.saved[tool_call_id]
                body = json.loads((self.storage.root / saved.artifact).read_text(encoding="utf-8"))
                attachments = []
                for part in body["image_attachments"]:
                    if part["type"] == "input_text":
                        attachments.append({"type": "text", "text": part["text"]})
                    elif part["type"] == "input_image":
                        encoded = base64.b64encode((self.storage.root / part["image_url"]).read_bytes()).decode("ascii")
                        attachments.append({"type": "image_url", "image_url": {
                            "url": f"data:{part['media_type']};base64,{encoded}", "detail": "auto",
                        }})
                    elif part["type"] == "input_file":
                        encoded = base64.b64encode((self.storage.root / part["file_path"]).read_bytes()).decode("ascii")
                        attachments.append({"type": "file", "file": {"filename": part["filename"],
                                            "file_data": f"data:{part['media_type']};base64,{encoded}"}})
                results.append({"tool_call_id": tool_call_id, "content": body["content"]})
            except (OSError, ValueError, KeyError) as exc:
                results.append({"tool_call_id": tool_call_id, "error": {
                    "type": type(exc).__name__, "message": str(exc),
                }})
            else:
                images.extend(attachments)
                observations.extend(body["image_attachments"])
                self.storage.event("tool_result/restored", source_call_id=tool_call_id, call_id=call_id,
                               artifact=saved.artifact)
        return ToolOutput(json.dumps({"results": results}, ensure_ascii=False),
                          images=images, observations=observations)

    def prepare_messages(self, messages: list[dict]) -> list[dict]:
        """Offload eligible appearances before a request; every fresh result gets a full first view."""
        replacements, removed, retained = {}, set(), []
        for use in self.uses:
            policy = self.saved[use.tool_call_id].policy
            user_age = self.user_query - use.user_query + 1
            tool_age = self.tool_query - use.tool_query + 1
            expired = user_age > policy.num_user_query or tool_age > policy.num_tool_query
            if use.seen and expired:
                preview = result_preview(use.message["content"], use.tool_call_id,
                                         preview_lines=policy.preview_lines, preview_chars=policy.preview_chars)
                replacements[id(use.message)] = {**use.message, "content": preview}
                if use.image_message is not None:
                    removed.add(id(use.image_message))
                self.storage.event("tool_result/offloaded", call_id=use.tool_call_id,
                               num_user_query=user_age, num_tool_query=tool_age,
                               original_chars=len(use.message["content"]), preview_chars=len(preview),
                               tool_images_removed=use.image_message is not None)
            else:
                use.seen = True
                retained.append(use)
        self.uses = retained
        # Replace objects, never mutate an earlier request's message or the raw evidence.
        return [replacements.get(id(message), message) for message in messages if id(message) not in removed]


GET_TOOL_RESULT_DEFINITION = tool_definitions(ToolResultStore)[0]
