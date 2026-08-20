"""全局运行参数(统一入口):全 benchmark 共享的基准值与环境变量。"""

import os

TIMEOUT = 3600.0  # 单题超时(秒,1 小时):参赛方 ask 与评测器评分的统一上限

# 评测器选择:claude 切到 Claude Code 评测器;缺省 LLM 评测器
JUDGE_EVALUATOR = os.environ.get("JUDGE_EVALUATOR", "llm")

# LLM 评测器:vLLM OpenAI 兼容端点与评分模型(可被构造参数覆盖)
LLM_JUDGE_URL = os.environ.get("LLM_JUDGE_URL", "http://localhost:8000/v1")
LLM_JUDGE_MODEL = os.environ.get("LLM_JUDGE_MODEL", "Qwen2.5-7B-Instruct")

# Claude 评测器:评分模型(缺省用 SDK 默认模型),需要 ANTHROPIC_API_KEY
CLAUDE_JUDGE_MODEL = os.environ.get("CLAUDE_JUDGE_MODEL", "")
