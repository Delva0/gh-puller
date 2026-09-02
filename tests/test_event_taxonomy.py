"""Contract tests for the canonical agent event algebra."""

import pytest

from gh_puller.agent.events import DELTA_TYPES, NON_STREAM_TYPES, TAXONOMY, fold_request_state, new_event


def _event(event_type: str, seq: int, **data) -> dict:
    return {**new_event(event_type, **data), "seq": seq, "session": "s"}


def test_taxonomy_separates_state_activity_and_markers() -> None:
    assert {
        "model/set", "context/set", "context/append",
        "context/append/user", "context/append/assistant", "context/append/tool",
    } <= TAXONOMY
    assert "header/set" not in TAXONOMY
    assert {
        "model/request", "model/response", "tool/start", "tool/end",
        "turn/start", "turn/end", "step/start", "step/end",
    } <= TAXONOMY
    assert NON_STREAM_TYPES == TAXONOMY - DELTA_TYPES


def test_message_shapes_are_native_and_extensible() -> None:
    specialized = new_event(
        "context/append/assistant",
        content=[{"type": "tool_call", "callId": "c1", "name": "read_file",
                  "arguments": {"path": "a.py"}}],
    )
    generic = new_event(
        "context/append", role="critic",
        content=[{"type": "annotation", "value": 1}],
    )
    assert specialized["data"]["content"][0]["arguments"] == {"path": "a.py"}
    assert generic["data"]["role"] == "critic"


@pytest.mark.parametrize("event_type", [
    "context/append/user", "context/append/assistant", "context/append/tool",
])
def test_specialized_append_derives_role(event_type: str) -> None:
    with pytest.raises(ValueError, match="derives role"):
        new_event(event_type, role="user", content=[])


def test_state_fold_ignores_activity_and_marker_placement() -> None:
    events = [
        _event("turn/end", 0, outcome="unusual"),
        _event("model/set", 1, model="m1", provider="p", parameters={}),
        _event("context/set", 2, messages=[{
            "role": "system", "content": [
                {"type": "text", "text": "instructions"},
                {"type": "tool_definition", "name": "read", "description": "",
                 "inputSchema": {}},
            ],
        }]),
        _event("context/append/user", 3, content=[{"type": "text", "text": "old"}]),
        _event("model/request", 4, requestId="r1"),
        _event("model/delta/text", 5, requestId="r1", index=0, text="ignored"),
        _event("context/set", 6, messages=[
            {"role": "system", "content": [
                {"type": "tool_definition", "name": "read", "description": "",
                 "inputSchema": {}},
            ]},
            {"role": "assistant", "content": [{"type": "text", "text": "summary"}]},
        ]),
        _event("context/append", 7, role="critic",
               content=[{"type": "text", "text": "note"}]),
        _event("model/set", 8, model="m2", parameters={"temperature": 0}),
    ]
    state = fold_request_state(events)
    assert state["model"] == {"model": "m2", "parameters": {"temperature": 0}}
    assert state["context"][0]["content"][0]["type"] == "tool_definition"
    assert [message["role"] for message in state["context"]] == [
        "system", "assistant", "critic",
    ]


def test_content_and_correlation_validation() -> None:
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
