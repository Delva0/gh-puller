"""Define the vLLM mechanism question bank and its automated evaluators."""

import json
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from gh_puller.benchmark.evaluators import ClaudeEvaluator, LLMEvaluator
from gh_puller.benchmark.judges.base import ParallelJudge
from gh_puller.benchmark.judges.vllm_mechanism.utils import (
    DIMENSIONS,
    MCP_SERVERS,
    SKILLS,
    auto_system_prompt,
    auto_user_prompt,
    coerce_verdict,
)
from gh_puller.benchmark.types import Answer
from gh_puller.envs import JUDGE_EVALUATOR


class VllmMechEvalMixin:
    """Share verdict normalization across this bank's evaluators."""

    def coerce(self, data) -> dict:
        return coerce_verdict(data)


class VllmMechLLMEvaluator(VllmMechEvalMixin, LLMEvaluator):
    """Configure prompts and inference parameters for the LLM evaluator."""

    def system_prompt(self, question: str, ref: str, answer: str) -> str:
        return auto_system_prompt()

    def user_prompt(self, question: str, ref: str, answer: str) -> str:
        return auto_user_prompt(question, ref, answer)

    def request_parameters(self, question: str, ref: str, answer: str) -> dict:
        return {
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }


class VllmMechClaudeEvaluator(VllmMechEvalMixin, ClaudeEvaluator):
    """Configure a tool-free Claude evaluator for this question bank."""

    def make_options(self, question: str, ref: str, answer: str) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt={"type": "text", "text": auto_system_prompt()},
            allowed_tools=[],
            mcp_servers=MCP_SERVERS,
            skills=SKILLS,
            permission_mode="acceptEdits",
            model=self.model or None,
        )

    def user_prompt(self, question: str, ref: str, answer: str) -> str:
        return auto_user_prompt(question, ref, answer)


class VllmMechJudge(ParallelJudge):
    """Score bundled questions concurrently with the configured evaluator."""

    judge_name = "vllm-mech-v0.1"

    def __init__(self):
        self.evaluator = VllmMechClaudeEvaluator() if JUDGE_EVALUATOR == "claude" else VllmMechLLMEvaluator()

    def load_questions(self) -> list:
        return json.loads((Path(__file__).parent / "questions.json").read_text())

    async def judge_one(self, q, a: Answer) -> dict:
        """Score one answer and retain its complete question context."""
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
        """Evaluate all questions and append overall and per-dimension means."""
        out = await super().__call__(ask)
        overall, dims = [], {k: [] for k in DIMENSIONS}
        for r in out["results"]:
            j = r.get("judgment", {})
            if isinstance(j.get("overall"), (int, float)):
                overall.append(j["overall"])
            for k, v in (j.get("dimensions") or {}).items():
                if k in dims and isinstance(v, (int, float)):
                    dims[k].append(v)
        out["summary"] = {
            "overall_mean": round(sum(overall) / len(overall), 2) if overall else None,
            "dimension_means": {k: (round(sum(vs) / len(vs), 2) if vs else None) for k, vs in dims.items()},
        }
        return out


JUDGE = VllmMechJudge()
