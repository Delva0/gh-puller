# Agent monitor hub

The FastAPI hub serves the built viewer and relays canonical agent events.

- `GET /` and `GET /viewer` return `static/agent_monitor_viewer.html`.
- `WS /ws` accepts producer batches and viewer commands.
- `index`, `history`, `subscribe`, `delete`, and `ping` form the viewer protocol.

Compact history is loaded from flat `<session-stem>.jsonl` files under
`AGENT_MONITOR_DIR`. Raw `model/delta/*` events are forwarded live but are not retained
in hub history. `session/end.data.outcome` determines terminal list state.

Sessions without a terminal event use the JSONL mtime as a lease. Producers touch the
file every `AGENT_MONITOR_HEARTBEAT_SECS`; the hub derives `aborted` after
`AGENT_MONITOR_LEASE_SECS` without progress. A later write restores `running` or applies
the terminal outcome.

Run the service:

```bash
uv --directory apps/agent-monitor/server run uvicorn hub:app --host 0.0.0.0 --port 8765
```

Run its tests:

```bash
uv --directory apps/agent-monitor/server run pytest -q
```

The producer URL normally ends in `/ws`, for example
`AGENT_MONITOR_WEBUI_URL=ws://localhost:8765/ws`.
