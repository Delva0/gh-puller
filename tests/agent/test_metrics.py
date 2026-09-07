"""Verify conservative measurements derived from canonical Agent logs."""

import json

import pytest

from gh_puller.agent.metrics import read_events, summarize


def event(kind, seq, elapsed, **data):
    return {"ts": 1_000 + elapsed / 1000, "elapsedMs": elapsed, "session": "s",
            "seq": seq, "type": kind, "data": data}


def complete_log():
    return [
        event("session/start", 0, 0, label="run"),
        event("turn/start", 1, 1),
        event("model/request", 2, 2, requestId="r1", model="configured", requestSha256="abc"),
        event("model/delta/reasoning", 3, 5, requestId="r1", index=0, text="private"),
        event("model/delta/text", 4, 8, requestId="r1", index=1, text="answer"),
        event("model/response", 5, 12, requestId="r1", model="served", output=[], stopReason="stop",
              usage={"input": 10, "output": 2}, rawUsage={"prompt_tokens": 10, "completion_tokens": 2}),
        event("tool/start", 6, 13, callId="c1", name="Read", arguments={"path": "secret.py"}),
        event("tool/end", 7, 15, callId="c1", error={"type": "IOError", "message": "private"}),
        event("turn/end", 8, 16, outcome="completed"),
        event("session/end", 9, 18, outcome="completed", reasonCode="completed", durationMs=18),
    ]


def test_summary_uses_monotonic_boundaries_without_copying_bodies() -> None:
    result = summarize(complete_log())
    assert result == {
        "session": "s", "outcome": "completed", "reason_code": "completed", "events_complete": True,
        "seconds": 0.018, "turns": [{"start": 1, "seconds": 0.015, "outcome": "completed"}],
        "requests": [{
            "requestId": "r1", "start": 2, "seconds": 0.01, "completed": True, "model": "served",
            "request_sha256": "abc", "usage": {"input": 10, "output": 2},
            "raw_usage": {"prompt_tokens": 10, "completion_tokens": 2}, "stop_reason": "stop",
            "delta_count": 2, "first_delta_seconds": 0.003, "max_delta_gap_seconds": 0.003, "error": None,
        }],
        "tools": [{"callId": "c1", "start": 13, "seconds": 0.002, "completed": True,
                   "name": "Read", "error": True}],
        "usage": {"input": 10, "output": 2}, "model_seconds": 0.01, "tool_seconds": 0.002,
    }
    assert "private" not in json.dumps(result) and "secret.py" not in json.dumps(result)


def test_incomplete_log_keeps_unknowns_unknown() -> None:
    events = complete_log()
    events.pop(4)
    events.pop(4)
    result = summarize(events)
    assert result["events_complete"] is False
    assert result["requests"][0]["completed"] is False
    assert result["requests"][0]["seconds"] is None
    assert result["requests"][0]["first_delta_seconds"] is None
    assert result["model_seconds"] is None and result["usage"] == {}


def test_failed_request_has_a_terminal_duration_without_usage() -> None:
    events = complete_log()
    events[5] = event("model/error", 5, 12, requestId="r1",
                      error={"type": "ReadTimeout", "message": "private"})
    result = summarize(events)
    request, = result["requests"]
    assert request["completed"] is False and request["error"] == "ReadTimeout"
    assert request["seconds"] == result["model_seconds"] == 0.01
    assert result["usage"] == {} and "private" not in json.dumps(result)


def test_summary_rejects_mixed_sessions() -> None:
    events = complete_log()
    events[-1]["session"] = "other"
    with pytest.raises(ValueError, match="one observation session"):
        summarize(events)


def test_jsonl_reader_preserves_order(tmp_path) -> None:
    events = complete_log()[:2]
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    assert list(read_events(path)) == events
