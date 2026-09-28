"""Own live agent contexts and replayable JSONL events in one backend process.

Agent-owned events reconnect browser histories to fresh native agent instances.
The application installs one synchronous event-bus adapter for its lifetime;
canonical events are routed by opaque session ID before observation is persisted.
"""

import asyncio
import contextlib
import hashlib
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import HTTPException
from gh_puller.agent.events import EventBus, is_event_type
from gh_puller.agents import AGENTS

from .config import Question, ServerSettings, resolve_settings, validate_capabilities
from .portable import restore
from .security import PublicTransport, SecretFilter
from .storage import PrivateStorage


class EventLimitError(RuntimeError):
    """End recording while preserving room for the final query status."""


def now():
    return datetime.now(UTC).isoformat()


def native_config(kind, public, server):
    return {"model": public.model, "base_url": public.base_url,
            **validate_capabilities(kind, public, server),
            "environment": {"date_utc": str(datetime.now(UTC).date())},
            "parameters": {"thinking": {"type": "enabled" if public.thinking else "disabled"},
                           **({"reasoning_effort": public.reasoning_effort} if public.thinking else {}),
                           "max_tokens": public.max_tokens,
                           "stream_options": {"include_usage": True}}}


def build_agent(kind, config, credentials, storage):
    connection = {"api_key": credentials["api_key"], "model_transport": PublicTransport()}
    connection.update({key: credentials.get(key, "") for key in AGENTS[kind].credential_names})
    return AGENTS[kind](config, storage, **connection)


@dataclass
class Session:
    owner: str
    kind: str
    server: ServerSettings
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    title: str = "新会话"
    created: str = field(default_factory=now)
    touched: float = field(default_factory=time.monotonic)
    public: object = None
    agent: object = None
    context: object = None
    task: asyncio.Task | None = None
    query_id: str | None = None
    query_started: float = 0
    finished_query_id: str | None = None
    requests: dict = field(default_factory=dict)
    credentials: dict = field(default_factory=dict)
    scrubber: SecretFilter = field(default_factory=SecretFilter)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    offsets: list = field(default_factory=list)
    closed: bool = False
    limited: bool = False
    log_size: int = 0
    history: list = field(default_factory=list)
    credential_base_url: str = ""

    def __post_init__(self):
        self.directory = TemporaryDirectory(prefix="agent-chat-", dir=self.server.temp_root)
        self.root = Path(self.directory.name)
        self.log = self.root / "events.jsonl"
        self.log.touch(mode=0o600)
        self.storage = PrivateStorage(self.root / "tools", self.scrubber, self.server.storage_bytes)
        self.scrubber.add([self.server.password])

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def view(self):
        return {"id": self.id, "agent": self.kind, "title": self.title, "created": self.created,
                "running": self.running, "query_id": self.query_id, "seq": len(self.offsets),
                "settings": self.public.model_dump() if self.public else None,
                "has_credentials": bool(self.credentials.get("api_key")),
                "configured_credentials": sorted(key for key, value in self.credentials.items() if value),
                "readonly": self.closed or self.limited}

    def emit(self, kind, data, **extra):
        if self.closed:
            return
        event = {"seq": len(self.offsets) + 1, "type": kind, "at": now(), "query_id": self.query_id,
                 "data": self.scrubber.clean(data), **extra}
        self.write_event(event)

    def write_event(self, event):
        kind = event["type"]
        encoded = (json.dumps(event, ensure_ascii=False) + "\n").encode()
        if kind != "query/end" and self.log_size + len(encoded) > self.server.event_bytes:
            if self.limited:
                return
            self.limited = True
            raise EventLimitError("会话事件达到上限，请导出记录后新建会话")
        with self.log.open("ab") as output:
            output.write(encoded)
        self.offsets.append(self.log_size)
        self.log_size += len(encoded)
        self.changed.set()

    def receive(self, event):
        kind, data = event["type"], event["data"]
        if not is_event_type(kind):
            return
        if kind in {"model/delta/text", "model/delta/reasoning"}:
            key = (kind, data["requestId"], data["index"])
            data = {**data, "text": self.scrubber.fragment(key, data["text"])}
            if not data["text"]:
                return
        elif kind in {"model/response", "model/error"}:
            self.flush_fragments(data["requestId"])
        self.emit(kind, data, source_seq=event["seq"], elapsed_ms=event["elapsedMs"])

    def flush_fragments(self, request_id=None):
        for key in list(self.scrubber.pending):
            kind, request, index = key
            if request_id is None or request == request_id:
                text = self.scrubber.fragment(key, "", final=True)
                if text:
                    self.emit(kind, {"requestId": request, "index": index, "text": text})

    def replay(self, after, limit=200):
        if self.closed or after >= len(self.offsets):
            return []
        with self.log.open("rb") as source:
            source.seek(self.offsets[max(0, after)])
            return [json.loads(line) for _, line in zip(range(limit), source, strict=False)]

    def finish(self, status, error="", answer=""):
        if self.finished_query_id == self.query_id:
            return
        try:
            self.flush_fragments()
        except EventLimitError as exc:
            status, error = "failed", str(exc)
        self.emit("query/end", {"status": status, "error": error, "answer": answer,
                               "duration_ms": round((time.monotonic() - self.query_started) * 1000)})
        self.finished_query_id = self.query_id
        self.touched = time.monotonic()


class SessionBus(EventBus):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager

    @property
    def enabled(self):
        return True

    def publish(self, event):
        if session := self.manager.sessions.get(event.get("session")):
            session.receive(event)


