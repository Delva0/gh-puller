"""评测器协议(仅基类):三种评测器(LLM/Claude/Human)结构兼容即可,无需继承。

评测器是底层静态工具:只有 evaluate 接口,无生命周期,被题库(上层)随意调用。
入参/输出约定由各实现自定(question/ref/answer 均为 Any),统一见各实现 docstring:
- LLM/Claude(自动评测):入参三字符串,输出 {"dimensions": {...}, "overall": 0-10, "reason": str},
  维度与 prompt 组装见 utils.py;任一失败不得抛出,降级输出。
- Human:入参三字符串,输出评审表单数据(结构由 judge_schema 定义)。
"""

from typing import Any, Protocol


class Evaluator(Protocol):
    """评测器协议。

    name: 评测器标识,写入 judgment["evaluator"]。
    evaluate: 评判单题,返回 JSON 可序列化 dict。题库负责拆字段、组装上下文,
    把返回 dict 原样放进 judgment。
    """

    name: str

    async def evaluate(self, question: Any, ref: Any, answer: Any) -> dict:
        ...
