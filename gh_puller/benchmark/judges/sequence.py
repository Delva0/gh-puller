"""sequence judge：逐题顺序评测的半抽象基础设施。

扩展点只有题目序列（本质就是一个 list，每元素 = 一题，dict 即可）。
评测按序列顺序逐题进行：逐题 ask、逐题 judge，循环与聚合输出由本设施提供；
各题库以继承方式接入：提供题目序列（或 override load_questions() 动态加载），
评判规则不符默认时 override judge_one()。tests/benchmark/judges/vllm_bank.py
即本设施的应用特例。
"""

from gh_puller.types import Answer


class SequenceJudge:
    """半抽象基类：子类 = 一套题目序列（+ 可选钩子覆盖）。"""

    # 扩展点：题目序列，本质 list（dict：{"id", "question", "ref_answer": [...]}）
    questions: list = []
    judge_name: str = "sequence"  # 输出中的 judge 标识

    def load_questions(self) -> list:
        """题目加载：默认取类属性 questions；子类可 override（如从 JSON 文件读）。"""
        return self.questions

    def question_text(self, q) -> str:
        """题目文本提取：默认 q["question"]；题目形态非 dict 时可 override。"""
        return q["question"]

    async def judge_one(self, q, a: Answer) -> dict:
        """单题评判：默认按 q["ref_answer"] 关键词（不区分大小写）命中计分；结果 = 上下文四字段 + judgment（评判）。子类可 override（可为 async，等待外部输入）。"""
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
        """逐题评测：加载题目序列 → 逐题 ask → 逐题 judge → 聚合输出。"""
        results = [
            await self.judge_one(q, await ask(self.question_text(q)))
            for q in self.load_questions()
        ]
        return {
            "judge": self.judge_name,
            "total_questions": len(results),
            "results": results,
        }
