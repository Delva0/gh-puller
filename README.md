# gh-puller

将 GitHub 开源仓库（源码库+PR/Issue）构建为知识库，对外暴露 REST 接口，回答任何与github仓库相关的问题。

## 功能

- **DeepWiki 兼容问答**：`gh_puller/deepwiki/`（引擎）+ `apps/deepwiki-webui`（FastAPI 后端 + Next.js 前端）。前端契约沿用 deepwiki-open，引擎替换为 Claude Code + 代码图谱；生成单一管道（cc/dsh/codex/opencode），wiki 生成中途落盘、重启可续跑
- **graphify 库索引**：`gh_puller/graphify.py` 封装 graphify CLI（extract/export/query）——纯本地 AST 建图，无 embedding/RAG；检索由 agent 按需调用 `graphify_query` 工具完成
- **Agent 统一入口 + 流式监控**：`gh_puller/agent/` 以 `Agent × Context` fold 和关联活动统一各类 Agent；`apps/agent-monitor` 通过文件、WS 与 OTel 观测运行过程
- **benchmark 评测框架**：`gh_puller/benchmark/` 按 REST 协议 v1 单点评测——一个题库 + 一个参赛方 endpoint，题库（`JUDGE`）自治
- **共享 UI**：`ui/` 基础组件包 `@gh-puller/ui`，apps 经 `workspace:*` 直引源码

## Roadmap

- [x] **deepwiki + graphify**：deepwiki + graphify 验证基础应用pipeline，构建 agent 可观测设施
- [ ] **commit 持久化图 + 跨仓**：跨仓 graphify 增量建图
- [ ] **PR/Issue 联动**: 基于commit图打通 github PR/Issue
- [ ] **agents 接入 + query 优化**: skill+cli、mcp、dsh插件，面向agent的query工具优化

## 快速上手

```bash
pnpm install
```

**问答服务**

```bash
# 终端 1：后端（默认 :8001）
uv --directory apps/deepwiki-webui/server run uvicorn app:app --port 8001
# 终端 2：前端（默认 :3000）
pnpm --dir apps/deepwiki-webui/web dev
```

**评测（可选）**

```bash
# 先启动一个实现协议 v1 的待测服务（唯一路由 POST /ask，如 :8001），然后：
uv run benchmark gh_puller/benchmark/judges/vllm_mechanism/bank.py --url http://localhost:8001
# 产物：outputs/<时间戳>/result.json
```

**Agent 可观测（可选）**

```bash
uv --directory apps/agent-monitor/server run uvicorn app:app --port 8765
# 浏览器 :8765；Agent 调用默认自动对接（AGENT_MONITOR_WEBUI_URL 默认 ws://localhost:8765/ws）
```
