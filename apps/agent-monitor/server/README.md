# Agent monitor server

The monitor is a local sidecar over the compact histories written by
`gh_puller.agent.sinks.FileSink`. The producer and server share
`AGENT_MONITOR_DIR`; those JSONL files are the durable source.

- `hub.py` scans files and projects session indexes, history pages, and leases. Its
  in-memory state is disposable and has no transport dependency.
- `app.py` serves the viewer, handles WebSocket frames and subscriptions, and merges
  raw live events into the projection for immediate display.

- `GET /` and `GET /viewer` return `static/agent_monitor_viewer.html`.
- `WS /ws` accepts producer batches and viewer commands.
- `index`, `history`, `subscribe`, `delete`, and `ping` form the viewer protocol.

Compact history uses flat `<session-stem>.jsonl` files. Raw `model/delta/*` events are
forwarded live but are absent from compact history. `session/end.data.outcome`
determines terminal list state. Deleting a session removes both its projection and
shared JSONL file.

Sessions without a terminal event use the JSONL mtime as a lease. Producers touch the
file every `AGENT_MONITOR_HEARTBEAT_SECS`; the hub derives `aborted` after
`AGENT_MONITOR_LEASE_SECS` without progress. A later write restores `running` or applies
the terminal outcome.

Run the service:

```bash
uv --directory apps/agent-monitor/server run uvicorn app:app --port 8765
```

Run its tests:

```bash
uv --directory apps/agent-monitor/server run pytest -q
```

The producer URL normally ends in `/ws`, for example
`AGENT_MONITOR_WEBUI_URL=ws://localhost:8765/ws`.
