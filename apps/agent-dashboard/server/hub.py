"""Agent 监控 Web/WS hub(FastAPI 端点层)。

生产端是 gh_puller.agent 的 WsSink(经 AGENT_MONITOR_WEBUI_URL 接入,主动向 /ws 推
{"type":"evt","event":...});浏览器查看端经同一端点订阅会话。GET / 与 /viewer
直接出构建好的单文件 viewer(agent_monitor_viewer.html);hub 只持内存状态
(每会话事件按 seq 索引;完备真源是 FileSink 磁盘 JSONL —— 启动种子加载后,
历史查看不再为空),写盘是 FileSink 的事,重启 hub 列表与历史均在。

协议:一连接一角色,首帧定角色(evt → 生产端,其余 → 查看端);查看端帧:
- index → {type:"index", sessions:[{session, run_id, label, provider, model, state,
  ts, last_ts, num_events}]}(last_ts 降序);
- history {session, beforeSeq?, max?} → {type:"history", session, events, hasMore,
  nextBeforeSeq}:磁盘+内存合并的 seq 升序页(beforeSeq 缺省读尾部;nextBeforeSeq
  为 oldest in-page,客户端以此翻旧页);
- subscribe {session} → {type:"evt_ready", session, lastSeq} 后实时
  {type:"evt","event":...} 推送(单订阅视图:一连接只盯一会话);
- ping → pong。
live 帧与生产端同构(带 seq),客户端按 seq 去重/接缝。
"""

import asyncio
import json
import sys
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from gh_puller import envs

_STATE_DIRS = ("running", "completed", "aborted")


def _log(msg: str) -> None:
    print(f"[agent-monitor] {msg}", file=sys.stderr, flush=True)


class _Session:
    """hub 内存中的单会话状态:seq 索引事件(完备真源在磁盘 JSONL)。"""

    def __init__(self, session: str, label: str = "", provider: str = "", model: str = "",
                 run_id: str | None = None):
        self.session = session
        self.label = label or session
        self.provider = provider
        self.model = model
        self.run_id = run_id
        self.state = "running"
        self.ts = time.time()
        self.last_ts = self.ts
        self.events: dict[int, dict] = {}  # seq → 事件(feed 实时写入;种子时载入)
        self.subscribers: set = set()  # live evt 订阅(查看端;单视图语义)


