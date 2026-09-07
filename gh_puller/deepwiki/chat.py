"""Stream DeepWiki chat answers through a configured Agent adapter.

This module owns conversation rendering and the deep-research response contract.
Shared generator configuration remains in :mod:`.utils`.
"""

from __future__ import annotations

from .. import envs  # Read attributes at call time so reloads and patches stay visible.
from ..utils import Repo, _estimate_tokens
from . import utils
from .utils import log

# Ask.tsx parses these headings verbatim to track and complete a research response.
# Additional conclusion or next-step headings would trigger its fallback matchers.

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
</guidelines>"""  # noqa: E501 - Preserve the upstream prompt as a single literal.

# --- Conversation rendering ---


def _render_natural_history(messages: list[dict]) -> str:
    """Render prior turns naturally, omitting history when the latest input is too large."""
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
    """Replace a research-continuation request with the original user question."""
    if "continue" in last.get("content", "").lower() and "research" in last.get("content", "").lower():
        for msg in messages:
            if msg.get("role") == "user" and "continue" not in msg.get("content", "").lower():
                last["content"] = msg["content"].strip()
                break


# --- Generation ---


async def _chat(
    *, generator: str | None = None, generator_config: dict | None = None, repo: Repo, messages: list[dict],
    language: str = "en", research_iteration: int = 1,
):
    """Stream one complete answer without protocol-level research turns."""
    if not messages:
        raise ValueError("No messages provided")
    last = messages[-1]
    if last.get("role") != "user":
        raise ValueError("Last message must be from the user")

    fmt = utils.prompt_fmt(repo, language=language)
    is_deep = last.get("mode") == "deep_research"
    if is_deep:
        # Reuse the original question when the frontend retries an incomplete response.
        _resolve_chat_continuation(last, messages)
        system = _DEEP_RESEARCH_ONE_SHOT_PROMPT.format(**fmt)
    else:
        system = utils._SIMPLE_CHAT_SYSTEM_PROMPT.format(**fmt)

    adapter = utils.adapt_generator(generator, generator_config=generator_config, system_prompt=system, repo=repo)
    history = _render_natural_history(messages)

    prompt = (
        history + f"{last.get('content', '')}"
    )
    try:
        async with adapter.session(session_name=f"chat:{repo.name}", run_id=f"chat:{repo.name}"):
            async for chunk in adapter.stream(prompt):
                yield chunk
    except Exception as e:  # The streaming protocol represents runtime failures as text.
        err = utils.failure(e)
        log(f"chat 生成器错误: {err}")
        yield f"\n\n(抱歉,本次请求处理失败: {err})"


async def chat_stream(
    *, generator: str | None = None, generator_config: dict | None = None, repo: Repo, messages: list[dict],
    language: str = "en", research_iteration: int = 1,
):
    """Stream one DeepWiki-compatible chat answer.

    Args:
        generator: Registered Agent adapter name, or the default when omitted.
        generator_config: Adapter-specific construction options.
        repo: Repository available to the Agent.
        messages: Ordered conversation messages ending with a user message.
        language: Response language code.
        research_iteration: Compatibility field retained for the endpoint contract.

    Yields:
        Plain-text response chunks.
    """
    async for chunk in _chat(
        generator=generator, generator_config=generator_config, repo=repo, messages=messages,
        language=language, research_iteration=research_iteration,
    ):
        yield chunk
