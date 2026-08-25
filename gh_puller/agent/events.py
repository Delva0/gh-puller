"""agent 调用的可观测事件模型(纯 dict 实现,零 SDK 依赖)。

事件流:UI 变化的原子单元,由 gh_puller.agent.adapters 的适配器归一化产出并经
EventBus 扇出(本模块只认普通 dict,严禁 import claude_agent_sdk / httpx ——
测试只喂假 dict)。
LLM 流:事件流的增量聚合(用户定义的语义)——thinking 的 chunked text 全部并入
1 个 thinking 块、content 的 chunked text 全部并入 1 个 content 块,按 round(轮次)
归并;llm 流单轮(user↔assistant 一次交互),agent 流多轮(直到无工具调用);
三方 LLM 工具调用无流式支持时单块单轮,终值一次捕获。
"""

import json
import time
import uuid

KINDS = frozenset(
    {
        "run.start",
        "block.start",
        "text.delta",
        "thinking.delta",
        "block.stop",
        "tool.use",  # 工具调用终值(事件流专用;聚合见 block.stop 的 tool_input)
        "tool.result",
        "message.assistant",
        "result",
        "error",
        "run.end",
    }
)

# LLM 流行的 type(聚合产物;与事件 kind 是两个命名空间)
LLM_STREAM_TYPES = frozenset(
    {
        "session.start",
        "round.start",
        "block.start",
        "block.delta",
        "block.end",
        "tool.result",
        "round.end",
        "session.end",
    }
)


def kind_of(evt: dict) -> str:
    """返回事件 kind;未知事件 → ValueError。"""
    k = evt.get("kind")
    if k not in KINDS:
        raise ValueError(f"未知事件 kind: {k!r}")
    return k


def new_event(evt_kind: str, **fields) -> dict:
    """构造事件:补 id/ts 信封,校验 kind 与字段可 JSON 序列化。

    会话属性(session/label/provider/model/seq/round)由适配器附加,不在此处。
    """
    if evt_kind not in KINDS:
        raise ValueError(f"未知事件 kind: {evt_kind!r}")
    evt = {"id": f"e-{uuid.uuid4().hex[:7]}", "ts": time.time(), "kind": evt_kind, **fields}
    json.dumps(evt)  # 校验可 JSON 化(如 set 字段 → TypeError)
    return evt


def truncate(text: str | None, n: int) -> tuple[int, str]:
    """返回 (原长度, 预览):超长截断加省略号;None/空 → (0, "")。"""
    if not text:
        return (0, "")
    if len(text) <= n:
        return (len(text), text)
    return (len(text), text[:n] + "…")


