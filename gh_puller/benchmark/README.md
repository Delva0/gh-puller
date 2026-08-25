# gh-puller benchmark 评测框架

单点评测:一个题库文件 + 一个参赛方 endpoint。框架只认识三样东西:ask 接口签名、题库导出的 `JUDGE`、judge 返回的 judgment(原样存档);题目形态、参考答案、评判逻辑、输出结构——全部由题库(出题人)自拟,框架零认知。

## 协议契约（REST 协议 v1）

本节是协议契约的**唯一权威**:服务方(参赛方实现)实现它,调用方(评测管线)调用它。
任何一侧都不持有自己的协议定义。

服务方只需提供一个 `base_url`(如 `http://localhost:8001`)即可接入。调用方只测试协议规定的这一条路由,
服务方端点上其他任何路由一律不测。

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
- 允许附加字段（如 `sources`），未知字段会被保留

示例：

```json
{"answer": "可以尝试开启量化、减小 KV cache……", "sources": ["issue#123"]}
```

**超时与重试**：单次请求超时 3600 秒；连接类错误自动重试 3 次，HTTP 错误不重试。

#### GET `{base_url}/openapi.json`

- 声明 `POST /ask` 路由（推荐，便于调用方探测）

### 接入检查（两关，任一失败即判定不可接入）

1. **路由探测**：优先读取 `GET {base_url}/openapi.json` 的 `paths`，检查是否声明 `POST /ask`；
   无法读取时向 `/ask` 发探测请求，返回 404 即视为路由缺失。
2. **冒烟测试**：向 `/ask` 发平凡问题（`ping`），必须返回 HTTP 200 且响应符合上述 schema。

协议契约另有代码化形式(`gh_puller/benchmark/protocol.py` 的 `ASK_PATH`/`OPENAPI_PATH`/`RESPONSE_SCHEMA`
与 `gh_puller/benchmark/types.py` 的 `Answer`),调用方与服务方共用;未来协议升级只在此加字段。

## 出题人约定（题库文件接口）

协议契约见上文。本节只规定 benchmark 一侧的**出题人约定**（题库文件接口）。

一套题库 = **一个 Python 文件**（导出 `JUDGE`）+ **数个 JSON 数据集**（judge 自行加载）。
题库文件可放在任意路径（插件式），打包后依然可用；仓库内 `gh_puller/benchmark/judges/` 为题库目录
（内置正式题库 `judges/vllm_mechanism/`），编写参考见 `gh_puller/benchmark/judges/vllm_mechanism/bank.py`。

```python
# my_bank.py —— 题库文件
from gh_puller.benchmark.types import Answer

class MyJudge:
    async def __call__(self, ask) -> dict:
        # 自行 load 自己的 JSON 数据集（题目/参考答案，格式自己定）
        # 自行调用服务方接口，自行评判，自行组织输出
        ...

JUDGE = MyJudge()
```

### `JUDGE` 接口

- `async def __call__(self, ask) -> Any`
- `ask`：pipeline 注入的服务方接口封装，签名 `async def ask(question: str) -> Answer`
  - 带超时；连接类错误已自动重试；其余异常向上抛，由 judge 自行决定如何处理（重试/跳过/记录）
- 返回值：任意 JSON 可序列化值（judgment），pipeline 原样存档，不做任何解释
- judge 自身异常 → 该次评测记为"裁判失败"，仍出存档文件

### 规则

- 一次运行只测**一套题库 + 一个 endpoint**；多题库/多服务方 = 二次开发者循环调用
- 多 judge 组合 = 基于现有 judge 自写组合类，框架不提供
- 题目形态、参考答案、JSON 格式、评判逻辑、输出结构——全部题库自拟，框架零认知

## 运行

```bash
uv run benchmark /任意路径/vllm_bank.py --url http://localhost:8000 --name 可选别名
```

输出：单对象存档 `outputs/<时间戳>/result.json`（默认输出目录，可用 `--out-dir <目录>` 覆盖），
字段 name/url/valid/invalid_reason/judgment/judge_error——`valid` 为端口合法性（接入检查是否通过），
`invalid_reason` 仅在非法时非空（出局原因），`judgment` 为 judge 返回值原样，`judge_error` 仅在裁判异常时非空。
