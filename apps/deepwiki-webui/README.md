# deepwiki-webui

DeepWiki 兼容 Web 界面(完整应用,`server/` + `web/` 两个子项目):

- `server/` — FastAPI 后端(独立 uv 项目,经 path 依赖吃根 `gh-puller` 包;契约与 deepwiki-open 一致)
- `web/` — Next.js 15 前端(根 pnpm 工作区成员,共享基础组件包 `@gh-puller/ui` 源码在 `ui/`)

## 启动

```bash
# 依赖安装(仓库根,单一 lockfile)
pnpm install

# 终端 1:后端(端口默认 8001,uvicorn CLI `--port` 指定;包内 env 见 gh_puller/envs.py,
# 服务端专属 env(PORT/DEEPWIKI_AUTH_*/调度参数)见 server/app.py 与 server/tasks.py)
uv --directory apps/deepwiki-webui/server run uvicorn app:app --port 8001

# 终端 2:前端(端口默认 3000)
pnpm --dir apps/deepwiki-webui/web dev
```

浏览器打开 `http://localhost:3000`。前端把 `/api/*` 白名单经 rewrites / 薄代理转到 `:8001`,
浏览器直连后端的 WebSocket 端口同为 `NEXT_PUBLIC_API_PORT`(默认 8001)。
各子项目的完整说明与环境变量见 `server/README.md`、`web/README.md`。
