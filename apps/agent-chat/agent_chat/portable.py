"""Validate browser event envelopes and delegate observed-context recovery to the package."""

import uuid
from datetime import datetime

from gh_puller.agent.events import CONTEXT_APPEND_TYPES, new_event, text_message
from gh_puller.agents import AGENTS
from gh_puller.agents.context import message_items
from pydantic import BaseModel, Field, JsonValue


class HistoryEvent(BaseModel):
    seq: int = Field(gt=0)
    type: str = Field(max_length=100)
    at: str
    query_id: str | None
    data: dict[str, JsonValue]
    source_seq: int | None = None
    elapsed_ms: float | None = None
    session: str | None = None
    ts: float | None = None
    elapsedMs: float | None = None


def open_history(records, session_id):
    """Fork a resumable prefix and migrate the app's former concatenated lifetimes."""
    if not records:
        return []
    first = next((event for event in records if event["type"] == "session/start"), None)
    start = first or {**records[0], "type": "session/start", "data": {"label": session_id}, "query_id": None}
    result = [start]
    instance, identity, boundary = None, None, True
    for event in records:
        kind, data = event["type"], event["data"]
        if kind in {"session/start", "session/end"}:
            boundary = True
            continue
        if kind == "agent/set":
            if boundary or identity != data["agent"]:
                instance = data.get("instance") or uuid.uuid4().hex
            else:
                instance = data.get("instance", instance)
            identity, boundary = data["agent"], False
            result.append({**event, "data": {**data, "instance": instance}})
        else:
            result.append(event)
    return [{**event, "seq": index + 1, "session": session_id,
             "ts": event.get("ts", datetime.fromisoformat(event["at"]).timestamp()),
             "elapsedMs": event.get("elapsedMs", event.get("elapsed_ms", 0))}
            for index, event in enumerate(result)]


def load_history(session, events):
    records = [event.model_dump(exclude_none=True) | {"query_id": event.query_id} for event in events]
    context = CONTEXT_APPEND_TYPES | {"context/set"}
    for index, event in enumerate(records):
        if event["seq"] != index + 1:
            raise ValueError("History event sequence must be contiguous")
        if event["type"] in context:
            new_event(event["type"], **event["data"])
    degraded = bool(records) and not any(event["type"] in context for event in records)
    if degraded:
        # Old app exports omitted canonical Context. Migrate observations, never private tool files.
        items = []
        for event in records:
            kind, data = event["type"], event["data"]
            if kind == "context/checkpoint":
                items = [item for message in data["messages"] if message["role"] != "system"
                         for item in message_items(message, [])]
            elif kind == "query/start":
                items.append(text_message("user", data["prompt"]))
            elif kind == "query/end" and data.get("answer"):
                answer = text_message("assistant", data["answer"])
                if not items or items[-1] != answer:
                    items.append(answer)
        migrated = {**records[-1], "seq": len(records) + 1, "type": "context/set", "data": {"items": items}}
        new_event("context/set", items=items)
        records.append(migrated)
    records = open_history(records, session.id)
    AGENTS[session.kind].validate_events(records, max_bytes=session.server.storage_bytes)
    session.history = records
    for event in records:
        session.write_event(session.scrubber.clean(event))
    if records:
        session.recorder.resume(records)
    return degraded


def restore(session):
    session.agent.load_events(session.history, max_bytes=session.server.storage_bytes)
    session.history = []
