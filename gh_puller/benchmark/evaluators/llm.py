"""LLM 评测器:经 vLLM OpenAI 兼容 API 做自动多维评分(零新依赖,复用 httpx)。

入参:question/ref/answer 均为 str;输出自动评测契约(dimensions/overall/reason)。
模型地址与型号可用环境变量 LLM_JUDGE_URL / LLM_JUDGE_MODEL 覆盖,或由题库构造时传参。
"""

import json
import os

import httpx

from gh_puller.benchmark.env import TIMEOUT as GLOBAL_TIMEOUT  # 单题评分超时上限(1 小时)
from gh_puller.benchmark.evaluators.utils import auto_system_prompt, auto_user_prompt, coerce_verdict

# 单题评分超时:connect 短(端点不可达时快速降级),read 取全局单题超时上限
TIMEOUT = httpx.Timeout(connect=5.0, read=GLOBAL_TIMEOUT, write=30.0, pool=5.0)


class LLMEvaluator:
    """vLLM 服务上的评分模型逐题评分。"""

    name = "llm"

    def __init__(self, url: str = "", model: str = ""):
        self.url = url or os.environ.get("LLM_JUDGE_URL", "http://localhost:8000/v1")
        self.model = model or os.environ.get("LLM_JUDGE_MODEL", "Qwen2.5-7B-Instruct")

    async def evaluate(self, question: str, ref: str, answer: str) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": auto_system_prompt()},
                {"role": "user", "content": auto_user_prompt(question, ref, answer)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            last_err: Exception | None = None
            for nudge in (False, True):  # 解析失败重试 1 次(第二次追加"只输出 JSON"提示)
                if nudge:
                    payload["messages"].append({"role": "user", "content": "只输出 JSON,不要任何其他内容。"})
                try:
                    r = await client.post(f"{self.url}/chat/completions", json=payload)
                    r.raise_for_status()
                    content = r.json()["choices"][0]["message"]["content"]
                    return coerce_verdict(json.loads(content))
                except Exception as e:  # 网络/HTTP/解析失败:继续下一轮,耗尽后降级
                    last_err = e
        return {"dimensions": {}, "overall": 0, "reason": f"评测失败: {type(last_err).__name__}: {last_err}"}
