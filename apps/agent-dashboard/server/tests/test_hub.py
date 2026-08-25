"""hub WS/HTTP 端到端测试(TestClient 直连 app,零网络出网)。

覆盖(事件溯源协议 v2,破坏性升级):
- index 会话列表(含 run_id/num_events/state);生产端 evt 广播给两个订阅查看端;
- subscribe:evt_ready{lastSeq} → 实时 evt 推送(带 seq);换会话替换订阅 + 断连清理;
- history:seq 升序分页(尾部/翻旧页/缺会话空页),磁盘+内存合并;
- ping→pong;GET /(与 /viewer)出 viewer HTML,其它路径 404;
- 重启种子:事件溯源 JSONL 进索引(running 保持 continuing),历史可回放(旧局限回归);
  旧 v1 聚合行/坏行文件跳过不崩。
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from gh_puller.agent import new_event

from hub import _Hub, create_app


def _evt(evt_type, session, *, seq, run_id=None, label="l", provider="claude",
         model="m", **data) -> dict:
    """测试用具:new_event 信封 + 会话属性(seq/run_id/label 显式,信封与 data 同值)。

    与适配器真实格式一致:会话属性同时落信封与会话属性载入的 session/start data。
    """
    return {**new_event(evt_type, **data), "session": session, "label": label,
            "provider": provider, "model": model, "run_id": run_id, "seq": seq}


def _user_evt(session, seq, text) -> dict:
    """常见 surface 事件便捷构造:user 文本消息。"""
    return _evt("user/message", session, seq=seq, turn=1, step=1,
                message={"role": "user", "content": [{"type": "text", "text": text}]},
                source={"kind": "user"}, surfaceOp="append")


def _until(ws, type_, limit=200) -> list[dict]:
    """收帧直到收到指定顶层 type(含);返回全部收到的帧。"""
    got = []
    for _ in range(limit):
        frame = json.loads(ws.receive_text())
        got.append(frame)
        if frame.get("type") == type_:
            return got
    raise AssertionError(f"未等到 {type_!r}: 收到 {len(got)} 帧")


@pytest.fixture
def client(tmp_path):
    """空 hub 的 app(可注入种子前的事件)。"""
    app = create_app(_Hub())
    with TestClient(app) as c:
        yield c


def test_index_subscribe_live_and_finalize(client):
    """生产端全流程 → 两个查看端:索引含 run_id/num_events;subscribe evt_ready(lastSeq)
    → live 广播;终态 completed 进索引。"""
    with client.websocket_connect("/ws") as producer:
        producer.send_text(json.dumps({"type": "evt", "event": _evt(
            "session/start", "s1", seq=0, run_id="chat:demo", label="chat:demo",
            provider="claude", model="")}, ensure_ascii=False))
        producer.send_text(json.dumps({"type": "evt", "event": _evt(
            "turn/start", "s1", seq=1, turn=1)}, ensure_ascii=False))
        producer.send_text(json.dumps({"type": "evt", "event": _evt(
            "step/start", "s1", seq=2, turn=1, step=1)}, ensure_ascii=False))
        producer.send_text(json.dumps({"type": "evt", "event": _user_evt("s1", 3, "hi")},
                                      ensure_ascii=False))

        with client.websocket_connect("/ws") as v1:
            v1.send_text(json.dumps({"type": "index"}, ensure_ascii=False))
            idx = json.loads(v1.receive_text())["sessions"]
            assert [s["session"] for s in idx] == ["s1"]
            assert idx[0]["state"] == "running"
            assert idx[0]["run_id"] == "chat:demo"
            assert idx[0]["num_events"] == 4

            # subscribe:登记后 evt_ready(lastSeq=3),live 无缝隙
            v1.send_text(json.dumps({"type": "subscribe", "session": "s1"}, ensure_ascii=False))
            ready = json.loads(v1.receive_text())
            assert ready["type"] == "evt_ready" and ready["lastSeq"] == 3

            with client.websocket_connect("/ws") as v2:
                v2.send_text(json.dumps({"type": "subscribe", "session": "s1"}, ensure_ascii=False))
                assert json.loads(v2.receive_text())["type"] == "evt_ready"

                # live:两查看端都收到新 chunk(带 seq)→ 终态
                producer.send_text(json.dumps({"type": "evt", "event": _evt(
                    "assistant/chunk", "s1", seq=4, turn=1, step=1,
                    chunk={"type": "text", "index": 0, "text": "世"})}, ensure_ascii=False))
                producer.send_text(json.dumps({"type": "evt", "event": _evt(
                    "session/end", "s1", seq=5, state="completed", ok=True,
                    duration_ms=10, text_chars=1, num_steps=1)}, ensure_ascii=False))
                for v in (v1, v2):
                    frames = [json.loads(v.receive_text()), json.loads(v.receive_text())]
                    assert [f["event"]["seq"] for f in frames if f["type"] == "evt"] == [4, 5]
                    assert frames[-1]["event"]["data"]["state"] == "completed"

        # 终态进索引
        with client.websocket_connect("/ws") as v3:
            v3.send_text(json.dumps({"type": "index"}, ensure_ascii=False))
            idx = json.loads(v3.receive_text())["sessions"]
            assert idx[0]["state"] == "completed"


def test_subscribe_missing_session_and_ping(client):
    with client.websocket_connect("/ws") as v:
        v.send_text(json.dumps({"type": "subscribe", "session": "nope"}, ensure_ascii=False))
        ready = json.loads(v.receive_text())
        assert ready == {"type": "evt_ready", "session": "nope", "lastSeq": None}
        v.send_text(json.dumps({"type": "ping"}, ensure_ascii=False))
        assert json.loads(v.receive_text())["type"] == "pong"


def test_history_pagination_tail_and_older(client):
    with client.websocket_connect("/ws") as producer:
        for i in range(10):
            producer.send_text(json.dumps({"type": "evt", "event": _evt(
                "assistant/chunk", "page1", seq=i, turn=1, step=1,
                chunk={"type": "text", "index": 0, "text": str(i)})}, ensure_ascii=False))
    with client.websocket_connect("/ws") as v:
        # 尾部页:最近 5 条(seq 5..9),翻旧分界 nextBeforeSeq=5
        v.send_text(json.dumps({"type": "history", "session": "page1", "max": 5}, ensure_ascii=False))
        page = json.loads(v.receive_text())
        assert [e["seq"] for e in page["events"]] == [5, 6, 7, 8, 9]
        assert page["hasMore"] is True and page["nextBeforeSeq"] == 5
        # 翻旧页:beforeSeq=5 之下最近 5 条(0..4),无更多
        v.send_text(json.dumps({"type": "history", "session": "page1", "beforeSeq": 5,
                                "max": 5}, ensure_ascii=False))
        page2 = json.loads(v.receive_text())
        assert [e["seq"] for e in page2["events"]] == [0, 1, 2, 3, 4]
        assert page2["hasMore"] is False and page2["nextBeforeSeq"] is None
        # 缺会话空页
        v.send_text(json.dumps({"type": "history", "session": "ghost"}, ensure_ascii=False))
        empty = json.loads(v.receive_text())
        assert empty["events"] == [] and empty["hasMore"] is False


def test_history_merges_disk_and_memory(tmp_path):
    """磁盘(经 seed)+ 内存 live 合并:同 seq 以内存为准,拼成完整 seq 序。"""
    (tmp_path / "sessions" / "completed").mkdir(parents=True)
    disk = [_evt("session/start", "merge1", seq=0, run_id="r",
                 label="l", provider="claude", model="m"),
            _user_evt("merge1", 1, "disk-msg")]
    (tmp_path / "sessions" / "completed" / "merge1.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in disk), encoding="utf-8")
    hub = _Hub()
    hub.seed(str(tmp_path))
    # live 续写(内存,仅 seq 2):与磁盘拼成 0..2 连续
    hub.sessions["merge1"].events[2] = _evt("assistant/chunk", "merge1", seq=2,
                                            turn=1, step=1,
                                            chunk={"type": "text", "index": 0, "text": "live"})
    app = create_app(hub)
    with TestClient(app) as c, c.websocket_connect("/ws") as v:
        v.send_text(json.dumps({"type": "history", "session": "merge1"}, ensure_ascii=False))
        page = json.loads(v.receive_text())
        assert [e["seq"] for e in page["events"]] == [0, 1, 2]


def test_get_serves_viewer_html():
    app = create_app(_Hub(), static_root=Path("does-not-exist"))
    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        assert "文件缺失" in c.get("/viewer").text
        assert c.get("/no-such-path").status_code == 404


def test_default_static_serves_viewer(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "agent_monitor_viewer.html").write_bytes(b"<html>viewer-pg</html>")
    app = create_app(_Hub(), static_root=static)
    with TestClient(app) as c:
        with c.websocket_connect("/ws") as v:
            v.send_text(json.dumps({"type": "ping"}, ensure_ascii=False))
            assert json.loads(v.receive_text())["type"] == "pong"
        assert c.get("/").status_code == 200
        assert "<html>viewer-pg</html>" in c.get("/").text
        assert c.get("/viewer").status_code == 200


def test_disk_seed_index_and_history(tmp_path):
    """重启种子(事件溯源格式):索引含 run_id/state,历史可回放 —— 旧局限回归。"""
    (tmp_path / "sessions" / "completed").mkdir(parents=True)
    (tmp_path / "sessions" / "running").mkdir()
    a = [
        _evt("session/start", "seed-a", seq=0, run_id=None, label="judge:llm",
             provider="openai", model="m"),
        _user_evt("seed-a", 1, "问"),
        _evt("assistant/message", "seed-a", seq=2, turn=1, step=1,
             message={"role": "assistant", "content": [{"type": "text", "text": "答"}]},
             surfaceOp="append"),
        _evt("session/end", "seed-a", seq=3, state="completed", ok=True,
             duration_ms=5, text_chars=1, num_steps=1),
    ]
    (tmp_path / "sessions" / "completed" / "seed-a.jsonl").write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in a), encoding="utf-8")
    r = _evt("session/start", "seed-r", seq=0, run_id="chat:demo", label="chat:demo",
             provider="claude", model="")
    (tmp_path / "sessions" / "running" / "seed-r.jsonl").write_text(
        json.dumps(r, ensure_ascii=False) + "\n", encoding="utf-8")

    hub = _Hub()
    hub.seed(str(tmp_path))
    by_session = {s["session"]: s for s in hub.index()}
    assert by_session["seed-a"]["state"] == "completed"
    assert by_session["seed-a"]["label"] == "judge:llm"
    assert by_session["seed-a"]["num_events"] == 4
    assert by_session["seed-r"]["state"] == "running"
    assert by_session["seed-r"]["run_id"] == "chat:demo"

    app = create_app(hub, static_root=tmp_path / "static")
    with TestClient(app) as c, c.websocket_connect("/ws") as v:
        v.send_text(json.dumps({"type": "history", "session": "seed-a"}, ensure_ascii=False))
        page = json.loads(v.receive_text())
        types_ = [e["type"] for e in page["events"]]
        assert types_ == ["session/start", "user/message", "assistant/message", "session/end"]
        assert page["hasMore"] is False


def test_seed_skips_old_format_and_corrupt_lines(tmp_path):
    """旧 v1 聚合行(无 seq/type)与坏行:整文件/半行跳过不崩;剩余会话照常。"""
    (tmp_path / "sessions" / "completed").mkdir(parents=True)
    old_format = [
        {"type": "session.start", "session": "old-a", "label": "l", "provider": "p",
         "model": "m", "state": "running", "ts": 1},
        {"type": "session.end", "state": "completed", "ts": 2},
    ]
    (tmp_path / "sessions" / "completed" / "old-a.jsonl").write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in old_format),
        encoding="utf-8")
    good = [_evt("session/start", "seed-b", seq=0, run_id=None, label="l",
                 provider="claude", model="")]
    bad_line = _evt("assistant/chunk", "seed-b", seq=1, turn=1, step=1,
                    chunk={"type": "text", "index": 0, "text": "ok"})
    (tmp_path / "sessions" / "completed" / "seed-b.jsonl").write_text(
        json.dumps(good[0], ensure_ascii=False) + "\n{broken\n"
        + json.dumps(bad_line, ensure_ascii=False) + "\n", encoding="utf-8")

    hub = _Hub()
    hub.seed(str(tmp_path))
    sessions = {s["session"]: s for s in hub.index()}
    assert "old-a" not in sessions  # 旧格式整文件跳过
    assert sessions["seed-b"]["num_events"] == 2  # 坏行跳过,其余载入
