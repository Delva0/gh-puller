"""gh_puller.agent.events 事件分类学与 LLM 流聚合器的本地测试。

纯 dict 驱动,零 SDK / 零网络(不依赖 API key / CLI):
- 覆盖:KINDS 分类、new_event 信封校验、truncate 截断,
  LlmAggregator 的归并语义(thinking/content 各并 1 块、agent 多轮 vs llm 单轮、
  兜底自动开块、三态状态机 running→completed/aborted、未知 kind 拒绝)。
"""

import pytest

from gh_puller.agent.events import (
    KINDS,
    LlmAggregator,
    aggregate_all,
    kind_of,
    new_event,
    truncate,
)


def evt(kind: str, **fields: dict) -> dict:
    """测试用具:构造带会话信封的事件(round 缺省 0)。"""
    base = {
        "id": f"e-{len(fields):07x}",
        "ts": 1.0,
        "session": "s1",
        "label": "test",
        "provider": "claude",
        "model": "",
        "round": 0,
    }
    base.update(fields)
    return {"kind": kind, **base}


# ---------------------------------------------------------------------------
# 事件分类学
# ---------------------------------------------------------------------------


def test_kind_of():
    for k in KINDS:
        assert kind_of(evt(k)) == k
    with pytest.raises(ValueError):
        kind_of(evt("bogus"))
    with pytest.raises(ValueError):
        kind_of({})


def test_new_event_envelope_and_jsonable():
    e = new_event("text.delta", text="hi")
    assert e["kind"] == "text.delta"
    assert e["id"].startswith("e-")
    assert isinstance(e["ts"], float)
    with pytest.raises(ValueError):
        new_event("nope")
    with pytest.raises(TypeError):  # set 不可 JSON 化
        new_event("run.start", bad={1})


def test_truncate():
    assert truncate(None, 40) == (0, "")
    assert truncate("", 40) == (0, "")
    assert truncate("abc", 40) == (3, "abc")
    assert truncate("abcdef", 3) == (6, "abc…")


# ---------------------------------------------------------------------------
# LlmAggregator:事件流 → LLM 流
# ---------------------------------------------------------------------------


def _cc_multi_round_events() -> list[dict]:
    """agent 多轮:第 0 轮思想+正文+工具调用,工具结果后进入第 1 轮。"""
    return [
        evt("run.start", prompt_chars=20, prompt_preview="how does auth work?"),
        evt("block.start", block_type="thinking"),
        evt("thinking.delta", text="分析"),
        evt("thinking.delta", text="代码"),
        evt("block.stop", block_type="thinking"),
        evt("block.start", block_type="content"),
        evt("text.delta", text="我在检"),
        evt("text.delta", text="查图谱"),
        evt("block.stop", block_type="content"),
        evt("block.start", block_type="tool_use", tool_id="t1", tool_name="graphify_query"),
        evt("block.stop", block_type="tool_use", tool_input={"question": "auth?"}),
        evt("tool.result", tool_name="graphify_query", tool_id="t1",
            is_error=False, content_chars=3, content_preview="lines"),
        evt("block.start", round=1, block_type="content"),
        evt("text.delta", round=1, text="答案是…"),
        evt("block.stop", round=1, block_type="content"),
        evt("run.end", round=1, ok=True),
    ]


def test_aggregator_cc_multi_round_merges_blocks():
    agg, lines = aggregate_all(_cc_multi_round_events())
    types = [ln["type"] for ln in lines]
    assert agg.state == "completed"
    assert types[0:3] == ["session.start", "round.start", "block.start"]
    assert types[-1] == "session.end" and lines[-1]["state"] == "completed"
    # thinking / content 各合并为 1 块(每轮 1 个 thinking + 1 个 content)
    thinking = [ln for ln in lines if ln["type"] == "block.start" and ln["block_type"] == "thinking"]
    content_r0 = [ln for ln in lines
                  if ln["type"] == "block.start" and ln["block_type"] == "content" and ln["round"] == 0]
    content_r1 = [ln for ln in lines
                  if ln["type"] == "block.start" and ln["block_type"] == "content" and ln["round"] == 1]
    assert len(thinking) == 1
    assert len(content_r0) == 1 and len(content_r1) == 1
    # 第 0 轮 chunk 归属两块:thinking=seq0(2 chunk),content=seq1(2 chunk)
    seqs = {ln["seq"] for ln in lines if ln["type"] == "block.delta" and ln["round"] == 0}
    assert seqs == {0, 1}
    # 工具块终值入 block.end;第 1 轮输入 = 工具结果
    tool_end = next(ln for ln in lines
                    if ln["type"] == "block.end" and ln.get("tool_input") is not None)
    assert tool_end["tool_input"] == {"question": "auth?"}
    rounds = [ln for ln in lines if ln["type"] == "round.start"]
    assert rounds[1]["input_kind"] == "tool" and rounds[1]["input_preview"] == "lines"
    assert len(rounds) == 2 and len([ln for ln in lines if ln["type"] == "round.end"]) == 2


