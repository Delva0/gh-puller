"""Derive portable activity measurements from one canonical Agent event log.

This is a read-only projection, not another recorder or an execution policy.
Only matched activity boundaries with ``elapsedMs`` produce durations. Missing
responses or counters remain unknown, and backend footer usage is not added to
per-request usage. Stream intervals measure observed deltas, not token speed or
server queue time. Incomplete event sequences cannot establish stream intervals.
"""

import json
from collections.abc import Iterable, Iterator
from pathlib import Path


def read_events(path: str | Path) -> Iterator[dict]:
    """Read canonical JSONL in recorded order without loading model bodies together.

    Args:
        path: A completed log, or a stable prefix ending at a complete JSONL line.
            Malformed or partial lines raise their JSON decoding error.
    """
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def _seconds(start, end):
    if start is None or end is None or end < start:
        return None
    return (end - start) / 1000


def _sum_known(values):
    values = list(values)
    return None if any(value is None for value in values) else sum(values)


def summarize(events: Iterable[dict]) -> dict:
    """Project request, tool and turn activity with conservative totals.

    Args:
        events: One session's canonical events in original sequence order. A
            compact or unfinished log is accepted with unavailable metrics left
            unknown. Multiple sessions must be summarized separately.

    Returns:
        JSON-compatible measurements without prompts, outputs or tool arguments.
        Request/tool durations are sums of spans, not a concurrency critical path.
        ``events_complete`` means contiguous sequence numbers, not a finished run.

    Raises:
        ValueError: Events contain more than one session or repeat an activity id.
    """
    session = None
    previous = -1
    complete = True
    requests, tools, turns = {}, {}, []
    start = finish = None
    footer = {}
    for event in events:
        identity = event.get("session")
        if session is not None and identity != session:
            raise ValueError("Summarize one observation session at a time")
        session = identity
        seq = event.get("seq")
        complete &= seq == previous + 1
        previous = seq if isinstance(seq, int) else previous
        kind, data, tick = event["type"], event["data"], event.get("elapsedMs")
        if kind == "session/start":
            start = tick
        elif kind == "session/end":
            finish, footer = tick, data
        elif kind == "turn/start":
            turns.append({"start": tick, "seconds": None, "outcome": None})
        elif kind == "turn/end" and turns:
            turns[-1].update(seconds=_seconds(turns[-1]["start"], tick), outcome=data.get("outcome"))
        elif kind in {"model/request", "tool/start"}:
            model = kind == "model/request"
            records, key = (requests, "requestId") if model else (tools, "callId")
            identifier = data[key]
            if identifier in records:
                raise ValueError(f"Repeated activity identity: {identifier}")
            record = {key: identifier, "start": tick, "seconds": None, "completed": False}
            if model:
                record.update(model=data.get("model"), request_sha256=data.get("requestSha256"),
                              usage=None, raw_usage=None, stop_reason=None, delta_count=0,
                              first_delta_seconds=None, max_delta_gap_seconds=None, last_delta=None, error=None)
            else:
                record.update(name=data.get("name"), error=None)
            records[identifier] = record
        elif kind.startswith("model/delta/") and data["requestId"] in requests:
            record = requests[data["requestId"]]
            if record["delta_count"] == 0:
                record["first_delta_seconds"] = _seconds(record["start"], tick)
            gap = _seconds(record["last_delta"], tick)
            if gap is not None:
                record["max_delta_gap_seconds"] = max(record["max_delta_gap_seconds"] or 0, gap)
            record["last_delta"] = tick
            record["delta_count"] += 1
        elif kind == "model/response" and data["requestId"] in requests:
            record = requests[data["requestId"]]
            record.update(seconds=_seconds(record["start"], tick), completed=True,
                          model=data.get("model", record["model"]), usage=data.get("usage"),
                          raw_usage=data.get("rawUsage"), stop_reason=data.get("stopReason"))
        elif kind == "model/error" and data["requestId"] in requests:
            record = requests[data["requestId"]]
            record.update(seconds=_seconds(record["start"], tick),
                          error=(data.get("error") or {}).get("type"))
        elif kind == "tool/end" and data["callId"] in tools:
            record = tools[data["callId"]]
            record.update(seconds=_seconds(record["start"], tick), completed=True, error="error" in data)
    rows = list(requests.values())
    fields = set.intersection(*(set(row["usage"] or {}) for row in rows)) if rows else set()
    usage = {field: sum(row["usage"][field] for row in rows) for field in sorted(fields)}
    for row in rows:
        del row["last_delta"]
        if not complete:
            row.update(first_delta_seconds=None, max_delta_gap_seconds=None)
    return {"session": session, "outcome": footer.get("outcome"), "reason_code": footer.get("reasonCode"),
            "events_complete": complete, "seconds": _seconds(start, finish), "turns": turns,
            "requests": rows, "tools": list(tools.values()), "usage": usage,
            "model_seconds": _sum_known(row["seconds"] for row in rows),
            "tool_seconds": _sum_known(row["seconds"] for row in tools.values())}