class LlmAggregator:
    """事件流 → LLM 流的增量聚合器(纯 dict,无 IO;一处实现,FileSink/hub 共用)。

    规则(用户定义的语义):
    - 每 round 至多 1 个 thinking 块 + 1 个 content 块(全部 chunk 合并并入),工具块 0..n;
    - round 边界由适配器计(事件信封携带 round):run.start 开第 0 轮(llm 流单轮),
      agent 流每个工具结果后的新一轮 assistant 产出进入下一轮(多轮);
    - 状态机:running → completed(run.end ok)/ aborted(error 或 run.end 非 ok)。
    """

    def __init__(self, session: str, label: str, provider: str, model: str):
        self.session = session
        self.label = label
        self.provider = provider
        self.model = model
        self.state = "running"
        self.lines: list[dict] = []  # 聚合产物(每行自描述,直接可写盘/回放)
        self._last_round = 0
        self._payload: dict = {}  # 供 session.end 汇总的字段
        self._meta: dict | None = None
        self._round_open = False
        self._open: str | None = None  # 当前块类型:thinking|content|tool_use|None
        self._seq = -1  # 每 round 内块序号(block.seq)
        self._pending_input_kind: str | None = None  # tool.result 后新一轮输入归属
        self._pending_input_preview: str | None = None
        self._done = False

    def feed(self, evt: dict) -> list[dict]:
        """喂一个事件流 dict,返回本次产生的 LLM 流行(可能为空)。"""
        if self._done:
            return []
        self._meta = evt.get("meta") or self._meta
        # kind 为点式命名(run.start / text.delta…),方法名把点换成下划线;
        # kind 为 tool.use 时无 handler(事件流专用,tool 终值经 block.stop 的 tool_input 聚合)
        handler = getattr(self, f"_on_{evt['kind'].replace('.', '_')}", None) \
            if evt.get("kind") in KINDS else None
        if handler is None:
            kind_of(evt)  # 未知 kind 抛 ValueError
            return []
        new = handler(evt) or []
        self.lines.extend(new)
        return new

    def _line(self, type_: str, evt: dict, **fields) -> dict:
        """构造一条 LLM 流行(self-描述行:含 ts;其余字段来自事件/聚合状态)。"""
        return {"type": type_, "ts": evt.get("ts"), **fields}

    # ---- 状态机 ----

    def _finalize(self, state: str, evt: dict) -> None:
        """终态:收尾开放块/轮次 → session.end(幂等)。"""
        self._done = True
        if self.state == "running":
            lines: list[dict] = self._on_block_stop_quiet(evt) if self._open else []
            lines += self._close_round(evt) if self._round_open else []
            self.state = state
            summary: dict = {
                "state": state,
                "duration_ms": self._payload.get("duration_ms"),
                "text_chars": self._payload.get("text_chars"),
                "num_rounds": self._last_round + 1,
            }
            if self._payload.get("usage"):
                summary["usage"] = self._payload["usage"]
            if state == "aborted" and evt.get("kind") == "error":
                summary["reason"] = f"{evt.get('exc_type', '')}: {evt.get('message', '')}"[:500]
            lines.append(self._line("session.end", evt, **summary))
            return lines
        return []

    def _on_block_stop_quiet(self, evt: dict) -> list[dict]:
        """收尾开放块(不重复产出 block.stop 之外的额外行)。"""
        if not self._open:
            return []
        line = self._line("block.end", evt, round=self._last_round, seq=self._seq, block_type=self._open)
        self._open = None
        return [line]

    def _close_round(self, evt: dict) -> list[dict]:
        self._round_open = False
        return [self._line("round.end", evt, round=self._last_round)]

    def _new_round(self, evt: dict) -> list[dict]:
        """开一轮(第 0 轮输入=prompt;工具结果后的下轮输入=tool.result 预览)。"""
        input_kind = self._pending_input_kind or "user"
        input_preview = self._pending_input_preview
        if input_kind == "user":
            input_preview = self._payload.get("prompt_preview")
        self._pending_input_kind = None
        self._pending_input_preview = None
        self._round_open = True
        self._seq = -1
        return [self._line("round.start", evt, round=self._last_round, input_kind=input_kind,
                           input_preview=input_preview)]

    def _ensure_turn(self, evt: dict) -> list[dict]:
        """需要 assistant 产出前:未开轮则开;已开则无操作。"""
        return self._new_round(evt) if not self._round_open else []

    # ---- 事件处理 ----

    def _on_run_start(self, evt: dict) -> list[dict]:
        self._payload.update(
            prompt_preview=evt.get("prompt_preview"),
            prompt_chars=evt.get("prompt_chars"),
            n_messages=evt.get("n_messages"),
            system_chars=evt.get("system_chars"),
            tool_names=evt.get("tool_names"),
            duration_ms=evt.get("duration_ms"),
        )
        head = self._line("session.start", evt, session=self.session, label=self.label,
                          provider=self.provider, model=self.model, state="running")
        if self._meta:
            head["meta"] = self._meta
        return [head] + self._ensure_turn(evt)

    def _on_block_start(self, evt: dict) -> list[dict]:
        lines: list[dict] = []
        r = evt.get("round", self._last_round)
        if r != self._last_round:
            lines += self._close_round(evt)
            self._last_round = r
        lines += self._ensure_turn(evt)
        self._seq += 1
        self._open = evt["block_type"]
        lines.append(
            self._line("block.start", evt, round=r, seq=self._seq, block_type=self._open,
                       tool_id=evt.get("tool_id"), tool_name=evt.get("tool_name"))
        )
        return lines

    def _on_text_delta(self, evt: dict) -> list[dict]:
        return self._on_delta(evt, "content")

    def _on_thinking_delta(self, evt: dict) -> list[dict]:
        return self._on_delta(evt, "thinking")

    def _on_delta(self, evt: dict, block_type: str) -> list[dict]:
        lines: list[dict] = []
        r = evt.get("round", self._last_round)
        if r != self._last_round:
            lines += self._close_round(evt)
            self._last_round = r
        lines += self._ensure_turn(evt)
        if self._open != block_type:  # 兜底路径(无 partial 事件)自动开块:合并为 1 个块
            self._seq += 1
            self._open = block_type
            lines.append(self._line("block.start", evt, round=r, seq=self._seq, block_type=block_type))
        lines.append(self._line("block.delta", evt, round=r, seq=self._seq, text=evt.get("text", "")))
        return lines

    def _on_block_stop(self, evt: dict) -> list[dict]:
        line = self._line("block.end", evt, round=self._last_round, seq=self._seq,
                          block_type=self._open or evt.get("block_type"))
        if evt.get("tool_input") is not None:
            line["tool_input"] = evt["tool_input"]
        self._open = None
        return [line]

    def _on_tool_result(self, evt: dict) -> list[dict]:
        self._pending_input_kind = "tool"
        self._pending_input_preview = evt.get("content_preview")
        return [self._line("tool.result", evt, round=self._last_round,
                           tool_name=evt.get("tool_name"), tool_id=evt.get("tool_id"),
                           is_error=bool(evt.get("is_error")),
                           content_chars=evt.get("content_chars"),
                           content_preview=evt.get("content_preview"))]

    def _on_message_assistant(self, evt: dict) -> list[dict]:
        # 一条主消息产出归整轮次的确定边界:无 open 块时无操作(tool.result 驱动的下一轮
        # 由适配器 `round` 增量触发,与 deepwiki 语义一致)
        lines = self._on_block_stop_quiet(evt) if self._open else []
        if evt.get("stop_reason"):
            self._payload["stop_reason"] = evt["stop_reason"]
        return lines

    def _on_result(self, evt: dict) -> list[dict]:
        self._payload.update(
            text_chars=evt.get("text_chars"),
            duration_ms=evt.get("duration_ms"),
            usage=evt.get("usage"),
            stop_reason=evt.get("stop_reason") or self._payload.get("stop_reason"),
        )
        return []

    def _on_error(self, evt: dict) -> list[dict]:
        return self._finalize("aborted", evt)

    def _on_run_end(self, evt: dict) -> list[dict]:
        self._payload.update(
            text_chars=evt.get("text_chars") or self._payload.get("text_chars"),
            duration_ms=evt.get("duration_ms") or self._payload.get("duration_ms"),
            usage=evt.get("usage") or self._payload.get("usage"),
        )
        return self._finalize("completed" if evt.get("ok") else "aborted", evt)


def aggregate_all(events: list[dict]) -> dict:
    """内置小工具(测试/种子用):整批事件跑一遍聚合器,返回 (聚合器, 全部行)。"""
    if not events:
        raise ValueError("事件列表为空,无法推断会话")
    agg = LlmAggregator(
        events[0].get("session", ""), events[0].get("label", ""),
        events[0].get("provider", ""), events[0].get("model", ""),
    )
    lines: list[dict] = []
    for evt in events:
        lines.extend(agg.feed(evt))
    return agg, lines
