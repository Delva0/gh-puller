"""Claude Code 评测器:经 claude_agent_sdk 起 headless agent 做自动多维评分。

入参:question/ref/answer 均为 str;输出自动评测契约(dimensions/overall/reason)。
每题一个全新 agent 会话,无状态、无生命周期;模型可用环境变量 CLAUDE_JUDGE_MODEL
覆盖(缺省用 SDK 默认模型),需要 ANTHROPIC_API_KEY。
"""

import json
import os

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

from gh_puller.benchmark.evaluators.utils import auto_system_prompt, auto_user_prompt, coerce_verdict


class ClaudeEvaluator:
    """headless Claude agent 逐题评分。"""

    name = "claude"

    def __init__(self, model: str = ""):
        self.model = model or os.environ.get("CLAUDE_JUDGE_MODEL", "")

    async def evaluate(self, question: str, ref: str, answer: str) -> dict:
        options = ClaudeAgentOptions(
            system_prompt={"type": "text", "text": auto_system_prompt()},
            allowed_tools=[],  # 纯评判,不授任何工具
            permission_mode="acceptEdits",
            model=self.model or None,
        )
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(auto_user_prompt(question, ref, answer))
                result = None
                async for msg in client.receive_response():
                    if isinstance(msg, ResultMessage):
                        result = msg.result
            if result is None:
                raise RuntimeError("agent 未产出最终结果")
            return coerce_verdict(json.loads(result))
        except Exception as e:  # SDK/解析异常:降级输出,不抛出
            return {"dimensions": {}, "overall": 0, "reason": f"评测失败: {type(e).__name__}: {e}"}
