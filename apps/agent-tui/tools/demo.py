"""Emit a small synthetic canonical session for screenshots and local exploration."""

# Standalone helper for the Cargo project, not a Python package.
# ruff: noqa: INP001

import json


def message(role, text):
    content_type = "output_text" if role == "assistant" else "input_text"
    return {"type": "message", "role": role, "content": [{"type": content_type, "text": text}]}


def main():
    thought = {
        "type": "reasoning",
        "content": [{
            "type": "reasoning_text",
            "text": "Check which events compaction removes and whether it preserves context event order.",
        }],
    }
    call = {
        "type": "function_call",
        "call_id": "read-1",
        "name": "read_file",
        "arguments": '{"path":"gh_puller/agent/sinks.py"}',
    }
    answer = message(
        "assistant",
        "## FileSink compaction\n\nCompleted logs reconstruct the same context. Compaction removes only "
        "model deltas; context events keep their original content and order.\n\n"
        "| Check | Result |\n|---|---|\n| Context events | Preserved |\n| Original seq | Preserved |\n"
        "| Open readers | Can finish reading the original file |\n\n"
        "```python\nassert fold_state(full_log) == fold_state(compact_log)\n```\n\n"
        "The replacement uses [os.replace](https://docs.python.org/3/library/os.html#os.replace) "
        "after the temporary file is complete.",
    )
    events = [
        (0, "session/start", {"label": "FileSink compaction"}),
        (0, "context/append/system", {"items": [message(
            "system", "Check code against the event contract and report any data loss.",
        )]}),
        (10, "turn/start", {}),
        (10, "context/append/user", {"items": [message(
            "user", "Does FileSink compaction preserve the context? "
            "Check event ordering and readers with an open file handle.",
        )]}),
        (20, "step/start", {}),
        (20, "model/request", {"requestId": "r1", "model": "offline-fixture"}),
        (100, "model/delta/reasoning", {
            "requestId": "r1", "index": 0, "text": "Check which events compaction removes",
        }),
        (
            400,
            "model/response",
            {"requestId": "r1", "output": [thought, call], "usage": {"input": 1280, "output": 93, "cacheRead": 1024}},
        ),
        (400, "context/append/assistant", {"items": [thought, call]}),
        (
            410,
            "tool/start",
            {"callId": "read-1", "name": "read_file", "arguments": {"path": "gh_puller/agent/sinks.py"}},
        ),
        (730, "tool/end", {"callId": "read-1", "result": "Read 300 lines"}),
        (
            730,
            "context/append/tool",
            {
                "items": [
                    {
                        "type": "function_call_output",
                        "call_id": "read-1",
                        "output": "FileSink writes and flushes every event. After session/end, _compact copies "
                        "all non-delta records to a temporary file, then calls os.replace. "
                        "Context records retain their original bytes and sequence numbers.",
                    },
                ],
            },
        ),
        (740, "step/end", {}),
        (750, "step/start", {}),
        (750, "model/request", {"requestId": "r2", "model": "offline-fixture"}),
        (900, "model/delta/text", {"requestId": "r2", "index": 0, "text": "## FileSink compaction"}),
        (
            2300,
            "model/response",
            {"requestId": "r2", "output": [answer], "usage": {"input": 1536, "output": 256, "cacheRead": 1024}},
        ),
        (2300, "context/append/assistant", {"items": [answer]}),
        (2310, "step/end", {}),
        (2320, "turn/end", {}),
        (2330, "session/end", {"outcome": "completed", "reasonCode": "completed"}),
    ]
    for seq, (at, kind, data) in enumerate(events):
        print(
            json.dumps(
                {"session": "demo", "seq": seq, "elapsedMs": at, "type": kind, "data": data}, ensure_ascii=False,
            ),
        )


if __name__ == "__main__":
    main()
