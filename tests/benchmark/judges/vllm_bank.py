"""Provide a placeholder question bank for end-to-end pipeline checks.

This SequenceJudge specialization supplies only the question sequence. Its questions
and heuristic judge are examples; benchmark authors provide production banks through
the ``--bank`` path.
"""

import json
from pathlib import Path

from gh_puller.benchmark.judges.base import SequenceJudge


class VllmJudge(SequenceJudge):
    """Load questions from vllm_questions.json and use keyword matching."""

    judge_name = "heuristic-demo"

    def load_questions(self) -> list:
        return json.loads((Path(__file__).parent / "vllm_questions.json").read_text())


JUDGE = VllmJudge()
