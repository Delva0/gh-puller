# agent-dashboard 后端(hub)

Agent 流式监控 Web/WS hub(FastAPI 端点层,独立 uv 项目)。生产端是 `gh_puller.agent`
的 WsSink(客户端,包内只留它);本服务承担原 `python -m gh_puller.agent serve` 的双角色:

- `WS /ws` — 一连接一角色,首帧定角色:`{"type":"evt",...}` 为生产端(事件接入),
  其余(`index`/`llm-subscribe`/`evt-subscribe`/`evt-replay`/`ping`)为查看端;
- `GET /` 与 `/viewer` — 直接出构建好的单文件 viewer(`static/agent_monitor_viewer.html`;

  缺件回退 `viewer 文件缺失` 文案,构建见仓库根 `pnpm -r build`)。

hub 只持内存状态(事件环 1000/会话、LLM 流行 500 行/会话),写盘是 FileSink 的事;
启动时从 `AGENT_MONITOR_DIR`(见 `gh_puller/envs.py`,默认 `~/.gh-puller/agent-monitor`)
种子历史,重启 hub 列表仍在。

## 启动

```bash
uv run uvicorn hub:app --host 0.0.0.0 --port 8765   # 端口默认与 envs.AGENT_MONITOR_PORT 一致
```

## 生产端接入

任何 LLM 调用(经 `gh_puller.agent` 的 `cc_*` / `llm_*` 包装)时开启 WS 通道:
WsSink 由 `ensure_bus` 惰性注册 —— 默认 `AGENT_MONITOR_WEBUI_URL=ws://localhost:8765/ws`,
hub 可达(tcp 探活)即自动对接,无需显式配置。

```bash
# 自定义/多 hub(逗号分隔,每地址一个 WsSink):
AGENT_MONITOR_WEBUI_URL=ws://localhost:8765/ws uv run benchmark ...
```

注意 WS 地址带 `/ws` 路径。协议帧格式测试见 `tests/test_hub.py`(`uv run pytest`)。
