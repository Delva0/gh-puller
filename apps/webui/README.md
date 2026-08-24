# gh-puller WebUI

DeepWiki 兼容前端（Next.js 15 + React 19 + Tailwind v4，前端契约与 MIT 协议 deepwiki-open 一致）。
配合后端使用：HTTP 端点层是后端项目自身（`app.py`，独立 uv 项目，FastAPI），引擎/任务层在 `gh_puller/deepwiki.py`。
Claude Code agent 生成仓库 wiki / 跑 codemap / 回答代码问题，
代码图谱检索由 `gh_puller/graphify.py` 提供（经 `graphify_query` 工具注入 agent）。

## 快速上手

前置：Node.js 18+（本项目使用 pnpm，`packageManager` 已锁定 pnpm@11.22.0），
后端为同目录独立 uv 项目（`pyproject.toml`；`uv sync` 自动安装 gh_puller / graphifyy 本地可编辑依赖）。

```bash
# 安装依赖
cd apps/webui && pnpm install

# 终端 1：启动后端（端口默认 8001，env 单点读取见 gh_puller/envs.py）
cd apps/webui
uv run uvicorn app:app --port 8001

# 终端 2：启动前端（端口默认 3000）
cd apps/webui && pnpm dev
```

浏览器打开 `http://localhost:3000`，首页输入仓库 URL（远程 HTTP(S) GitHub/GitLab/Bitbucket 或有
`git` 的本地路径），点击生成 wiki；随后可对仓库提问（Ask，deep_research 模式可用）与生成 codemap。

## 环境变量

前端（设于启动命令前或 `.env`，示例见 `.env.example`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SERVER_BASE_URL` | `http://localhost:8001` | 服务端代理 / rewrites 的后端地址；浏览器端代码不会内联它 |
| `NEXT_PUBLIC_API_PORT` | `8001` | 浏览器直连后端 HTTP/WS 的端口（与后端 `PORT` 联动） |
| `NEXT_PUBLIC_WS_BASE_URL` | 按 host + `NEXT_PUBLIC_API_PORT` 推导 | 浏览器直连后端 WebSocket 的地址（省略时按当前页 host 推导） |

后端（全部收敛在 `gh_puller/envs.py`）：`ANTHROPIC_API_KEY`（或已登录的 Claude Code CLI，agent 必需）、
`CLAUDE_AGENT_MODEL`（空 = SDK 缺省模型）、`DEEPWIKI_ROOT`（产物根目录，默认 `~/.adalflow`：
`repos/` 克隆目录与 `wikicache/` 缓存都在其下；目录须可写，必要时 `sudo chown -R delva ~/.adalflow`）、
`PORT`（默认 8001）、
`DEEPWIKI_AUTH_MODE` / `DEEPWIKI_AUTH_CODE`（wiki 删除授权）、`DEEPWIKI_MAX_CONCURRENT_WIKI_TASKS` 等调度参数。

## 端口与契约

| 前端路径 | 后端目标 |
|---|---|
| `/api/wiki_cache*`、`/export/wiki*`、`/local_repo/structure`、`/api/auth/*`、`/api/lang/config` | `next.config.ts` rewrites 直转 |
| `/api/wiki/*`、`/api/chat/*` 等其余 | `src/app/api/*/route.ts` 薄代理（fetch 后端） |
| `/ws/chat`、`/ws/codemap`（浏览器直连） | 后端 `ws://<host>:<端口>/...` |

## 目录结构

| 路径 | 说明 |
|---|---|
| `app.py` / `pyproject.toml` / `tests/` | 后端 Python 项目根（FastAPI 端点层；与前端同目录共存，`uv run uvicorn app:app` 启动） |
| `src/app` | 页面与 API 路由（`[owner]/[repo]` 会话页、`wiki/projects` 项目列表） |
| `src/components` | UI 组件（Ask / WikiView / CodeMap 等，WebUI 核心） |
| `src/utils/` | `websocketClient.ts`（WS 直连）、`wikiTask.ts`（任务轮询封装） |
| `src/messages/` | 语言包（裁剪后仅 en/zh，与后端 `/lang/config` 同步） |
