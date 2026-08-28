# agent-monitor 后端(hub)

Agent 流式监控 Web/WS hub(FastAPI 端点层,独立 uv 项目)。生产端是 `gh_puller.agent`
的 WsSink(客户端,包内只留它);本服务承担原 `python -m gh_puller.agent serve` 的双角色:

- `WS /ws` — 一连接一角色,首帧定角色:`{"type":"evt",...}` 为生产端(事件接入),
  其余(`index`/`history`/`subscribe`/`ping`)为查看端;
- `GET /` 与 `/viewer` — 直接出构建好的单文件 viewer(`static/agent_monitor_viewer.html`;

  缺件回退 `viewer 文件缺失` 文案,构建见仓库根 `pnpm -r build`)。

hub 只持内存状态(每会话全量事件,seq 索引),写盘是 FileSink 的事;磁盘布局为
扁平 `<uuid>.jsonl`(隐式分类学:会话键 = 事件内 `session/start` 的
`session` 字段,状态 = 有无 `session/end` 行),启动时从 `AGENT_MONITOR_DIR`
(见 `gh_puller/envs.py`,默认 `~/.gh-puller/agent-sessions`)种子历史,重启 hub
列表仍在;`index` 时对 running 会话按文件 mtime 按需重判(自愈残留死会话)。

## 租约:崩溃残留(孤儿会话)判定

生产端进程被 SIGKILL/崩溃时,`session/end` 永远不会写入 —— 文件停留在无终态行
的"崩溃残留"。hub 以 **文件 mtime 租约**兜底判定死亡,不依赖死进程写任何东西:

- 会话运行期,生产端静默超过 `AGENT_MONITOR_HEARTBEAT_SECS`(默认 30s,无落盘
  事件时)就补发一条 `session/heartbeat` —— 活会话的 JSONL mtime 至多每间隔动一次;
- hub 的租约扫描(生命周期任务,每 `LEASE/4` 一拍):无终态行 **且** mtime 静止
  超过 `AGENT_MONITOR_LEASE_SECS`(默认 150s)的 running 会话 → 判定孤儿,
  状态派生为 `aborted`(纯内存态,**不写盘**;派生规则无状态,重启后自动重推);
- 自愈:文件若再移动(新事件/终态行补写)→ 状态自动回 `running` / `completed`;
  翻转为 aborted 时向在线查看端广播一次 index(幂等,每拍至多一次)。

时钟语义:mtime 是 hub 本机文件系统时间,天然无生产端时钟漂移问题。调参:LEASE
应 ≥ 3~5×HEARTBEAT(活动期零心跳行;`AGENT_MONITOR_HEARTBEAT_SECS=0` 退化为纯
事件 mtime 语义)。详见 `docs/agent-monitor.md` §1/§2/§3。

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
