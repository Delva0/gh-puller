"""chat 主线:一次 chat 问答的流式应答(纯文本 chunk 序列,前后端协议同原
research_chat)。

入口 chat_stream 按 choice.generator 内联分派(cc/dsh/codex → _agent_chat 现代
agent 模式;llm → _llm_chat 原式单次补全,分派规则与 wiki._wiki_pipeline 同);
本主线专用 helper:历史转写(_render_natural_history / _build_turn_history)、
continuation 回退(_resolve_chat_continuation)、深研究模板常量(标题字符串逐字
匹配前端 Ask.tsx 的提取/完成判定正则,见常量注释)。
跨功能通用 helper(四路装配/检索簇/research 协议/提示词共性)在 utils。
"""

from __future__ import annotations


def _resolve_chat_continuation(last: dict, messages: list[dict]) -> None:
    """continuation 回退(移植 research.py):末条含 continue+research 时换回首个用户消息(就地改 last['content'])。"""
    if "continue" in last.get("content", "").lower() and "research" in last.get("content", "").lower():
        for msg in messages:
            if msg.get("role") == "user" and "continue" not in msg.get("content", "").lower():
                last["content"] = msg["content"].strip()
                break