class _Hub:
    """hub 内存状态:磁盘种子 + live 事件接收 + 按会话订阅广播。"""

    def __init__(self):
        self.sessions: dict[str, _Session] = {}
        self.root: str | None = None  # AGENT_MONITOR_DIR(history 合并磁盘用)

    def seed(self, root: str | None) -> None:
        """启动种子:扫描 sessions/{running,completed,aborted}/*.jsonl 载入事件溯源格式。

        旧(聚合 LLM 行)格式 / 坏行不可折叠 → 跳过并日志(不影响其余会话);
        running 会话保持 running,live 续写经 feed(同 seq 覆盖,天然去重)。
        """
        if not root:
            return
        self.root = root
        base = Path(root) / "sessions"
        for state in _STATE_DIRS:
            for path in sorted((base / state).glob("*.jsonl")):
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except Exception as exc:
                    _log(f"hub 种子跳过 {path.name}: 读取失败 {exc}")
                    continue
                events: dict[int, dict] = {}
                head: dict | None = None
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        evt = json.loads(line)
                    except Exception:
                        _log(f"hub 种子跳过半行 {path.name}: 不可解析")
                        continue
                    if head is None and evt.get("type") == "session/start":
                        head = evt
                    if "seq" not in evt or "type" not in evt:
                        continue  # 旧 v1 聚合行:无 seq/type,整体不可折叠
                    events[int(evt["seq"])] = evt
                if head is None or not events:
                    _log(f"hub 种子跳过 {path.name}: 非事件溯源格式")
                    continue
                d = head.get("data") or {}
                sess = _Session(path.stem, head.get("label") or d.get("label"),
                                head.get("provider") or d.get("provider"),
                                head.get("model") or d.get("model"),
                                run_id=head.get("run_id") or d.get("run_id"))
                sess.state = state
                sess.ts = head.get("ts") or sess.ts
                sess.last_ts = max((e.get("ts") or sess.ts) for e in events.values())
                sess.events = events
                self.sessions[path.stem] = sess

    def index(self) -> list[dict]:
        """会话索引(最近更新在前):供左列表渲染。"""
        return sorted(
            (
                {"session": s.session, "run_id": s.run_id, "label": s.label,
                 "provider": s.provider, "model": s.model, "state": s.state,
                 "ts": s.ts, "last_ts": s.last_ts, "num_events": len(s.events)}
                for s in self.sessions.values()
            ),
            key=lambda x: x["last_ts"],
            reverse=True,
        )

    async def _push(self, subs: set, payload: str) -> None:
        """向订阅集合逐端发一帧;发送失败记日志并剔除该端(不中断其余端)。"""
        for sub in list(subs):
            try:
                await sub.send_text(payload)
            except Exception as exc:
                _log(f"hub 订阅端断连,移除订阅: {type(exc).__name__}: {exc}")
                subs.discard(sub)

    def _session_events(self, sess: _Session) -> dict[int, dict]:
        """磁盘 + 内存合并事件(seq 键;内存更新,覆盖磁盘同 seq —— live 优先)。"""
        merged: dict[int, dict] = {}
        if self.root:
            base = Path(self.root) / "sessions"
            for cand in (
                base / "running" / f"{sess.session}.jsonl",
                base / "completed" / f"{sess.session}.jsonl",
                base / "aborted" / f"{sess.session}.jsonl",
            ):
                if not cand.exists():
                    continue
                for line in cand.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not line.strip():
                        continue
                    try:
                        evt = json.loads(line)
                    except Exception:
                        continue
                    if "seq" not in evt:
                        continue  # 旧格式行跳过
                    merged[int(evt["seq"])] = evt
        merged.update(sess.events)
        return merged

    def _history_page(self, sess: _Session, before: int | None, max_n: int):
        """seq 升序分页:取 beforeSeq(缺省无限)之下最近 max_n 条;翻旧页用 nextBeforeSeq。"""
        merged = self._session_events(sess)
        seqs = sorted(k for k in merged if before is None or k < before)
        page = seqs[-max_n:]
        has_more = len(seqs) > max_n
        next_before = page[0] if (has_more and page) else None
        return [merged[k] for k in page], has_more, next_before

    async def feed(self, evt: dict) -> None:
        """生产端事件:入会话(惰性建)→ seq 索引 → 订阅端 live 推送;终态更新 state。"""
        sid = evt.get("session") or ""
        if not sid:
            return
        sess = self.sessions.get(sid)
        if sess is None:
            d = evt.get("data") or {}
            sess = _Session(sid, evt.get("label") or d.get("label"),
                            evt.get("provider") or d.get("provider"),
                            evt.get("model") or d.get("model"),
                            run_id=evt.get("run_id") or d.get("run_id") or None)
            self.sessions[sid] = sess
        seq = evt.get("seq")
        if seq is not None:
            sess.events[int(seq)] = evt
        sess.last_ts = evt.get("ts") or time.time()
        if evt.get("type") == "session/start":
            d = evt.get("data") or {}
            sess.label = evt.get("label") or d.get("label") or sess.label
            sess.provider = evt.get("provider") or d.get("provider") or sess.provider
            sess.model = evt.get("model") or d.get("model") or sess.model
            sess.run_id = evt.get("run_id") or d.get("run_id") or sess.run_id
        elif evt.get("type") == "session/end":
            sess.state = (evt.get("data") or {}).get("state", "completed")
        await self._push(sess.subscribers, json.dumps({"type": "evt", "event": evt}, ensure_ascii=False))


