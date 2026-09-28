"""Expose cookie-authenticated chat APIs and same-origin static assets."""

import asyncio
import contextlib
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from gh_puller.agent.events import set_active_bus
from gh_puller.agents import AGENTS
from pydantic import BaseModel, Field, SecretStr

from .config import AgentKind, PublicSettings, Question, ServerSettings, catalog
from .runtime import SessionManager, build_agent

COOKIE = "agent_chat_access"
LOGIN_TTL = 7 * 24 * 3600


class Login(BaseModel):
    password: SecretStr


class CreateSession(BaseModel):
    agent: AgentKind = next(iter(AGENTS))


class Rename(BaseModel):
    title: str = Field(min_length=1, max_length=100)


def create_app(settings: ServerSettings | None = None, *, agent_factory=build_agent):
    """Create one process-local application; test factories never enter production configuration."""
    settings = settings or ServerSettings.from_env()
    manager = SessionManager(settings, agent_factory)
    identities = {}
    attempts = defaultdict(deque)

    @asynccontextmanager
    async def lifespan(app):
        set_active_bus(manager.bus)
        cleanup = asyncio.create_task(manager.cleanup_loop())
        try:
            yield
        finally:
            cleanup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup
            await manager.close()
            identities.clear()
            set_active_bus(None)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if request.method in {"POST", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin and urlsplit(origin).netloc != request.headers.get("host")
            ):
                return JSONResponse({"detail": "仅接受本站请求"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "请求必须使用 JSON"}, status_code=415)
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 128 * 1024:
                    return JSONResponse({"detail": "请求过大"}, status_code=413)
                body.extend(chunk)
            # Starlette's cached request replays this bounded body to downstream handlers.
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # FastAPI's default error body includes rejected inputs, which may contain credentials.
        fields = [".".join(map(str, error["loc"][1:])) for error in exc.errors()]
        return JSONResponse({"detail": "配置格式不正确：" + ", ".join(fields)}, status_code=422)

    def authenticate(request: Request):
        token = request.cookies.get(COOKIE, "")
        identity = identities.get(token)
        if not identity or identity[1] < time.monotonic():
            identities.pop(token, None)
            raise HTTPException(401, "请重新输入访问口令")
        return identity[0]

    Owner = Annotated[str, Depends(authenticate)]

    @app.post("/api/auth/login")
    async def login(body: Login, request: Request, response: Response):
        current = time.monotonic()
        for key in list(attempts):
            while attempts[key] and attempts[key][0] < current - 60:
                attempts[key].popleft()
            if not attempts[key]:
                del attempts[key]
        address = request.client.host if request.client else "unknown"
        if len(attempts[address]) >= 8:
            raise HTTPException(429, "尝试过于频繁，请一分钟后重试")
        attempts[address].append(current)
        supplied = hashlib.sha256(body.password.get_secret_value().encode()).digest()
        expected = hashlib.sha256(settings.password.encode()).digest()
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(401, "访问口令不正确")
        attempts.pop(address, None)
        for key, value in list(identities.items()):
            if value[1] < current:
                del identities[key]
        token = request.cookies.get(COOKIE, "")
        if token not in identities:
            if len(identities) >= 1000:
                raise HTTPException(429, "登录会话已满")
            token = secrets.token_urlsafe(32)
        owner = identities.get(token, (secrets.token_hex(16),))[0]
        identities[token] = (owner, current + LOGIN_TTL)
        response.set_cookie(COOKIE, token, httponly=True, secure=settings.secure_cookie,
                            samesite="strict", max_age=LOGIN_TTL, path="/")
        return {"authenticated": True}

    @app.get("/api/auth/me")
    async def me(owner: Owner):
        return {"authenticated": True}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response, owner: Owner):
        for session in list(manager.sessions.values()):
            if session.owner == owner:
                await manager.delete(session)
        identities.pop(request.cookies.get(COOKIE, ""), None)
        response.delete_cookie(COOKIE, path="/", secure=settings.secure_cookie, httponly=True, samesite="strict")
        return {"authenticated": False}

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "revision": settings.revision}

    @app.get("/api/catalog")
    async def capabilities(owner: Owner):
        return {"agents": catalog(settings), "defaults": PublicSettings().model_dump(),
                "default_agent": next(iter(AGENTS)),
                "revision": settings.revision, "idle_minutes": settings.idle_seconds / 60}

    @app.get("/api/sessions")
    async def list_sessions(owner: Owner):
        return [session.view() for session in manager.sessions.values() if session.owner == owner]

    @app.post("/api/sessions", status_code=201)
    async def create(body: CreateSession, owner: Owner):
        capability = next((item for item in catalog(settings) if item["id"] == body.agent), None)
        if capability is None:
            raise HTTPException(422, "未知 agent")
        if not capability["available"]:
            raise HTTPException(422, capability["reason"])
        return manager.create(owner, body.agent).view()

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str, owner: Owner):
        return manager.get(owner, session_id).view()

    @app.patch("/api/sessions/{session_id}")
    async def rename(session_id: str, body: Rename, owner: Owner):
        session = manager.get(owner, session_id)
        if not body.title.strip():
            raise HTTPException(422, "标题不能为空")
        session.title = session.scrubber.text(body.title.strip())
        return session.view()

    @app.delete("/api/sessions/{session_id}")
    async def delete(session_id: str, owner: Owner):
        await manager.delete(manager.get(owner, session_id))
        return {"deleted": True}

    @app.post("/api/sessions/{session_id}/questions", status_code=202)
    async def submit(session_id: str, body: Question, owner: Owner):
        return manager.submit(manager.get(owner, session_id), body)

    @app.post("/api/sessions/{session_id}/stop")
    async def stop(session_id: str, owner: Owner):
        await manager.stop(manager.get(owner, session_id))
        return {"stopped": True}

    @app.get("/api/sessions/{session_id}/events")
    async def events(session_id: str, request: Request, owner: Owner, after: int = 0):
        session = manager.get(owner, session_id, touch=False)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(422, "事件序号无效") from None
        if cursor < 0 or cursor > len(session.offsets):
            raise HTTPException(409, "事件序号超出会话范围")

        async def subscribe():
            nonlocal cursor
            while not await request.is_disconnected():
                if session.closed:
                    yield "event: expired\ndata: {}\n\n"
                    return
                session.changed.clear()
                batch = session.replay(cursor)
                for event in batch:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if cursor < len(session.offsets):
                    continue
                if not session.running:
                    yield "event: idle\ndata: {}\n\n"
                    return
                try:
                    await asyncio.wait_for(session.changed.wait(), timeout=15)
                except TimeoutError:
                    yield ": heartbeat\n\n"

        return StreamingResponse(subscribe(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})

    @app.get("/api/sessions/{session_id}/export")
    async def export(session_id: str, owner: Owner):
        session = manager.get(owner, session_id)
        snapshot = len(session.offsets)

        async def download():
            yield json.dumps({"version": 1, "session": session.view()}, ensure_ascii=False)[:-1] + ',"events":['
            cursor = 0
            while cursor < snapshot:
                for event in session.replay(cursor, min(200, snapshot - cursor)):
                    yield ("," if cursor else "") + json.dumps(event, ensure_ascii=False)
                    cursor += 1
                if session.closed:
                    break
            yield "]}"

        return StreamingResponse(download(), media_type="application/json",
                                 headers={"Content-Disposition": 'attachment; filename="events.json"'})

    if settings.static_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=settings.static_dir / "assets"), name="assets")

        @app.get("/")
        async def index():
            return FileResponse(settings.static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def application():
    return create_app()
