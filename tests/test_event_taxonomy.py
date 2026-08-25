"""gh_puller.agent.events 事件溯源模型的本地测试(分类学/信封/折叠恢复规范)。

纯 dict 驱动,零 SDK / 零网络(不依赖 API key / CLI):
- 覆盖:TAXONOMY 分类(type_of/surface/log 拆分)、new_event 信封校验、
  surface 字段强制校验、ignorable 自动标记、truncate 截断;
- 折叠恢复规范 oracle:与 ui/src/monitor/surface.ts 同语义(contract),
  验证任意时刻 messages 可按 seq 前推面逐前缀恢复(append 与 replace 两种 surfaceOp、
  空 content assistant/message 跳过、seq 间隙与未知必需类型报错)。
"""

import pytest

from gh_puller.agent.events import (
    LOG_TYPES,
    SURFACE_TYPES,
    TAXONOMY,
    new_event,
    truncate,
    type_of,
)


def evt(seq: int, evt_type: str, **data: dict) -> dict:
    """测试用具:构造带完整信封的事件(seq 显式;id 确定性,与 new_event 无关)。"""
    step = data.get("step", 1)
    return {
        "id": f"e-{seq:07x}",
        "ts": 1.0,
        "seq": seq,
        "session": "s1",
        "label": "test",
        "provider": "claude",
        "model": "",
        "type": evt_type,
        "data": {"turn": 1, "step": step, **data},
    }


# ---------------------------------------------------------------------------
# 分类学
# ---------------------------------------------------------------------------


def test_type_of():
    for t in TAXONOMY:
        assert type_of(evt(0, t)) == t
    with pytest.raises(ValueError):
        type_of(evt(0, "bogus"))
    with pytest.raises(ValueError):
        type_of({})


def test_surface_and_log_split():
    assert SURFACE_TYPES <= TAXONOMY
    assert LOG_TYPES <= TAXONOMY
    assert SURFACE_TYPES.isdisjoint(LOG_TYPES)
    # surface 恰为三件(折叠集合,无增删)
    assert {"user/message", "assistant/message", "tool/result"} == SURFACE_TYPES


# ---------------------------------------------------------------------------
# 信封
# ---------------------------------------------------------------------------


def test_new_event_envelope_and_jsonable():
    e = new_event("user/message", message={"role": "user", "content": [{"type": "text", "text": "hi"}]},
                  source={"kind": "user"}, surfaceOp="append")
    assert e["type"] == "user/message"
    assert e["id"].startswith("e-")
    assert isinstance(e["ts"], float)
    assert e["data"]["message"]["role"] == "user"
    assert "seq" not in e  # seq 由适配器分配,new_event 不写
    with pytest.raises(ValueError):
        new_event("nope")
    with pytest.raises(TypeError):  # set 不可 JSON 化
        new_event("session/start", meta={"bad": {1}})


def test_new_event_surface_validation():
    # surface 缺 message / 缺合法 surfaceOp 直接报错(防折叠不可复现)
    with pytest.raises(ValueError):
        new_event("user/message", source={"kind": "user"}, surfaceOp="append")
    with pytest.raises(ValueError):
        new_event("assistant/message", message={"role": "assistant", "content": []})
    with pytest.raises(ValueError):
        new_event("assistant/message", message={"role": "assistant", "content": []},
                  surfaceOp="replace")  # 无 start/end 的非 append op 不合法


def test_new_event_replace_op_and_ignorable():
    e = new_event(
        "user/message", message={"role": "user", "content": [{"type": "text", "text": "新"}]},
        source={"kind": "context", "label": "注记"}, surfaceOp={"op": "replace", "start": 3, "end": 3},
    )
    assert e["data"]["surfaceOp"] == {"op": "replace", "start": 3, "end": 3}
    log = new_event("request/header", header={"config": {"provider": "openai", "model": "m"}}, reason="initial")
    assert log.get("ignorable") is True  # 日志型事件带 ignorable 标记
    assert "ignorable" not in new_event(
        "session/start", label="l", provider="openai", model="m")  # 必需事件无标记


def test_truncate():
    assert truncate(None, 40) == (0, "")
    assert truncate("", 40) == (0, "")
    assert truncate("abc", 40) == (3, "abc")
    assert truncate("abcdef", 3) == (6, "abc…")


# ---------------------------------------------------------------------------
# 折叠恢复规范 oracle(与 ui/src/monitor/surface.ts 同语义;契约)
# ---------------------------------------------------------------------------


