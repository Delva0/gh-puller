# 方法（内置方法目录）

方法 = 实现 REST 协议（唯一路由 `POST /ask`）的 HTTP 服务。
本目录存放内置方法；外部方法（仓库外自建，协议只认 `base_url`）地位等同。

## 协议

协议契约（唯一权威）：`gh_puller/protocol.md`。方法实现该契约的唯一路由 `POST /ask`。

## 约定

- 每个方法一个独立子包：`gh_puller/methods/<方法名>/`
- app 定义于 `<方法名>/server.py`，启动：`uv run uvicorn gh_puller.methods.<方法名>.server:app --port <端口>`
- 配置入口：每种方法自带 `<方法名>/env.py`（环境变量，可覆盖默认值）；方法之间互不依赖

## 方法列表

| 方法 | 说明 |
|---|---|
| `llm_ask` | 纯 LLM 问答：`POST /ask` → LLM(question) → answer（最简基线） |

## 自测（以 llm_ask 为例）

```bash
uv run uvicorn gh_puller.methods.llm_ask.server:app --port 8001
curl -s -X POST http://localhost:8001/ask -H 'Content-Type: application/json' -d '{"question":"ping"}'
```
