"""Implement the minimal LLM-backed ``/ask`` fixture.

Questions go directly to an OpenAI-compatible endpoint. Failures propagate so FastAPI
returns an error for the caller to handle.
"""

import httpx
from fastapi import FastAPI

from .envs import LLM_ASK_API_KEY, LLM_ASK_MODEL, LLM_ASK_URL
from .envs import TIMEOUT as GLOBAL_TIMEOUT

# Fail fast on unreachable endpoints while allowing long model inference.
TIMEOUT = httpx.Timeout(connect=5.0, read=GLOBAL_TIMEOUT, write=30.0, pool=5.0)

SYSTEM_PROMPT = "你是开源代码库问答助手。基于你的知识直接、准确地用中文回答以下问题,不要输出任何多余内容。"

app = FastAPI(title="llm_ask", description="用 LLM 包装的 ask 路由(方法:纯 LLM 问答)")


async def ask_llm(question: str) -> str:
    """Send a question to the configured LLM endpoint and return its answer."""
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
    """Answer the protocol's sole route and include the model as optional metadata."""
    text = await ask_llm(payload["question"])
    return {"answer": text, "model": LLM_ASK_MODEL}
