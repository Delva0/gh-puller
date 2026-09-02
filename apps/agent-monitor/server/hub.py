"""Serve persisted and live canonical agent events over HTTP and WebSocket.

The hub indexes compact JSONL histories, forwards raw live events, and derives a
session lease state. It does not interpret replayable model context; clients fold the
canonical stream. See ``gh_puller.agent.events`` for event semantics.
"""

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from gh_puller import envs
from gh_puller.agent.events import NON_STREAM_TYPES

# The lease should remain several times longer than the producer heartbeat.
_DEFAULT_LEASE_SECS = int(os.environ.get("AGENT_MONITOR_LEASE_SECS", "150"))


def _session_state(outcome: str | None) -> str:
    """Map a terminal protocol outcome to the monitor's coarse list state."""
    return "completed" if outcome == "completed" else "aborted"


def _log(msg: str) -> None:
    print(f"[agent-monitor] {msg}", file=sys.stderr, flush=True)


def _file_stem(session: str) -> str:
    """Map a session id to the flat filename stem used by ``FileSink``."""
    return session.rsplit("/", 1)[-1]


class _Session:
    """Hold one compact history index and its raw-stream high-water mark."""

    def __init__(self, session: str, label: str = "", provider: str = "", model: str = "",
                 run_id: str | None = None, generator: str = ""):
        self.session = session
        self.label = label or session
        self.provider = provider
        self.generator = generator
        self.model = model
        self.run_id = run_id
        self.state = "running"
        self.ts = time.time()
        self.last_ts = self.ts
        self.events: dict[int, dict] = {}
        self.last_seq: int | None = None
        self.subscribers: set = set()
        self.disk_mtime: float = 0.0
        self.history_mtime: float = 0.0


