"""Project the actual native conversation into observable, replaceable context items."""

import json

from gh_puller.agent.context import instruction, system_message, tool_defs
from gh_puller.agent.events import function_call_item, function_output_item, reasoning_item, text_message


def context_messages(items):
    """Read the shared Context vocabulary without importing another agent's private state."""
    messages, group = [], None
    for item in items:
        kind, role = item["type"], item.get("role", "assistant")
        if role not in {"user", "assistant"}:
            continue
        if kind == "function_call_output":
            output = item["output"]
            messages.append({"role": "tool", "tool_call_id": item["call_id"],
                             "content": output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)})
            group = None
            continue
        if kind not in {"message", "reasoning", "function_call"}:
            continue
        key = item.get("id", "").rsplit("-", 1)[0] or None
        if (not messages or messages[-1]["role"] != role or role == "user"
                or (key is not None and key != group) or (kind == "message" and messages[-1].get("content"))):
            messages.append({"role": role, "content": None})
        group = key
        message = messages[-1]
        if kind == "function_call":
            message.setdefault("tool_calls", []).append({"id": item["call_id"], "type": "function",
                "function": {"name": item["name"], "arguments": item["arguments"]}})
        else:
            text = "".join(part.get("text", "<image>" if part["type"] == "input_image" else
                                   "<file>" if part["type"] == "input_file" else "")
                           for part in item.get("content", []))
            message["reasoning_content" if kind == "reasoning" else "content"] = text
    return messages


def complete_calls(messages):
    """Close interrupted calls before a new turn; never invent an unobserved tool result."""
    result, pending = [], {}
    for message in [*messages, None]:
        if message is not None and message["role"] == "tool":
            if message["tool_call_id"] in pending:
                result.append(message)
                pending.pop(message["tool_call_id"])
            continue
        result.extend({"role": "tool", "tool_call_id": call_id,
                       "content": "Tool output was not observed before this history prefix ended."}
                      for call_id in pending)
        pending = {call["id"]: None for call in message.get("tool_calls", [])} if message else {}
        if message is not None:
            result.append(message)
    return result


def message_items(message: dict, definitions: list[dict], image_observation: dict | None = None) -> list[dict]:
    """Preserve text and call identity; display image references without base64 text."""
    role = message["role"]
    if role == "system":
        return [system_message([instruction(message["content"]), tool_defs(definitions)])]
    if role == "tool":
        return [function_output_item(message["tool_call_id"], message["content"])]
    if image_observation is not None:
        return [image_observation]
    items = []
    if message.get("reasoning_content"):
        items.append(reasoning_item(message["reasoning_content"]))
    content = message.get("content")
    if isinstance(content, str) and content:
        items.append(text_message(role, content))
    elif isinstance(content, list):
        parts = []
        for part in content:
            if part.get("type") == "image_url":
                url = part["image_url"]["url"]
                parts.append({"type": "input_image", "image_url":
                              "[inline image in native request]" if url.startswith("data:") else url})
            elif part.get("type") == "file":
                parts.append({"type": "input_file", "filename": part["file"]["filename"],
                              "file_path": "[inline file in native request]"})
            else:
                parts.append({**part, "type": "input_text" if part.get("type") == "text" else part.get("type")})
        items.append({"type": "message", "role": role, "content": parts})
    items.extend(function_call_item(call["id"], call["function"]["name"], call["function"]["arguments"])
                 for call in message.get("tool_calls", []))
    return items


class ContextMirror:
    """Use one projection for appends and arbitrary future context replacements."""

    def __init__(self, recorder, definitions: list[dict]):
        self.recorder, self.definitions = recorder, definitions
        self.sequence = self.query = self.step = 0
        self.records: dict[object, tuple] = {}

    @staticmethod
    def key(message: dict):
        return ("tool", message["tool_call_id"]) if message["role"] == "tool" else id(message)

    def project(self, message: dict, *, producer: str | None = None,
                image_observation: dict | None = None) -> list[dict]:
        key = self.key(message)
        if key not in self.records:
            self.sequence += 1
            self.records[key] = (message, f"m{self.sequence}",
                                 {"query": self.query, "step": self.step, "producer": producer or message["role"]},
                                 image_observation)
        _, prefix, metadata, image_observation = self.records[key]
        self.records[key] = (message, prefix, metadata, image_observation)
        return [{**item, "id": f"{prefix}-{index}", "metadata": metadata}
                for index, item in enumerate(message_items(message, self.definitions, image_observation))]

    def append(self, message: dict, *, role: str | None = None, image_observation: dict | None = None) -> None:
        self.recorder.append_context(self.project(message, producer=role, image_observation=image_observation),
                                     role=role)

    def synchronize(self, messages: list[dict], *, force: bool = False) -> None:
        """Publish current memory, including removals; keep earlier events as immutable history."""
        items = [item for message in messages for item in self.project(message)]
        if force or items != self.recorder.context():
            self.recorder.set_context(items)
        live = {self.key(message) for message in messages}
        self.records = {key: record for key, record in self.records.items() if key in live}
