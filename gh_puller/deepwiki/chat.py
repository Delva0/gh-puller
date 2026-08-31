"""chat 主线:一次 chat 问答的流式应答(纯文本 chunk 序列,协议同 deepwiki-open research_chat)。

入口 chat_stream 恒走单一生成器管道(_chat);本主线专用 helper:历史转写、
continuation 回退、深研究模板常量(前端匹配契约见本文件常量注释)。
跨功能通用 helper 在 utils,经本模块属性调用(utils.xxx 调用时取 ——
monkeypatch 活性)。
"""

from __future__ import annotations

from .. import envs  # 模块对象绑定:属性一律调用时取(patch/强刷活性)
from ..utils import Repo, _estimate_tokens
from . import utils
from .utils import log

# ---------------------------------------------------------------------------
# 深研究模板(折叠版:一次回答完成整轮研究。标题字符串必须逐字匹配前端
# Ask.tsx 的提取/完成判定正则
# (Research Plan / Research Update {n} / Final Conclusion);禁 "## Next Steps"
# (它会截断 plan 提取并影响完成判定)与 "## Conclusion"/"## Summary"
# (完成判定的次选触发词)。)
# ---------------------------------------------------------------------------

_DEEP_RESEARCH_ONE_SHOT_PROMPT = """<role>
You are an expert code analyst examining the {repo_type} repository: {repo_url} ({repo_name}).
You are conducting a COMPLETE single-run Deep Research of the latest user query, aimed at a definitive answer rather than an intermediate round.
IMPORTANT:You MUST respond in {language_name} language.
</role>

<guidelines>
- This is the ONLY response of the research process: finish the entire investigation in this one answer.
- USE YOUR TOOLS (Read / Grep / Glob) to inspect the repository code for evidence before writing; repeated tool rounds are expected.
- Your answer MUST contain EXACTLY these sections, in this order:
  1. Begin with "## Research Plan" - the approach and initial findings for the investigation
  2. Then one or more progress sections "## Research Update 1", "## Research Update 2", ... with deeper findings from your tool exploration
  3. End with "## Final Conclusion" - a complete, definitive answer to the original question, citing specific files and line numbers
- NEVER stop after the Plan section: keep investigating until you can write a Final Conclusion.
- NEVER write "## Next Steps"; NEVER respond with "Continue the research".
- Do NOT use "## Conclusion" or "## Summary" as section headings (only "## Final Conclusion").
- Focus EXCLUSIVELY on the user's query; cite specific files and code sections when relevant.
</guidelines>"""  # noqa: E501 - prompt 原文移植,单行语义不拆

# ---------------------------------------------------------------------------
# 对话历史转写(自然转写)
# ---------------------------------------------------------------------------


def _render_natural_history(messages: list[dict]) -> str:
    """对话历史自然转写(无 <turn>/<conversation_history> 伪标签);输入过大时省略历史。"""
    history_parts: list[str] = []
    if len(messages) > 1:
        last = messages[-1]
        if _estimate_tokens(last.get("content", "")) > envs.CHAT_TOKEN_LIMIT_ESTIMATE:
            log(f"输入过大(估算 {_estimate_tokens(last.get('content', ''))} tokens),省略对话历史")
        else:
            for i in range(0, len(messages) - 1, 2):
                user, assistant = messages[i], messages[i + 1]
                if user.get("role") == "user" and assistant.get("role") == "assistant":
                    history_parts.append(
                        f"User: {user.get('content', '')}\nAssistant: {assistant.get('content', '')}",
                    )
    if history_parts:
        return "Previous conversation:\n" + "\n\n".join(history_parts) + "\n\n"
    return ""


def _resolve_chat_continuation(last: dict, messages: list[dict]) -> None:
    """continuation 回退(移植 research.py):末条含 continue+research 时换回首个用户消息(就地改 last['content'])。"""
    if "continue" in last.get("content", "").lower() and "research" in last.get("content", "").lower():
        for msg in messages:
            if msg.get("role") == "user" and "continue" not in msg.get("content", "").lower():
                last["content"] = msg["content"].strip()
                break


# ---------------------------------------------------------------------------
# 实现(单一生成器管道:一次回答完成;无协议级轮转)
# ---------------------------------------------------------------------------


async def _chat(
    *, generator: str | None = None, generator_config: dict | None = None, repo: Repo, messages: list[dict],
    language: str = "en", research_iteration: int = 1,
):
    """一次 chat 问答的流式应答(纯文本 chunk 序列,前后端协议同原 research_chat);

    现代模式:一次提问,generator 内部多轮工具调用完成,不做协议级轮转。
    """
    if not messages:
        raise ValueError("No messages provided")
    last = messages[-1]
    if last.get("role") != "user":
        raise ValueError("Last message must be from the user")

    # 注:未索引的前置校验属端点守卫,已上移到应用层,生成器内不再重复
    fmt = utils.prompt_fmt(repo, language=language)
    is_deep = last.get("mode") == "deep_research"
    if is_deep:
        # 折叠:一次回答完成整轮研究(生成器内部多轮工具调用);continuation 回退
        # 保留作偏差兜底(首轮缺 Final Conclusion 时前端续跑轮为同一问题重跑)
        _resolve_chat_continuation(last, messages)
        system = _DEEP_RESEARCH_ONE_SHOT_PROMPT.format(**fmt)
    else:
        system = utils._SIMPLE_CHAT_SYSTEM_PROMPT.format(**fmt)

    # 对话历史自然转写(无 <turn> 伪标签);输入过大时省略历史。引擎不传
    # context 类"假日志"事件(监控事件由适配器内 EventRecorder 发布)。
    # 工具指引(tool_note)由上层经 generator_config 注入 —— 引擎零工具假设。
    adapter = utils.adapter(generator, generator_config=generator_config, system_prompt=system, repo=repo)
    history = _render_natural_history(messages)

    prompt = (
        history + (generator_config or {}).get("tool_note", "")
        + f"<query>\n{last.get('content', '')}\n</query>\n\nAssistant: "
    )
    try:
        async with adapter.session(session_name=f"chat:{repo.name}", run_id=f"chat:{repo.name}"):
            async for chunk in adapter.stream(prompt):
                yield chunk
    except Exception as e:  # 执行期失败降级为可读错误文本(同原 stream_and_fallback 语义)
        err = utils.failure(e)  # RequestFailedError 先转「generator 执行失败」再降级(同原包装时序)
        log(f"chat 生成器错误: {err}")
        yield f"\n\n(抱歉,本次请求处理失败: {err})"


# ---------------------------------------------------------------------------
# 服务入口(端点层从 app.py 直呼)
# ---------------------------------------------------------------------------


async def chat_stream(
    *, generator: str | None = None, generator_config: dict | None = None, repo: Repo, messages: list[dict],
    language: str = "en", research_iteration: int = 1,
):
    """一次 chat 问答的流式应答(纯文本 chunk 序列,前后端协议同原 research_chat);单一生成器管道。"""
    async for chunk in _chat(
        generator=generator, generator_config=generator_config, repo=repo, messages=messages,
        language=language, research_iteration=research_iteration,
    ):
        yield chunk
