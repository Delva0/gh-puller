"""方法:llm_ask——用 LLM 包装的 ask 路由(纯 LLM 问答,最简)。

收到 question 后直接调 vLLM OpenAI 兼容端点生成回答,失败向上抛(FastAPI 返回 500,由调用方自行处理)。
"""

import httpx
from fastapi import FastAPI

from .envs import LLM_ASK_API_KEY, LLM_ASK_MODEL, LLM_ASK_URL
from .envs import TIMEOUT as GLOBAL_TIMEOUT

# 方法内 LLM 调用超时:connect 短(端点不可达时快速失败),read 取本方法超时上限
TIMEOUT = httpx.Timeout(connect=5.0, read=GLOBAL_TIMEOUT, write=30.0, pool=5.0)

SYSTEM_PROMPT = "你是开源代码库问答助手。基于你的知识直接、准确地用中文回答以下问题,不要输出任何多余内容。"

app = FastAPI(title="llm_ask", description="用 LLM 包装的 ask 路由(方法:纯 LLM 问答)")


async def ask_llm(question: str) -> str:
    """向 LLM 端点发问,返回回答文本。"""
    payload = {
        "model": LLM_ASK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {LLM_ASK_API_KEY}"} if LLM_ASK_API_KEY else None
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(f"{LLM_ASK_URL}/chat/completions", json=payload, headers=headers)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


@app.post("/ask")
async def ask(payload: dict) -> dict:
    """协议唯一路由:POST /ask → LLM 问答;响应附 model 字段演示协议容忍附加字段。"""
    text = await ask_llm(payload["question"])
    return {"answer": text, "model": LLM_ASK_MODEL}
