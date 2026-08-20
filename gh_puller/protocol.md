# 协议契约（REST 协议 v1）

本文件是协议契约的**唯一权威**：服务方（如 `gh_puller/methods/` 下的方法）实现它，调用方（如评测管线）调用它。
任何一侧都不持有自己的协议定义。

服务方只需提供一个 `base_url`（如 `http://localhost:8001`）即可接入。调用方只测试协议规定的这一条路由，
服务方端点上其他任何路由一律不测。

## 唯一路由

### POST `{base_url}/ask`

**请求体**（JSON）：

- `question`：字符串，必填，非空
- 允许附加字段（如 `context`），未知字段不会被拒绝（协议前向兼容）

示例：

```json
{"question": "vllm 推理时显存不足怎么办？", "context": {"repo": "vllm-project/vllm"}}
```

**响应体**（JSON）：

- `answer`：字符串，必填，非空
- 允许附加字段（如 `sources`），未知字段会被保留

示例：

```json
{"answer": "可以尝试开启量化、减小 KV cache……", "sources": ["issue#123"]}
```

**超时与重试**：单次请求超时 3600 秒；连接类错误自动重试 3 次，HTTP 错误不重试。

### GET `{base_url}/openapi.json`

- 声明 `POST /ask` 路由（推荐，便于调用方探测）

## 接入检查（两关，任一失败即判定不可接入）

1. **路由探测**：优先读取 `GET {base_url}/openapi.json` 的 `paths`，检查是否声明 `POST /ask`；
   无法读取时向 `/ask` 发探测请求，返回 404 即视为路由缺失。
2. **冒烟测试**：向 `/ask` 发平凡问题（`ping`），必须返回 HTTP 200 且响应符合上述 schema。
