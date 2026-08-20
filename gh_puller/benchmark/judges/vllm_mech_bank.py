"""v0.1 正式题库:机制/原理类(vLLM 核心机制)。

judge 用自动评测器(LLM/Claude)按多维标准评分;也可注入 HumanEvaluator 人工评审。
每题 ref_answer 为 3-4 个断言式要点(拼接成文本传给评测器,作 accuracy 判定的基准),
四字段上下文保留原始列表。
"""

import json
import os
from pathlib import Path

from gh_puller.benchmark.evaluators import ClaudeEvaluator, DIMENSIONS, LLMEvaluator
from gh_puller.benchmark.judges.sequence import SequenceJudge
from gh_puller.benchmark.types import Answer


class VllmMechJudge(SequenceJudge):
    """应用特例:题目来自 vllm_mech_questions.json,评判用评测器多维评分。"""

    judge_name = "vllm-mech-v0.1"

    def __init__(self):
        # 评测器选择:环境变量 JUDGE_EVALUATOR=claude 切到 Claude Code 评测器;默认 LLM 评测器
        self.evaluator = ClaudeEvaluator() if os.environ.get("JUDGE_EVALUATOR") == "claude" else LLMEvaluator()

    def load_questions(self) -> list:
        return json.loads((Path(__file__).parent / "vllm_mech_questions.json").read_text())

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
