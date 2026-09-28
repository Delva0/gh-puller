"""Validate browser event envelopes and delegate observed-context recovery to the package."""

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
    AGENTS[session.kind].validate_events(records, max_bytes=session.server.storage_bytes)
    session.history = records
    for event in records:
        session.write_event(session.scrubber.clean(event))
    return degraded


def restore(session):
    session.agent.load_events(session.history, max_bytes=session.server.storage_bytes)
    session.history = []
