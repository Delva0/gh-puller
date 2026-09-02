# gh-puller WebUI 后端

DeepWiki 兼容后端 HTTP 服务（FastAPI 端点层），独立 uv 项目（`pyproject.toml`）。

前端（Next.js 15）在 `apps/deepwiki-webui/web/`，其 README 含完整启动说明；
pnpm 工作区根在仓库根（根 `package.json` + `pnpm-workspace.yaml`）。

## 启动

```bash
uv --directory apps/deepwiki-webui/server run uvicorn app:app --port 8001
```

- 引擎（纯数据 dataclass/dict + 纯函数式生成协议、缓存与状态 IO，**零 pydantic、零 Request 概念、零
  graphify/claude_agent_sdk 依赖**）由 `gh_puller` 包提供（`gh_puller/deepwiki/`）;wiki 任务调度与执行
  (注册表、主流程、进度落盘投影)在本目录 `tasks.py`;HTTP 端点与 wire 契约（请求/响应 pydantic 校验,唯一验证面）
  在本目录 `app.py` / `schemas.py`;gh-puller-mcp 组装（索引保障/MCP 工具桌 + runtime_config 覆盖构造参数注入）
  唯一收容点在本目录 `generators.py`。图后端 = `apps/gh-puller-mcp/`（MCP 服务器,后端透传 C 二进制
  `codebase-memory-mcp`;运行前提:`uv` 在 PATH、二进制按 GH_PULLER_MCP_BINARY/PATH/`~/.local/bin`
  解析）;索引 db 落 `<CBM_CACHE_DIR>/<project>.db`（缺省 `~/.cache/codebase-memory-mcp`）
  —— **索引就绪只表示 db 存在,不保证二进制可用**;旧 graphify 产物的索引（`DEEPWIKI_ROOT/graphify/` 下
  `graph.json`）不复用,此类仓库需重新 prepare 一次
- 包内 env 统一在 `gh_puller/envs.py` 单点读取（引擎/agent/benchmark 消费）；仅本服务端消费的 env
  （auth `DEEPWIKI_AUTH_MODE`/`DEEPWIKI_AUTH_CODE`、`PORT`、`DEEPWIKI_GENERATOR`(缺省生成器,
  空选型经 app 边界注入；引擎空选型=内建 cc)、wiki 调度
  `DEEPWIKI_MAX_CONCURRENT_WIKI_TASKS`/`DEEPWIKI_WIKI_PAGE_CONCURRENCY`/`DEEPWIKI_WIKI_PAGE_RETRIES`/
  `DEEPWIKI_WIKI_TASK_TTL_SECONDS`）为 `app.py`/`tasks.py` 模块顶快照。服务端 `load_dotenv()`
  自 cwd 向上找仓库根 `.env`,先于任何 gh_puller 导入与本模块快照；仓库根 `.env` 已 gitignore,
  可放 `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` 等进程级兜底凭证（由 agent SDK 直读进程环境）
- target 契约:generator + generator_config(cc/dsh/codex/opencode = 本地配置文件路径 `config_path`,
  服务端纯透传给 Agent),见 `gh_puller/agent/adapters:AGENTS`;
  gh-puller-mcp 工具桌由本目录 `generators.py` 经 `runtime_config` 注入 generator_config
  (引擎 adapter 只做白名单透传);工具桌档位 scout(只读正查面),变更面(index_repository 等)
  仅服务器进程内建图使用
- 契约测试：`uv run pytest`（`tests/test_app.py`，不调用 Claude agent）
