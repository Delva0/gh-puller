"""Share event-capture assertions across local Agent adapter tests."""

import asyncio

from gh_puller import agent
from gh_puller.agent import sinks
from gh_puller.agent.events import fold_state, new_event


async def capture(tmp_path) -> list[dict]:
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    events: list[dict] = []
    sinks.ensure_bus().add(receiver(events))
    return events


def receiver(target: list[dict]):
    async def receive(event: dict) -> None:
        target.append(event)

    return receive


async def settle() -> None:
    await asyncio.sleep(0.03)


async def collect(stream) -> str:
    return "".join([part async for part in stream])


def event(event_type: str, seq: int, session: str = "s", **data) -> dict:
    return {**new_event(event_type, **data), "seq": seq, "session": session}


def context_labels(context: list[dict]) -> list[str]:
    return [item.get("role", item["type"]) for item in context]


def context_at_requests(events: list[dict]) -> list[list[str]]:
    return [
        context_labels(fold_state(events[:index])["context"])
        for index, item in enumerate(events)
        if item["type"] == "model/request"
    ]


def assert_inferences(events: list[dict]) -> None:
    requests = [
        item["data"]["requestId"]
        for item in events
        if item["type"] == "model/request"
    ]
    responses = [item for item in events if item["type"] == "model/response"]
    assert [item["data"]["requestId"] for item in responses] == requests
    for response in responses:
        if not response["data"]["output"]:
            continue
        index = events.index(response)
        commit = events[index + 1]
        assert commit["type"] == "context/append/assistant"
        assert commit["data"]["items"] == response["data"]["output"]
