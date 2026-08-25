"""Agent 监控 Web/WS hub(FastAPI 端点层)。

生产端是 gh_puller.agent 的 WsSink(经 AGENT_MONITOR_WS_URL 接入,主动向 /ws 推
{"type":"evt","event":...});浏览器查看端经同一端点订阅会话。GET / 与 /viewer
直接出构建好的单文件 viewer(agent-monitor.html);hub 只持内存状态
(事件环 1000/会话、LLM 流行 500 行/会话),写盘是 FileSink 的事,启动时从磁盘
种子历史,重启 hub 列表仍在。

协议:一连接一角色,首帧定角色(evt → 生产端,其余 → 查看端);单订阅视图,
回放与 live 无缝衔接,帧携带 per-session 单调 id 供客户端去重。
"""

import asyncio
import json
import sys
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from gh_puller import envs
from gh_puller.agent import KINDS, LlmAggregator

_STATE_DIRS = ("running", "completed", "aborted")


def _log(msg: str) -> None:
    print(f"[agent-monitor] {msg}", file=sys.stderr, flush=True)


class _Session:
    """hub 内存中的单会话状态:事件环 + LLM 流行环(行即最小缓存单元,id 供客户端去重)。"""

    def __init__(self, session: str, label: str = "", provider: str = "", model: str = ""):
        self.session = session
        self.label = label or session
        self.provider = provider
        self.model = model
        self.state = "running"
        self.ts = time.time()
        self.last_ts = self.ts
        self.events: deque[dict] = deque(maxlen=1000)
        self.lines: deque[dict] = deque(maxlen=500)  # {"id": int, "line": dict}
        self.agg: LlmAggregator | None = None
        self.subscribers: set = set()
        self.evt_subscribers: set = set()  # 原始事件订阅(live evt 推送;与 llm 行订阅独立集合)
        self._lid = 0


class _Hub:
    """hub 内存状态:磁盘种子 + 事件流聚合 + 按会话订阅广播。"""

    def __init__(self):
        self.sessions: dict[str, _Session] = {}

    def seed(self, root: str | None) -> None:
        """启动种子:扫描 AGENT_MONITOR_DIR/sessions/{running,completed,aborted}/*.jsonl。

        running 会话标记 running 且保持现场;重启窗口内到来的后续事件直接从聚合器续写
        (可能缺 run.start 头 —— 少块头,内容不丢,见 _Session.agg 注释)。
        """
        if not root:
            return
        base = Path(root) / "sessions"
        for state in _STATE_DIRS:
            for path in sorted((base / state).glob("*.jsonl")):
                try:
                    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                except Exception:  # 半行损坏(崩溃写盘中断):整文件跳过,事件流仍完整
                    _log(f"hub 种子跳过 {path.name}: 不可解析")
                    continue
                if not lines:
                    continue
                head = lines[0]
                sess = _Session(path.stem, head.get("label"), head.get("provider"), head.get("model"))
                sess.state = state
                sess.ts = head.get("ts") or sess.ts
                sess.last_ts = lines[-1].get("ts") or sess.ts
                for i, line in enumerate(lines):
                    sess.lines.append({"id": i, "line": line})
                sess._lid = len(lines)
                self.sessions[path.stem] = sess

    def index(self) -> list[dict]:
        """会话索引(最近更新在前):供左列表渲染。"""
        return sorted(
            (
                {"session": s.session, "label": s.label, "provider": s.provider, "model": s.model,
                 "state": s.state, "ts": s.ts, "last_ts": s.last_ts}
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

    async def feed(self, evt: dict) -> None:
        """生产端事件:入事件环 → 聚合器续写 LLM 流行 → 向该会话订阅者推送。

        会话按首个事件惰性建;聚合器拒绝未知 kind 时事件流仍照收(推送线路独立)。
        """
        sid = evt.get("session") or ""
        if not sid:
            return
        sess = self.sessions.get(sid)
        if sess is None:
            sess = _Session(sid, evt.get("label", ""), evt.get("provider", ""), evt.get("model", ""))
            self.sessions[sid] = sess
        sess.events.append(evt)
        sess.last_ts = evt.get("ts") or time.time()
        if sess.agg is None:
            sess.agg = LlmAggregator(sid, sess.label, sess.provider, sess.model)
        pushes: list[dict] = []
        if evt["kind"] in KINDS:
            for line in sess.agg.feed(evt):
                sess._lid += 1
                pushes.append({"id": sess._lid, "line": line})
                sess.lines.append(pushes[-1])
                if line["type"] == "session.end":
                    sess.state = line["state"]
        # 原始事件 live 推送(事件流视图;与 LLM 行订阅独立,失败互不影响)
        await self._push(sess.evt_subscribers, json.dumps({"type": "evt", "event": evt}, ensure_ascii=False))
        for wrapped in pushes:
            await self._push(sess.subscribers, json.dumps(
                {"type": "llm", "session": sid, "id": wrapped["id"], "line": wrapped["line"]},
                ensure_ascii=False))


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
    elif kind == "llm-subscribe":
        sid = frame.get("session") or ""
        sess = hub.sessions.get(sid)
        for s in hub.sessions.values():
            s.subscribers.discard(ws)  # 单视图:一条连接只盯一个会话
        if sess is None:
            await ws.send_text(json.dumps({"type": "llm_ready", "session": sid}))
            return
        sess.subscribers.add(ws)  # 先登记再拉快照:快照与实时推送无缝隙
        for wrapped in list(sess.lines):
            await ws.send_text(json.dumps(
                {"type": "llm", "session": sid, "id": wrapped["id"], "line": wrapped["line"]},
                ensure_ascii=False))
        await ws.send_text(json.dumps({"type": "llm_ready", "session": sid}))
    elif kind == "evt-subscribe":
        sid = frame.get("session") or ""
        sess = hub.sessions.get(sid)
        for s in hub.sessions.values():
            s.evt_subscribers.discard(ws)  # 单视图:一条连接只盯一个会话
        if sess is None:
            await ws.send_text(json.dumps({"type": "evt_ready", "session": sid}))
            return
        sess.evt_subscribers.add(ws)  # 先登记再回放:回放与 live 推送无缝隙
        for evt in list(sess.events):
            await ws.send_text(json.dumps({"type": "evt", "event": evt}, ensure_ascii=False))
        await ws.send_text(json.dumps({"type": "evt_ready", "session": sess.session}))
    elif kind == "evt-replay":
        sess = hub.sessions.get(frame.get("session") or "")
        if sess is None:
            return
        for evt in list(sess.events):
            await ws.send_text(json.dumps({"type": "evt", "event": evt}, ensure_ascii=False))
        await ws.send_text(json.dumps({"type": "evt_ready", "session": sess.session}))
    elif kind == "ping":
        await ws.send_text(json.dumps({"type": "pong"}))


async def _viewer_loop(ws: WebSocket, hub: _Hub, first: dict) -> None:
    """查看端(浏览器/回环测试):页面帧应答;断连清理两套订阅。"""
    try:
        await _viewer_frame(ws, hub, first)
        while True:
            await _viewer_frame(ws, hub, json.loads(await ws.receive_text()))
    except WebSocketDisconnect:
        pass
    finally:
        for sess in hub.sessions.values():
            sess.subscribers.discard(ws)
            sess.evt_subscribers.discard(ws)


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
        else:  # index/llm-subscribe/evt-replay/ping:一律按查看端处理
            await _viewer_loop(ws, h, first)

    return app


app = create_app()
