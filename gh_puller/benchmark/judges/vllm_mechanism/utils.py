"""Configure scoring dimensions, prompts, and verdict parsing for this bank."""

# Shared 0-10 scoring dimensions for automated evaluators.
DIMENSIONS = {
    "code_essence": "接近代码细节与本质",
    "detail": "内容详细度",
    "file_links": "文件链接数量",
    "time_precision": "时间精确到 commit/版本",
    "accuracy": "最终答案准确",
    "logic_depth": "补充问题背后的逻辑",
    "latent_need": "解决提问者潜在需求",
}

# This bank grants its Claude evaluator no tools.
MCP_SERVERS: dict = {}
SKILLS: list = []


def auto_system_prompt() -> str:
    """Build scoring instructions from ``DIMENSIONS``."""
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
    """Build one evaluation request with its required JSON output shape."""
    keys = ", ".join(DIMENSIONS)
    return (
        f"题目:\n{question}\n\n参考答案要点:\n{ref}\n\n参赛方回答:\n{answer}\n\n"
        f"请按以下维度逐维打分(每维 0-10 的整数):{keys}。\n"
        "并给出综合分 overall(0-10)与简短理由 reason。\n"
        "只输出 JSON,格式:"
        f'{{"dimensions": {{"{keys} 各键: 0-10"}}, "overall": 0-10, "reason": "一句话理由"}}'
    )


def coerce_verdict(data) -> dict:
    """Validate a verdict, fill dimensions, and clamp numeric scores to 0-10."""
    if not isinstance(data, dict):
        raise TypeError("评测输出不是 JSON 对象")
    dims = data.get("dimensions")
    if not isinstance(dims, dict):
        raise TypeError("评测输出缺 dimensions")
    return {
        "dimensions": {k: min(max(float(dims.get(k, 0)), 0.0), 10.0) for k in DIMENSIONS},
        "overall": min(max(float(data.get("overall", 0)), 0.0), 10.0),
        "reason": str(data.get("reason", "")),
    }
