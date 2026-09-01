"""agent 调用统一入口 + 流式监控(事件模型/适配器/观测通道按关注点拆分)。

其他模块一律经本包生成器访问 ClaudeSDKClient / httpx。

架构:events.py(事件模型) ⊂ sinks.py(观测通道) ⊂ generators/(逐生成器一
文件:config 世界 + 适配器本体;base.py 共享骨架,utils.py 共享原子)。
- 生成器契约(构造期注入/stream/result/唯一会话入口)见 generators/
  __init__.py 包 docstring;逐生成器语义各自文件自述。
- 观测通道与运行时重配见 sinks.py / envs.py(默认与启用条件挂 env;
  本模块仅导出运行时 `configure`)。
- 事件模型(分类学/折叠恢复规范)见 events.py 模块 docstring。

管道:适配器归一化 SDK/HTTP 对象 → 事件 dict → EventBus 扇出 →
sink worker 消费(publish/线程模型见 events.py EventBus)。
"""

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
)
from .sinks import configure

__all__ = [
    "GENERATORS",
    "ClaudeCode",
    "ClaudeConfig",
    "Codex",
    "CodexConfig",
    "Dsh",
    "DshConfig",
    "OpenAI",
    "OpenAIConfig",
    "OpenCode",
    "OpenCodeConfig",
    "RequestFailedError",
    "configure",
]
