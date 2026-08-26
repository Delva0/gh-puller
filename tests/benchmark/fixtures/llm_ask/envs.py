"""llm_ask 方法全局运行参数(统一入口):本方法独立自包含,不依赖任何其他模块。"""

import os

TIMEOUT = 3600.0  # 方法内 LLM 调用超时上限(秒)

# LLM 问答端点:OpenAI 兼容(vLLM 本地部署密钥留空;云端端点如 DeepSeek 填 LLM_ASK_API_KEY)
LLM_ASK_URL = os.environ.get("LLM_ASK_URL", "http://localhost:8000/v1")
LLM_ASK_MODEL = os.environ.get("LLM_ASK_MODEL", "Qwen2.5-7B-Instruct")
LLM_ASK_API_KEY = os.environ.get("LLM_ASK_API_KEY", "")  # 端点认证密钥,留空不发 Authorization