def fold_events(events: list[dict]) -> tuple[list[int], dict[int, dict]]:
    """规范折叠:按 seq 升序重放,返回 (surface 节点 seq 有序列表, seq→事件映射)。

    校验:seq 必须连续 0 起始(间隙 → ValueError);未知类型按 ignorable 规则
    (带标记跳过,否则报错 —— 防读者静默吞掉必需的事故)。
    """
    evs = sorted(events, key=lambda e: e["seq"])
    order = [e["seq"] for e in evs]
    if order != list(range(len(evs))):
        raise ValueError(f"seq 不连续: {order}")
    nodes: list[int] = []
    by_seq: dict[int, dict] = {}
    for evt in evs:
        t = evt.get("type")
        if t not in TAXONOMY:
            if evt.get("ignorable"):
                continue
            raise ValueError(f"未知必需事件 type: {t!r}")
        by_seq[evt["seq"]] = evt
        if t not in SURFACE_TYPES:
            continue
        op = evt["data"]["surfaceOp"]
        if op == "append":
            nodes.append(evt["seq"])
        else:
            start, end = op["start"], op["end"]
            try:
                si, ei = nodes.index(start), nodes.index(end)
            except ValueError:
                raise ValueError(f"replace 引用不存在的节点: {op!r}") from None
            if si > ei:
                raise ValueError(f"replace 区间倒置: {op!r}")
            nodes[si:ei + 1] = [evt["seq"]]
    return nodes, by_seq


def derive_message(evt: dict) -> dict | None:
    """surface 事件 → (模型可见)消息;非 surface/空 content assistant → None。"""
    msg = evt["data"]["message"]
    if evt["type"] == "assistant/message":
        return msg if msg.get("content") else None
    return msg


def messages_at(events: list[dict], x: int) -> list[dict]:
    """任意时刻 x(seq 排他)的 messages = 折叠 seq < x 的事件前缀再派生。

    先折前缀再派生:replace 若不在前缀内,被遮蔽节点此刻仍可见 —— 这正是
    "每时每刻恢复"的语义。
    """
    nodes, by_seq = fold_events([e for e in events if e["seq"] < x])
    out = []
    for seq in nodes:
        m = derive_message(by_seq[seq])
        if m is not None:
            out.append(m)
    return out


def user_msg(text: str, *, source: dict | None = None, surface: str | dict = "append",
             start: int = 0, end: int = 0) -> dict:
    """user/message 便捷构造(source 缺省 user;surface 为 append 或 replace op)。"""
    src = source if source is not None else {"kind": "user"}
    data = {"message": {"role": "user", "content": [{"type": "text", "text": text}]},
            "source": src, "surfaceOp": surface}
    if isinstance(surface, dict):
        data["surfaceOp"] = {"op": "replace", "start": start, "end": end}
    return data


def test_fold_cc_multi_request_exact_contexts():
    """cc 多请求(工具链):每个前缀的 messages 与期望逐项一致。"""
    events = [
        evt(0, "session/start", label="wiki:structure", provider="claude", model=""),
        evt(1, "turn/start", turn=1),
        evt(2, "step/start", step=1),
        evt(3, "user/message", **user_msg("how does auth work?")),
        evt(4, "request/header", header={"config": {"provider": "claude", "model": ""},
                                         "system": "s", "tools": []}, reason="initial", partial=True),
        evt(5, "assistant/chunk", chunk={"type": "thinking", "index": 0, "text": "分"}),
        evt(6, "assistant/chunk", chunk={"type": "thinking", "index": 0, "text": "析"}),
        evt(7, "assistant/chunk", chunk={"type": "text", "index": 0, "text": "检"}),
        evt(8, "assistant/chunk", chunk={"type": "text", "index": 0, "text": "查"}),
        evt(9, "assistant/message",
            message={"role": "assistant", "content": [
                {"type": "thinking", "thinking": "分析"},
                {"type": "text", "text": "检查"},
                {"type": "tool_use", "id": "t1", "name": "graphify_query",
                 "input": {"question": "auth?"}},
            ]},
            usage={"input_tokens": 5, "output_tokens": 3}, surfaceOp="append", sourceSeqs=[5, 6, 7, 8]),
        evt(10, "tool/call", callId="t1", name="graphify_query", arguments='{"question": "auth?"}'),
        evt(11, "tool/result",
            message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "auth: 详见 codemap", "is_error": False},
            ]},
            surfaceOp="append", sourceSeqs=[10]),
        evt(12, "step/end", step=1),
        evt(13, "step/start", step=2),
        evt(14, "assistant/chunk", chunk={"type": "text", "index": 0, "text": "答案"}),
        evt(15, "assistant/chunk", chunk={"type": "text", "index": 0, "text": "是…"}),
        evt(16, "assistant/message",
            message={"role": "assistant", "content": [{"type": "text", "text": "答案是…"}]},
            usage={"input_tokens": 6, "output_tokens": 2}, surfaceOp="append", sourceSeqs=[14, 15]),
        evt(17, "step/end", step=2),
        evt(18, "turn/end", turn=1, reason="completed"),
        evt(19, "session/end", state="completed", ok=True, duration_ms=100, text_chars=4),
    ]
    fold_events(events)
    # 请求 1 平面(首个 chunk seq=5):该步 user 消息已折叠入
    assert messages_at(events, 5) == [
        {"role": "user", "content": [{"type": "text", "text": "how does auth work?"}]},
    ]
    # 请求 2 平面(seq=14):user + assistant(工具调用) + tool_result
    assert messages_at(events, 14) == [
        {"role": "user", "content": [{"type": "text", "text": "how does auth work?"}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "分析"},
            {"type": "text", "text": "检查"},
            {"type": "tool_use", "id": "t1", "name": "graphify_query", "input": {"question": "auth?"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "auth: 详见 codemap", "is_error": False},
        ]},
    ]
    # 收尾:加上第二条 assistant 正文
    tail = messages_at(events, 20)
    assert len(tail) == 4 and tail[-1]["content"][0]["type"] == "text"


