"""假参赛方（测试夹具）：实现 protocol.md 的 REST 协议，用于端到端自测评测管线。

角色：模拟一个"合规参赛方"，让 pipeline 可以本地跑通，不依赖真实后端。
它不是被测对象，也不是框架代码，只是测试支撑物。

启动（合法形态，有 /ask 路由）：
    uv run uvicorn tests.benchmark.fixtures.dummy_server:app --port 8001
    uv run benchmark tests/benchmark/judges/vllm_bank.py --url http://localhost:8001

负路径自测（缺 /ask 路由形态）：置环境变量 DUMMY_NO_ASK=1 再启动，
pipeline 应判定该端口非法（invalid_reason 含"返回 404，路由缺失"）：
    DUMMY_NO_ASK=1 uv run uvicorn tests.benchmark.fixtures.dummy_server:app --port 8002
"""

import os

from fastapi import FastAPI

app = FastAPI()

if not os.environ.get("DUMMY_NO_ASK"):  # 置 1 时模拟"缺少 /ask 路由"的参赛方（负路径自测）


    @app.post("/ask")
    def ask(payload: dict) -> dict:
        # 回声式回答：原样包含问题以便 heuristic 裁判命中要点；sources 字段演示协议容忍附加字段
        q = payload.get("question", "")
        return {"answer": f"关于「{q}」的回答如下：{q}", "sources": ["dummy:0001"]}