def _viewer_html(static_root: Path) -> bytes:
    """读静态产物(static/agent_monitor_viewer.html);缺件回退文案。"""
    path = static_root / "agent_monitor_viewer.html"
    if path.exists():
        return path.read_bytes()
    _log(f"viewer 未构建:请运行仓库根 pnpm build(期望 {path})")
    return "viewer 文件缺失".encode()


async def _producer_loop(ws: WebSocket, hub: _Hub, first: dict) -> None:
    """生产端(gh_puller WsSink):feed 事件/应答 ping。"""
    await hub.feed(first.get("event") or {})
    try:
        while True:
            frame = json.loads(await ws.receive_text())
            data = frame.get("type")
            if data == "evt":
                await hub.feed(frame.get("event") or {})
            elif data == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass


async def _viewer_frame(ws: WebSocket, hub: _Hub, frame: dict) -> None:
    kind = frame.get("type")
    if kind == "index":
        await ws.send_text(json.dumps({"type": "index", "sessions": hub.index()}, ensure_ascii=False))
    elif kind == "history":
        sid = frame.get("session") or ""
        sess = hub.sessions.get(sid)
        if sess is None:
            await ws.send_text(json.dumps(
                {"type": "history", "session": sid, "events": [], "hasMore": False,
                 "nextBeforeSeq": None}, ensure_ascii=False))
            return
        events, has_more, next_before = hub._history_page(
            sess, frame.get("beforeSeq"), int(frame.get("max") or 200))
        await ws.send_text(json.dumps(
            {"type": "history", "session": sid, "events": events, "hasMore": has_more,
             "nextBeforeSeq": next_before}, ensure_ascii=False))
    elif kind == "subscribe":
        sid = frame.get("session") or ""
        sess = hub.sessions.get(sid)
        for s in hub.sessions.values():
            s.subscribers.discard(ws)  # 单视图:一条连接只盯一个会话
        last_seq = None
        if sess is not None:
            sess.subscribers.add(ws)  # 先登记再应答:live 推送无缝隙
            if sess.events:
                last_seq = max(sess.events)
        await ws.send_text(json.dumps(
            {"type": "evt_ready", "session": sid, "lastSeq": last_seq}, ensure_ascii=False))
    elif kind == "ping":
        await ws.send_text(json.dumps({"type": "pong"}))


async def _viewer_loop(ws: WebSocket, hub: _Hub, first: dict) -> None:
    """查看端(浏览器/回环测试):页面帧应答;断连清理订阅。"""
    try:
        await _viewer_frame(ws, hub, first)
        while True:
            await _viewer_frame(ws, hub, json.loads(await ws.receive_text()))
    except WebSocketDisconnect:
        pass
    finally:
        for sess in hub.sessions.values():
            sess.subscribers.discard(ws)


def create_app(hub: _Hub | None = None, *, static_root: Path | None = None) -> FastAPI:
    """组装 FastAPI 应用(测试可注入空 hub / 静态目录);缺省 hub 启动时磁盘种子。"""
    h = hub if hub is not None else _Hub()
    if hub is None:
        h.seed(envs.AGENT_MONITOR_DIR)
    root = static_root if static_root is not None else Path(__file__).parent / "static"

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/", include_in_schema=False)
    @app.get("/viewer", include_in_schema=False)
    async def index() -> Response:
        return Response(_viewer_html(root), media_type="text/html; charset=utf-8")

    @app.websocket("/ws")
    async def ws(ws: WebSocket) -> None:
        await ws.accept()
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
        except Exception:  # 超时/断开/非 JSON:静默释放
            return
        if first.get("type") == "evt":
            await _producer_loop(ws, h, first)
        else:  # index/history/subscribe/ping:一律按查看端处理
            await _viewer_loop(ws, h, first)

    return app


app = create_app()
