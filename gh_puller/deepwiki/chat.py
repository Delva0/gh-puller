"""chat 主线:一次 chat 问答的流式应答(纯文本 chunk 序列,前后端协议同原
research_chat)。

入口 chat_stream 按 generator 内联分派(cc/dsh/codex → _agent_chat 现代
agent 模式:一次提问 agent 内部多轮工具调用;llm → _llm_chat 原式单次补全,
分派规则与 wiki._wiki_pipeline 同);本主线专用 helper:历史转写
(_render_natural_history / _build_turn_history)、continuation 回退
(_resolve_chat_continuation)、深研究模板常量(标题字符串必须逐字匹配前端
Ask.tsx 的提取/完成判定正则,见常量注释)。跨功能通用 helper(四路装配/
检索簇/research 协议/提示词共性常量)在 utils,经本模块属性调用
(utils.xxx 调用时取 —— monkeypatch 活性)。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from .. import envs, graphify  # 模块对象绑定:属性一律调用时取(patch/强刷活性)
from ..utils import Repo, _estimate_tokens
from . import utils
from .utils import log

# ---------------------------------------------------------------------------
# 深研究模板(agent 路 = 折叠版:一次回答完成整轮研究;llm 路 = 原版 5 轮协议,
# 前端迭代驱动。标题字符串必须逐字匹配前端 Ask.tsx 的提取/完成判定正则
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
- USE YOUR TOOLS (Read / Grep / Glob / graphify_query) to inspect the repository code for evidence before writing; repeated tool rounds are expected.
- Your answer MUST contain EXACTLY these sections, in this order:
  1. Begin with "## Research Plan" - the approach and initial findings for the investigation
  2. Then one or more progress sections "## Research Update 1", "## Research Update 2", ... with deeper findings from your tool exploration
  3. End with "## Final Conclusion" - a complete, definitive answer to the original question, citing specific files and line numbers
- NEVER stop after the Plan section: keep investigating until you can write a Final Conclusion.
- NEVER write "## Next Steps"; NEVER respond with "Continue the research".
- Do NOT use "## Conclusion" or "## Summary" as section headings (only "## Final Conclusion").
- Focus EXCLUSIVELY on the user's query; cite specific files and code sections when relevant.
</guidelines>"""

_DEEP_RESEARCH_FIRST_ITERATION_PROMPT = """<role>
You are an expert code analyst examining the {repo_type} repository: {repo_url} ({repo_name}).
You are conducting a multi-turn Deep Research process to thoroughly investigate the specific topic in the user's query.
Your goal is to provide detailed, focused information EXCLUSIVELY about this topic.
IMPORTANT:You MUST respond in {language_name} language.
</role>

<guidelines>
- This is the first iteration of a multi-turn research process focused EXCLUSIVELY on the user's query
- Start your response with "## Research Plan"
- Outline your approach to investigating this specific topic
- If the topic is about a specific file or feature (like "Dockerfile"), focus ONLY on that file or feature
- Clearly state the specific topic you're researching to maintain focus throughout all iterations
- Identify the key aspects you'll need to research
- Provide initial findings based on the information available
- End with "## Next Steps" indicating what you'll investigate in the next iteration
- Do NOT provide a final conclusion yet - this is just the beginning of the research
- Do NOT include general repository information unless directly relevant to the query
- Focus EXCLUSIVELY on the specific topic being researched - do not drift to related topics
- Your research MUST directly address the original question
- NEVER respond with just "Continue the research" as an answer - always provide substantive research findings
- Remember that this topic will be maintained across all research iterations
</guidelines>

<style>
- Be concise but thorough
- Use markdown formatting to improve readability
- Cite specific files and code sections when relevant
</style>"""

_DEEP_RESEARCH_INTERMEDIATE_ITERATION_PROMPT = """<role>
You are an expert code analyst examining the {repo_type} repository: {repo_url} ({repo_name}).
You are currently in iteration {research_iteration} of a Deep Research process focused EXCLUSIVELY on the latest user query.
Your goal is to build upon previous research iterations and go deeper into this specific topic without deviating from it.
IMPORTANT:You MUST respond in {language_name} language.
</role>

<guidelines>
- CAREFULLY review the conversation history to understand what has been researched so far
- Your response MUST build on previous research iterations - do not repeat information already covered
- Identify gaps or areas that need further exploration related to this specific topic
- Focus on one specific aspect that needs deeper investigation in this iteration
- Start your response with "## Research Update {research_iteration}"
- Clearly explain what you're investigating in this iteration
- Provide new insights that weren't covered in previous iterations
- If this is iteration 3, prepare for a final conclusion in the next iteration
- Do NOT include general repository information unless directly relevant to the query
- Focus EXCLUSIVELY on the specific topic being researched - do not drift to related topics
- If the topic is about a specific file or feature (like "Dockerfile"), focus ONLY on that file or feature
- NEVER respond with just "Continue the research" as an answer - always provide substantive research findings
- Your research MUST directly address the original question
- Maintain continuity with previous research iterations - this is a continuous investigation
</guidelines>

<style>
- Be concise but thorough
- Focus on providing new information, not repeating what's already been covered
- Use markdown formatting to improve readability
- Cite specific files and code sections when relevant
</style>"""

