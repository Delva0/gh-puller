"""评测器(自动化评测基础设施):LLM(vLLM API)/Claude Code(SDK)/Human(web 评审)。

评测器是底层静态工具,只有 evaluate 接口、无生命周期,被题库(上层)随意调用;
接口契约与评分维度见 base.py。
"""

from gh_puller.benchmark.evaluators.base import Evaluator
from gh_puller.benchmark.evaluators.claude import ClaudeEvaluator
from gh_puller.benchmark.evaluators.human import HumanEvaluator
from gh_puller.benchmark.evaluators.llm import LLMEvaluator
from gh_puller.benchmark.evaluators.utils import DIMENSIONS

__all__ = ["Evaluator", "DIMENSIONS", "LLMEvaluator", "ClaudeEvaluator", "HumanEvaluator"]
