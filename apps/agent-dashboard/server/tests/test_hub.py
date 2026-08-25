"""hub WS/HTTP 端到端测试(TestClient 直连 app,零网络出网)。

覆盖(原 tests/test_monitor_ws.py 全部场景):
- index 列表(含 state);生产端 evt 广播给两个订阅查看端;
- llm-subscribe:回放 LLM 流行(带 id)→ llm_ready → 实时追加(id 连续递增);
- 增量契约:block.delta 帧只携本块文本;原始事件帧全字段原样转发;
- evt-subscribe 回放+live+换会话替换订阅+断连清理;evt-replay 回放事件、ping→pong;
- GET /(与 /viewer)出 viewer HTML,其它路径 404;
- 重启种子:预写磁盘 sessions/{completed,running}/*.jsonl → hub 索引含 state,回放正确。
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gh_puller.agent import new_event
from hub import _Hub, create_app


def _evt(kind, session, *, seq, round_, **fields) -> dict:
    evt = {"session": session, "label": "l", "provider": "claude", "model": "m", "seq": seq, "round": round_}
    evt.update(fields)
    return new_event(kind, **evt)


def _until(ws, type_, limit=200) -> list[dict]:
    """收帧直到收到指定 type(含);返回全部收到的帧。

    llm 帧的 type 在 frame["line"]["type"] 层(顶层为 "llm"),一并匹配。
    """
    got = []
    for _ in range(limit):
        frame = json.loads(ws.receive_text())
        got.append(frame)
        if frame.get("type") == type_ or frame.get("line", {}).get("type") == type_:
            return got
    raise AssertionError(f"未等到 {type_!r}: 收到 {len(got)} 帧")


@pytest.fixture
def client(tmp_path):
    """空 hub 的 app(可注入种子前的事件)。"""
    app = create_app(_Hub())
    with TestClient(app) as c:
        yield c


def test_index_then_two_viewers_replay_and_live(client):
    """生产端全流程 → 两个查看端:索引空→有;回放带 id;新事件广播,id 连续;终态 completed。"""
    with client.websocket_connect("/ws") as producer:
        # 生产端:run.start + 第 0 轮 content 块增量
        producer.send_text(json.dumps({"type": "evt", "event": _evt(
            "run.start", "s1", seq=0, round_=0, label="chat:demo",
            provider="claude", model="", prompt_chars=9, n_messages=1)}, ensure_ascii=False))
        producer.send_text(json.dumps({"type": "evt", "event": _evt(
            "block.start", "s1", seq=1, round_=0, block_type="content")}, ensure_ascii=False))
        producer.send_text(json.dumps({"type": "evt", "event": _evt(
            "text.delta", "s1", seq=2, round_=0, text="你好")}, ensure_ascii=False))

        with client.websocket_connect("/ws") as v1:
            v1.send_text(json.dumps({"type": "index"}, ensure_ascii=False))
            idx = json.loads(v1.receive_text())["sessions"]
            assert [s["session"] for s in idx] == ["s1"]
            assert idx[0]["state"] == "running"

            # 回放:4 行(id 1..4)→ llm_ready
            v1.send_text(json.dumps({"type": "llm-subscribe", "session": "s1"}, ensure_ascii=False))
            replay = _until(v1, "llm_ready")
            assert [f["id"] for f in replay if f["type"] == "llm"] == [1, 2, 3, 4]
            assert [f["line"]["type"] for f in replay if f["type"] == "llm"] == [
                "session.start", "round.start", "block.start", "block.delta"]
            # 增量:delta 帧只携本块文本(未拼接累计)
            assert [f["line"]["text"] for f in replay
                    if f["type"] == "llm" and f["line"]["type"] == "block.delta"] == ["你好"]

            with client.websocket_connect("/ws") as v2:
                v2.send_text(json.dumps({"type": "llm-subscribe", "session": "s1"}, ensure_ascii=False))
                _until(v2, "llm_ready")

                # 实时:两查看端都收到 文本增量 → 块结束 → 终态
                producer.send_text(json.dumps({"type": "evt", "event": _evt(
                    "text.delta", "s1", seq=3, round_=0, text="世界")}, ensure_ascii=False))
                producer.send_text(json.dumps({"type": "evt", "event": _evt(
                    "block.stop", "s1", seq=4, round_=0, block_type="content")}, ensure_ascii=False))
                producer.send_text(json.dumps({"type": "evt", "event": _evt(
                    "run.end", "s1", seq=5, round_=0, ok=True, state="completed", num_rounds=1)}, ensure_ascii=False))

                for v in (v1, v2):
                    frames = _until(v, "session.end")
                    assert [f["id"] for f in frames if f["type"] == "llm"] == [5, 6, 7, 8]
                    assert [f["line"]["type"] for f in frames if f["type"] == "llm"] == [
                        "block.delta", "block.end", "round.end", "session.end"]
                    # 增量:live delta 只携新块文本,无拼接帧
                    assert [f["line"]["text"] for f in frames
                            if f["type"] == "llm" and f["line"]["type"] == "block.delta"] == ["世界"]
                    assert "你好世界" not in [f["line"].get("text", "")
                                              for f in frames if f["type"] == "llm"]
                    assert frames[-1]["line"]["state"] == "completed"

        # 终态进索引
        with client.websocket_connect("/ws") as v3:
            v3.send_text(json.dumps({"type": "index"}, ensure_ascii=False))
            idx = json.loads(v3.receive_text())["sessions"]
            assert idx[0]["state"] == "completed"


def test_evt_replay_and_ping(client):
    with client.websocket_connect("/ws") as producer:
        producer.send_text(json.dumps({"type": "evt", "event": _evt(
            "run.start", "ep1", seq=0, round_=0, label="wiki:x")}, ensure_ascii=False))
        producer.send_text(json.dumps({"type": "evt", "event": _evt(
            "text.delta", "ep1", seq=1, round_=0, text="x")}, ensure_ascii=False))
    with client.websocket_connect("/ws") as v:
        v.send_text(json.dumps({"type": "evt-replay", "session": "ep1"}, ensure_ascii=False))
        stop = _until(v, "evt_ready")
        assert [f["event"]["kind"] for f in stop if f["type"] == "evt"] == ["run.start", "text.delta"]
        v.send_text(json.dumps({"type": "ping"}, ensure_ascii=False))
        assert json.loads(v.receive_text())["type"] == "pong"


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
        assert c.get("/").status_code == 200
        assert "<html>viewer-pg</html>" in c.get("/").text
        assert c.get("/viewer").status_code == 200


def test_disk_seed_index_and_replay(tmp_path):
    """重启种子:磁盘 sessions/**/*.jsonl 进索引(running 保持 running),回放行 id 从 0 起。"""
    (tmp_path / "sessions" / "completed").mkdir(parents=True)
    (tmp_path / "sessions" / "running").mkdir()
    a = [
        {"type": "session.start", "session": "seed-a", "label": "judge:llm", "provider": "openai",
         "model": "m", "state": "running", "ts": 1},
        {"type": "round.start", "round": 0, "input_kind": "user", "ts": 2},
        {"type": "session.end", "state": "completed", "ts": 3},
    ]
    (tmp_path / "sessions" / "completed" / "seed-a.jsonl").write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in a), encoding="utf-8")
    r = {"type": "session.start", "session": "seed-r", "label": "chat:demo", "provider": "claude",
         "model": "", "state": "running", "ts": 1}
    (tmp_path / "sessions" / "running" / "seed-r.jsonl").write_text(
        json.dumps(r, ensure_ascii=False) + "\n", encoding="utf-8")

    hub = _Hub()
    hub.seed(str(tmp_path))
    by_session = {s["session"]: s for s in hub.index()}
    assert by_session["seed-a"]["state"] == "completed"
    assert by_session["seed-a"]["label"] == "judge:llm"
    assert by_session["seed-r"]["state"] == "running"
    assert by_session["seed-r"]["label"] == "chat:demo"

    app = create_app(hub, static_root=tmp_path / "static")
    with TestClient(app) as c, c.websocket_connect("/ws") as v:
        v.send_text(json.dumps({"type": "llm-subscribe", "session": "seed-a"}, ensure_ascii=False))
        frames = _until(v, "llm_ready")
        llms = [f for f in frames if f["type"] == "llm"]
        assert [f["id"] for f in llms] == [0, 1, 2]
        assert [f["line"]["type"] for f in llms] == ["session.start", "round.start", "session.end"]


def test_evt_subscribe_replay_live_and_replace():
    """evt-subscribe:回放 + evt_ready → live 推送 → 换会话替换订阅 → 断连清理。"""
    hub = _Hub()
    app = create_app(hub)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as producer:
            ev_run1 = _evt("run.start", "s1", seq=0, round_=0, label="chat:x")
            ev_delta = _evt("text.delta", "s1", seq=1, round_=0, text="你好")
            ev_run2 = _evt("run.start", "s2", seq=0, round_=0, label="wiki:y")
            for evt in (ev_run1, ev_delta, ev_run2):
                producer.send_text(json.dumps({"type": "evt", "event": evt}, ensure_ascii=False))

            with client.websocket_connect("/ws") as v:
                # 回放 s1 两个事件 → evt_ready(原始事件全字段原样转发)
                v.send_text(json.dumps({"type": "evt-subscribe", "session": "s1"}, ensure_ascii=False))
                replay = _until(v, "evt_ready")
                assert [f["event"] for f in replay if f["type"] == "evt"] == [ev_run1, ev_delta]

                # live:s1 新事件实时到达(ep 编号帧里无 evt_ready,直接收下一帧)
                ev_block = _evt("block.start", "s1", seq=2, round_=0, block_type="content")
                producer.send_text(json.dumps({"type": "evt", "event": ev_block}, ensure_ascii=False))
                frame = json.loads(v.receive_text())
                assert frame["type"] == "evt" and frame["event"] == ev_block

                # 换订阅至 s2:回放后仅收 s2 的 live(s1 新事件不达)
                v.send_text(json.dumps({"type": "evt-subscribe", "session": "s2"}, ensure_ascii=False))
                replay2 = _until(v, "evt_ready")
                assert [f["event"]["kind"] for f in replay2 if f["type"] == "evt"] == ["run.start"]
                producer.send_text(json.dumps({"type": "evt", "event": _evt(
                    "text.delta", "s1", seq=3, round_=0, text="x")}, ensure_ascii=False))
                producer.send_text(json.dumps({"type": "evt", "event": _evt(
                    "run.end", "s2", seq=1, round_=0, ok=True)}, ensure_ascii=False))
                frame = json.loads(v.receive_text())
                assert frame["event"]["kind"] == "run.end" and frame["event"]["session"] == "s2"

    # 断连后两套订阅集合均清理
    assert hub.sessions["s1"].subscribers == set()
    assert hub.sessions["s1"].evt_subscribers == set()
    assert hub.sessions["s2"].evt_subscribers == set()