def test_aggregator_openai_single_round():
    events = [
        evt("run.start", provider="openai", prompt_chars=9, prompt_preview="judge q"),
        evt("block.start", provider="openai", block_type="content"),
        evt("text.delta", provider="openai", text="{"),
        evt("block.stop", provider="openai", block_type="content"),
        evt("result", provider="openai", text_chars=30, duration_ms=50,
            usage={"input_tokens": 1, "output_tokens": 2}),
        evt("run.end", provider="openai", ok=True, duration_ms=50),
    ]
    _, lines = aggregate_all(events)
    assert len([ln for ln in lines if ln["type"] == "round.start"]) == 1
    assert len([ln for ln in lines if ln["type"] == "round.end"]) == 1
    assert lines[-1]["state"] == "completed"
    assert lines[-1]["usage"] == {"input_tokens": 1, "output_tokens": 2}


def test_aggregator_fallback_auto_block():
    """无 partial 事件时 AssistantMessage 兜底:直接 text.delta → 自动开 content 块。"""
    events = [evt("run.start", prompt_preview="p"), evt("text.delta", text="整块文本"),
              evt("run.end", ok=True)]
    _, lines = aggregate_all(events)
    starts = [ln for ln in lines if ln["type"] == "block.start"]
    assert len(starts) == 1 and starts[0]["block_type"] == "content"


def test_aggregator_aborted_by_error():
    events = [
        evt("run.start", prompt_preview="p"),
        evt("text.delta", text="部分"),
        evt("error", exc_type="RuntimeError", message="agent 执行失败", stage="run"),
    ]
    agg, lines = aggregate_all(events)
    assert agg.state == "aborted"
    assert lines[-1]["state"] == "aborted"
    assert "RuntimeError" in lines[-1]["reason"]
    assert agg.feed(evt("text.delta", text="x")) == []  # 终态后续事件被忽略(幂等)
    assert agg.lines == lines


def test_aggregator_run_end_aborted():
    _, lines = aggregate_all([evt("run.start"), evt("run.end", ok=False)])
    assert lines[-1]["state"] == "aborted"


def test_aggregator_unknown_kind():
    agg = LlmAggregator("s1", "test", "claude", "")
    with pytest.raises(ValueError):
        agg.feed({"kind": "bogus"})


def test_aggregator_delta_carries_chunk_only():
    """增量契约:delta 行只携带本块文本(不拼接累计、不重发早前行),feed 只产出该事件归属行。"""
    agg = LlmAggregator("s1", "test", "claude", "")
    first = agg.feed(evt("run.start", prompt_preview="p"))
    second = agg.feed(evt("text.delta", text="你好"))
    third = agg.feed(evt("text.delta", text="世界"))
    # 首个 text.delta 触发兜底自动开块(block.start + delta);后续 delta 只产出本行
    assert [ln["type"] for ln in second] == ["block.start", "block.delta"]
    assert [ln["type"] for ln in third] == ["block.delta"]
    delta_texts = [ln["text"] for ln in second + third if ln["type"] == "block.delta"]
    assert delta_texts == ["你好", "世界"]
    # 任一输出行不得携带累计拼接文本(流量杀手:逐帧重发累计全文)
    assert "你好世界" not in [ln.get("text", "") for ln in first + second + third]
    assert [ln for ln in first if ln["type"] == "block.delta"] == []
