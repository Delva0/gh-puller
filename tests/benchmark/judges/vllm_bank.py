"""测试用样例题库（占位）：端到端自测评测管线，非正式题库。

sequence judge 的一个应用特例：扩展点只有题目序列。
题目为占位样例、judge 为演示实现（heuristic 命中），真实题库由出题人编写，
放 gh_puller/benchmark/judges/（或任意路径，--bank 指向即可）。
"""

import json
from pathlib import Path

from gh_puller.benchmark.judges.base import SequenceJudge


class VllmJudge(SequenceJudge):
    """应用特例：题目来自 vllm_questions.json，评判用默认关键词命中。"""

    judge_name = "heuristic-demo"

    def load_questions(self) -> list:
        return json.loads((Path(__file__).parent / "vllm_questions.json").read_text())


JUDGE = VllmJudge()
