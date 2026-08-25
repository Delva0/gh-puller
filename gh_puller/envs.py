"""全局运行参数(统一入口):全包环境变量单点在读,deepwiki/agent/graphify/benchmark 共用。

deepwiki.py(以及其调用的子流程)不直接读 os.environ,一律从本模块 import
常量,缺省值语义与 deepwiki-open 原后端一致(仅 DEEPWIKI_ROOT 的产物路径
保持 ~/.adalflow 以复用既有缓存;agent 与 graphify 相关 key 见下。
benchmark 评测相关 key 见文末分节)。
"""

import os

# ---- Claude agent(SDK) ----
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_AGENT_MODEL = os.environ.get("CLAUDE_AGENT_MODEL", "")  # 空 → SDK 缺省模型

# ---- 产物根目录:repos/ 克隆目录、graphify-out/ 索引、wikicache/ 缓存 ----
DEEPWIKI_ROOT = os.path.expanduser(os.environ.get("DEEPWIKI_ROOT", "~/.gh-puller/deepwiki"))

# ---- wiki 删除授权(与原后端同式:字符串真值判定) ----
WIKI_AUTH_MODE = os.environ.get("DEEPWIKI_AUTH_MODE", "False").lower() in ["true", "1", "t"]
WIKI_AUTH_CODE = os.environ.get("DEEPWIKI_AUTH_CODE", "")

# ---- wiki 任务调度(与 api/services/wiki/tasks.py 同式) ----
MAX_CONCURRENT_WIKI_TASKS = int(os.environ.get(
    "DEEPWIKI_MAX_CONCURRENT_WIKI_TASKS", max(1, (os.cpu_count() or 2) // 2)))
WIKI_PAGE_CONCURRENCY = int(os.environ.get("DEEPWIKI_WIKI_PAGE_CONCURRENCY", "4"))
WIKI_PAGE_RETRIES = int(os.environ.get("DEEPWIKI_WIKI_PAGE_RETRIES", "2"))
WIKI_TASK_TTL_SECONDS = int(os.environ.get("DEEPWIKI_WIKI_TASK_TTL_SECONDS", "300"))

# ---- HTTP 服务 ----
PORT = int(os.environ.get("PORT", "8001"))  # uvicorn 端口(前端 NEXT_PUBLIC_API_PORT 联动)

# ---- graphify 索引 ----
GRAPHIFY_OUT = os.environ.get("GRAPHIFY_OUT", "graphify-out")

# ---- chat 输入上限的粗略估算(不引 tiktoken:以字符数/4 近似 token) ----
CHAT_TOKEN_LIMIT_ESTIMATE = int(os.environ.get("DEEPWIKI_CHAT_TOKEN_LIMIT", "7500"))

# ---- agent 流式监控(文件观测默认恒开;Web/WS 经 AGENT_MONITOR_WEBUI_URL,OTel 经 AGENT_MONITOR_PHOENIX_URL) ----
# 两 URL 均逗号分隔多地址(每地址一个 sink 实例,预留);空 → 不启用该类 sink。
# 新 OTel 后端(如 AGENT_MONITOR_LANGFUSE_URL,默认 "")= 此处一个常量 + sinks._OTEL_BACKENDS 表一条。
AGENT_MONITOR_DIR = os.path.expanduser(os.environ.get("AGENT_MONITOR_DIR", "~/.gh-puller/agent-monitor"))
# 默认 = 内部 agent-dashboard hub(apps/agent-dashboard,AGENT_MONITOR_PORT 联动)
AGENT_MONITOR_WEBUI_URL = os.environ.get("AGENT_MONITOR_WEBUI_URL", "ws://localhost:8765/ws")
AGENT_MONITOR_PORT = int(os.environ.get("AGENT_MONITOR_PORT", "8765"))
# 启用条件:端点可达(ensure_bus 构建时 TCP 探活)+ opentelemetry 可导入
AGENT_MONITOR_PHOENIX_URL = os.environ.get("AGENT_MONITOR_PHOENIX_URL", "http://localhost:6006/")

# ---- benchmark 评测(单题超时/评测器选择与端点) ----
TIMEOUT = 3600.0  # 单题超时(秒,1 小时):参赛方 ask 与评测器评分的统一上限

# 评测器选择:claude 切到 Claude Code 评测器;缺省 LLM 评测器
JUDGE_EVALUATOR = os.environ.get("JUDGE_EVALUATOR", "llm")

# LLM 评测器:vLLM OpenAI 兼容端点与评分模型(可被构造参数覆盖)
LLM_JUDGE_URL = os.environ.get("LLM_JUDGE_URL", "http://localhost:8000/v1")
LLM_JUDGE_MODEL = os.environ.get("LLM_JUDGE_MODEL", "Qwen2.5-7B-Instruct")
LLM_JUDGE_API_KEY = os.environ.get("LLM_JUDGE_API_KEY", "")  # 端点认证密钥,留空不发 Authorization

# Claude 评测器:评分模型(缺省用 SDK 默认模型),需要 ANTHROPIC_API_KEY
CLAUDE_JUDGE_MODEL = os.environ.get("CLAUDE_JUDGE_MODEL", "")