_DEEP_RESEARCH_FINAL_ITERATION_PROMPT = """<role>
You are an expert code analyst examining the {repo_type} repository: {repo_url} ({repo_name}).
You are in the final iteration of a Deep Research process focused EXCLUSIVELY on the latest user query.
Your goal is to synthesize all previous findings and provide a comprehensive conclusion that directly addresses this specific topic and ONLY this topic.
IMPORTANT:You MUST respond in {language_name} language.
</role>

<guidelines>
- This is the final iteration of the research process
- CAREFULLY review the entire conversation history to understand all previous findings
- Synthesize ALL findings from previous iterations into a comprehensive conclusion
- Start with "## Final Conclusion"
- Your conclusion MUST directly address the original question
- Stay STRICTLY focused on the specific topic - do not drift to related topics
- Include specific code references and implementation details related to the topic
- Highlight the most important discoveries and insights about this specific functionality
- Provide a complete and definitive answer to the original question
- Do NOT include general repository information unless directly relevant to the query
- Focus exclusively on the specific topic being researched
- NEVER respond with "Continue the research" as an answer - always provide a complete answer
- If the topic is about a specific file or feature (like "Dockerfile"), focus ONLY on that file or feature
- Ensure your conclusion builds on and references key findings from previous iterations
</guidelines>

<style>
- Be concise but thorough
- Use markdown formatting to improve readability
- Cite specific files and code sections when relevant
- Structure your response with clear headings
- End with actionable insights or recommendations when appropriate
</style>"""


# ---------------------------------------------------------------------------
# llm 路单次补全协议(原版 research_chat 语义:检索上下文注入 + token 超限简化
# 重试;名称即 chat,wiki(llm 路 structure/page)与 codemap(llm 路)复用)
# ---------------------------------------------------------------------------


def build_service_prompt(
    system_prompt: str, query: str, *, conversation_history: str = "", context: str = "",
    simplify: bool = False,
) -> str:
    """原版 api/chat/_prompts.py prompt_builder 逐字移植(单条 user 消息)。

    结构:/no_think + 系统提示词 → <conversation_history> → 检索上下文
    (<START_OF_CONTEXT> 包裹;为空注"无检索增强"note)或简化 note(输入超限时)
    → <query>…</query> + Assistant:。"""
    prompt = f"/no_think {system_prompt}\n\n"
    if conversation_history:
        prompt += f"<conversation_history>\n{conversation_history}</conversation_history>\n\n"
    if not simplify:
        if context.strip():
            prompt += f"<START_OF_CONTEXT>\n{context}\n<END_OF_CONTEXT>\n\n"
        else:
            prompt += "<note>Answering without retrieval augmentation.</note>\n\n"
    else:
        prompt += "<note>Answering without retrieval augmentation due to input size constraints.</note>\n\n"
    return prompt + f"<query>\n{query}\n</query>\n\nAssistant: "


def _is_token_limit_error(exc: Exception) -> bool:
    """原版 api/chat/__init__.py is_token_limit_error 的判断子串(大小写不敏感)。"""
    error_message = str(exc).lower()
    return any(k in error_message for k in (
        "maximum context length", "token limit", "too many tokens",
    ))


