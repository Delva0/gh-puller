# gh-puller

将 GitHub 开源仓库（含 PR/Issue）构建为知识库并搭载 agent，对外暴露 REST 接口，回答任何与代码库相关的问题。

## 功能

- **DeepWiki 兼容问答**：`gh_puller/deepwiki.py`（引擎）+ `apps/deepwiki-webui`（FastAPI 后端 + Next.js 前端）。前端契约沿用 deepwiki-open，引擎替换为 Claude Code agent + graphify；生成双路（cc/llm），wiki 生成中途落盘、重启可续跑
- **graphify 库索引**：`gh_puller/graphify.py` 封装 graphify CLI（extract/export/query）——纯本地 AST 建图，无 embedding/RAG；检索由 agent 按需调用 `graphify_query` 工具完成
- **agent 统一入口 + 流式监控**：`gh_puller/agent/` 事件溯源式事件模型 + 文件/WS/OTel 观测通道；`apps/agent-dashboard` 实时查看 CC 与 LLM 调用过程
- **benchmark 评测框架**：`gh_puller/benchmark/` 按 REST 协议 v1 单点评测——一个题库 + 一个参赛方 endpoint，题库（`JUDGE`）自治
- **共享 UI**：`ui/` 基础组件包 `@gh-puller/ui`，apps 经 `workspace:*` 直引源码

## 哲学

- **协议契约代码化**：`benchmark/protocol.py` + `types.py` 是 ask 请求/响应的唯一权威定义，调用方与服务方共用；未知字段不拒绝，协议前向兼容
- **管线零认知**：pipeline 只认识 ask 接口签名、题库导出的 `JUDGE`、judgment 原样存档——题目形态、评判逻辑全由出题人自拟，pipeline 只存档、不解释
- **双方独立发展**：参赛方（服务方）与评测框架互不见面，仅通过 REST 协议互操作
- **不建 RAG，建图**：索引是代码本体的 AST 建图，检索交给 agent 按需查工具，而非 chunk-embed 相似度检索
- **事件溯源**：监控日志无损 append-only；LLM 消息上下文是 surface 节点的派生，不是快照

## 快速上手

```bash
# 依赖安装（仓库根，单一 pnpm lockfile）
pnpm install
```

**问答服务（deepwiki-webui）**

```bash
# 终端 1：后端（默认 :8001）
cd apps/deepwiki-webui/server && uv run uvicorn app:app --port 8001
# 终端 2：前端（默认 :3000）
cd apps/deepwiki-webui/web && pnpm dev
```

浏览器打开 `http://localhost:3000`。

**评测（benchmark）**

```bash
# 先启动一个实现协议 v1 的参赛方服务（唯一路由 POST /ask，如 :8001），然后：
uv run benchmark gh_puller/benchmark/judges/vllm_mechanism/bank.py --url http://localhost:8001
# 产物：outputs/<时间戳>/result.json
```

**agent 监控（agent-dashboard，可选）**

```bash
cd apps/agent-dashboard/server && uv run uvicorn hub:app --port 8765
# 浏览器 :8765；LLM 调用默认自动对接（AGENT_MONITOR_WEBUI_URL 默认 ws://localhost:8765/ws）
```

更多文档：`gh_puller/benchmark/README.md`（协议契约 + 出题人约定）、`docs/agent-monitor.md`（流式监控）。
