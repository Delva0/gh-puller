"""评测器(自动化评测基础设施):LLM(vLLM API)/Claude Code(SDK)/Human(web 评审)。

评测器是底层静态工具,只有 evaluate 接口、无生命周期,被题库(上层)随意调用;
LLM/Claude 为半抽象基类,评测内容由题库子类提供(见 judges/vllm_mech/utils.py);
接口契约见 base.py。
"""

from gh_puller.benchmark.evaluators.base import Evaluator
from gh_puller.benchmark.evaluators.claude import ClaudeEvaluator
from gh_puller.benchmark.evaluators.human import HumanEvaluator
from gh_puller.benchmark.evaluators.llm import LLMEvaluator

__all__ = ["Evaluator", "LLMEvaluator", "ClaudeEvaluator", "HumanEvaluator"]
