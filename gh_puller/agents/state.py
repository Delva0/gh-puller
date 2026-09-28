"""Record and replay CommonAgent-owned memory in the canonical event stream.

``search/state`` replaces changed memory fields; ``search/artifact`` records immutable
tool bytes once. Both are private to the current search Agent identity. Foreign agents
read only the shared Context vocabulary. Recovery restores data at an event prefix,
not Python stacks, HTTP connections or external process execution.
"""

import base64
import copy
import json
import re
from dataclasses import asdict
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ..agent.events import DELTA_TYPES, EventRecorder, _ensure_maybe_bus, fold_state
from ..tools.githost_api_utils import APIProvider
from ..tools.tool_bash import DockerBashTools
from ..tools.tool_offload import OffloadPolicy, ResultUse, SavedResult
from ..tools.tool_web import WebTools
from .context import context_messages


class Memory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, JsonValue]] = Field(default_factory=list)
    sequence: int = Field(default=0, ge=0)
    providers: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    saved: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    uses: list[dict[str, JsonValue]] = Field(default_factory=list)
    user_query: int = Field(default=0, ge=0)
    tool_query: int = Field(default=0, ge=0)
    query: int = Field(default=0, ge=0)
    step: int = Field(default=0, ge=0)
    completed: list[tuple[int, int]] = Field(default_factory=list)
    answer: str = ""
    early_answers: list[dict[str, JsonValue]] = Field(default_factory=list)


def providers(agent):
    return {agent.tool_registry.registered_names[name]: provider
            for name, provider in agent.tool_registry.providers.items()}


def provider_state(provider, clock_origin):
    if isinstance(provider, APIProvider):
        api = provider.api
        return {"responses": api.responses, "reads": api._reads.responses,
                **{key: getattr(api, key) for key in ("unavailable", "cooldowns", "cooldown") if hasattr(api, key)},
                "resource_names": [[list(key), value] for key, value in getattr(api, "resource_names", {}).items()]}
    if isinstance(provider, WebTools):
        return {"resources": provider.resources, **{key: getattr(provider, key) + clock_origin
                if getattr(provider, key) else 0 for key in ("next_search", "search_cooldown")}}
    if isinstance(provider, DockerBashTools):
        return {**provider.save(), "connection": provider.sandbox.identity}
    return {}


def snapshot(agent):
    messages = agent.messages[1:]
    positions = {id(message): index for index, message in enumerate(messages)}
    return Memory(
        messages=messages, sequence=agent.storage.sequence,
        providers={name: provider_state(provider, agent._clock_origin) for name, provider in providers(agent).items()},
        saved={key: asdict(value) for key, value in agent.tool_results.saved.items()},
        uses=[{"tool_call_id": use.tool_call_id, "message": positions[id(use.message)],
               "image_message": positions.get(id(use.image_message)), "user_query": use.user_query,
               "tool_query": use.tool_query, "seen": use.seen}
              for use in agent.tool_results.uses if id(use.message) in positions],
        user_query=agent.tool_results.user_query, tool_query=agent.tool_results.tool_query,
        query=agent.context.query, step=agent.context.step, completed=sorted(agent.completed_steps),
        answer=getattr(agent, "final_answer", ""), early_answers=agent.early_answers.answers,
    ).model_dump(mode="json")


class MemoryRecorder(EventRecorder):
    """Observe state changes at event boundaries without changing the base Agent protocol."""

    def __init__(self, owner, recorder):
        super().__init__(recorder.session, agent=recorder.agent, config=recorder.config,
                         label=recorder.label, run_id=recorder.run_id)
        self.owner, self.previous = owner, {}

    def capture(self):
        bus = _ensure_maybe_bus()
        if not self.owner._state_ready or bus is None or not bus.enabled:
            return
        state = snapshot(self.owner)
        changed = {key: value for key, value in state.items() if self.previous.get(key) != value}
        if changed:
            super().event("search/state", version=1, agent=self.agent, values=changed)
            self.previous = copy.deepcopy(state)

    def event(self, event_type, **data):
        if event_type not in DELTA_TYPES:
            self.capture()
        return super().event(event_type, **data)

    def artifact(self, name):
        bus = _ensure_maybe_bus()
        if bus is not None and bus.enabled:
            self.capture()
            super().event("search/artifact", version=1, agent=self.agent, name=name,
                          content=base64.b64encode(self.owner.storage.read(name)).decode())


def file_name(value):
    path = PurePosixPath(value)
    if not value or len(value) > 256 or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("Invalid observed file path")
    return value


