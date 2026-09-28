"""Restore observed search-agent context and evidence from event streams.

Checkpoint payloads contain native messages and explicitly recorded tool evidence,
not arbitrary Python memory. Target agents keep their own instructions and tools.
"""

import base64
import copy
import json
import re
from dataclasses import asdict
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ..tools.githost_api_utils import APIProvider
from ..tools.tool_offload import OffloadPolicy, ResultUse, SavedResult
from ..tools.tool_web import WebTools


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    messages: list[dict[str, JsonValue]]
    files: dict[str, str] = Field(default_factory=dict)
    sequence: int = Field(default=0, ge=0)
    providers: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    saved: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    uses: list[dict[str, JsonValue]] = Field(default_factory=list)
    user_query: int = Field(default=0, ge=0)
    tool_query: int = Field(default=0, ge=0)


def file_name(value):
    path = PurePosixPath(value)
    if not value or len(value) > 256 or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("Invalid checkpoint file path")
    return value


def providers(agent):
    """Identify evidence stores by provider rather than by the selected agent."""
    for provider in agent.tool_registry.providers.values():
        if isinstance(provider, APIProvider):
            yield provider.api.provider, provider.api.responses
        elif isinstance(provider, WebTools):
            yield "web", provider.resources


def snapshot(agent):
    state = agent._history
    state.providers.update({name: copy.deepcopy(values) for name, values in providers(agent)})
    messages = agent.messages[1:]
    positions = {id(message): index for index, message in enumerate(messages)}
    files = {}
    for path in agent.storage.root.rglob("*"):
        name = path.relative_to(agent.storage.root).as_posix()
        if path.is_file() and name not in agent._history_files:
            files[name] = base64.b64encode(agent.storage.read(name)).decode()
    state = Checkpoint(
        messages=messages, files=files, sequence=agent.storage.sequence,
        providers=state.providers,
        saved={key: asdict(value) for key, value in agent.tool_results.saved.items()},
        uses=[{"tool_call_id": use.tool_call_id, "message": positions[id(use.message)],
               "image_message": positions.get(id(use.image_message)), "user_query": use.user_query,
               "tool_query": use.tool_query, "seen": use.seen}
              for use in agent.tool_results.uses if id(use.message) in positions],
        user_query=agent.tool_results.user_query, tool_query=agent.tool_results.tool_query,
    )
    agent._history_files.update(files)
    agent._history = state
    return state.model_dump()


def validate_state(state, files):
    """Validate every path subsequently read directly by a package tool."""
    if state.version != 1:
        raise ValueError("Unsupported context checkpoint version")

    def reference(name):
        if file_name(name) not in files:
            raise ValueError("Checkpoint evidence file is missing")

    for values in state.providers.values():
        for metadata in values.values():
            if not isinstance(metadata, dict) or not isinstance(metadata.get("body_file"), str):
                raise TypeError("Invalid evidence reference")
            reference(metadata["body_file"])
    for saved in state.saved.values():
        reference(saved["artifact"])
        OffloadPolicy(**saved["policy"])
        body = json.loads(files[saved["artifact"]])
        for part in body.get("image_attachments", []):
            if part.get("type") == "input_image":
                reference(part["image_url"])
            elif part.get("type") == "input_file":
                reference(part["file_path"])
    pending = set()
    for message in state.messages:
        role = message.get("role")
        if role not in {"user", "assistant", "tool"}:
            raise ValueError("Invalid checkpoint message role")
        if role == "tool":
            if message.get("tool_call_id") not in pending:
                raise ValueError("Unmatched tool output")
            pending.remove(message["tool_call_id"])
        else:
            if pending:
                raise ValueError("Missing tool outputs")
            for call in message.get("tool_calls", []):
                if (role != "assistant" or not isinstance(call.get("id"), str)
                        or call["id"] in pending or call.get("type") != "function"
                        or not isinstance(call.get("function", {}).get("arguments"), str)):
                    raise ValueError("Invalid tool call")
                pending.add(call["id"])
    if pending:
        raise ValueError("Unfinished tool outputs")
    for use in state.uses:
        index = use["message"]
        image = use.get("image_message")
        if (type(index) is not int or not 0 <= index < len(state.messages)
                or use["tool_call_id"] not in state.saved
                or state.messages[index].get("tool_call_id") != use["tool_call_id"]
                or (image is not None and (type(image) is not int or not 0 <= image < len(state.messages)))):
            raise ValueError("Invalid retained result")


