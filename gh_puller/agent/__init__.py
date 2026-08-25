"""agent 调用统一入口 + 流式监控(事件模型/适配器/观测通道按关注点拆分)。

其他模块不再直接调用 ClaudeSDKClient / httpx,一律经本包函数(无感,对外语义不变):
- cc_stream / cc_text / cc_result(adapters.py):Claude Code(SDK)调用。文本增量
  StreamEvent `text_delta` 优先、AssistantMessage 兜底(仅在未产出任何增量时)、
  ResultMessage.is_error → RuntimeError("agent 执行失败: ...") —— 与 deepwiki
  原 `_agent_stream` 漏斗逐字节一致;thinking/工具增量只进监控事件流,不改变产出。
- llm_complete / llm_stream(adapters.py):OpenAI 兼容端点(httpx);异常原样抛,重试留给调用方。
- configure / ensure_bus / EventBus / FileSink / WsSink(sinks.py):监控运行时重配、
  惰性构建总线与文件/WS 观测通道(AGENT_MONITOR_DIR / AGENT_MONITOR_WEBUI_URL,
  逗号分隔多地址,每地址一个 sink 实例;OtelSink 亦在 sinks.py,经
  AGENT_MONITOR_PHOENIX_URL 启用 —— 端点可达 + opentelemetry 可导入才注册,
  置空关闭;需显式 `from gh_puller.agent.sinks import OtelSink`,本模块不导出)。
- KINDS / LLM_STREAM_TYPES / LlmAggregator / new_event / truncate(events.py):
  纯 dict 事件模型与 LLM 流聚合器,零 SDK 依赖,FS/hub 共用。

管道:适配器归一化 SDK/HTTP 对象 → 事件流 dict → EventBus 扇出(publish 仅
put_nowait 到每 sink 的 asyncio.Queue,永不阻塞调用)→ sink worker 消费(文件写盘)。
线程模型:v1 只有异步调用方,publish 为 loop-affine;若未来出现线程调用方,
须自行经 loop.call_soon_threadsafe 转发。
"""

from .adapters import cc_result, cc_stream, cc_text, llm_complete, llm_stream
from .events import (
    KINDS,
    LLM_STREAM_TYPES,
    LlmAggregator,
    aggregate_all,
    kind_of,
    new_event,
    truncate,
)
from .sinks import EventBus, FileSink, WsSink, configure, ensure_bus

__all__ = [
    "KINDS",
    "LLM_STREAM_TYPES",
    "LlmAggregator",
    "aggregate_all",
    "kind_of",
    "new_event",
    "truncate",
    "EventBus",
    "FileSink",
    "WsSink",
    "configure",
    "ensure_bus",
    "cc_stream",
    "cc_text",
    "cc_result",
    "llm_complete",
    "llm_stream",
]
