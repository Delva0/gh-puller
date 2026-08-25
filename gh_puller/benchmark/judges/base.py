"""judge 半抽象基类提供:SequenceJudge(逐题顺序)与 ParallelJudge(逐题并发)。

扩展点只有题目序列(本质就是一个 list,每元素 = 一题,dict 即可)。
评测逐题进行:逐题 ask、逐题 judge,循环与聚合输出由本设施提供;
各题库以继承方式接入:提供题目序列(或 override load_questions() 动态加载),
评判规则不符默认时 override judge_one()。tests/benchmark/judges/vllm_bank.py
即 SequenceJudge 的应用特例。

ParallelJudge 扩展点、输出契约与 SequenceJudge 完全一致,仅执行方式从逐题顺序改为
逐题并发(信号量限流)。子类只需把继承基类从 SequenceJudge 换成 ParallelJudge。
注意:并发只适合无状态自动评测器(LLMEvaluator/ClaudeEvaluator,每题独立 client);
人工评审(HumanEvaluator,共享可变状态)场景禁止使用本类,必须用 SequenceJudge。
"""

import asyncio

from gh_puller.benchmark.types import Answer


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


class ParallelJudge(SequenceJudge):
    """半抽象基类：逐题并发 ask+judge（信号量限流）；扩展点同 SequenceJudge。"""

    # 扩展点：最大并发数（须 >= 1；Semaphore(0) 会抛 ValueError，不 clamp）
    max_concurrency: int = 4

    async def __call__(self, ask) -> dict:
        """逐题评测（并发版）：加载题目序列 → 并发 ask+judge → 单题失败容错 → 聚合输出。

        gather 保序：results[i] 恒对应第 i 题，与完成先后无关（summary 聚合不受影响）。
        单题异常 → 该题记录 error 占位结果，其余题继续；不中断整体评测。
        max_concurrency=1 时退化为严格串行。
        """
        sem = asyncio.Semaphore(self.max_concurrency)

        async def run_one(q):
            async with sem:  # 限流粒度：包住 ask+judge 整段（参赛方与评测器负载同步受限）
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
            for q, r in zip(questions, results)
        ]
        return {"judge": self.judge_name, "total_questions": len(results), "results": results}
