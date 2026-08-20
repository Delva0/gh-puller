"""parallel judge：逐题并发评测的半抽象基础设施。

扩展点、输出契约与 SequenceJudge 完全一致，仅执行方式从逐题顺序改为
逐题并发（信号量限流）。子类只需把继承基类从 SequenceJudge 换成 ParallelJudge。
注意：并发只适合无状态自动评测器（LLMEvaluator/ClaudeEvaluator，每题独立 client）；
人工评审（HumanEvaluator，共享可变状态）场景禁止使用本类，必须用 SequenceJudge。
"""

import asyncio

from gh_puller.benchmark.judges.sequence import SequenceJudge


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
