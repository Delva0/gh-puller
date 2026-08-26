"""agent 调用统一入口 + 流式监控(事件模型/适配器/观测通道按关注点拆分)。

其他模块不再直接调用 ClaudeSDKClient / httpx,一律经本包函数(无感,对外语义不变)。
架构:events.py(事件模型) ⊂ sinks.py(观测通道) ⊂ run.py(单次运行事件发布器) ⊂
generators.py(BaseGenerator 基类 + ClaudeCode/OpenAI/Dsh/Codex 四个生成器 + 解析/统一分派):
- cc_stream / cc_text / cc_result(adapters.py):Claude Code(SDK)调用。文本增量
  StreamEvent `text_delta` 优先、AssistantMessage 兜底(仅在未产出任何增量时)、
  ResultMessage.is_error → RuntimeError("agent 执行失败: ...") —— 与 deepwiki
  原 `_agent_stream` 漏斗逐字节一致;thinking/工具增量只进监控事件流,不改变产出。
- llm_complete / llm_stream(adapters.py):OpenAI 兼容端点(httpx);异常原样抛,重试留给调用方。
- dsh_stream / dsh_text / dsh_result(adapters.py):DeepSeek Harness(SDK)调用。
  dsh 原生事件 1:1 投影为监控事件流;非 completed 的 finish_reason →
  RuntimeError("agent 执行失败: ...")(与 cc is_error 语义对齐)。
- codex_stream / codex_text / codex_result(adapters.py):OpenAI Codex(SDK)调用。
  codex 通知流合成 TAXONOMY(无 seq 编号 → cc 式合成);turn 非 completed →
  RuntimeError("agent 执行失败: ...")(与 cc is_error 语义对齐)。
- configure / ensure_bus / EventBus / FileSink / WsSink(sinks.py):监控运行时重配、
  惰性构建总线与文件/WS 观测通道(AGENT_MONITOR_DIR / AGENT_MONITOR_WEBUI_URL,
  逗号分隔多地址,每地址一个 sink 实例;OtelSink 亦在 sinks.py,经
  AGENT_MONITOR_PHOENIX_URL 启用 —— 端点可达 + opentelemetry 可导入才注册,
  置空关闭;需显式 `from gh_puller.agent.sinks import OtelSink`,本模块不导出)。
- TAXONOMY / SURFACE_TYPES / LOG_TYPES / new_event / type_of / truncate(events.py):
  事件溯源式纯 dict 事件模型(折叠恢复规范见 events.py 模块 docstring),零 SDK 依赖。

管道:适配器归一化 SDK/HTTP 对象 → 事件 dict(envelope) → EventBus 扇出(publish 仅
put_nowait 到每 sink 的 asyncio.Queue,永不阻塞调用)→ sink worker 消费(文件写盘)。
线程模型:v1 只有异步调用方,publish 为 loop-affine;若未来出现线程调用方,
须自行经 loop.call_soon_threadsafe 转发。
"""

from .generators import (
    GENERATORS,
    RequestFailedError,
    cc_result,
    cc_stream,
    cc_text,
    codex_result,
    codex_stream,
    codex_text,
    dsh_cordis_path,
    dsh_result,
    dsh_stream,
    dsh_text,
    generate_result,
    generate_stream,
    generate_text,
    llm_complete,
    llm_stream,
    resolve_generator,
)
from .events import LOG_TYPES, SURFACE_TYPES, TAXONOMY, new_event, truncate, type_of
from .sinks import EventBus, FileSink, WsSink, configure, ensure_bus

__all__ = [
    "TAXONOMY",
    "SURFACE_TYPES",
    "LOG_TYPES",
    "new_event",
    "type_of",
    "truncate",
    "EventBus",
    "FileSink",
    "WsSink",
    "configure",
    "ensure_bus",
    "GENERATORS",
    "resolve_generator",
    "RequestFailedError",
    "generate_stream",
    "generate_text",
    "generate_result",
    "cc_stream",
    "cc_text",
    "cc_result",
    "dsh_stream",
    "dsh_text",
    "dsh_result",
    "dsh_cordis_path",
    "codex_stream",
    "codex_text",
    "codex_result",
    "llm_complete",
    "llm_stream",
]
