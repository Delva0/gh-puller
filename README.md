# gh-puller

将 GitHub 开源仓库（含 PR/Issue）构建为知识库并搭载 agent，对外暴露 REST 接口，回答任何与代码库相关的问题。

当前阶段：**benchmark 评测框架**（评估参赛后端答案质量的测试管线）。

## 目录结构

| 路径 | 说明 |
|---|---|
| `gh_puller/benchmark/` | 评测框架（正式代码）：pipeline 调度 + REST 协议 + 协议层类型 |
| `gh_puller/benchmark/judges/` | 题库目录（预留）：出题人编写的真实题库放这里；编写约定见 protocol.md |
| `tests/` | 项目测试目录，首层按子系统分（`tests/benchmark/` = benchmark 的测试，未来 `tests/kb/`、`tests/agent/` 各归各） |
| `tests/benchmark/judges/` | 测试用占位题库（端到端自测，真实题库到位后替换） |
| `tests/benchmark/fixtures/` | 测试夹具：假参赛方（实现协议的 FastAPI 服务器） |
| `archive/` | 旧代码归档（勿动） |

## 快速上手（端到端自测）

```bash
# 终端 1：启动假参赛方（测试夹具）
uv run uvicorn tests.benchmark.fixtures.dummy_server:app --port 8001

# 终端 2：跑一次评测（一个题库 + 一个 endpoint）
uv run benchmark tests/benchmark/judges/vllm_bank.py --url http://localhost:8001
```

结果：当前目录生成 `result_<时间戳>.json`（单对象存档：`valid` / `invalid_reason` / `judgment` / `judge_error`）。

## 文档入口

- `gh_puller/benchmark/protocol.md` —— **参赛方规则**（REST 协议 v1：唯一路由 `POST /ask`、资格检查）+ **出题人约定**（题库文件 `JUDGE` 接口）

## 核心设计（一句话）

题库自治 + 接口注入：pipeline 把参赛方接口封装成 `ask(question) -> Answer` 注入 `judge.__call__(ask)`，judge 自行加载题目数据、问参赛方、评判并组织输出；pipeline 对判定结果只存档、不解释。