async def llm_research_chat(
    system: str, query: str, *, generator: str | None, generator_config: dict | None,
    repo: Repo, session_name: str, run_id: str,
    conversation_history: str = "",
):
    """原版 research_chat 语义(流式整口):

    - 最后一问估算超 CHAT_TOKEN_LIMIT_ESTIMATE(原 MAX_INPUT_TOKENS=7500)→ 跳过检索;
    - 检索上下文 = 图谱子图→真实代码窗(原 RAG 的适配点),经 prompt_builder 同式
      拼装(<START_OF_CONTEXT> 包裹;为空注"无检索增强"note);图谱失败/超预算
      直接 raise(检索失败不继续);
    - stream_and_fallback:token 超限 → 简化提示词重试(去掉检索上下文)→ 致歉;
      其余异常 → "Error with openai API: {e}" 文本进流。
    """
    context_text = ""
    if _estimate_tokens(query) > envs.CHAT_TOKEN_LIMIT_ESTIMATE:
        log(f"输入过大(估算 {_estimate_tokens(query)} tokens),跳过检索上下文(原版 MAX_INPUT_TOKENS 语义)")
    else:
        ctx = await graphify_context(repo, query)  # 图谱失败/超预算 → raise
        context_text = format_subgraph_context(ctx["blocks"])

    prompt = build_service_prompt(
        system, query, conversation_history=conversation_history, context=context_text
    )
    simplified = build_service_prompt(
        system, query, conversation_history=conversation_history, simplify=True
    )
    try:
        async for chunk in utils.llm_stream(
            prompt, generator=generator, generator_config=generator_config,
            session_name=session_name, run_id=run_id,
        ):
            yield chunk
    except Exception as e:
        if _is_token_limit_error(e):
            log("token 超限,简化为无检索上下文重试")
            try:
                async for chunk in utils.llm_stream(
                    simplified, generator=generator, generator_config=generator_config,
                    session_name=session_name, run_id=run_id,
                ):
                    yield chunk
            except Exception as e2:  # noqa: BLE001 - 简化重试失败 → 致歉文本(原版同式)
                log(f"简化重试失败: {e2}")
                yield (
                    "\nI apologize, but your request is too large for me to process. "
                    "Please try a shorter query or break it into smaller parts."
                )
        else:
            log(f"chat llm 错误: {e}")
            yield f"\nError with openai API: {e}"


# ---------------------------------------------------------------------------
# 检索工具簇(graphify 子图 → 真实代码行窗;llm 路专用:graphify_context 由
# llm_research_chat 与 codemap(llm 路)调用;失败即 raise,不许"带病继续")
# ---------------------------------------------------------------------------


# 纯 LLM 路径单文件内联截断(字符)
_FILE_INLINE_CAP = 8000


def subgraph_hits(answer: str) -> dict[str, list[int]]:
    """解析 graphify.query 的 answer 标注 → {file: [行号...]}。

    NODE 行形如 `NODE <label> [src=<file> loc=L<line> community=<cid>]`,
    EDGE 行以 ` at=<file>:L<line>` 结尾;容忍 [i] TRUNCATED / over-budget
    前缀行与 ... (truncated 尾行;非匹配行忽略,loc 缺失/为空的文件跳过)。
    """
    hits: dict[str, list[int]] = {}
    for raw in answer.splitlines():
        m = re.match(r"^NODE\s+.+?\s+\[([^\]]*)\]$", raw)
        if m:
            src = re.search(r"\bsrc=(\S+)\b", m.group(1))
            loc = re.search(r"\bloc=(L?\d+)\b", m.group(1))
            if src and loc:
                hits.setdefault(src.group(1), []).append(int(loc.group(1).lstrip("L")))
            continue
        m = re.search(r"\bat=([^:\s]+):(L\d+)$", raw)
        if m:
            hits.setdefault(m.group(1), []).append(int(m.group(2)[1:]))
    return {p: sorted(set(lines)) for p, lines in hits.items()}


