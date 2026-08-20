"""Claude Code 评测器:经 claude_agent_sdk 起 headless agent 做自动多维评分(半抽象基类)。

机制(SDK 会话、无状态逐题)由本类提供;agent 配置(options)、查询文本与
判定解析是扩展点,由应用层题库以子类挂接,如 judges/vllm_mech/utils.py 的
auto_* 提示词与 MCP_SERVERS/SKILLS。模型可用环境变量 CLAUDE_JUDGE_MODEL
覆盖(缺省用 SDK 默认模型),需要 ANTHROPIC_API_KEY。
"""

import json
import os

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage


class ClaudeEvaluator:
    """headless Claude agent 逐题评分(半抽象基类:agent 配置由题库子类提供)。"""

    name = "claude"

    def __init__(self, model: str = ""):
        self.model = model or os.environ.get("CLAUDE_JUDGE_MODEL", "")

    def make_options(self, question: str, ref: str, answer: str) -> ClaudeAgentOptions:
        """agent 配置组装(system_prompt/工具授权/模型等);题库子类必须提供。"""
        raise NotImplementedError

    def user_prompt(self, question: str, ref: str, answer: str) -> str:
        """单题请求文本(query);题库子类必须提供。"""
        raise NotImplementedError

    def coerce(self, data) -> dict:
        """判定规范化(维度补齐/限幅);题库子类必须提供。"""
        raise NotImplementedError

    async def evaluate(self, question: str, ref: str, answer: str) -> dict:
        try:
            async with ClaudeSDKClient(options=self.make_options(question, ref, answer)) as client:
                await client.query(self.user_prompt(question, ref, answer))
                result = None
                async for msg in client.receive_response():
                    if isinstance(msg, ResultMessage):
                        result = msg.result
            if result is None:
                raise RuntimeError("agent 未产出最终结果")
            return self.coerce(json.loads(result))
        except Exception as e:  # SDK/解析异常:降级输出,不抛出
            return {"dimensions": {}, "overall": 0, "reason": f"评测失败: {type(e).__name__}: {e}"}
