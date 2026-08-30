"""agent 调用统一入口 + 流式监控(事件模型/适配器/观测通道按关注点拆分)。

其他模块不再直接调用 ClaudeSDKClient / httpx,一律经本包函数(无感,对外语义不变)。
架构:events.py(事件模型) ⊂ sinks.py(观测通道) ⊂ configs.py(generator config
世界:TypedDict 契约/SDK 字段映射/header 投影/隔离组合装配) ⊂ generators.py
(BaseGenerator 基类 + ClaudeCode/OpenAI/Dsh/Codex 四个生成器;API 契约见
generators.py 模块 docstring —— stream 流式产出 assistant 文本增量,result 只拿
最后一轮输出,无 text):
- config 在生成器**构造时期**注入:`GENERATORS[gid](config)` 得到适配器实例,
  stream/result 只收运行时参数(prompt/会话/run 元数据);键集白名单校验在
  上层(各 Config TypedDict 即契约,见 configs.py)。
- ClaudeCode(SDK):流式产出 assistant 文本增量(StreamEvent `text_delta` 优先、
  AssistantMessage 兜底);is_error → RequestFailedError;thinking/工具增量只进
  监控事件流,不改变产出。
- OpenAI(httpx):OpenAI 兼容端点;complete/stream 收请求体 payload(异常原样抛,
  重试留给调用方);result = complete 语义。
- Dsh(DeepSeek Harness SDK):dsh 原生事件 1:1 投影为监控事件流;result =
  RunResult.final_response;非 completed 的 finish_reason → RequestFailedError
  (与 cc is_error 语义对齐)。
- Codex(OpenAI Codex SDK):codex 通知流合成 TAXONOMY(无 seq 编号 → cc 式合成);
  result = TurnResult.final_response;turn 非 completed → RequestFailedError
  (与 cc is_error 语义对齐)。
- configure / ensure_bus / EventBus / FileSink / WsSink(sinks.py):监控运行时重配、
  惰性构建总线与文件/WS 观测通道(AGENT_MONITOR_DIR / AGENT_MONITOR_WEBUI_URL,
  逗号分隔多地址,每地址一个 sink 实例;OtelSink 亦在 sinks.py,经
  AGENT_MONITOR_PHOENIX_URL 启用 —— 端点可达 + opentelemetry 可导入才注册,
  置空关闭;需显式 `from gh_puller.agent.sinks import OtelSink`,本模块不导出)。
- TAXONOMY / SURFACE_TYPES / LOG_TYPES / new_event / type_of / truncate(events.py):
  事件溯源式纯 dict 事件模型(折叠恢复规范见 events.py 模块 docstring),零 SDK 依赖。

管道:适配器归一化 SDK/HTTP 对象 → 事件 dict(envelope) → EventBus 扇出 → sink worker 消费
(publish 语义与线程模型见 events.py EventBus;观测通道见 sinks.py)。
"""

from .configs import ClaudeConfig, CodexConfig, DshConfig, OpenAIConfig, codex_home_path, dsh_cordis_path
from .events import LOG_TYPES, SURFACE_TYPES, TAXONOMY, new_event, truncate, type_of
from .generators import GENERATORS, ClaudeCode, Codex, Dsh, OpenAI, RequestFailedError
from .sinks import EventBus, FileSink, WsSink, configure, ensure_bus

__all__ = [
                      "GENERATORS",
                      "LOG_TYPES",
                      "SURFACE_TYPES",
                      "TAXONOMY",
                      "ClaudeCode",
                      "ClaudeConfig",
                      "Codex",
                      "CodexConfig",
                      "Dsh",
                      "DshConfig",
                      "EventBus",
                      "FileSink",
                      "OpenAI",
                      "OpenAIConfig",
                      "RequestFailedError",
                      "WsSink",
                      "codex_home_path",
                      "configure",
                      "dsh_cordis_path",
                      "ensure_bus",
                      "new_event",
                      "truncate",
                      "type_of",
]
