"""全局运行参数(统一入口):gh_puller 包内环境变量单点在读,deepwiki/agent/graphify/benchmark 共用。

deepwiki 引擎(以及其调用的子流程)的 env 缺省不直接读 os.environ,一律从本模块
import 常量;子进程注入/直读类变量(GRAPHIFY_OUT、GRAPHIFY_MCP_PYTHON、
FALKORDB_PASSWORD 等)与凭证类进程环境由各自消费方直读,不在本模块。
benchmark 评测相关 key 见文末分节。

仅各 app 消费的 env 不在本模块(归属各自的 app 模块):
- apps/deepwiki-webui/server/app.py(服务端快照):DEEPWIKI_AUTH_MODE / DEEPWIKI_AUTH_CODE / PORT /
  DEEPWIKI_GENERATOR(空选型缺省生成器,app 边界注入;引擎空选型 = 内建 cc);
- apps/deepwiki-webui/server/tasks.py(调度快照):DEEPWIKI_MAX_CONCURRENT_WIKI_TASKS、
  DEEPWIKI_WIKI_PAGE_CONCURRENCY / DEEPWIKI_WIKI_PAGE_RETRIES、DEEPWIKI_WIKI_TASK_TTL_SECONDS;
- apps/agent-monitor/server/hub.py:AGENT_MONITOR_LEASE_SECS(_DEFAULT_LEASE_SECS)。

已删除的历史快照(常量无消费方;env 变量名仍有效):
- 凭证/模型名组 ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL、OPENAI_API_KEY / OPENAI_BASE_URL、
  DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL、CC_MODEL / DSH_MODEL / CODEX_MODEL / LLM_MODEL ——
  由 agent SDK/生成器直读进程环境(app 侧 load_dotenv 兜底),说明见 apps/deepwiki-webui/web/README.md;
- 死名 DEEPWIKI_PROVIDER、GRAPHIFY_OUT(缺省在 webui 图封装层)、AGENT_MONITOR_PORT(退役)。
"""

import os

# ---- 生成器配置文件(file 类契约:generator_config.config_path 的 env 缺省) ----
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

# ---- 产物根目录:repos/ 克隆目录、图产物索引根、wiki/ 缓存容器(内部按项目分 <repo_key>/ 文件夹) ----
DEEPWIKI_ROOT = os.path.expanduser(os.environ.get("DEEPWIKI_ROOT", "~/.gh-puller/deepwiki"))

# ---- chat 输入上限的粗略估算(不引 tiktoken:以字符数/4 近似 token) ----
CHAT_TOKEN_LIMIT_ESTIMATE = int(os.environ.get("DEEPWIKI_CHAT_TOKEN_LIMIT", "7500"))

# ---- agent 流式监控(文件观测默认恒开;Web/WS 经 AGENT_MONITOR_WEBUI_URL,OTel 经 AGENT_MONITOR_PHOENIX_URL) ----
# 两 URL 均逗号分隔多地址(每地址一个 sink 实例,预留);空 → 不启用该类 sink。
# 新 OTel 后端(如 AGENT_MONITOR_LANGFUSE_URL,默认 "")= 此处一个常量 + sinks._OTEL_BACKENDS 表一条。
# 该目录即会话 jsonl 落盘根(无 sessions 子层):~/.gh-puller/agent-sessions/<uuid>.jsonl;
# hub(apps/agent-monitor/server/hub.py)为同一目录的读端(共享契约,单点在本模块)。
AGENT_MONITOR_DIR = os.path.expanduser(os.environ.get("AGENT_MONITOR_DIR", "~/.gh-puller/agent-sessions"))
# 默认 = 内部 agent-monitor hub(apps/agent-monitor;hub 端口经 uvicorn CLI --port 指定,默认 8765)
AGENT_MONITOR_WEBUI_URL = os.environ.get("AGENT_MONITOR_WEBUI_URL", "ws://localhost:8765/ws")
# 会话心跳:静默超时(无落盘事件)的补发间隔;hub 租约按"文件 mtime 静止 > LEASE"判孤儿
# (租约缺省在 hub 侧 AGENT_MONITOR_LEASE_SECS;LEASE 需 ≥ 3~5×HEARTBEAT,HEARTBEAT=0
# 可退化为纯事件 mtime 语义)。
AGENT_MONITOR_HEARTBEAT_SECS = int(os.environ.get("AGENT_MONITOR_HEARTBEAT_SECS", "30"))
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

# Claude 评测器:评分模型(缺省用 SDK 默认模型;凭证由 SDK 直读进程环境)
CLAUDE_JUDGE_MODEL = os.environ.get("CLAUDE_JUDGE_MODEL", "")
