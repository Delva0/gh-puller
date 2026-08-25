# gh-puller

将 GitHub 开源仓库（含 PR/Issue）构建为知识库并搭载 agent，对外暴露 REST 接口，回答任何与代码库相关的问题。

当前阶段：**benchmark 评测框架**（评估参赛后端答案质量的测试管线）。

## 目录结构

| 路径 | 说明 |
|---|---|
| `gh_puller/benchmark/` | 评测框架（正式代码）：pipeline 调度 + REST 协议 + 协议层类型 |
| `gh_puller/benchmark/judges/` | 题库目录：出题人编写的真实题库放这里（内置 vllm_mechanism/）；编写约定见 README.md |
| `archive/` | 旧代码归档（勿动） |

## 快速上手（端到端自测）

> 参赛方（服务方）与 `benchmark/`（评测框架）独立发展，仅通过 REST 协议互操作。

先启动一个参赛方服务：任意实现 REST 协议 v1 的服务（唯一路由 `POST /ask`，见
`gh_puller/benchmark/README.md`），记下其 `base_url`（如 `http://localhost:8001`）。
然后跑一次评测（一个题库 + 一个 endpoint）：

```bash
uv run benchmark gh_puller/benchmark/judges/vllm_mechanism/bank.py --url http://localhost:8001
```

结果：`outputs/<时间戳>/` 下生成 `result.json`（默认输出目录；`--out-dir` 可覆盖。单对象存档：`valid` / `invalid_reason` / `judgment` / `judge_error`）。

## 文档入口

- `gh_puller/benchmark/README.md` —— **协议契约**（REST 协议 v1：唯一路由 `POST /ask`、请求/响应格式、接入检查）+ **出题人约定**（题库文件 `JUDGE` 接口）
- `docs/agent-monitor.md` —— **LLM 调用流式监控**（文件 sink 默认开 + Web/WS hub；CC 与 openai 调用统一观测）

## 核心设计（一句话）

题库自治 + 接口注入：pipeline 把参赛方接口封装成 `ask(question) -> Answer` 注入 `judge.__call__(ask)`，judge 自行加载题目数据、问参赛方、评判并组织输出；pipeline 对判定结果只存档、不解释。