class _Hub:
    """Merge disk seeds and live events, then serve per-session subscriptions."""

    def __init__(self, lease_secs: float | None = None):
        self.sessions: dict[str, _Session] = {}
        self.viewers: set = set()
        self.root: str | None = None
        self.lease_secs = _DEFAULT_LEASE_SECS if lease_secs is None else lease_secs

    def _file_for(self, session: str) -> Path | None:
        """Return the flat history path when disk history is configured."""
        if not self.root:
            return None
        return Path(self.root) / f"{_file_stem(session)}.jsonl"

    def seed(self, root: str | None) -> None:
        """Load compact JSONL sessions and infer terminal or leased state."""
        if not root:
            return
        self.root = root
        base = Path(root)
        for path in sorted(base.glob("*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception as exc:
                _log(f"hub 种子跳过 {path.name}: 读取失败 {exc}")
                continue
            events: dict[int, dict] = {}
            last_seq: int | None = None
            head: dict | None = None
            end_state: str | None = None
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
                if evt.get("type") == "session/end":
                    end_state = _session_state((evt.get("data") or {}).get("outcome"))
                if "seq" not in evt or "type" not in evt:
                    continue
                seq = int(evt["seq"])
                last_seq = seq if last_seq is None else max(last_seq, seq)
                if evt.get("type") in NON_STREAM_TYPES:
                    events[seq] = evt
            if head is None or not events:
                _log(f"hub 种子跳过 {path.name}: 非事件溯源格式")
                continue
            d = head.get("data") or {}
            sid = head.get("session") or path.stem
            model_event = next((event for event in reversed(events.values())
                                if event.get("type") == "model/set"), None)
            model_data = (model_event or {}).get("data") or {}
            sess = _Session(sid, d.get("label"), model_data.get("provider") or "",
                            model_data.get("model") or "",
                            run_id=d.get("runId"),
                            generator=head.get("generator") or d.get("generator") or "")
            sess.state = end_state or "running"
            sess.ts = head.get("ts") or sess.ts
            sess.last_ts = max((e.get("ts") or sess.ts) for e in events.values())
            sess.events = events
            sess.last_seq = last_seq
            try:
                sess.disk_mtime = path.stat().st_mtime
                sess.history_mtime = sess.disk_mtime
            except OSError:
                sess.disk_mtime = 0.0
            if (end_state is None and sess.disk_mtime
                    and time.time() - sess.disk_mtime > self.lease_secs):
                sess.state = "aborted"
            self.sessions[sid] = sess

    def _recheck_state(self, sess: _Session) -> None:
        """Recheck a changed file tail for terminal state or producer revival."""
        if sess.state == "completed":
            return
        path = self._file_for(sess.session)
        if path is None or not path.exists():
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if mtime == sess.disk_mtime:
            return
        was_aborted = sess.state == "aborted"
        sess.disk_mtime = mtime
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            return
        for line in reversed(tail.splitlines()):
            if not line.strip():
                continue
            try:
                evt = json.loads(line)
            except Exception:  # noqa: S112 - Continue past a partial trailing line.
                continue
            if evt.get("type") == "session/end":
                sess.state = _session_state((evt.get("data") or {}).get("outcome"))
                sess.last_ts = max(sess.last_ts, evt.get("ts") or 0)
            elif was_aborted:
                sess.state = "running"
            break

    async def scan(self) -> None:
        """Mark unterminated histories aborted after their file lease expires."""
        flipped = False
        for sess in self.sessions.values():
            if sess.state == "completed":
                continue
            self._recheck_state(sess)
            if sess.state != "running":
                continue
            path = self._file_for(sess.session)
            if path is None or not path.exists():
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if time.time() - mtime > self.lease_secs:
                sess.state = "aborted"
                flipped = True
                _log(f"hub 租约:会话 {sess.session} 无终态行且文件静止超过 "
                     f"{self.lease_secs:g}s → 孤儿(aborted)")
        if flipped:
            await self._broadcast_index()

    def index(self) -> list[dict]:
        """Return session summaries ordered by latest activity."""
        for sess in self.sessions.values():
            self._recheck_state(sess)
        return sorted(
            (
                {"session": s.session, "run_id": s.run_id, "label": s.label,
                 "generator": s.generator, "provider": s.provider, "model": s.model,
                 "state": s.state, "ts": s.ts, "last_ts": s.last_ts,
                 "num_events": (s.last_seq + 1) if s.last_seq is not None else 0}
                for s in self.sessions.values()
            ),
            key=lambda x: x["last_ts"],
            reverse=True,
        )

    async def delete(self, session: str) -> None:
        """Delete one in-memory and persisted session, then broadcast the index."""
        self.sessions.pop(session, None)
        path = self._file_for(session)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                _log(f"hub 删除会话磁盘文件失败 {path.name}: {exc}")
        await self._broadcast_index()

    async def _push(self, subs: set, payload: str) -> None:
        """Send one frame to subscribers, removing failed connections."""
        for sub in list(subs):
            try:
                await sub.send_text(payload)
            except Exception as exc:
                _log(f"hub 订阅端断连,移除订阅: {type(exc).__name__}: {exc}")
                subs.discard(sub)

    async def _broadcast_index(self) -> None:
        """Broadcast the complete session index to every viewer."""
        await self._push(self.viewers, json.dumps(
            {"type": "index", "sessions": self.index()}, ensure_ascii=False))

    def _session_events(self, sess: _Session) -> dict[int, dict]:
        """Merge changed compact disk history into the live cache by sequence."""
        cand = self._file_for(sess.session)
        if cand is not None and cand.exists():
            try:
                mtime = cand.stat().st_mtime
            except OSError:
                return sess.events
            if mtime == sess.history_mtime:
                return sess.events
            disk: dict[int, dict] = {}
            for line in cand.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    evt = json.loads(line)
                except Exception:  # noqa: S112 - Ignore partial or corrupt lines.
                    continue
                if "seq" not in evt:
                    continue
                if evt.get("type") in NON_STREAM_TYPES:
                    disk[int(evt["seq"])] = evt
            disk.update(sess.events)
            sess.events = disk
            sess.history_mtime = mtime
        return sess.events

    def _history_page(self, sess: _Session, before: int | None, max_n: int):
        """Return the latest sequence-ordered page below ``before``."""
        merged = self._session_events(sess)
        seqs = sorted(k for k in merged if before is None or k < before)
        page = seqs[-max_n:]
        has_more = len(seqs) > max_n
        next_before = page[0] if (has_more and page) else None
        return [merged[k] for k in page], has_more, next_before

    def _record(self, evt: dict) -> tuple[_Session | None, bool]:
        """Record one event and return its session plus whether index metadata changed."""
        sid = evt.get("session") or ""
        if not sid:
            return None, False
        created = self.sessions.get(sid) is None
        sess = self.sessions.get(sid)
        if sess is None:
            d = evt.get("data") or {}
            sess = _Session(sid, evt.get("label") or d.get("label"),
                            evt.get("provider") or d.get("provider"),
                            evt.get("model") or d.get("model"),
                            run_id=d.get("runId") or None,
                            generator=evt.get("generator") or d.get("generator") or "")
            self.sessions[sid] = sess
        seq = evt.get("seq")
        if seq is not None:
            seq = int(seq)
            sess.last_seq = seq if sess.last_seq is None else max(sess.last_seq, seq)
            if evt.get("type") in NON_STREAM_TYPES:
                sess.events[seq] = evt
        sess.last_ts = evt.get("ts") or time.time()
        if evt.get("type") == "session/start":
            d = evt.get("data") or {}
            sess.label = d.get("label") or sess.label
            sess.generator = d.get("generator") or sess.generator
            sess.run_id = d.get("runId") or sess.run_id
        elif evt.get("type") == "model/set":
            d = evt.get("data") or {}
            sess.provider = d.get("provider") or sess.provider
            sess.model = d.get("model") or sess.model
        elif evt.get("type") == "session/end":
            sess.state = _session_state((evt.get("data") or {}).get("outcome"))
        return sess, created or evt.get("type") in {"model/set", "session/end"}

    async def feed_batch(self, events: list[dict]) -> None:
        """Record a producer batch and forward one batch per subscribed session."""
        live: dict[str, list[dict]] = {}
        index_changed = False
        for evt in events:
            sess, changed = self._record(evt)
            if sess is None:
                continue
            index_changed = index_changed or changed
            if sess.subscribers:
                live.setdefault(sess.session, []).append(evt)
        for sid, batch in live.items():
            await self._push(self.sessions[sid].subscribers, json.dumps(
                {"type": "evts", "events": batch}, ensure_ascii=False))
        if index_changed:
            await self._broadcast_index()

    async def feed(self, evt: dict) -> None:
        """Backward-compatible single-event producer entry."""
        sess, index_changed = self._record(evt)
        if sess is None:
            return
        if sess.subscribers:
            await self._push(sess.subscribers, json.dumps(
                {"type": "evt", "event": evt}, ensure_ascii=False))
        if index_changed:
            await self._broadcast_index()


def _viewer_html(static_root: Path) -> bytes:
    """Read the built viewer, returning a diagnostic page when absent."""
    path = static_root / "agent_monitor_viewer.html"
    if path.exists():
        return path.read_bytes()
    _log(f"viewer 未构建:请运行仓库根 pnpm build(期望 {path})")
    return "viewer 文件缺失".encode()


async def _producer_loop(ws: WebSocket, hub: _Hub, first: dict) -> None:
    """Receive producer batches and answer keepalive pings."""
    if first.get("type") == "evts":
        await hub.feed_batch(first.get("events") or [])
    else:
        await hub.feed(first.get("event") or {})
    try:
        while True:
            frame = json.loads(await ws.receive_text())
            data = frame.get("type")
            if data == "evt":
                await hub.feed(frame.get("event") or {})
            elif data == "evts":
                await hub.feed_batch(frame.get("events") or [])
            elif data == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        _log(f"hub 生产端帧处理失败: {type(exc).__name__}: {exc}")


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
            s.subscribers.discard(ws)
        last_seq = None
        if sess is not None:
            sess.subscribers.add(ws)
            last_seq = sess.last_seq
        await ws.send_text(json.dumps(
            {"type": "evt_ready", "session": sid, "lastSeq": last_seq}, ensure_ascii=False))
    elif kind == "delete":
        await hub.delete(frame.get("session") or "")
    elif kind == "ping":
        await ws.send_text(json.dumps({"type": "pong"}))


async def _viewer_loop(ws: WebSocket, hub: _Hub, first: dict) -> None:
    """Serve viewer frames and remove subscriptions on disconnect."""
    try:
        await _viewer_frame(ws, hub, first)
        while True:
            await _viewer_frame(ws, hub, json.loads(await ws.receive_text()))
    except WebSocketDisconnect:
        pass
    finally:
        hub.viewers.discard(ws)
        for sess in hub.sessions.values():
            sess.subscribers.discard(ws)


async def _lease_loop(h: _Hub) -> None:
    """Scan leases continuously without letting failures stop the service."""
    while True:
        try:
            await h.scan()
        except Exception as exc:
            _log(f"hub 租约扫描异常: {exc}")
        await asyncio.sleep(max(1.0, h.lease_secs / 4))


def create_app(hub: _Hub | None = None, *, static_root: Path | None = None) -> FastAPI:
    """Build the FastAPI app, seeding disk history for the default hub."""
    h = hub if hub is not None else _Hub()
    if hub is None:
        h.seed(envs.AGENT_MONITOR_DIR)
    root = static_root if static_root is not None else Path(__file__).parent / "static"

    @asynccontextmanager
    async def _lifespan(_app):
        scan_task = asyncio.create_task(_lease_loop(h))
        try:
            yield
        finally:
            scan_task.cancel()
            await asyncio.gather(scan_task, return_exceptions=True)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)

    @app.get("/", include_in_schema=False)
    @app.get("/viewer", include_in_schema=False)
    async def index() -> Response:
        return Response(_viewer_html(root), media_type="text/html; charset=utf-8")

    @app.websocket("/ws")
    async def ws(ws: WebSocket) -> None:
        await ws.accept()
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
        except Exception:
            return
        if first.get("type") in {"evt", "evts"}:
            await _producer_loop(ws, h, first)
        else:
            h.viewers.add(ws)
            await _viewer_loop(ws, h, first)

    return app


app = create_app()