def legacy_messages(events):
    """Recover available observations without pretending they include native attachments."""
    messages = []
    for event in events:
        data = event["data"]
        if event["type"] == "query/start":
            messages.append({"role": "user", "content": str(data.get("prompt", ""))})
        elif event["type"] == "model/response":
            message = {"role": "assistant", "content": None}
            calls = []
            for item in data.get("output", []):
                if item.get("type") == "function_call":
                    calls.append({"id": item["call_id"], "type": "function", "function": {
                        "name": item["name"], "arguments": item["arguments"]}})
                elif item.get("type") in {"message", "reasoning"}:
                    text = "".join(part.get("text", "") for part in item.get("content", []))
                    message["content" if item["type"] == "message" else "reasoning_content"] = text
            if calls:
                message["tool_calls"] = calls
            messages.append(message)
        elif event["type"] == "tool/end":
            messages.append({"role": "tool", "tool_call_id": data.get("callId"),
                             "content": str(data.get("result") or json.dumps(data.get("error")))})
        elif (event["type"] == "query/end" and data.get("answer")
              and (not messages or messages[-1].get("content") != data["answer"])):
            messages.append({"role": "assistant", "content": data["answer"]})
    # Legacy nested PTC calls are observations, not native assistant calls.
    paired, pending = [], {}
    for message in messages:
        if message["role"] == "tool":
            if message["tool_call_id"] in pending:
                pending[message["tool_call_id"]] = message
            continue
        for call_id, output in pending.items():
            paired.append(output or {"role": "tool", "tool_call_id": call_id,
                                     "content": "Tool output unavailable after interrupted session."})
        pending = {call["id"]: None for call in message.get("tool_calls", [])}
        paired.append(message)
    for call_id, output in pending.items():
        paired.append(output or {"role": "tool", "tool_call_id": call_id,
                                 "content": "Tool output unavailable after interrupted session."})
    return paired


def read_events(events, max_bytes):
    files, state, checkpoint_at = {}, Checkpoint(messages=[]), -1
    for index, event in enumerate(events):
        if event["seq"] != index + 1:
            raise ValueError("History event sequence must be contiguous")
        if event["type"] == "context/checkpoint":
            state = Checkpoint.model_validate(event["data"])
            for name, encoded in state.files.items():
                file_name(name)
                if name in files:
                    raise ValueError("Duplicate checkpoint file")
                data = base64.b64decode(encoded, validate=True)
                if sum(map(len, files.values())) + len(data) > max_bytes:
                    raise ValueError("Checkpoint files exceed the storage limit")
                files[name] = data
            validate_state(state, files)
            checkpoint_at = index
    tail = events[checkpoint_at + 1:]
    degraded = any(event["type"] == "query/start" for event in tail)
    if degraded:
        first_query = next(index for index, event in enumerate(tail) if event["type"] == "query/start")
        state.messages.extend(legacy_messages(tail[first_query:]))
        # Old references must never accidentally resolve to newly allocated evidence.
        state.sequence = max(state.sequence, max((int(value) for event in tail for value in re.findall(
            r"\b(\d{4,12})-(?:github|gitcode|web|tool)", json.dumps(event["data"]),
        )), default=0))
        state.messages.append({"role": "user", "content": (
            "[History recovery: this older or interrupted part of the conversation has no native checkpoint. "
            "Its observations are retained, but saved tool references and images may be unavailable. "
            "Fetch original sources again when needed; do not assume missing evidence was recovered.]"
        )})
    validate_state(state, files)
    return state, files, degraded


def restore(agent, events, max_bytes):
    state, files, degraded = read_events(events, max_bytes)
    for name, data in files.items():
        agent.storage.write(name, data)
    agent.storage.sequence = state.sequence
    agent._history, agent._history_files = state, set(files)
    agent.messages = [agent.messages[0], *copy.deepcopy(state.messages)]
    for name, values in providers(agent):
        values.update(copy.deepcopy(state.providers.get(name, {})))
    agent.tool_results.saved = {key: SavedResult(value["artifact"], OffloadPolicy(**value["policy"]))
                                for key, value in state.saved.items()}
    messages = agent.messages[1:]
    agent.tool_results.uses = [ResultUse(
        **{key: value for key, value in use.items() if key not in {"message", "image_message"}},
        message=messages[use["message"]],
        image_message=messages[use["image_message"]] if use.get("image_message") is not None else None,
    ) for use in state.uses]
    agent.tool_results.user_query, agent.tool_results.tool_query = state.user_query, state.tool_query
    agent.context.query = state.user_query
    agent.context.synchronize(agent.messages, force=True)
    return degraded
