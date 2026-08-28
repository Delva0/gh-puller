# agent-monitor

Agent 流式监控仪表盘(完整应用,`web/` + `server/` 两个子项目):

- `server/` — FastAPI hub(WS `/ws` 对接 `gh_puller.agent` 客户端 WsSink 事件流,`GET /` 返回单文件 viewer HTML;独立 uv 项目,经 path 依赖吃根 `gh-puller` 包)
- `web/` — React/Vite 查看端(单文件构建 → `server/static/agent_monitor_viewer.html`;根 pnpm 工作区成员,共享基础组件包 `@gh-puller/ui` 源码在 `ui/`)

## 启动

```bash
# 依赖安装(仓库根,单一 pnpm lockfile)
pnpm install

# 终端 1:hub(默认端口 8765,与 envs.AGENT_MONITOR_PORT 一致)
uv --directory apps/agent-monitor/server run uvicorn hub:app --port 8765

# 终端 2(可选,开发热更):web
pnpm --dir apps/agent-monitor/web dev

# 构建 viewer(单文件产物落入 server/static/)
pnpm --dir apps/agent-monitor/web build
```

浏览器打开 `http://localhost:8765/` 即查看端;生产侧 LLM 调用默认自动对接
(`AGENT_MONITOR_WEBUI_URL` 默认 `ws://localhost:8765/ws` 且 hub 可达才注册;
可逗号分隔多个 hub),实时上屏。
各子项目的完整说明见 `server/README.md`。
