"""LLM 评测器:经 vLLM OpenAI 兼容 API 做自动多维评分(半抽象基类)。

机制(HTTP 调用、解析失败重试、降级输出)由本类提供;请求体(payload)与
判定解析是扩展点,由应用层题库以子类挂接,如 judges/vllm_mech/utils.py 的
auto_* 提示词与 coerce_verdict。模型地址与型号可用环境变量
LLM_JUDGE_URL / LLM_JUDGE_MODEL 覆盖,或由题库构造时传参。
"""

import json
import os

import httpx

from gh_puller.benchmark.env import TIMEOUT as GLOBAL_TIMEOUT  # 单题评分超时上限(1 小时)

# 单题评分超时:connect 短(端点不可达时快速降级),read 取全局单题超时上限
TIMEOUT = httpx.Timeout(connect=5.0, read=GLOBAL_TIMEOUT, write=30.0, pool=5.0)


class LLMEvaluator:
    """vLLM 服务上的评分模型逐题评分(半抽象基类:请求体由题库子类提供)。"""

    name = "llm"

    # 扩展点:解析失败重试时向 payload["messages"] 追加的提示(机制默认,题库可覆盖)
    retry_nudge: str = "只输出 JSON,不要任何其他内容。"

    def __init__(self, url: str = "", model: str = ""):
        self.url = url or os.environ.get("LLM_JUDGE_URL", "http://localhost:8000/v1")
        self.model = model or os.environ.get("LLM_JUDGE_MODEL", "Qwen2.5-7B-Instruct")

    def make_payload(self, question: str, ref: str, answer: str) -> dict:
        """chat/completions 请求体组装(OpenAI 兼容契约:须含可追加的 messages 键);题库子类必须提供。"""
        raise NotImplementedError

    def coerce(self, data) -> dict:
        """判定规范化(维度补齐/限幅);题库子类必须提供。"""
        raise NotImplementedError

    async def evaluate(self, question: str, ref: str, answer: str) -> dict:
        payload = self.make_payload(question, ref, answer)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            last_err: Exception | None = None
            for nudge in (False, True):  # 解析失败重试 1 次(第二次追加"只输出 JSON"提示)
                if nudge:
                    payload["messages"].append({"role": "user", "content": self.retry_nudge})
                try:
                    r = await client.post(f"{self.url}/chat/completions", json=payload)
                    r.raise_for_status()
                    content = r.json()["choices"][0]["message"]["content"]
                    return self.coerce(json.loads(content))
                except Exception as e:  # 网络/HTTP/解析失败:继续下一轮,耗尽后降级
                    last_err = e
        return {"dimensions": {}, "overall": 0, "reason": f"评测失败: {type(last_err).__name__}: {last_err}"}