def subgraph_src_blocks(
    save_path: str,
    hits: dict[str, list[int]],
    *,
    radius: int = 12,
    per_file_cap: int = _FILE_INLINE_CAP,
    budget_chars: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """命中行窗提取:{file: [行号]} → [{path,text,start_line,end_line}...], (blocks, degraded)。

    每文件:命中行 ±radius 展开、相邻窗(间距 ≤ 2*radius)合并、夹到文件行界;
    单文件累计字符封顶 per_file_cap(溢出窗截断,后续窗丢弃);
    全量累计超 budget_chars(缺省 CHAT_TOKEN_LIMIT_ESTIMATE*4)即整组降级
    (返回 ([], True));调用方(graphify_context)据此 raise。
    OSError/解码失败的文件跳过(其余文件正常)。
    """
    budget = budget_chars if budget_chars is not None else envs.CHAT_TOKEN_LIMIT_ESTIMATE * 4
    blocks: list[dict[str, Any]] = []
    total = 0
    for path in sorted(hits):
        try:
            full_text = Path(save_path, path).read_text(encoding="utf-8")
        except OSError:
            continue
        lines = full_text.splitlines()
        n_lines = len(lines)
        # 命中行 → 合并后的窗区间
        windows: list[tuple[int, int]] = []
        for line in sorted(set(hits[path])):
            start = max(1, line - radius)
            end = min(n_lines, line + radius)
            if windows and start <= windows[-1][1] + 1:
                windows[-1] = (windows[-1][0], max(windows[-1][1], end))
            else:
                windows.append((start, end))
        file_chars = 0
        for start, end in windows:
            seg_text = "\n".join(lines[start - 1:end])
            if not seg_text.strip():
                continue
            remain = per_file_cap - file_chars
            if len(seg_text) > remain:
                if remain <= 0:
                    break
                seg_text = seg_text[:remain]
                end = start + seg_text.count("\n")
            file_chars += len(seg_text)
            if total + len(seg_text) > budget:  # 先按单文件截断,再整体预算判断
                return [], True
            blocks.append({"path": path, "text": seg_text, "start_line": start, "end_line": end})
            total += len(seg_text)
    return blocks, False


def format_subgraph_context(blocks: list[dict[str, Any]]) -> str:
    """代码窗 → 原版 _format_context 同式文本(chat/codemap 共用)。

    按文件分组:每组 `## File Path: {path}` 头 + 每窗 `[lines A-B]\n<code>`
    (窗间空行);文件段以原版同式(`"\n\n" + "-"*10 + "\n\n".join(parts)`)
    联结。空输入 → ""(上层 prompt_builder 会注入"无检索增强"note)。"""
    if not blocks:
        return ""  # 原版由调用方兜底(无文档时不调用);空输入保持 "" 供 prompt_builder 注 note
    groups: dict[str, list[dict[str, Any]]] = {}
    for b in blocks:
        groups.setdefault(b["path"], []).append(b)
    context_parts: list[str] = []
    for path, blks in groups.items():
        chunk_texts = [f"[lines {b['start_line']}-{b['end_line']}]\n{b['text']}" for b in blks]
        context_parts.append(f"## File Path: {path}\n\n" + "\n\n".join(chunk_texts))
    return "\n\n" + ("-" * 10) + "\n\n".join(context_parts)


async def graphify_context(repo: Repo, question: str) -> dict[str, Any]:
    """graphify.query + 子图 → 真实代码行窗。

    检索是正确作答的前提:图谱查询失败与超预算降级均直接 raise(不再以
    "无检索增强"降级继续)。
    """
    try:
        result = await asyncio.to_thread(
            graphify.query, question, graph_path=str(utils.graph_path(repo))
        )
        answer = result.get("answer") or ""
    except Exception as exc:
        raise RuntimeError(f"代码图谱不可用: {type(exc).__name__}: {exc}") from exc
    hits = subgraph_hits(answer)
    if not hits:
        return {"hits": {}, "blocks": []}
    blocks, degraded = subgraph_src_blocks(repo.save_path, hits)
    if degraded:
        raise RuntimeError("检索上下文超出预算")
    return {"hits": hits, "blocks": blocks}


# ---------------------------------------------------------------------------
# 对话历史转写(agent 路自然转写;llm 路原版 <turn> 成对序列)
# ---------------------------------------------------------------------------


def _render_natural_history(messages: list[dict]) -> str:
    """agent 路对话历史:自然转写(无 <turn>/<conversation_history> 伪标签);输入过大时省略历史。"""
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
                        f"User: {user.get('content', '')}\nAssistant: {assistant.get('content', '')}"
                    )
    if history_parts:
        return "Previous conversation:\n" + "\n\n".join(history_parts) + "\n\n"
    return ""


def _build_turn_history(messages: list[dict]) -> str:
    """LLM 路对话历史:恒拼 <turn> 成对序列(原版无裁剪;输入过大仅跳过检索上下文)。"""
    turns = ""
    for i in range(0, len(messages) - 1, 2):
        user, assistant = messages[i], messages[i + 1]
        if user.get("role") == "user" and assistant.get("role") == "assistant":
            turns += (
                f"<turn>\n<user>{user.get('content', '')}</user>\n"
                f"<assistant>{assistant.get('content', '')}</assistant>\n</turn>\n"
            )
    return f"<conversation_history>\n{turns}</conversation_history>\n\n" if turns else ""


def _resolve_chat_continuation(last: dict, messages: list[dict]) -> None:
    """continuation 回退(移植 research.py):末条含 continue+research 时换回首个用户消息(就地改 last['content'])。"""
    if "continue" in last.get("content", "").lower() and "research" in last.get("content", "").lower():
        for msg in messages:
            if msg.get("role") == "user" and "continue" not in msg.get("content", "").lower():
                last["content"] = msg["content"].strip()
                break


# ---------------------------------------------------------------------------
# 双路实现(agent 路折叠一次回答;llm 路原式 research_chat 等价)
# ---------------------------------------------------------------------------


