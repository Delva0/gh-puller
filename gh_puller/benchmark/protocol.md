# Benchmark 出题人约定

协议契约（REST 协议 v1：唯一路由 `POST /ask`、请求/响应格式、接入检查）见 **`gh_puller/protocol.md`**（唯一权威）。
本文件只规定 benchmark 一侧的**出题人约定**（题库文件接口）。

## 出题人约定（题库文件接口）

一套题库 = **一个 Python 文件**（导出 `JUDGE`）+ **数个 JSON 数据集**（judge 自行加载）。
题库文件可放在任意路径（插件式），打包后依然可用；仓库内 `gh_puller/benchmark/judges/` 为预留题库目录，
编写参考见测试用占位题库 `tests/benchmark/judges/vllm_bank.py`。

```python
# my_bank.py —— 题库文件
from gh_puller.types import Answer

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

输出：单对象存档 `result_<时间戳>.json`（默认写当前目录，可用 `--out <目录>` 指定），字段 name/url/valid/invalid_reason/judgment/judge_error——`valid` 为端口合法性（接入检查是否通过），`invalid_reason` 仅在非法时非空（出局原因），`judgment` 为 judge 返回值原样，`judge_error` 仅在裁判异常时非空。