def test_fold_replace_op_after_context_modify():
    """上下文修改:replace 遮蔽折叠(如聊天历史被 trim 后整条替换),折叠只认 surfaceOp;
    context/modify 自身只做解释,不折动。"""
    events = [
        evt(0, "session/start", label="chat:r", provider="claude", model=""),
        evt(1, "turn/start", turn=1),
        evt(2, "step/start", step=1),
        evt(3, "user/message", **user_msg("旧问题")),
        evt(4, "context/modify", target="chat-history", kind="trim", cause="token-limit",
            detail="省略对话历史", removed={"n_turns": 1, "est_tokens": 100}),
        evt(5, "request/header", header={"config": {"provider": "claude", "model": ""}}, reason="initial"),
        evt(6, "user/message", **user_msg("新问题", surface={"op": "replace", "start": 3, "end": 3},
                                          start=3, end=3)),
    ]
    nodes, _ = fold_events(events)
    assert nodes == [6]  # replace 遮蔽旧节点:最终可见只有新消息
    # replace 生效前(seq<6):旧消息仍可见(每时每刻恢复的语义)
    assert messages_at(events, 4) == [
        {"role": "user", "content": [{"type": "text", "text": "旧问题"}]},
    ]
    # 生效后:派生只剩新消息
    assert messages_at(events, 10) == [
        {"role": "user", "content": [{"type": "text", "text": "新问题"}]},
    ]


def test_fold_empty_assistant_skipped():
    """usage-only assistant/message(surfaceOp 仍 append)不进入消息派生。"""
    events = [
        evt(0, "session/start", label="l", provider="openai", model="m"),
        evt(1, "turn/start", turn=1),
        evt(2, "step/start", step=1),
        evt(3, "user/message", **user_msg("q")),
        evt(4, "assistant/message", message={"role": "assistant", "content": []},
            usage={"input_tokens": 1, "output_tokens": 0}, surfaceOp="append"),
        evt(5, "assistant/message", message={"role": "assistant", "content": [{"type": "text", "text": "a"}]},
            surfaceOp="append"),
        evt(6, "session/end", state="completed", ok=True),
    ]
    fold_events(events)
    assert messages_at(events, 7) == [
        {"role": "user", "content": [{"type": "text", "text": "q"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
    ]


def test_fold_gap_and_unknown_type_errors():
    """seq 间隙 → 报错(调用方须先以 history 修补再折叠);未知必需类型 → 报错。"""
    with pytest.raises(ValueError, match="不连续"):
        fold_events([evt(0, "session/start", label="l"), evt(2, "turn/start", turn=1)])
    ok = [evt(0, "session/start", label="l"),
          {**evt(1, "bogus/type", note="未来版本的可忽略事件"), "ignorable": True},
          evt(2, "session/end", state="completed", ok=True)]
    nodes, _ = fold_events(ok)  # ignorable 未知类型安全跳过
    assert nodes == []
    with pytest.raises(ValueError, match="未知必需事件"):
        fold_events([evt(0, "session/start", label="l"),
                     evt(1, "bogus/type", note="未知且无标记 → 必须报错")])


def test_fold_replace_unknown_shadow_raises():
    """replace 引用不存在节点 → 报错(日志损坏/跨会话合并前暴露)。"""
    events = [evt(0, "session/start", label="l"),
              evt(1, "user/message", **user_msg("x")),
              evt(2, "user/message", **user_msg("y", surface={"op": "replace", "start": 5, "end": 5},
                                               start=5, end=5))]
    with pytest.raises(ValueError, match="不存在的节点"):
        fold_events(events)