def validate_memory(state, files):
    """Check every reference that a restored provider can dereference as a local file."""
    def reference(name):
        if file_name(name) not in files:
            raise ValueError("Observed evidence file is missing")

    for provider in state.providers.values():
        for key in ("responses", "resources", "reads"):
            for metadata in provider.get(key, {}).values():
                reference(metadata["body_file"])
    for saved in state.saved.values():
        reference(saved["artifact"])
        OffloadPolicy(**saved["policy"])
        for part in json.loads(files[saved["artifact"]]).get("image_attachments", []):
            if part.get("type") == "input_image":
                reference(part["image_url"])
            elif part.get("type") == "input_file":
                reference(part["file_path"])
    for message in state.messages:
        if message.get("role") not in {"user", "assistant", "tool"}:
            raise ValueError("Invalid native message role")
    for use in state.uses:
        index, image = use["message"], use.get("image_message")
        if (type(index) is not int or not 0 <= index < len(state.messages)
                or use["tool_call_id"] not in state.saved
                or state.messages[index].get("tool_call_id") != use["tool_call_id"]
                or (image is not None and (type(image) is not int or not 0 <= image < len(state.messages)))):
            raise ValueError("Invalid retained result")


def read_events(events, identity, max_bytes):
    """Fold only the latest Agent's own private events, never resurrect an older Agent's memory."""
    latest = max((i for i, event in enumerate(events) if event["type"] == "agent/set"), default=-1)
    source = events[latest]["data"]["agent"] if latest >= 0 else None
    values, files = {}, {}
    for event in events[latest + 1:] if source == identity else []:
        kind, data = event["type"], event["data"]
        if kind in {"search/state", "search/artifact"}:
            if data["agent"] != source or data["version"] != 1:
                raise ValueError("Unsupported search memory event")
            if kind == "search/state":
                values.update(data["values"])
            else:
                name = file_name(data["name"])
                body = base64.b64decode(data["content"], validate=True)
                if name in files or sum(map(len, files.values())) + len(body) > max_bytes:
                    raise ValueError("Duplicate or oversized observed file")
                files[name] = body
    if source == identity and values:
        state = Memory.model_validate(values)
        validate_memory(state, files)
        return state, files
    context = fold_state(events)["context"]
    sequence = max(map(int, re.findall(r"\b(\d{4,12})-(?:github|gitcode|web|tool)", json.dumps(context))), default=0)
    return Memory(messages=context_messages(context), sequence=sequence), {}


def restore(agent, events, max_bytes):
    state, files = read_events(events, agent.agent, max_bytes)
    recorder = agent._require_event_recorder()
    agent._state_ready = False
    try:
        for name, data in files.items():
            agent.storage.write(name, data)
            recorder.artifact(name)
        agent.storage.sequence = state.sequence
        agent.messages = [agent.messages[0], *copy.deepcopy(state.messages)]
        for name, provider in providers(agent).items():
            saved = copy.deepcopy(state.providers.get(name, {}))
            if isinstance(provider, APIProvider) and saved:
                provider.api.responses.update(saved.pop("responses"))
                provider.api._reads.responses.update(saved.pop("reads"))
                names = saved.pop("resource_names")
                if hasattr(provider.api, "resource_names"):
                    provider.api.resource_names = {tuple(key): value for key, value in names}
                for key, value in saved.items():
                    setattr(provider.api, key, value)
            elif isinstance(provider, WebTools) and saved:
                provider.resources.update(saved["resources"])
                for key in ("next_search", "search_cooldown"):
                    setattr(provider, key, saved[key] - agent._clock_origin if saved[key] else 0)
            elif isinstance(provider, DockerBashTools) and saved.get("connection") == provider.sandbox.identity:
                provider.restore(saved)
        agent.tool_results.saved = {key: SavedResult(value["artifact"], OffloadPolicy(**value["policy"]))
                                    for key, value in state.saved.items()}
        messages = agent.messages[1:]
        agent.tool_results.uses = [ResultUse(
            **{key: value for key, value in use.items() if key not in {"message", "image_message"}},
            message=messages[use["message"]],
            image_message=messages[use["image_message"]] if use.get("image_message") is not None else None,
        ) for use in state.uses]
        agent.tool_results.user_query, agent.tool_results.tool_query = state.user_query, state.tool_query
        agent.context.query, agent.context.step = state.query, state.step
        agent.completed_steps, agent.final_answer = set(state.completed), state.answer
        agent.early_answers.answers = copy.deepcopy(state.early_answers)
    finally:
        agent._state_ready = True
    recorder.previous = {}
    agent.context.synchronize(agent.messages, force=True)
