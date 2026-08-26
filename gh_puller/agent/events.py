"""agent 调用的可观测事件模型(事件溯源式;纯 dict 实现,零 SDK 依赖)。

对齐 deepseek-harness 的核心不变量:无损 append-only 事件日志,seq 每 session 从 0
连续单调(**流式事件流内稠密**;非流式投影侧允许洞,见下);LLM messages 上下文
是 surface 节点的派生(折叠)而非快照:

事件流按粒度分为两级:
- 流式事件流(agent 事件流)= TAXONOMY 全集(STREAM_TYPES):含 assistant/chunk
  原始增量,可还原实时(逐字)上下文;WS/OTel 通道承载;
- 非流式事件流 = TAXONOMY − {assistant/chunk}(NON_STREAM_TYPES):message 粒度,
  assistant/message 已是定型全量,可还原任意时刻的消息上下文;filesink 只落此级。

seq 序列是流式事件流的稠密序号;文件侧(非流式投影)按行跳过 chunk,因此
**文件内 seq 允许洞,洞 = 被跳过的 assistant/chunk**。折叠契约只做 seq 排序
与 seq < x 比较,不要求稠密 —— 读者(含前端)不得假定文件 seq 连续。
- surface 事件 user/message / assistant/message / tool/result 携带全量消息与
  surfaceOp(append 或 {op:'replace', start, end});
- request/header 快照(ignorable 日志型)给出 config/system/tools ——
  任意时刻的请求 payload = 该时刻前最近的 header + 折叠到该时刻的 surface;
- context/inject / context/modify 是上下文注入/修改的解释事件(日志型,不折动),
  折叠正确性与它们无关 —— 丢弃也只是少了解释,不会误解消息历史。

折叠恢复规范(与 ui/src/monitor/surface.ts 同份语义,本模块不落 Python 实现,
契约测试见 tests/test_event_taxonomy.py):
    messages(X) = [derive(evt) for node in surface_nodes if node.seq < X and derive 非空]
    derive: user/message→data.message; assistant/message→content 非空 ? message : None;
            tool/result→data.message; 其余→None

本模块只认普通 dict,严禁 import claude_agent_sdk / httpx —— 测试只喂假 dict。
seq/run_id 等会话属性由适配器(_Run, adapters.py)附加;new_event 只造 id/ts/type/data
信封(seq 不在此分配,防止双写)。
"""

import json
import time
import uuid

# 事件全量 taxonomy(点号→斜杠命名,与 dsh type 风格对齐)
TAXONOMY = frozenset(
    {
        "session/start",
        "session/end",
        "turn/start",
        "turn/end",
        "step/start",
        "step/end",
        "user/message",
        "assistant/chunk",  # 原始流增量(text/thinking/tool_input),非 surface
        "assistant/message",
        "tool/call",
        "tool/result",
        "request/header",
        "request/context",
        "context/inject",
        "context/modify",
        "error",
    }
)

# surface 事件:消息上下文的可折叠集合(必带全量 message 与合法 surfaceOp)
SURFACE_TYPES = frozenset({"user/message", "assistant/message", "tool/result"})

# 事件流两级划分:流式事件流(agent 事件流,完整 taxonomy,可还原实时上下文)
# 与非流式事件流(逐行跳 chunk,message 粒度,可还原任意时刻消息上下文)。
# filesink 只落非流式事件流(NON_STREAM_TYPES);WS/OTel 通道承载完整流式事件流。
STREAM_TYPES = TAXONOMY
NON_STREAM_TYPES = TAXONOMY - {"assistant/chunk"}

# ignorable 日志型事件:读者可安全跳过(不影响消息派生/请求重建);缺失 ignorable
# 标记的未知类型 → 必须可解析(读者应报错,防静默丢消息)
LOG_TYPES = frozenset(
    {"request/header", "request/context", "context/inject", "context/modify"}
)


def type_of(evt: dict) -> str:
    """返回事件 type;未知类型 → ValueError。"""
    t = evt.get("type")
    if t not in TAXONOMY:
        raise ValueError(f"未知事件 type: {t!r}")
    return t


def new_event(evt_type: str, **data) -> dict:
    """构造事件信封:补 id/ts,type/data 分组,校验 type 与字段可 JSON 序列化。

    会话属性(session/run_id/label/provider/model/seq)由适配器附加,不在此处;
    surface 事件强制校验 message + surfaceOp(防适配器漏字段导致折叠不可复现)。
    """
    if evt_type not in TAXONOMY:
        raise ValueError(f"未知事件 type: {evt_type!r}")
    evt = {"id": f"e-{uuid.uuid4().hex[:7]}", "ts": time.time(), "type": evt_type, "data": data}
    if evt_type in SURFACE_TYPES:
        if "message" not in data:
            raise ValueError(f"surface 事件缺 message 字段: {evt_type!r}")
        op = data.get("surfaceOp")
        if op != "append" and not (isinstance(op, dict) and op.get("op") == "replace"):
            raise ValueError(f"surface 事件缺合法 surfaceOp: {evt_type!r}")
    if evt_type in LOG_TYPES:
        evt["ignorable"] = True
    json.dumps(evt)  # 校验可 JSON 化(如 set 字段 → TypeError)
    return evt


def truncate(text: str | None, n: int) -> tuple[int, str]:
    """返回 (原长度, 预览):超长截断加省略号;None/空 → (0, "")。

    仅用于 OTel 等第三方通道的预览属性;监控事件本身全量不截断。
    """
    if not text:
        return (0, "")
    if len(text) <= n:
        return (len(text), text)
    return (len(text), text[:n] + "…")
