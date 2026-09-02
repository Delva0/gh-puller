"""Expose the local agent monitor projection over HTTP and WebSocket.

FastAPI owns transport framing, viewer subscriptions, and live fan-out. History,
session metadata, and filesystem leases belong to ``hub``.
"""

import asyncio
import json
import logging
import os
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from gh_puller import envs

from hub import Hub

_LOG = logging.getLogger("agent-monitor")
_DEFAULT_LEASE_SECS = float(os.environ.get("AGENT_MONITOR_LEASE_SECS", "150"))


class _Connections:
    """Own viewer connections and their selected sessions."""

    def __init__(self):
        self.viewers: set[WebSocket] = set()
        self.subscribers: dict[str, set[WebSocket]] = {}

    def add(self, websocket: WebSocket) -> None:
        self.viewers.add(websocket)

    def remove(self, websocket: WebSocket) -> None:
        self.viewers.discard(websocket)
        for session_id, subscribers in list(self.subscribers.items()):
            subscribers.discard(websocket)
            if not subscribers:
                self.subscribers.pop(session_id)

    def subscribe(self, websocket: WebSocket, session_id: str) -> None:
        self.remove(websocket)
        self.add(websocket)
        if session_id:
            self.subscribers.setdefault(session_id, set()).add(websocket)

    async def send(self, websocket: WebSocket, frame: dict) -> None:
        await self._push({websocket}, frame)

    async def broadcast_index(self, hub: Hub) -> None:
        await self._push(self.viewers, {"type": "index", "sessions": hub.index()})

    async def publish(self, events: list[dict], *, batched: bool) -> None:
        by_session: dict[str, list[dict]] = {}
        for event in events:
            session_id = event.get("session") or ""
            if session_id and self.subscribers.get(session_id):
                by_session.setdefault(session_id, []).append(event)
        for session_id, batch in by_session.items():
            frame = (
                {"type": "evts", "events": batch}
                if batched
                else {
                    "type": "evt",
                    "event": batch[0],
                }
            )
            await self._push(self.subscribers[session_id], frame)

    async def _push(self, targets: Iterable[WebSocket], frame: dict) -> None:
        sockets = list(targets)
        if not sockets:
            return
        payload = json.dumps(frame, ensure_ascii=False)
        results = await asyncio.gather(
            *(websocket.send_text(payload) for websocket in sockets),
            return_exceptions=True,
        )
        for websocket, result in zip(sockets, results, strict=True):
            if isinstance(result, BaseException):
                _LOG.info("viewer disconnected during send: %s", type(result).__name__)
                self.remove(websocket)


def _viewer_html(static_root: Path) -> bytes:
    path = static_root / "agent_monitor_viewer.html"
    if path.exists():
        return path.read_bytes()
    _LOG.warning("viewer is missing; run pnpm --dir apps/agent-monitor/web build")
    return b"viewer file is missing"


async def _apply_producer_frame(
    websocket: WebSocket,
    hub: Hub,
    connections: _Connections,
    frame: dict,
) -> None:
    kind = frame.get("type")
    if kind == "ping":
        await websocket.send_text(json.dumps({"type": "pong"}))
        return
    if kind == "evt":
        events = [frame.get("event") or {}]
        batched = False
    elif kind == "evts":
        events = frame.get("events") or []
        batched = True
    else:
        return
    index_changed = hub.ingest(events)
    await connections.publish(events, batched=batched)
    if index_changed:
        await connections.broadcast_index(hub)


async def _producer_loop(
    websocket: WebSocket,
    hub: Hub,
    connections: _Connections,
    first: dict,
) -> None:
    """Relay producer frames until disconnect without owning their persistence."""
    try:
        await _apply_producer_frame(websocket, hub, connections, first)
        while True:
            frame = json.loads(await websocket.receive_text())
            await _apply_producer_frame(websocket, hub, connections, frame)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        _LOG.warning("producer frame failed: %s: %s", type(exc).__name__, exc)


async def _viewer_frame(
    websocket: WebSocket,
    hub: Hub,
    connections: _Connections,
    frame: dict,
) -> None:
    kind = frame.get("type")
    if kind == "index":
        if hub.scan():
            await connections.broadcast_index(hub)
        else:
            await connections.send(websocket, {"type": "index", "sessions": hub.index()})
    elif kind == "history":
        session_id = frame.get("session") or ""
        before = frame.get("beforeSeq")
        events, has_more, next_before = hub.history(
            session_id,
            before=int(before) if before is not None else None,
            limit=int(frame.get("max") or 200),
        )
        await connections.send(
            websocket,
            {
                "type": "history",
                "session": session_id,
                "events": events,
                "hasMore": has_more,
                "nextBeforeSeq": next_before,
            },
        )
    elif kind == "subscribe":
        session_id = frame.get("session") or ""
        connections.subscribe(websocket, session_id)
        await connections.send(
            websocket,
            {
                "type": "evt_ready",
                "session": session_id,
                "lastSeq": hub.last_seq(session_id),
            },
        )
    elif kind == "delete":
        hub.delete(frame.get("session") or "")
        await connections.broadcast_index(hub)
    elif kind == "ping":
        await connections.send(websocket, {"type": "pong"})


async def _viewer_loop(
    websocket: WebSocket,
    hub: Hub,
    connections: _Connections,
    first: dict,
) -> None:
    """Serve viewer commands and release its subscription on disconnect."""
    connections.add(websocket)
    try:
        await _viewer_frame(websocket, hub, connections, first)
        while True:
            frame = json.loads(await websocket.receive_text())
            await _viewer_frame(websocket, hub, connections, frame)
    except WebSocketDisconnect:
        pass
    finally:
        connections.remove(websocket)


async def _scan_loop(hub: Hub, connections: _Connections) -> None:
    while True:
        await asyncio.sleep(min(5.0, max(1.0, hub.lease_secs / 4)))
        try:
            if hub.scan():
                await connections.broadcast_index(hub)
        except Exception as exc:  # A failed scan must not stop later discovery.
            _LOG.warning("history scan failed: %s", exc)


def create_app(hub: Hub | None = None, *, static_root: Path | None = None) -> FastAPI:
    """Build the local monitor application.

    Args:
        hub: Projection to expose. ``None`` binds the app to ``AGENT_MONITOR_DIR``.
        static_root: Directory containing the built single-file viewer. ``None`` uses
            the server's ``static`` directory.
    """
    projection = hub or Hub(envs.AGENT_MONITOR_DIR, lease_secs=_DEFAULT_LEASE_SECS)
    connections = _Connections()
    root = static_root or Path(__file__).parent / "static"

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        projection.scan()
        task = asyncio.create_task(_scan_loop(projection, connections))
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @application.get("/", include_in_schema=False)
    @application.get("/viewer", include_in_schema=False)
    async def index() -> Response:
        return Response(_viewer_html(root), media_type="text/html; charset=utf-8")

    @application.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            first = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=10))
        except Exception:
            return
        if first.get("type") in {"evt", "evts"}:
            await _producer_loop(websocket, projection, connections, first)
        else:
            await _viewer_loop(websocket, projection, connections, first)

    return application


app = create_app()