class SessionManager:
    def __init__(self, settings, factory=build_agent):
        self.settings, self.factory = settings, factory
        self.sessions = {}
        self.bus = SessionBus(self)
        self.preparations = {}

    def create(self, owner, kind, *, replacing=None):
        if len(self.sessions) - bool(replacing) >= self.settings.max_sessions:
            raise HTTPException(429, "服务的活跃会话已满，请删除不再使用的会话后重试")
        session = Session(owner, kind, self.settings)
        self.sessions[session.id] = session
        return session

    def get(self, owner, session_id, *, touch=True):
        session = self.sessions.get(session_id)
        if session is None or session.owner != owner or session.closed:
            raise HTTPException(404, "执行会话已失效，可从浏览器历史恢复")
        if touch:
            session.touched = time.monotonic()
        return session

    def submit(self, session, question: Question):
        public = resolve_settings(session.kind, question.settings)
        fingerprint = hashlib.sha256(json.dumps(
            {"prompt": question.prompt, "settings": public.model_dump()}, sort_keys=True,
        ).encode()).hexdigest()
        if question.request_id in session.requests:
            if session.requests[question.request_id] != fingerprint:
                raise HTTPException(409, "请求标识已用于另一条问题")
            return {"request_id": question.request_id, "duplicate": True}
        if session.running:
            raise HTTPException(409, "当前会话正在处理上一条问题")
        if session.limited or len(session.requests) >= 100:
            raise HTTPException(409, "会话达到保留上限，请新建会话")
        if sum(item.running for item in self.sessions.values()) >= self.settings.max_running:
            raise HTTPException(429, "服务正在处理其他请求，请稍后重试")
        try:
            validate_capabilities(session.kind, public, self.settings)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if session.public:
            before, after = session.public.model_dump(), public.model_dump()
            if before["options"] != after["options"]:
                raise HTTPException(409, "Agent 配置已改变，请确认重建后继续")
            if before["base_url"] != after["base_url"] and not question.credentials.values().get("api_key"):
                raise HTTPException(422, "更换模型地址需要重新输入 API Key")
        credentials = {**session.credentials,
                       **{key: value for key, value in question.credentials.values().items() if value}}
        if (session.credential_base_url and session.credential_base_url != public.base_url
                and not question.credentials.values().get("api_key")):
            raise HTTPException(422, "更换模型地址需要重新输入 API Key")
        if not credentials.get("api_key"):
            raise HTTPException(422, "请在设置中输入模型 API Key")
        try:
            AGENTS[session.kind].configuration.validate_credentials(public.options, credentials)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        session.credentials, session.public = credentials, public
        session.credential_base_url = public.base_url
        session.scrubber.add(credentials.values())
        session.requests[question.request_id] = fingerprint
        session.query_id = question.request_id
        session.query_started = time.monotonic()
        if len(session.requests) == 1:
            session.title = session.scrubber.text(" ".join(question.prompt.split()))[:36] or "新会话"
        try:
            session.emit("query/start", {"prompt": question.prompt, "settings": public.model_dump(),
                                         "agent": session.kind, "source_revision": self.settings.revision,
                                         "data_boundary": AGENTS[session.kind].data_boundary})
        except EventLimitError as exc:
            session.finish("failed", str(exc))
            raise HTTPException(409, str(exc)) from None
        session.task = asyncio.create_task(self.run(session, question.prompt))
        return {"request_id": question.request_id, "duplicate": False}

    async def run(self, session, prompt):
        status, error, answer = "completed", "", ""
        try:
            async with asyncio.timeout(self.settings.run_seconds):
                config = native_config(session.kind, session.public, self.settings)
                await AGENTS[session.kind].prepare(config, self.preparations)
                if session.agent is None:
                    session.agent = self.factory(session.kind, config, session.credentials, session.storage)
                    session.context = session.agent.session(session=session.id)
                    try:
                        await session.context.__aenter__()
                    except BaseException:
                        session.agent = session.context = None
                        raise
                    if session.history:
                        try:
                            restore(session)
                        except BaseException:
                            try:
                                await session.context.__aexit__(*sys.exc_info())
                            finally:
                                session.agent = session.context = None
                            raise
                else:
                    session.agent.update_config(config)
                    session.agent.set_credentials(session.credentials)
                answer = await session.agent.result(prompt)
        except asyncio.CancelledError:
            status, error = "cancelled", "已停止"
        except TimeoutError:
            status, error = "failed", "查询超时，已停止；可以继续提问或新建会话"
        except Exception as exc:  # Expose sanitized failures without logging request bodies or credentials.
            status, error = "failed", str(exc) or type(exc).__name__
        finally:
            session.finish(status, error, answer)

    async def stop(self, session):
        if session.running:
            session.task.cancel()
            try:
                await session.task
            except asyncio.CancelledError:
                # A task cancelled before its coroutine starts cannot run its finally block.
                session.finish("cancelled", "已停止")

    async def delete(self, session):
        await self.stop(session)
        try:
            if session.context:
                await session.context.__aexit__(None, None, None)
        finally:
            session.closed = True
            session.changed.set()
            self.sessions.pop(session.id, None)
            session.agent = session.context = session.task = None
            session.credentials.clear()
            session.scrubber.secrets.clear()
            session.scrubber.pending.clear()
            session.directory.cleanup()

    async def expire(self):
        for session in list(self.sessions.values()):
            if not session.running and time.monotonic() - session.touched > self.settings.idle_seconds:
                await self.delete(session)

    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(30)
            await self.expire()

    async def close(self):
        for session in list(self.sessions.values()):
            with contextlib.suppress(Exception):
                await self.delete(session)
        if self.preparations:
            await asyncio.gather(*self.preparations.values(), return_exceptions=True)
