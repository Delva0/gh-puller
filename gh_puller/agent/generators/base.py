"""生成器共享骨架:BaseGenerator 基类 + 包内共享原子(失败异常/异常分类,utils)。

依赖只指向标准库、事件层(.events)、envs;差异经本基类下沉,各生成器文件自持
本体与 config 世界。API 契约见包 docstring(__init__.py)。
"""

import contextlib
import sys
from collections.abc import AsyncIterator

from ... import envs  # 模块对象绑定:属性一律调用时取(patch/强刷活性)
from ..events import EventRecorder, _session_id


class BaseGenerator:
    """Shared skeleton: per-generator differences = client access shape (synthesis/projection/direct HTTP).

    Subclasses only implement the difference-driving stream loop and the final-round
    result semantics; public kwargs (session/run metadata) are uniform per param.
    config is injected at construction time (copy kept in self.config); runtime methods
    take no config; with semantics (session() as the only conversation entry, out-of-block
    calls → RuntimeError) live in the package docstring (__init__.py). Subclass hooks:
    _enter/_exit enter/reap the client within the session lifecycle. Monitor-side agent
    meta is only generator + config (event envelope = generator/model and config-derived events).
    """

    generator = ""  # 生成管线 id(子类覆盖)
    provider = ""  # 会话快照的后端域(子类覆盖)

    def __init__(self, config: dict):
        self.config = dict(config)  # 副本防 SDK 篡改
        self._event_recorder: EventRecorder | None = None  # 会话级监控装配(session 进入时启动;块外 None)

    @contextlib.asynccontextmanager
    async def session(self, *, session: str | None = None, session_ns: str | None = None,
                      run_id: str | None = None, session_name: str | None = None,
                      context: list[dict] | None = None, retry: dict | None = None,
                      meta: dict | None = None,
                      error_stage=None, epilogue=True, prologue=True):
        """Run one upstream conversation (client and monitoring share the lifetime): enter assembles, exit closes.

        Orchestration: session-level event_recorder assembled and started (init_config →
        session/start) → client enter (_enter, child spawn) → yield (stream/result inside
        the block) → exit: client reap (_exit) + teardown (error event + finish).
        Subclasses covering differently (protocol-to-parse, dual-authority epilogue,
        start prologue, http/parse classification) override this method forwarding the
        differing params. One session = one conversation; instances are not reused concurrently.

        Args:
            session: Explicit session id; auto-derived from session_ns/run_id/session_name when omitted.
            session_ns: Namespace for the auto-derived session id (<ns>/<uuid4>).
            run_id: Run id recorded in the session snapshot.
            session_name: Human-readable session label.
            context: Context-message list (replayed as context/modify events before the run).
            retry: Retry metadata dict recorded in session/start.
            meta: Arbitrary metadata for the session snapshot.
            error_stage: Exception classifier producing the error-stage label (or a string).
            epilogue: Whether to emit turn/session finals (callable allowed).
            prologue: Whether to emit turn/step starts (callable allowed).
        """
        event_recorder = self._recorder(session=session, session_ns=session_ns,
                        run_id=run_id, session_name=session_name, meta=meta)
        event_recorder.init_config(self.config)
        event_recorder.start(context=context, retry=retry, prologue=prologue)
        self._event_recorder = event_recorder  # 已启动的会话级装配;stream/result 直接引用
        ok = False
        try:
            # 保鲜:会话期间每 interval 触一次会话文件 mtime(只动时间戳、不发事件、
            # 绝不 busy-loop);判态语义(hub 租约)见 events.py。退出先停保鲜再收尾:
            # 停后 mtime 冻结,session/end 恒为最后一条合法行。≤0 → 不起保鲜任务。
            heartbeat_secs = envs.AGENT_MONITOR_HEARTBEAT_SECS
            if heartbeat_secs and heartbeat_secs > 0:
                event_recorder.start_keepwarm(heartbeat_secs)
            await self._enter()  # 客户端进入(子进程 spawn);失败仍经收尾(不含 error 事件)
            try:
                yield
            except Exception as exc:
                # 只捕获 Exception(消费者提前关闭的 GeneratorExit、CancelledError 为
                # BaseException,不落 error 事件,靠下面 finally 兜底 finish(False));
                event_recorder.error(exc, error_stage(exc) if error_stage else "run")
                raise
            finally:
                await self._exit(sys.exc_info())  # 客户端回收(异常上下文原样)
            ok = True
        finally:
            await event_recorder.stop_keepwarm()
            event_recorder.finish(ok, epilogue=epilogue() if callable(epilogue) else epilogue)
            self._event_recorder = None

    def _require_event_recorder(self) -> EventRecorder:
        """Event recorder of the active session; calling stream/result outside the block raises a contract error."""
        if self._event_recorder is None:
            raise RuntimeError("stream/result 只能在 async with gen.session(...) 块内调用")
        return self._event_recorder

    async def _enter(self) -> None:
        """Subclass hook: enter the client (same semantics as its `__aenter__`)."""
        raise NotImplementedError

    async def _exit(self, exc) -> None:
        """Subclass hook (within the session lifecycle): reap the underlying client (exc = exception context trio)."""
        raise NotImplementedError

    def _recorder(self, *, session: str | None = None, session_ns: str | None = None,
             run_id: str | None = None, session_name: str | None = None,
             meta: dict | None = None,
             generator: str | None = None) -> EventRecorder:
        """Build the EventRecorder from public kwargs (session-id rule, envelope generator from class attrs)."""
        return EventRecorder(_session_id(session, session_ns, run_id, session_name),
                    generator=generator or self.generator,
                    provider=self.provider or None,
                    model=self.config.get("model") or None,
                    label=session_name, run_id=run_id, meta=meta)

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        """Stream the assistant text increments (subclasses implement); only callable inside a session block.

        Args:
            prompt: Runtime payload (prompt string; llm: payload dict + request-level timeout/headers).

        Returns:
            Async iterator of assistant text deltas.
        """
        raise NotImplementedError

    async def result(self, prompt: str) -> str:
        """Return the final round's assistant output (subclasses implement); only callable inside a session block.

        Args:
            prompt: Runtime payload (see `stream`).
        """
        raise NotImplementedError
