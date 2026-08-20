"""v0.1 正式题库:机制/原理类(vLLM 核心机制)。

judge 用自动评测器(LLM/Claude)按多维标准评分;也可注入 HumanEvaluator 人工评审。
评测器为半抽象基类,评分维度/提示词/判定解析/工具授权等扩展点取
本包 utils.py(本题库专属配置),以子类挂接。
每题 ref_answer 为 3-4 个断言式要点(拼接成文本传给评测器,作 accuracy 判定的基准),
四字段上下文保留原始列表。
"""

import json
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from gh_puller.benchmark.env import JUDGE_EVALUATOR
from gh_puller.benchmark.evaluators import ClaudeEvaluator, LLMEvaluator
from gh_puller.benchmark.judges.parallel import ParallelJudge
from gh_puller.benchmark.judges.vllm_mech.utils import (
    DIMENSIONS,
    MCP_SERVERS,
    SKILLS,
    auto_system_prompt,
    auto_user_prompt,
    coerce_verdict,
)
from gh_puller.types import Answer


class VllmMechEvalMixin:
    """题库特化扩展点:判定解析(LLM/Claude 评测器共用)。"""

    def coerce(self, data) -> dict:
        return coerce_verdict(data)


class VllmMechLLMEvaluator(VllmMechEvalMixin, LLMEvaluator):
    """题库特化扩展点:chat/completions 请求体组装(评分规则 + 单题请求)。"""

    def make_payload(self, question: str, ref: str, answer: str) -> dict:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": auto_system_prompt()},
                {"role": "user", "content": auto_user_prompt(question, ref, answer)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }


class VllmMechClaudeEvaluator(VllmMechEvalMixin, ClaudeEvaluator):
    """题库特化扩展点:agent 配置与查询文本组装(纯评分,不授任何工具)。"""

    def make_options(self, question: str, ref: str, answer: str) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt={"type": "text", "text": auto_system_prompt()},
            allowed_tools=[],  # 纯评分,不授任何工具(MCP/Skill 配置见本包 utils.py)
            mcp_servers=MCP_SERVERS,
            skills=SKILLS,
            permission_mode="acceptEdits",
            model=self.model or None,
        )

    def user_prompt(self, question: str, ref: str, answer: str) -> str:
        return auto_user_prompt(question, ref, answer)


class VllmMechJudge(ParallelJudge):
    """应用特例:题目来自 questions.json,评判用评测器多维评分(并行,默认 LLM 评测器)。"""

    judge_name = "vllm-mech-v0.1"

    def __init__(self):
        # 评测器选择:JUDGE_EVALUATOR=claude 切到 Claude Code 评测器;默认 LLM 评测器
        self.evaluator = VllmMechClaudeEvaluator() if JUDGE_EVALUATOR == "claude" else VllmMechLLMEvaluator()

    def load_questions(self) -> list:
        return json.loads((Path(__file__).parent / "questions.json").read_text())

    async def judge_one(self, q, a: Answer) -> dict:
        """单题评判:评测器逐维打分;结果 = 上下文四字段 + judgment(评测器名 + 判定)。"""
        ref = q["ref_answer"]
        verdict = await self.evaluator.evaluate(q["question"], "\n".join(ref), a.text)
        return {
            "id": q.get("id", ""),
            "question": q["question"],
            "ref_answer": list(ref),
            "answer": a.text,
            "judgment": {"evaluator": self.evaluator.name, **verdict},
        }

    async def __call__(self, ask) -> dict:
        """逐题评测 + 聚合 summary(overall 均值与每维均值;自动评测器才有,人工表单无则 None)。"""
        out = await super().__call__(ask)  # 复用逐题循环:ask → judge_one
        overall, dims = [], {k: [] for k in DIMENSIONS}
        for r in out["results"]:
            j = r.get("judgment", {})
            if isinstance(j.get("overall"), (int, float)):
                overall.append(j["overall"])
            for k in dims:
                v = (j.get("dimensions") or {}).get(k)
                if isinstance(v, (int, float)):
                    dims[k].append(v)
        out["summary"] = {
            "overall_mean": round(sum(overall) / len(overall), 2) if overall else None,
            "dimension_means": {k: (round(sum(vs) / len(vs), 2) if vs else None) for k, vs in dims.items()},
        }
        return out


JUDGE = VllmMechJudge()
