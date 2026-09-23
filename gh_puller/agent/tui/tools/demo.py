"""Emit a small synthetic canonical session for screenshots and local exploration."""

# Standalone helper for the Cargo project, not a Python package.
# ruff: noqa: INP001

import json


def message(role, text):
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


def main():
    thought = {
        "type": "reasoning",
        "content": [{"type": "reasoning_text", "text": "先核对事件契约，再检查日志结束时的原子替换。"}],
    }
    call = {
        "type": "function_call",
        "call_id": "read-1",
        "name": "read_file",
        "arguments": '{"path":"gh_puller/agent/events.py"}',
    }
    answer = message(
        "assistant",
        "## 上下文已重建\n\n日志在结束后完成压缩，当前视图保留完整上下文。\n\n"
        "| 检查项 | 结果 |\n|---|---|\n| context/set | 整体替换 |\n| 工具调用和结果 | 按 call_id 配对 |\n"
        "| 原始 seq | 保留 |\n\n```rust\nassert_eq!(live_context, compact_context);\n```\n\n"
        "[Ratatui 文档](https://ratatui.rs/) · 支持中文选区、宽表格和代码横向滚动。",
    )
    events = [
        (0, "session/start", {"label": "Agent · Context observer"}),
        (0, "context/append/system", {"items": [message("system", "You are a helpful coding agent.")]}),
        (10, "turn/start", {}),
        (10, "context/append/user", {"items": [message("user", "请检查 FileSink 压缩后是否仍能重建相同的上下文。")]}),
        (20, "step/start", {}),
        (20, "model/request", {"requestId": "r1", "model": "offline-fixture"}),
        (100, "model/delta/reasoning", {"requestId": "r1", "index": 0, "text": "先核对事件契约"}),
        (
            400,
            "model/response",
            {"requestId": "r1", "output": [thought, call], "usage": {"input": 1280, "output": 93, "cacheRead": 1024}},
        ),
        (400, "context/append/assistant", {"items": [thought, call]}),
        (
            410,
            "tool/start",
            {"callId": "read-1", "name": "read_file", "arguments": {"path": "gh_puller/agent/events.py"}},
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
                        "output": "context/set replaces Item[]. context/append/* appends Item[]. "
                        "Activity never changes the fold.",
                    },
                ],
            },
        ),
        (740, "step/end", {}),
        (750, "step/start", {}),
        (750, "model/request", {"requestId": "r2", "model": "offline-fixture"}),
        (900, "model/delta/text", {"requestId": "r2", "index": 0, "text": "## 上下文已重建"}),
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
