"""Validate browser event envelopes and delegate observed-context recovery to the package."""

from gh_puller.agents import AGENTS
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
    degraded = AGENTS[session.kind].validate_events(records, max_bytes=session.server.storage_bytes)
    session.history = records
    for event in records:
        session.write_event(session.scrubber.clean(event))
    return degraded


def snapshot(session):
    if session.agent is not None:
        return session.scrubber.clean(session.agent.export_context())
    return None


def restore(session):
    session.agent.load_events(session.history, max_bytes=session.server.storage_bytes)
    session.history = []
