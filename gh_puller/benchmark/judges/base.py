"""Provide sequential and bounded-concurrency bases for question-bank judges.

Banks supply question data and may override extraction or scoring. ``ParallelJudge``
preserves input order but is suitable only for evaluators without shared mutable state.
"""

import asyncio
from typing import ClassVar

from gh_puller.benchmark.types import Answer


class SequenceJudge:
    """Ask and judge each question sequentially."""

    questions: ClassVar[list] = []
    judge_name: str = "sequence"

    def load_questions(self) -> list:
        """Return the class-level question list by default."""
        return self.questions

    def question_text(self, q) -> str:
        """Extract question text from the default dictionary representation."""
        return q["question"]

    async def judge_one(self, q, a: Answer) -> dict:
        """Score case-insensitive reference-keyword coverage for one answer."""
        ref = q["ref_answer"]
        text = a.text.lower()
        hits = sum(k.lower() in text for k in ref)
        return {
            "id": q.get("id", ""),
            "question": self.question_text(q),
            "ref_answer": list(ref),
            "answer": a.text,
            "judgment": {"hits": hits, "total": len(ref)},
        }

    async def __call__(self, ask) -> dict:
        """Evaluate all questions sequentially and aggregate their results."""
        results = [
            await self.judge_one(q, await ask(self.question_text(q)))
            for q in self.load_questions()
        ]
        return {
            "judge": self.judge_name,
            "total_questions": len(results),
            "results": results,
        }


class ParallelJudge(SequenceJudge):
    """Ask and judge questions concurrently under a shared limit."""

    max_concurrency: int = 4

    async def __call__(self, ask) -> dict:
        """Evaluate concurrently, retaining input order and per-question failures."""
        sem = asyncio.Semaphore(self.max_concurrency)

        async def run_one(q):
            async with sem:  # Limit participant and evaluator load together.
                return await self.judge_one(q, await ask(self.question_text(q)))

        questions = self.load_questions()
        results = await asyncio.gather(*(run_one(q) for q in questions), return_exceptions=True)
        results = [
            r if not isinstance(r, Exception) else {
                "id": q.get("id", ""),
                "question": self.question_text(q),
                "ref_answer": list(q.get("ref_answer", [])),
                "error": f"{type(r).__name__}: {r}",
            }
            for q, r in zip(questions, results, strict=True)
        ]
        return {"judge": self.judge_name, "total_questions": len(results), "results": results}
