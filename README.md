# gh-puller

将 GitHub 开源仓库（含 PR/Issue）构建为知识库并搭载 agent，对外暴露 REST 接口，回答任何与代码库相关的问题。

当前阶段：**benchmark 评测框架**（评估参赛后端答案质量的测试管线）。

## 目录结构

| 路径 | 说明 |
|---|---|
| `gh_puller/benchmark/` | 评测框架（正式代码）：pipeline 调度 + REST 协议 + 协议层类型 |
| `gh_puller/benchmark/judges/` | 题库目录（预留）：出题人编写的真实题库放这里；编写约定见 protocol.md |
| `gh_puller/llm_ask/` | 内置方法：实现协议的服务（独立自包含，与 benchmark 独立发展） |
| `archive/` | 旧代码归档（勿动） |

## 快速上手（端到端自测）

> `benchmark/`（评测框架）与内置方法（如 `llm_ask/`）独立发展，仅通过 REST 协议互操作。

```bash
# 终端 1：启动内置方法 llm_ask（纯 LLM 问答，需 LLM_ASK_URL 端点可达）
uv run uvicorn gh_puller.llm_ask.server:app --port 8001

# 终端 2：跑一次评测（一个题库 + 一个 endpoint）
uv run benchmark gh_puller/benchmark/judges/vllm_mech/bank.py --url http://localhost:8001
```

结果：`outputs/<时间戳>/` 下生成 `result.json`（默认输出目录；`--out-dir` 可覆盖。单对象存档：`valid` / `invalid_reason` / `judgment` / `judge_error`）。

## 文档入口

- `gh_puller/protocol.md` —— **协议契约**（REST 协议 v1：唯一路由 `POST /ask`、请求/响应格式、接入检查）
- `gh_puller/benchmark/protocol.md` —— **出题人约定**（题库文件 `JUDGE` 接口）

## 核心设计（一句话）

题库自治 + 接口注入：pipeline 把参赛方接口封装成 `ask(question) -> Answer` 注入 `judge.__call__(ask)`，judge 自行加载题目数据、问参赛方、评判并组织输出；pipeline 对判定结果只存档、不解释。
