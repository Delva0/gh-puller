"""Contract tests for the canonical Agent event algebra."""

import asyncio
import json

import pytest

from gh_puller.agent.events import (
    DELTA_TYPES,
    EVENT_TYPES,
    EventBus,
    EventRecorder,
    _normalize_usage,
    fold_state,
    function_call_item,
    function_output_item,
    is_compact_event,
    is_event_type,
    new_event,
    reasoning_item,
    set_active_bus,
    text_message,
)


def _event(event_type: str, seq: int, **data) -> dict:
    return {**new_event(event_type, **data), "seq": seq, "session": "s"}


def test_event_families_keep_state_and_activity_independent() -> None:
    assert {
        "agent/set",
        "context/set",
        "context/append",
        "context/append/system",
        "context/append/user",
        "context/append/assistant",
        "context/append/tool",
    } <= EVENT_TYPES
    assert is_event_type("agent/set/mode")
    assert is_event_type("agent/set/model")
    assert not is_event_type("agent/set/mode/detail")
    assert not is_event_type("model/set")
    assert all(not is_compact_event(event_type) for event_type in DELTA_TYPES)
    assert is_compact_event("agent/set/custom")


def test_context_appends_atomic_responses_style_item_batches() -> None:
    system_item = text_message("system", "instructions")
    assistant_items = [
        reasoning_item("why"),
        text_message("assistant", "done"),
        function_call_item("c1", "read_file", {"path": "a.py"}),
    ]
    custom_item = {
        "type": "message",
        "role": "critic",
        "content": [{"type": "annotation", "value": 1}],
    }
    system = new_event("context/append/system", items=[system_item])
    assistant = new_event("context/append/assistant", items=assistant_items)
    custom = new_event("context/append", items=[custom_item])
    assert system["data"] == {"items": [system_item]}
    assert assistant["data"]["items"] == assistant_items
    assert json.loads(assistant_items[-1]["arguments"]) == {"path": "a.py"}
    assert custom["data"] == {"items": [custom_item]}


def test_model_response_enforces_one_inference_order() -> None:
    output = [
        reasoning_item("why"),
        text_message("assistant", "answer"),
        function_call_item("c1", "read", "{}"),
    ]
    event = new_event("model/response", requestId="r1", output=output)
    assert event["data"]["output"] == output
    with pytest.raises(ValueError, match="reasoning -> message -> function_call"):
        new_event("model/response", requestId="r1", output=[output[1], output[0]])
    with pytest.raises(ValueError, match="unsupported output item"):
        new_event(
            "model/response", requestId="r1",
            output=[function_output_item("c1", "result")],
        )


def test_usage_normalization_omits_empty_reports_and_maps_codex_fields() -> None:
    assert _normalize_usage({"input_tokens": 0, "output_tokens": 0}) is None
    assert _normalize_usage({
        "input_tokens": 20,
        "output_tokens": 5,
        "cached_input_tokens": 12,
        "cache_write_input_tokens": 3,
        "reasoning_output_tokens": 4,
    }) == {
        "input": 20,
        "output": 5,
        "cacheRead": 12,
        "cacheWrite": 3,
        "reasoning": 4,
    }


def test_fold_restores_agent_and_item_context_at_every_prefix() -> None:
    rules = text_message("system", "rules")
    old = text_message("user", "old")
    summary = text_message("system", "summary")
    note = {
        "type": "message",
        "role": "critic",
        "content": [{"type": "input_text", "text": "note"}],
    }
    events = [
        _event(
            "agent/set", 0, agent="custom",
            config={"mode": "default", "cwd": "/x"}, observedBy="adapter",
        ),
        _event("context/append/system", 1, items=[rules]),
        _event("turn/end", 2, outcome="unusual"),
        _event("context/append/user", 3, items=[old]),
        _event("model/request", 4, requestId="r1", model="m1"),
        _event("model/delta/text", 5, requestId="r1", index=0, text="ignored"),
        _event("context/set", 6, items=[summary]),
        _event("context/append", 7, items=[note]),
        _event("agent/set/mode", 8, mode="plan"),
    ]
    for end in range(len(events) + 1):
        fold_state(events[:end])
    assert fold_state(events[:4]) == {
        "agent": {"agent": "custom", "config": {"mode": "default", "cwd": "/x"}},
        "context": [rules, old],
    }
    assert fold_state(events) == {
        "agent": {"agent": "custom", "config": {"mode": "plan", "cwd": "/x"}},
        "context": [summary, note],
    }


@pytest.mark.asyncio
async def test_recorder_redacts_credentials_before_publication() -> None:
    events = []
    bus = EventBus()

    async def receive(event: dict) -> None:
        events.append(event)

    bus.add(receive)
    set_active_bus(bus)
    config = {
        "api_key": "sk-secret",
        "authKey": "auth-secret",
        "headers": {"Authorization": "Bearer secret"},
        "nested": {"api_token": "token-secret", "max_tokens": 10},
    }
    EventRecorder("s", agent="custom", config=config).start()
    await asyncio.sleep(0)
    observed = next(event for event in events if event["type"] == "agent/set")["data"]["config"]
    assert observed == {
        "api_key": "<redacted>",
        "authKey": "<redacted>",
        "headers": {"Authorization": "<redacted>"},
        "nested": {"api_token": "<redacted>", "max_tokens": 10},
    }
    assert config["api_key"] == "sk-secret"
    bus.shutdown()
    set_active_bus(None)


def test_payload_and_correlation_validation() -> None:
    with pytest.raises(TypeError, match="agent/set requires config"):
        new_event("agent/set", agent="x", config=[])
    with pytest.raises(ValueError, match="requires mode"):
        new_event("agent/set/mode", value="plan")
    with pytest.raises(TypeError, match="list items"):
        new_event("context/append/user", items="text")
    with pytest.raises(ValueError, match="requestId"):
        new_event("model/request")
    with pytest.raises(ValueError, match="callId"):
        new_event("tool/start", name="x", arguments={})
    with pytest.raises(TypeError, match="message requires role"):
        new_event(
            "model/response", requestId="r1",
            output=[{"type": "message", "content": []}],
        )
    with pytest.raises(ValueError, match="exactly one"):
        new_event("tool/end", callId="c1", result="ok", error={"type": "error"})
