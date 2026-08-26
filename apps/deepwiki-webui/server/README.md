# gh-puller WebUI 后端

DeepWiki 兼容后端 HTTP 服务（FastAPI 端点层），独立 uv 项目（`pyproject.toml`）。

前端（Next.js 15）在 `apps/deepwiki-webui/web/`，其 README 含完整启动说明；
pnpm 工作区根在仓库根（根 `package.json` + `pnpm-workspace.yaml`）。

## 启动

```bash
cd apps/deepwiki-webui/server
uv run uvicorn app:app --port 8001
```

- 引擎/任务层（wiki 生成、chat、codemap、缓存）由 `gh_puller/deepwiki.py` 提供
- 环境变量统一在 `gh_puller/envs.py` 单点读取（服务端 `load_dotenv()` 自 cwd 向上找仓库根 `.env`；
  仓库根 `.env` 已 gitignore,可放 `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` 等进程级兜底凭证）
- target 契约:generator + generator_config(cc/dsh/codex = 本地配置文件路径 `config_path`,
  服务端纯透传给 agent SDK;llm = provider/model/凭证 dict),见 `gh_puller/agent/adapters.py:GENERATORS`
- 契约测试：`uv run pytest`（`tests/test_app.py`，不调用 Claude agent）
