"""Tests for the monitor's observable HTTP and WebSocket protocol."""

from fastapi.testclient import TestClient
from gh_puller.agent.events import new_event

from app import create_app
from hub import Hub


def _event(event_type: str, seq: int, session: str = "s", **data) -> dict:
    return {**new_event(event_type, **data), "session": session, "seq": seq}


def test_http_routes_serve_the_built_viewer(tmp_path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "agent_monitor_viewer.html").write_text("<html>monitor</html>", encoding="utf-8")

    with TestClient(create_app(Hub(), static_root=static)) as client:
        assert "monitor" in client.get("/").text
        assert client.get("/viewer").status_code == 200
        assert client.get("/missing").status_code == 404


def test_producer_events_flow_through_the_public_websocket_protocol() -> None:
    with TestClient(create_app(Hub())) as client, client.websocket_connect("/ws") as viewer:
        viewer.send_json({"type": "index"})
        assert viewer.receive_json() == {"type": "index", "sessions": []}

        with client.websocket_connect("/ws") as producer:
            producer.send_json(
                {
                    "type": "evts",
                    "events": [
                        _event("session/start", 0, label="demo"),
                        _event("agent/set", 1, agent="custom", config={"model": "m"}),
                        _event("context/append/user", 2, content=[{"type": "text", "text": "q"}]),
                    ],
                },
            )
            index = viewer.receive_json()
            assert index["type"] == "index"
            assert index["sessions"][0]["session"] == "s"

            viewer.send_json({"type": "subscribe", "session": "s"})
            assert viewer.receive_json() == {"type": "evt_ready", "session": "s", "lastSeq": 2}
            viewer.send_json({"type": "history", "session": "s", "max": 1000})
            history = viewer.receive_json()
            assert [event["seq"] for event in history["events"]] == [0, 1, 2]

            producer.send_json(
                {
                    "type": "evts",
                    "events": [
                        _event("model/request", 3, requestId="r1"),
                        _event("model/delta/text", 4, requestId="r1", index=0, text="a"),
                        _event("session/end", 5, outcome="completed", durationMs=1),
                    ],
                },
            )
            live = viewer.receive_json()
            assert live["type"] == "evts"
            assert [event["seq"] for event in live["events"]] == [3, 4, 5]
            assert viewer.receive_json()["sessions"][0]["state"] == "completed"

            viewer.send_json({"type": "history", "session": "s", "max": 1000})
            compact = viewer.receive_json()
            assert [event["seq"] for event in compact["events"]] == [0, 1, 2, 3, 5]
