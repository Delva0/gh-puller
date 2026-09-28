"""Share Agent lifecycle, Context replay and the caller-visible failure contract."""

import contextlib
import copy
import sys
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from .. import envs
from .events import EventRecorder, _session_id, fold_state

if TYPE_CHECKING:
    from .events import FailureReason


class RequestFailedError(Exception):
    """Report an Agent failure with a caller-readable detail string."""

    def __init__(self, detail: Any, *, reason_code: "FailureReason" = "error"):
        """Report an adapter-classified failure without implementing a stopping policy.

        Args:
            detail: Caller-visible failure detail.
            reason_code: Canonical classification; see gh_puller.agent.events.
        """
        super().__init__(detail)
        self.detail = str(detail)
        self.reason_code = reason_code


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
                      session_name: str | None = None, recorder: EventRecorder | None = None):
        """Bind one reusable Agent client to a canonical observation session.

        Args:
            session: Explicit observation-session id.
            run_id: Optional caller correlation recorded on ``session/start``.
            session_name: Human-readable label and fallback id namespace.
            recorder: Open caller-owned recorder. Binding and releasing this client
                do not start or end its session; the caller owns its heartbeat too.
                Bind clients sequentially. Each initializes fresh Context; call
                ``load_events`` inside the scope to transfer observed Context.
        """
        event_recorder = recorder or self._recorder(
            session=session, run_id=run_id, session_name=session_name)
        if recorder is None:
            event_recorder.start()
        else:
            if any(value is not None for value in (session, run_id, session_name)):
                raise ValueError("Recorder owns the session identity")
            event_recorder.bind_agent(self.agent, self.config)
            if event_recorder.context():
                event_recorder.set_context([])
        self._event_recorder = event_recorder
        ok = False
        try:
            try:
                heartbeat_secs = envs.AGENT_MONITOR_HEARTBEAT_SECS
                if recorder is None and heartbeat_secs and heartbeat_secs > 0:
                    event_recorder.start_keepwarm(heartbeat_secs)
                await self._enter()
            except BaseException as exc:  # Cancellation is a terminal observation, not a swallowed error.
                event_recorder.error(exc, phase="initialize")
                raise
            try:
                yield
            except BaseException as exc:  # Preserve caller cancellation while still observing it.
                event_recorder.error(exc, phase="run")
                raise
            finally:
                try:
                    await self._exit(sys.exc_info())
                except BaseException as exc:  # A cleanup failure must not hide the preceding run failure.
                    event_recorder.error(exc, phase="cleanup")
                    raise
            ok = True
        finally:
            try:
                if recorder is None:
                    await event_recorder.stop_keepwarm()
            except BaseException as exc:  # Footer delivery is also required when cleanup is interrupted.
                ok = False
                event_recorder.error(exc, phase="cleanup")
                raise
            finally:
                if recorder is None:
                    event_recorder.finish(ok)
                else:
                    event_recorder.end_turn(outcome="completed" if ok else "failed")
                self._event_recorder = None

    def _require_event_recorder(self) -> EventRecorder:
        """Return the active recorder or reject a call outside ``session``."""
        if self._event_recorder is None:
            raise RuntimeError("stream/result 只能在 async with agent.session(...) 块内调用")
        return self._event_recorder

    async def _enter(self) -> None:
        """Enter the client; the hook owns rollback if initialization fails."""
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

    def load_events(self, events):
        """Replay shared Context through the adapter's native-context hook.

        Args:
            events: Ordered event dictionaries, optionally ending mid-turn. Only Context
                events are read here. Adapters can additionally replay their own tools.

        Raises:
            NotImplementedError: The adapter cannot apply Context to its native client.
        """
        recorder = self._require_event_recorder()
        items = copy.deepcopy(fold_state(events)["context"])
        self._load_context(items)
        recorder.set_context(items)

    def _load_context(self, items):
        """Apply canonical items to native memory before publishing the restored Context.

        Args:
            items: Folded source Context. Adapters may replace items in place to
                reflect their actual native Context, including their system/tool policy.
        """
        raise NotImplementedError("This agent cannot apply Context to its native client")
