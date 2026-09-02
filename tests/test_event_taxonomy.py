"""Contract tests for the canonical Agent event algebra."""

import pytest

from gh_puller.agent.events import (
    DELTA_TYPES,
    EVENT_TYPES,
    fold_state,
    is_compact_event,
    is_event_type,
    new_event,
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


def test_message_shapes_are_native_and_extensible() -> None:
    system = new_event(
        "context/append/system",
        content=[{"type": "text", "text": "instructions"}],
    )
    assistant = new_event(
        "context/append/assistant",
        content=[{
            "type": "tool_call",
            "callId": "c1",
            "name": "read_file",
            "arguments": {"path": "a.py"},
        }],
    )
    custom = new_event(
        "context/append",
        role="critic",
        content=[{"type": "annotation", "value": 1}],
    )
    assert system["data"]["content"][0]["text"] == "instructions"
    assert assistant["data"]["content"][0]["arguments"] == {"path": "a.py"}
    assert custom["data"]["role"] == "critic"


@pytest.mark.parametrize("role", ["system", "user", "assistant", "tool"])
def test_specialized_append_derives_role(role: str) -> None:
    with pytest.raises(ValueError, match="derives role"):
        new_event(f"context/append/{role}", role=role, content=[])


def test_fold_restores_agent_and_context_at_every_prefix() -> None:
    events = [
        _event(
            "agent/set", 0, agent="custom",
            config={"mode": "default", "cwd": "/x"}, observedBy="adapter",
        ),
        _event("context/append/system", 1, content=[{"type": "text", "text": "rules"}]),
        _event("turn/end", 2, outcome="unusual"),
        _event("context/append/user", 3, content=[{"type": "text", "text": "old"}]),
        _event("model/request", 4, requestId="r1", model="m1"),
        _event("model/delta/text", 5, requestId="r1", index=0, text="ignored"),
        _event("context/set", 6, messages=[
            {"role": "system", "content": [{"type": "text", "text": "summary"}]},
        ]),
        _event("context/append", 7, role="critic", content=[{"type": "text", "text": "note"}]),
        _event("agent/set/mode", 8, mode="plan"),
    ]
    assert fold_state(events[:4]) == {
        "agent": {"agent": "custom", "config": {"mode": "default", "cwd": "/x"}},
        "context": [
            {"role": "system", "content": [{"type": "text", "text": "rules"}]},
            {"role": "user", "content": [{"type": "text", "text": "old"}]},
        ],
    }
    assert fold_state(events) == {
        "agent": {"agent": "custom", "config": {"mode": "plan", "cwd": "/x"}},
        "context": [
            {"role": "system", "content": [{"type": "text", "text": "summary"}]},
            {"role": "critic", "content": [{"type": "text", "text": "note"}]},
        ],
    }


def test_payload_and_correlation_validation() -> None:
    with pytest.raises(TypeError, match="agent/set requires config"):
        new_event("agent/set", agent="x", config=[])
    with pytest.raises(ValueError, match="requires mode"):
        new_event("agent/set/mode", value="plan")
    with pytest.raises(TypeError, match="list content"):
        new_event("context/append/user", content="text")
    with pytest.raises(ValueError, match="requestId"):
        new_event("model/request")
    with pytest.raises(ValueError, match="callId"):
        new_event("tool/start", name="x", arguments={})
    with pytest.raises(TypeError, match="message role"):
        new_event("model/response", requestId="r1", message={"content": []})
    with pytest.raises(ValueError, match="exactly one"):
        new_event("tool/end", callId="c1", result="ok", error={"type": "error"})
