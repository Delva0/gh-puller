"""全局运行参数(统一入口):全包环境变量单点在读,deepwiki/agent/graphify/benchmark 共用。

deepwiki.py(以及其调用的子流程)不直接读 os.environ,一律从本模块 import
常量,缺省值语义与 deepwiki-open 原后端一致(仅 DEEPWIKI_ROOT 的产物路径
保持 ~/.adalflow 以复用既有缓存;agent 与 graphify 相关 key 见下。
benchmark 评测相关 key 见文末分节)。
"""

import os

# ---- provider 连接配置(全项目统一语义:provider = 模型服务提供方) ----
# 各 provider 的模型路由/默认 base URL 见 gh_puller.agent.configs.py 契约
# (OpenAIConfig/DshConfig 概念键);凭证解析优先级 显式 target > 本组环境变量 >
# SDK 原生登录/默认值(解析在 deepwiki.utils._resolve_generator)。
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "")  # 空 → SDK 原生端点
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "")  # 空 → 生成器类属性缺省(官方端点)
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")  # 显式走 options.api_key;空由 SDK 读进程环境
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "")  # 端点覆写(本地 proxy/mock),空走官方

# ---- generator 默认模型(空 → SDK/端点缺省,与 provider 模型目录无关) ----
CC_MODEL = os.environ.get("CC_MODEL", "")  # cc + anthropic(object 兼容遗留;file 类配置随文件)
DSH_MODEL = os.environ.get("DSH_MODEL", "")  # dsh + deepseek(空 → dsh 组合缺省 deepseek-v4-flash)
CODEX_MODEL = os.environ.get("CODEX_MODEL", "")  # codex + openai
LLM_MODEL = os.environ.get("LLM_MODEL", "")  # llm + openai(httpx 直连缺省模型;路由在 base_url 后拼 /chat/completions)

# ---- generator 配置文件(file 类契约:generator_config.config_path 的 env 缺省) ----
# 显式 config_path > 下列 env > generator 类属性缺省(cc= ~/.claude/settings.json;
# dsh/codex 无缺省 → 生成器内置隔离组合)。
DEEPWIKI_CC_CONFIG = os.environ.get("DEEPWIKI_CC_CONFIG", "")  # cc:Claude settings JSON 路径
DEEPWIKI_CODEX_CONFIG = os.environ.get("DEEPWIKI_CODEX_CONFIG", "")  # codex:config.toml 路径

# ---- dsh 运行隔离(非凭证) ----
DSH_SESSION_ROOT = os.path.expanduser(os.environ.get("DSH_SESSION_ROOT", "~/.gh-puller/dsh-sessions"))
# runtime 进程 cwd(也是它读取 .env 的加载点):必须远离任务 checkout —— 仓库自带
# .env(可含 DEEPSEEK_*/其它字面键)会注入子进程(隔离链上唯一真实泄漏口,runtime_cwd
# 缺省 = 任务仓库 cwd)。cc 路径无此面(SDK 不做 cwd .env 加载)。
DSH_RUNTIME_CWD = os.path.expanduser(os.environ.get("DSH_RUNTIME_CWD", "~/.gh-puller/dsh-runtime"))
# dsh 的文件类 config 缺省 env(承接旧"自定义组合文件"语义;经 resolve 解析为 config_path)
DEEPWIKI_DSH_CORDIS = os.environ.get("DEEPWIKI_DSH_CORDIS", "")

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
# 默认 = 内部 agent-monitor hub(apps/agent-monitor,AGENT_MONITOR_PORT 联动)
AGENT_MONITOR_WEBUI_URL = os.environ.get("AGENT_MONITOR_WEBUI_URL", "ws://localhost:8765/ws")
AGENT_MONITOR_PORT = int(os.environ.get("AGENT_MONITOR_PORT", "8765"))
# 启用条件:端点可达(ensure_bus 构建时 TCP 探活)+ opentelemetry 可导入
AGENT_MONITOR_PHOENIX_URL = os.environ.get("AGENT_MONITOR_PHOENIX_URL", "http://localhost:6006/")
# OTel 导出 service.name(缺省 gh-puller;OtelSink 构建读用)
OTEL_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "gh-puller")

# ---- benchmark 评测(单题超时/评测器选择与端点) ----
TIMEOUT = 3600.0  # 单题超时(秒,1 小时):参赛方 ask 与评测器评分的统一上限

# 评测器选择:claude 切到 Claude Code 评测器;缺省 LLM 评测器
JUDGE_EVALUATOR = os.environ.get("JUDGE_EVALUATOR", "llm")

# LLM 评测器:vLLM OpenAI 兼容端点与评分模型(可被构造参数覆盖)
LLM_JUDGE_URL = os.environ.get("LLM_JUDGE_URL", "http://localhost:8000/v1")
LLM_JUDGE_MODEL = os.environ.get("LLM_JUDGE_MODEL", "Qwen2.5-7B-Instruct")
LLM_JUDGE_API_KEY = os.environ.get("LLM_JUDGE_API_KEY", "")  # 端点认证密钥,留空不发 Authorization

# ---- DeepWiki 默认 target(generator;file 类配置随 config_path 缺省,object 类接 *_MODEL/API_KEY) ----
# "cc"=Claude Code agent;"dsh"=DeepSeek Harness agent;"codex"=OpenAI Codex agent;"llm"=纯 LLM 单次补全
DEEPWIKI_GENERATOR = os.environ.get("DEEPWIKI_GENERATOR", "cc")
DEEPWIKI_PROVIDER = os.environ.get("DEEPWIKI_PROVIDER", "")  # object 类默认 provider(空 → 生成器类属性默认)

# Claude 评测器:评分模型(缺省用 SDK 默认模型),需要 ANTHROPIC_API_KEY
CLAUDE_JUDGE_MODEL = os.environ.get("CLAUDE_JUDGE_MODEL", "")
