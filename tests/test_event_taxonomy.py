"""Test event-envelope construction at the Python event-model boundary."""

import pytest

from gh_puller.agent.events import new_event, truncate


def test_new_event_envelope_and_jsonable():
    event = new_event(
        "user/message",
        message={"role": "user", "content": [{"type": "text", "text": "hi"}]},
        source={"kind": "user"},
        surfaceOp="append",
    )
    assert event["type"] == "user/message"
    assert event["id"].startswith("e-")
    assert isinstance(event["ts"], float)
    assert event["data"]["message"]["role"] == "user"
    assert "seq" not in event
    with pytest.raises(ValueError, match="未知事件 type"):
        new_event("nope")
    with pytest.raises(TypeError):
        new_event("session/start", meta={"bad": {1}})


def test_new_event_surface_validation():
    with pytest.raises(ValueError, match="缺 message 字段"):
        new_event("user/message", source={"kind": "user"}, surfaceOp="append")
    with pytest.raises(ValueError, match="缺合法 surfaceOp"):
        new_event("assistant/message", message={"role": "assistant", "content": []})
    with pytest.raises(ValueError, match="缺合法 surfaceOp"):
        new_event(
            "assistant/message",
            message={"role": "assistant", "content": []},
            surfaceOp="replace",
        )


def test_new_event_replace_op_and_ignorable():
    event = new_event(
        "user/message",
        message={"role": "user", "content": [{"type": "text", "text": "新"}]},
        source={"kind": "context", "label": "注记"},
        surfaceOp={"op": "replace", "start": 3, "end": 3},
    )
    assert event["data"]["surfaceOp"] == {"op": "replace", "start": 3, "end": 3}
    assert new_event("config/init", config={}).get("ignorable") is True
    assert "ignorable" not in new_event("session/start", label="l")


def test_truncate():
    assert truncate(None, 40) == (0, "")
    assert truncate("", 40) == (0, "")
    assert truncate("abc", 40) == (3, "abc")
    assert truncate("abcdef", 3) == (6, "abc…")