async def _agent_chat(
    *, generator: str | None = None, generator_config: dict | None = None, repo: Repo, messages: list[dict],
    language: str = "en", research_iteration: int = 1,
):
    """一次 chat 问答的流式应答(纯文本 chunk 序列,前后端协议同原 research_chat);
    现代 agent 模式:一次提问,agent 内部多轮工具调用完成,不做协议级轮转。"""
    if not messages:
        raise ValueError("No messages provided")
    last = messages[-1]
    if last.get("role") != "user":
        raise ValueError("Last message must be from the user")

    # 注:未索引的前置校验属端点守卫,已上移到应用层,生成器内不再重复
    fmt = utils.prompt_fmt(repo, language=language)
    is_deep = last.get("mode") == "deep_research"
    if is_deep:
        # 折叠:一次回答完成整轮研究(agent 内部多轮工具调用);continuation 回退
        # 保留作偏差兜底(首轮缺 Final Conclusion 时前端续跑轮为同一问题重跑)
        _resolve_chat_continuation(last, messages)
        system = _DEEP_RESEARCH_ONE_SHOT_PROMPT.format(**fmt)
    else:
        system = utils._SIMPLE_CHAT_SYSTEM_PROMPT.format(**fmt)

    # 对话历史自然转写(无 <turn> 伪标签);输入过大时省略历史。引擎不传
    # context 类"假日志"事件(监控事件由适配器内 EventRecorder 发布)
    adapter = utils.adapter(generator, generator_config=generator_config, system_prompt=system, repo=repo)
    history = _render_natural_history(messages)

    prompt = history + utils.agent_note(adapter.generator) + f"<query>\n{last.get('content', '')}\n</query>\n\nAssistant: "
    try:
        async for chunk in adapter.stream(
            prompt, session_name=f"chat:{repo.name}", run_id=f"chat:{repo.name}",
        ):
            yield chunk
    except Exception as e:  # 执行期失败降级为可读错误文本(同原 stream_and_fallback 语义)
        err = utils.failure(e)  # RequestFailedError 先转「agent 执行失败」再降级(同原包装时序)
        log(f"chat agent 错误: {err}")
        yield f"\n\n(抱歉,本次请求处理失败: {err})"


async def _llm_chat(
    *, generator: str | None = None, generator_config: dict | None = None, repo: Repo, messages: list[dict],
    language: str = "en", research_iteration: int = 1,
):
    """llm 路 chat:原版 research_chat 等价(模式/迭代选模板、原版历史拼接、
    输入过大跳过检索、prompt_builder 拼装、token 超限简化重试)。"""
    if not messages:
        raise ValueError("No messages provided")
    last = messages[-1]
    if last.get("role") != "user":
        raise ValueError("Last message must be from the user")

    fmt = utils.prompt_fmt(repo, language=language)
    is_deep = last.get("mode") == "deep_research"
    if is_deep:
        # 原版复刻:5 轮迭代由前端驱动,后端只按迭代号选模板 + 拼历史
        _resolve_chat_continuation(last, messages)
        if research_iteration == 1:
            system = _DEEP_RESEARCH_FIRST_ITERATION_PROMPT.format(**fmt)
        elif research_iteration >= 5:
            system = _DEEP_RESEARCH_FINAL_ITERATION_PROMPT.format(**fmt)
        else:
            system = _DEEP_RESEARCH_INTERMEDIATE_ITERATION_PROMPT.format(
                **fmt, research_iteration=research_iteration
            )
    else:
        system = utils._SIMPLE_CHAT_SYSTEM_PROMPT.format(**fmt)

    history = _build_turn_history(messages)
    async for chunk in llm_research_chat(
        system, last.get("content", ""), generator=generator, generator_config=generator_config, repo=repo,
        session_name=f"chat:{repo.name}", run_id=f"chat:{repo.name}",
        conversation_history=history,
    ):
        yield chunk


# ---------------------------------------------------------------------------
# 服务入口(端点层从 app.py 直呼;分派规则与 wiki._wiki_pipeline 同)
# ---------------------------------------------------------------------------


async def chat_stream(
    *, generator: str | None = None, generator_config: dict | None = None, repo: Repo, messages: list[dict],
    language: str = "en", research_iteration: int = 1,
):
    """一次 chat 问答的流式应答(纯文本 chunk 序列,前后端协议同原 research_chat);按 generator 分派双路。"""
    gen = utils.resolve_generator(generator, generator_config)[0]
    impl = _agent_chat if gen in ("cc", "dsh", "codex") else _llm_chat
    async for chunk in impl(
        generator=generator, generator_config=generator_config, repo=repo, messages=messages,
        language=language, research_iteration=research_iteration,
    ):
        yield chunk
