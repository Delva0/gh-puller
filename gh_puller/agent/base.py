"""Define the common Agent lifecycle and caller-visible failure contract."""

import contextlib
import sys
from collections.abc import AsyncIterator
from typing import Any

from .. import envs
from .events import EventRecorder, _session_id


class RequestFailedError(Exception):
    """Report an Agent failure with a caller-readable detail string."""

    def __init__(self, detail: Any):
        super().__init__(detail)
        self.detail = str(detail)


class BaseAgent:
    """Share client lifetime and one reusable observation session across adapters."""

    agent = ""

    def __init__(self, config: dict):
        """Create an adapter from its complete opaque configuration.

        Args:
            config: Adapter configuration recorded without semantic interpretation.
        """
        self.config = dict(config)
        self._event_recorder: EventRecorder | None = None

    @contextlib.asynccontextmanager
    async def session(self, *, session: str | None = None, run_id: str | None = None,
                      session_name: str | None = None):
        """Bind one reusable Agent client to a canonical observation session.

        Args:
            session: Explicit observation-session id.
            run_id: Optional caller correlation recorded on ``session/start``.
            session_name: Human-readable label and fallback id namespace.
        """
        event_recorder = self._recorder(
            session=session, run_id=run_id, session_name=session_name)
        event_recorder.start()
        self._event_recorder = event_recorder
        ok = False
        try:
            heartbeat_secs = envs.AGENT_MONITOR_HEARTBEAT_SECS
            if heartbeat_secs and heartbeat_secs > 0:
                event_recorder.start_keepwarm(heartbeat_secs)
            await self._enter()
            try:
                yield
            except Exception as exc:
                event_recorder.error(exc)
                raise
            finally:
                await self._exit(sys.exc_info())
            ok = True
        finally:
            await event_recorder.stop_keepwarm()
            event_recorder.finish(ok)
            self._event_recorder = None

    def _require_event_recorder(self) -> EventRecorder:
        """Return the active recorder or reject a call outside ``session``."""
        if self._event_recorder is None:
            raise RuntimeError("stream/result 只能在 async with agent.session(...) 块内调用")
        return self._event_recorder

    async def _enter(self) -> None:
        """Subclass hook: enter the client (same semantics as its `__aenter__`)."""
        raise NotImplementedError

    async def _exit(self, exc) -> None:
        """Subclass hook (within the session lifecycle): reap the underlying client (exc = exception context trio)."""
        raise NotImplementedError

    def _recorder(self, *, session: str | None = None, run_id: str | None = None,
                  session_name: str | None = None, agent: str | None = None) -> EventRecorder:
        """Build a recorder for one session."""
        return EventRecorder(
            _session_id(session, run_id, session_name), agent=agent or self.agent,
            config=self.config, label=session_name, run_id=run_id)

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        """Stream the assistant text increments (subclasses implement); only callable inside a session block.

        Args:
            prompt: User text for the next session turn.

        Returns:
            Async iterator of assistant text deltas.
        """
        raise NotImplementedError

    async def result(self, prompt: str) -> str:
        """Return the final round's assistant output (subclasses implement); only callable inside a session block.

        Args:
            prompt: User text for the next session turn.
        """
        raise NotImplementedError
