"""Generator lifecycle shared by every provider adapter."""

import contextlib
import sys
from collections.abc import AsyncIterator

from ... import envs
from ..events import EventRecorder, _session_id


class BaseGenerator:
    """Share client lifetime and one reusable observation session across adapters."""

    generator = ""
    provider = ""

    def __init__(self, config: dict):
        self.config = dict(config)
        self._event_recorder: EventRecorder | None = None

    @contextlib.asynccontextmanager
    async def session(self, *, session: str | None = None, run_id: str | None = None,
                      session_name: str | None = None):
        """Bind one reusable generator client to a canonical observation session.

        Args:
            session: Explicit observation-session id.
            run_id: Optional caller correlation recorded on ``session/start``.
            session_name: Human-readable label and fallback id namespace.
        """
        event_recorder = self._recorder(
            session=session, run_id=run_id, session_name=session_name)
        event_recorder.start()
        model = self.config.get("model")
        if model:
            parameters = {
                key: value for key, value in self.config.items()
                if key not in {"model", "api_key", "base_url", "system_prompt", "mcp_servers"}
            }
            event_recorder.set_model(model, provider=self.provider or None, parameters=parameters)
        instructions = ([{"type": "text", "text": self.config["system_prompt"]}]
                        if self.config.get("system_prompt") else [])
        event_recorder.set_header(instructions=instructions, tools=[])
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

    def _recorder(self, *, session: str | None = None, run_id: str | None = None,
                  session_name: str | None = None, generator: str | None = None) -> EventRecorder:
        """Build a recorder for one session."""
        return EventRecorder(
            _session_id(session, run_id, session_name), generator=generator or self.generator,
            label=session_name, run_id=run_id)

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
