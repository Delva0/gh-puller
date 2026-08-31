"""agent 调用统一入口 + 流式监控(事件模型/适配器/观测通道按关注点拆分)。

其他模块不再直接调用 ClaudeSDKClient / httpx,一律经本包函数(无感,对外语义不变)。

架构:events.py(事件模型) ⊂ sinks.py(观测通道) ⊂ generators/(逐生成器一
文件:config 世界 + 适配器本体;base.py 共享骨架,utils.py 共享原子)。
- 生成器契约(构造期注入/stream/result/唯一会话入口)见 generators/
  __init__.py 包 docstring;逐生成器语义各自文件自述。
- 观测通道与运行时重配见 sinks.py / envs.py(默认与启用条件挂 env;
  OtelSink 需显式导入,本模块不导出)。
- 事件模型(分类学/折叠恢复规范)见 events.py 模块 docstring。

管道:适配器归一化 SDK/HTTP 对象 → 事件 dict → EventBus 扇出 →
sink worker 消费(publish/线程模型见 events.py EventBus)。
"""

from .events import LOG_TYPES, SURFACE_TYPES, TAXONOMY, new_event, truncate, type_of
from .generators import (
    GENERATORS,
    ClaudeCode,
    ClaudeConfig,
    Codex,
    CodexConfig,
    Dsh,
    DshConfig,
    OpenAI,
    OpenAIConfig,
    OpenCode,
    OpenCodeConfig,
    RequestFailedError,
    codex_home_path,
    dsh_cordis_path,
)
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
                      "OpenCode",
                      "OpenCodeConfig",
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
