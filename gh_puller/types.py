"""协议层数据类型：ask 请求/响应结构的代码化（调用方与服务方共用的唯一定义）。

题目（Question）、参考答案（RefAnswer）等形态由各题库自拟，不在此定义。
未来协议升级（如响应携带 agent 输出过程）只在此加字段。
"""

from dataclasses import dataclass


@dataclass
class Answer:
    text: str  # 服务方回答文本（协议必填字段）
