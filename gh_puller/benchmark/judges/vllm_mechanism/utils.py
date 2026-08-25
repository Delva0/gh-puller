"""vllm_mechanism 题库的自动评测配置:评分维度、提示词组装、判定解析、Claude 工具授权。

LLM/Claude 评测器为半抽象基础设施,evaluate 机制由基类提供,评测内容
(本文件)由题库提供并挂接为子类扩展点;扩展点即本题库的专属评判规则。
"""

# 自动评测共用的评分维度(每维 0-10)
DIMENSIONS = {
    "code_essence": "接近代码细节与本质",  # 是否触及核心机制/代码级细节
    "detail": "内容详细度",
    "file_links": "文件链接数量",  # 具体文件/文档链接(如 vllm/vllm/attention/...)
    "time_precision": "时间精确到 commit/版本",
    "accuracy": "最终答案准确",
    "logic_depth": "补充问题背后的逻辑",
    "latent_need": "解决提问者潜在需求",
}

# Claude 评测器工具授权配置:本题库纯评分,不授任何工具;需要时在此扩展
MCP_SERVERS: dict = {}
SKILLS: list = []


def auto_system_prompt() -> str:
    """自动评测器的评分规则,随 DIMENSIONS 变动。"""
    dims = "\n".join(f"- {k}: {v}" for k, v in DIMENSIONS.items())
    return (
        "你是 vLLM 技术知识评测的评分员,负责评判参赛方对给定题目的回答。\n"
        f"评分维度(每维 0-10 分):\n{dims}\n"
        "评分原则:越接近代码细节与本质、内容越详细、给出的文件链接越多、"
        "涉及的时间/版本越能精确落到 commit、最终答案越准确、越能补充问题背后的逻辑、"
        "越能解决提问者的潜在需求,得分越高;回答与题目无关或存在事实错误时给低分。\n"
        "只输出 JSON,不要输出任何其他内容。"
    )


def auto_user_prompt(question: str, ref: str, answer: str) -> str:
    """自动评测的单题请求(题目 + 参考答案要点 + 参赛方回答 + JSON 输出规格)。"""
    keys = ", ".join(DIMENSIONS)
    return (
        f"题目:\n{question}\n\n参考答案要点:\n{ref}\n\n参赛方回答:\n{answer}\n\n"
        f"请按以下维度逐维打分(每维 0-10 的整数):{keys}。\n"
        "并给出综合分 overall(0-10)与简短理由 reason。\n"
        "只输出 JSON,格式:"
        f'{{"dimensions": {{"{keys} 各键: 0-10"}}, "overall": 0-10, "reason": "一句话理由"}}'
    )


def coerce_verdict(data) -> dict:
    """自动评测输出规范化:维度补齐/限幅 0-10、overall 限幅、reason 兜底;结构不合法时抛异常由调用方降级。"""
    if not isinstance(data, dict):
        raise ValueError("评测输出不是 JSON 对象")
    dims = data.get("dimensions")
    if not isinstance(dims, dict):
        raise ValueError("评测输出缺 dimensions")
    return {
        "dimensions": {k: min(max(float(dims.get(k, 0)), 0.0), 10.0) for k in DIMENSIONS},
        "overall": min(max(float(data.get("overall", 0)), 0.0), 10.0),
        "reason": str(data.get("reason", "")),
    }
