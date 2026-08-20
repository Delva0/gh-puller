# Benchmark 协议

本文件规定两部分：**参赛方规则**（REST 协议）与**出题人约定**（题库文件接口）。

## 一、参赛方规则（REST 协议 v1）

参赛方只需提供一个 `base_url`（如 `http://localhost:8001`）即可参赛。
评测管线**只测试协议规定的这一条路由**，参赛方端点上其他任何路由一律不测。

### 唯一路由

#### POST `{base_url}/ask`

**请求体**（JSON）：

- `question`：字符串，必填，非空
- 允许附加字段（如 `context`），未知字段不会被拒绝（协议前向兼容）

示例：

```json
{"question": "vllm 推理时显存不足怎么办？", "context": {"repo": "vllm-project/vllm"}}
```

**响应体**（JSON）：

- `answer`：字符串，必填，非空
- 允许附加字段（如 `sources`），未知字段会被保留并随 `Answer` 传递

示例：

```json
{"answer": "可以尝试开启量化、减小 KV cache……", "sources": ["issue#123"]}
```

**超时与重试**：单题超时 3600 秒；连接类错误自动重试 3 次，HTTP 错误不重试。

### 参赛资格检查（两关，任一失败即取消资格）

1. **路由探测**：优先读取 `GET {base_url}/openapi.json` 的 `paths`，检查是否声明 `POST /ask`；
   无法读取时向 `/ask` 发探测请求，返回 404 即视为路由缺失。
2. **冒烟测试**：向 `/ask` 发平凡问题（`ping`），必须返回 HTTP 200 且响应符合上述 schema。

## 二、出题人约定（题库文件接口）

一套题库 = **一个 Python 文件**（导出 `JUDGE`）+ **数个 JSON 数据集**（judge 自行加载）。
题库文件可放在任意路径（插件式），打包后依然可用；仓库内 `gh_puller/benchmark/judges/` 为预留题库目录，
编写参考见测试用占位题库 `tests/benchmark/judges/vllm_bank.py`。

```python
# my_bank.py —— 题库文件
from gh_puller.benchmark.types import Answer

class MyJudge:
    async def __call__(self, ask) -> dict:
        # 自行 load 自己的 JSON 数据集（题目/参考答案，格式自己定）
        # 自行调用参赛方接口，自行评判，自行组织输出
        ...

JUDGE = MyJudge()
```

### `JUDGE` 接口

- `async def __call__(self, ask) -> Any`
- `ask`：pipeline 注入的参赛方接口封装，签名 `async def ask(question: str) -> Answer`
  - 带超时；连接类错误已自动重试；其余异常向上抛，由 judge 自行决定如何处理（重试/跳过/记录）
- 返回值：任意 JSON 可序列化值（judgment），pipeline 原样存档，不做任何解释
- judge 自身异常 → 该次评测记为"裁判失败"，仍出存档文件

### 规则

- 一次运行只测**一套题库 + 一个 endpoint**；多题库/多参赛方 = 二次开发者循环调用
- 多 judge 组合 = 基于现有 judge 自写组合类，框架不提供
- 题目形态、参考答案、JSON 格式、评判逻辑、输出结构——全部题库自拟，框架零认知

## 三、运行

```bash
uv run benchmark /任意路径/vllm_bank.py --url http://localhost:8000 --name 可选别名
```

输出：单对象存档 `result_<时间戳>.json`（默认写当前目录，可用 `--out <目录>` 指定），字段 name/url/valid/invalid_reason/judgment/judge_error——`valid` 为端口合法性（资格检查是否通过），`invalid_reason` 仅在非法时非空（出局原因），`judgment` 为 judge 返回值原样，`judge_error` 仅在裁判异常时非空。

## 四、本地自测

```bash
# 终端 1：启动假参赛方（测试夹具，位于 tests/benchmark/fixtures/）
uv run uvicorn tests.benchmark.fixtures.dummy_server:app --port 8001

# 终端 2：跑评测（--bank 用测试占位题库）
uv run benchmark \
  --bank tests/benchmark/judges/vllm_bank.py \
  --url http://localhost:8001
```
