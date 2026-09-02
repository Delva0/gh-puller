# agent-monitor

Agent 流式监控应用，由 `web/` 与 `server/` 组成：

- `server/` — 本地 sidecar。`hub.py` 投影共享 JSONL 历史，`app.py` 提供 FastAPI、WS 与 viewer 静态入口。
- `web/` — React/Vite 查看端。单一 canonical fold 驱动原生 Agent、Context 与 Events 视图，构建为 `server/static/agent_monitor_viewer.html`。

`web/src/` 中，`events/` 只包含事件状态与折叠，`monitor/` 负责 hub 同步，
`views/` 直接渲染折叠结果，`vendor/dsh/` 只提供视觉原语。

## 启动

```bash
# 依赖安装(仓库根,单一 pnpm lockfile)
pnpm install

# 终端 1:hub(默认端口 8765,由 uvicorn CLI `--port` 指定)
uv --directory apps/agent-monitor/server run uvicorn app:app --port 8765

# 终端 2(可选,开发热更):web
pnpm --dir apps/agent-monitor/web dev

# 构建 viewer(单文件产物落入 server/static/)
pnpm --dir apps/agent-monitor/web build
```

浏览器打开 `http://localhost:8765/`。生产侧 Agent 调用默认自动对接
(`AGENT_MONITOR_WEBUI_URL` 默认 `ws://localhost:8765/ws` 且 hub 可达才注册;
可逗号分隔多个 hub),实时上屏。
生产进程的 FileSink 与 server 必须共享 `AGENT_MONITOR_DIR`;JSONL 是持久事实源，
WS 只负责实时加速。
各子项目的完整说明见 `server/README.md`。
