"""Project local agent history files into queryable monitor sessions.

The compact JSONL files written by ``gh_puller.agent.sinks.FileSink`` are the
durable source. ``Hub`` keeps a disposable in-memory projection and merges live
events only to bridge filesystem-to-viewer latency. Transport and subscriptions
belong to ``app``.
"""

import json
import logging
import time
from pathlib import Path

from gh_puller.agent.events import fold_state, is_compact_event

_LOG = logging.getLogger("agent-monitor")


def _session_state(outcome: str | None) -> str:
    return "completed" if outcome == "completed" else "aborted"


def _file_stem(session: str) -> str:
    return session.rsplit("/", 1)[-1]


class _Session:
    """Hold one session projection and its compact event cache."""

    def __init__(self, session: str, path: Path | None = None):
        now = time.time()
        self.session = session
        self.path = path
        self.label = session
        self.agent = ""
        self.run_id: str | None = None
        self.state = "running"
        self.ts = now
        self.last_ts = now
        self.events: dict[int, dict] = {}
        self.last_seq: int | None = None
        self.disk_size = -1
        self.disk_mtime = 0.0
        self.live_seen_at = 0.0
        self.live_last_ts = 0.0


class Hub:
    """Index compact histories and merge their corresponding live events."""

    def __init__(self, root: str | Path | None = None, *, lease_secs: float = 150):
        """Create a disposable projection over a FileSink directory.

        Args:
            root: Shared compact-history directory. ``None`` creates a live-only
                projection, primarily for embedding and tests.
            lease_secs: File inactivity interval after which an unterminated session
                is reported as aborted.
        """
        self.root = Path(root) if root else None
        self.lease_secs = lease_secs
        self._sessions: dict[str, _Session] = {}

    def _path_for(self, session: str) -> Path | None:
        if self.root is None:
            return None
        return self.root / f"{_file_stem(session)}.jsonl"

    def _new_session(self, session: str, path: Path | None = None) -> _Session:
        value = _Session(session, path or self._path_for(session))
        self._sessions[session] = value
        return value

    def _project(self, session: _Session) -> None:
        ordered = [session.events[seq] for seq in sorted(session.events)]
        starts = [event for event in ordered if event.get("type") == "session/start"]
        if starts:
            head = starts[0]
            data = head.get("data") or {}
            session.label = data.get("label") or session.session
            session.run_id = data.get("runId")
            session.ts = head.get("ts") or session.ts

        agent = fold_state(ordered)["agent"]
        if agent is not None:
            session.agent = agent.get("agent") or ""

        ends = [event for event in ordered if event.get("type") == "session/end"]
        if ends:
            session.state = _session_state((ends[-1].get("data") or {}).get("outcome"))
        elif session.disk_mtime or session.live_seen_at:
            lease_at = max(session.disk_mtime, session.live_seen_at)
            session.state = "aborted" if time.time() - lease_at > self.lease_secs else "running"

        timestamps = [event.get("ts") for event in ordered if event.get("ts") is not None]
        if timestamps:
            session.last_ts = max(session.live_last_ts, *timestamps)

    def _load_path(self, path: Path) -> _Session | None:
        try:
            stat = path.stat()
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            _LOG.warning("cannot read history %s: %s", path.name, exc)
            return None

        events: dict[int, dict] = {}
        last_seq: int | None = None
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                seq = int(event["seq"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            last_seq = seq if last_seq is None else max(last_seq, seq)
            if is_compact_event(event.get("type") or ""):
                events[seq] = event

        head = next((event for event in events.values() if event.get("type") == "session/start"), None)
        if head is None:
            return None
        session_id = head.get("session") or path.stem
        session = self._sessions.get(session_id) or self._new_session(session_id, path)
        session.path = path
        session.events.update(events)
        if last_seq is not None:
            session.last_seq = last_seq if session.last_seq is None else max(session.last_seq, last_seq)
        session.disk_size = stat.st_size
        session.disk_mtime = stat.st_mtime
        self._project(session)
        return session

    def _refresh_path(self, path: Path, session: _Session | None = None) -> _Session | None:
        try:
            stat = path.stat()
        except OSError:
            return session
        if session is None or stat.st_size != session.disk_size:
            return self._load_path(path) or session
        session.disk_mtime = stat.st_mtime
        self._project(session)
        return session

    def scan(self) -> bool:
        """Refresh all histories and leases, including files created since startup.

        Returns:
            Whether the externally visible session index changed.
        """
        before = self.index()
        if self.root is not None:
            by_path = {session.path: session for session in self._sessions.values() if session.path is not None}
            for path in sorted(self.root.glob("*.jsonl")):
                self._refresh_path(path, by_path.get(path))
        return before != self.index()

    def index(self) -> list[dict]:
        """Return session summaries ordered by latest event time."""
        return sorted(
            (
                {
                    "session": session.session,
                    "run_id": session.run_id,
                    "label": session.label,
                    "agent": session.agent,
                    "state": session.state,
                    "ts": session.ts,
                    "last_ts": session.last_ts,
                    "num_events": (session.last_seq + 1) if session.last_seq is not None else 0,
                }
                for session in self._sessions.values()
            ),
            key=lambda item: item["last_ts"],
            reverse=True,
        )

    def history(
        self,
        session_id: str,
        *,
        before: int | None = None,
        limit: int = 200,
    ) -> tuple[list[dict], bool, int | None]:
        """Return one sequence-ordered compact history page.

        Args:
            session_id: Canonical session identifier.
            before: Exclusive sequence upper bound. ``None`` starts from the newest
                retained event.
            limit: Maximum number of events in the page.

        Returns:
            Events, whether an older page exists, and its exclusive upper bound.
        """
        session = self._sessions.get(session_id)
        path = session.path if session is not None else self._path_for(session_id)
        if path is not None:
            session = self._refresh_path(path, session)
        if session is None:
            return [], False, None
        seqs = sorted(seq for seq in session.events if before is None or seq < before)
        page = seqs[-max(1, limit) :]
        has_more = len(seqs) > len(page)
        next_before = page[0] if has_more and page else None
        return [session.events[seq] for seq in page], has_more, next_before

    def last_seq(self, session_id: str) -> int | None:
        """Return a session's raw-stream high-water mark, if known.

        Args:
            session_id: Canonical session identifier.
        """
        session = self._sessions.get(session_id)
        return session.last_seq if session is not None else None

    def delete(self, session_id: str) -> bool:
        """Delete one projected session and its shared history file.

        Args:
            session_id: Canonical session identifier.

        Returns:
            Whether an in-memory session or history file existed.
        """
        if not session_id:
            return False
        session = self._sessions.pop(session_id, None)
        path = session.path if session is not None else self._path_for(session_id)
        existed = session is not None or (path is not None and path.exists())
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                _LOG.warning("cannot delete history %s: %s", path.name, exc)
        return existed

    def ingest(self, events: list[dict]) -> bool:
        """Merge a raw live batch into the disposable projection.

        Args:
            events: Canonical events in producer order. Stream deltas advance the
                high-water mark but are not retained in compact history.

        Returns:
            Whether session identity or terminal state changed.
        """
        changed = False
        for event in events:
            changed = self._record(event) or changed
        return changed

    def _record(self, event: dict) -> bool:
        session_id = event.get("session") or ""
        if not session_id:
            return False
        session = self._sessions.get(session_id)
        created = session is None
        if session is None:
            session = self._new_session(session_id)

        event_type = event.get("type")
        seq = event.get("seq")
        if seq is not None:
            seq = int(seq)
            session.last_seq = seq if session.last_seq is None else max(session.last_seq, seq)
            if is_compact_event(event_type or ""):
                session.events[seq] = event

        previous_state = session.state
        session.live_seen_at = time.time()
        session.live_last_ts = max(session.live_last_ts, event.get("ts") or session.live_seen_at)
        session.last_ts = session.live_last_ts
        if is_compact_event(event_type or ""):
            self._project(session)
        if event_type != "session/end" and session.state == "aborted":
            session.state = "running"
        return (
            created
            or previous_state != session.state
            or event_type
            in {
                "session/start",
                "agent/set",
                "session/end",
            }
        )
