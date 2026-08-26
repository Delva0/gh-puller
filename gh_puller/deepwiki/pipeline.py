"""双路包装类:单路生成协议的统一入口(WikiPipeline 基类 + Agent/Llm 两路)。

职责边界:
- 本模块 = 生成协议包装层:提示词组装、对话历史转写、检索上下文注入、agent 交付件
  路径、llm 单次补全生成链收进各 pipeline 类(self._xxx());底层支撑(共享提示词段、
  XML 解析/引用后处理、图索引、任务状态机、agent SDK / llm 端点通道)保持模块级,
  留在 deepwiki 主干(gh_puller/deepwiki/__init__.py)。
- 与主干的关系(服务端):主干持有 models/状态机/缓存/日志与通道,以模块属性方式
  服务本模块;本模块只消费不持有 —— 一律经 `deepwiki.` 前缀在**调用时**取
  (deepwiki._agent_write_file / deepwiki.generate_stream / deepwiki._log /
  deepwiki.envs.X / deepwiki.WikiTask...),顶层不做主干属性快照或绑定
  (测试 monkeypatch 与运行期配置切换均依赖此)。
- 循环引用契约:`from gh_puller import deepwiki` 是唯一跨可见度顶层 import
  (仅绑定模块对象,属性全在调用时取);可顶层直接 import 的白名单:utils 纯工具+Repo、
  envs、graphify —— 与 deepwiki.envs / deepwiki.graphify 是同一模块对象,经
  deepwiki.X 的 monkeypatch 同样生效;模型/请求类仅 TYPE_CHECKING 注解
  (配合 `from __future__ import annotations`)。

后续拆分备注(2026-08,源:单文件期模块包化而来,git log --follow 可溯):提示词
常量可独立 prompts.py;generate_stream/generate_result 抽象口可拆 dispatcher 以中断
与主干耦合;LlmWikiPipeline._subgraph_* 检索工具簇可独立 retrieval.py;_wiki_pipeline /
_service_pipeline 未来可合并单一分派函数。
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, TYPE_CHECKING, Mapping

from gh_puller import deepwiki  # 仅绑定模块对象;顶层不取主干属性(见循环引用契约)
from .. import envs, graphify  # 与 deepwiki.envs / deepwiki.graphify 同一模块对象
from ..utils import (
    Repo,
    _estimate_tokens,
    _event,
    _extract_json,
    _find_readme_path,
    _phase,
    _sanitize_path_seg,
    _strip_markdown_fences,
)

# ---------------------------------------------------------------------------
# 提示词(原文移植自 deepwiki-open api/prompts.py 与 api/services/wiki/prompts.py)
# ---------------------------------------------------------------------------

_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "zh": "Mandarin Chinese (中文)",
}

_LANGUAGE_NAMES_RAW = {
    "en": "English",
    "ja": "Japanese (日本語)",
    "zh": "Mandarin Chinese (中文)",
    "zh-tw": "Traditional Chinese (繁體中文)",
    "es": "Spanish (Español)",
    "kr": "Korean (한국어)",
    "vi": "Vietnamese (Tiếng Việt)",
    "pt-br": "Brazilian Portuguese (Português Brasileiro)",
    "fr": "Français (French)",
    "ru": "Русский (Russian)",
}


def _language_name(language: str) -> str:
    """语言名(缺省 English;未知语言也回退 English,与原 lang.json 语义一致)。"""
    return _LANGUAGE_NAMES_RAW.get(language, "English")


_SIMPLE_CHAT_SYSTEM_PROMPT = """<role>
You are an expert code analyst examining the {repo_type} repository: {repo_url} ({repo_name}).
You provide direct, concise, and accurate information about code repositories.
You NEVER start responses with markdown headers or code fences.
IMPORTANT:You MUST respond in {language_name} language.
</role>

<guidelines>
- Answer the user's question directly without ANY preamble or filler phrases
- DO NOT include any rationale, explanation, or extra comments.
- DO NOT start with preambles like "Okay, here's a breakdown" or "Here's an explanation"
- DO NOT start with markdown headers like "## Analysis of..." or any file path references
- DO NOT start with ```markdown code fences
- DO NOT end your response with ``` closing fences
- DO NOT start by repeating or acknowledging the question
- JUST START with the direct answer to the question

<example_of_what_not_to_do>
```markdown
## Analysis of `adalflow/adalflow/datasets/gsm8k.py`

This file contains...
```
</example_of_what_not_to_do>

- Format your response with proper markdown including headings, lists, and code blocks WITHIN your answer
- For code analysis, organize your response with clear sections
- Think step by step and structure your answer logically
- Start with the most relevant information that directly addresses the user's query
- Be precise and technical when discussing code
- Your response language should be in the same language as the user's query
</guidelines>

<style>
- Use concise, direct language
- Prioritize accuracy over verbosity
- When showing code, include line numbers and file paths when relevant
- Use markdown formatting to improve readability
</style>"""



# codemap 生成 - 阶段 1:分析代码并产出 codemap 骨架(带 JSON 输出格式与引用接地规则)
_CODEMAP_SKELETON_PROMPT = """<role>
You are an expert code analyst building a "codemap" for the {repo_type} repository: {repo_url} ({repo_name}).
A codemap is a structured, step-by-step guide that answers a usage/how-to question, where every
step is grounded in REAL source code from the repository.
IMPORTANT: All human-readable text (titles, labels, summary) MUST be written in {language_name} language.
</role>

<task>
Using ONLY the code provided in <START_OF_CONTEXT>...<END_OF_CONTEXT>, produce a codemap that
answers the user's question. Organise the answer as numbered sections (1, 2, 3...), each containing
ordered sub-steps (1a, 1b, 1c...). Every sub-step must reference the real source it comes from.
</task>

<grounding_rules>
- You may ONLY cite files that appear in the context as a "## File Path: <path>" header. Never invent a path.
- Each chunk in the context is prefixed with a "[lines A-B]" marker. Use those numbers to fill start_line/end_line.
- The "snippet" field MUST be copied VERBATIM from the context (an exact substring). Do not paraphrase it.
- The "code" field is a short example snippet illustrating the step; keep it minimal and runnable-looking.
- If the context does not contain enough to answer, produce fewer sections rather than fabricating.
</grounding_rules>

<output_format>
Output ONLY a single JSON object, no markdown fences, no commentary before or after. Shape:
{{
  "title": "<short title of the guide>",
  "summary": "<1-3 sentence intro; you may reference steps like [1a] [2a]>",
  "sections": [
    {{
      "id": "1",
      "title": "<section title>",
      "guide": "",
      "diagram": "",
      "steps": [
        {{
          "id": "1a",
          "label": "<short step title>",
          "code": "<example code>",
          "citation": {{
            "file_path": "<path from a ## File Path header>",
            "start_line": <int>,
            "end_line": <int>,
            "snippet": "<verbatim substring from that file's context>"
          }}
        }}
      ]
    }}
  ]
}}
Leave every "guide" and "diagram" field as an empty string "" in this phase.
</output_format>"""

# codemap 生成 - 阶段 2:填充散文指南与 mermaid 图
_CODEMAP_ENRICH_PROMPT = """<role>
You are enriching an existing codemap skeleton for the {repo_type} repository: {repo_url} ({repo_name}).
IMPORTANT: All prose MUST be written in {language_name} language.
</role>

<task>
You are given a codemap JSON skeleton (in <SKELETON>) and the original source context.
For EACH section, write:
- "guide": a concise prose explanation (2-4 sentences) of what the section accomplishes.
- "diagram": a valid Mermaid diagram source string (e.g. a "graph LR" or "flowchart TD") that
  illustrates the flow of that section. Use only Mermaid syntax; do NOT wrap it in ```mermaid fences.
Keep every other field (title, summary, steps, citations, ids) EXACTLY as given. Do not add or remove steps.
</task>

<output_format>
Output ONLY the complete updated JSON object with the same shape as the skeleton, now with
"guide" and "diagram" filled for each section. No markdown fences, no commentary.
</output_format>"""


def _build_page_prompt(title: str, file_links: str, language: str) -> str:
    """单个 wiki 页面生成提示词(移植 generatePageContent;file_links 为预建的 "- [path](url)" 行)。"""
    return f"""You are an expert technical writer and software architect.
Your task is to generate a comprehensive and accurate technical wiki page in Markdown format about a specific feature, system, or module within a given software project.

You will be given:
1. The "[WIKI_PAGE_TOPIC]" for the page you need to create.
2. A list of "[RELEVANT_SOURCE_FILES]" from the project that you MUST use as the sole basis for the content. You have access to the full content of these files. You MUST use AT LEAST 5 relevant source files for comprehensive coverage - if fewer are provided, search for additional related files in the codebase.

CRITICAL STARTING INSTRUCTION:
The very first thing on the page MUST be a `<details>` block listing ALL the `[RELEVANT_SOURCE_FILES]` you used to generate the content. There MUST be AT LEAST 5 source files listed - if fewer were provided, you MUST find additional related files to include.
Do not provide any acknowledgements, disclaimers, apologies, or any other preface before the `<details>` block. JUST START with the `<details>` block.
Format the block EXACTLY like the following template, reproducing it verbatim (do not add line numbers, do not convert the links to plain text, do not add any other text):
<details>
<summary>Relevant source files</summary>

The following files were used as context for generating this wiki page:

{file_links}
<!-- Add additional relevant files if fewer than 5 were provided -->
</details>

Immediately after the `<details>` block, the main title of the page should be a H1 Markdown heading: `# {title}`.

Based ONLY on the content of the `[RELEVANT_SOURCE_FILES]`:

1.  **Introduction:** Start with a concise introduction (1-2 paragraphs) explaining the purpose, scope, and high-level overview of "{title}" within the context of the overall project. If relevant, and if information is available in the provided files, link to other potential wiki pages using the format `[Link Text](#page-anchor-or-id)`.

2.  **Detailed Sections:** Break down "{title}" into logical sections using H2 (`##`) and H3 (`###`) Markdown headings. For each section:
    *   Explain the architecture, components, data flow, or logic relevant to the section's focus, as evidenced in the source files.
    *   Identify key functions, classes, data structures, API endpoints, or configuration elements pertinent to that section.

3.  **Mermaid Diagrams:**
    *   EXTENSIVELY use Mermaid diagrams (e.g., `flowchart TD`, `sequenceDiagram`, `classDiagram`, `erDiagram`, `graph TD`) to visually represent architectures, flows, relationships, and schemas found in the source files.
    *   Ensure diagrams are accurate and directly derived from information in the `[RELEVANT_SOURCE_FILES]`.
    *   Provide a brief explanation before or after each diagram to give context.
    *   CRITICAL: All diagrams MUST follow strict vertical orientation:
       - Use "graph TD" (top-down) directive for flow diagrams
       - NEVER use "graph LR" (left-right)
       - Maximum node width should be 3-4 words
       - For sequence diagrams:
         - Start with "sequenceDiagram" directive on its own line
         - Define ALL participants at the beginning using "participant" keyword
         - Optionally specify participant types: actor, boundary, control, entity, database, collections, queue
         - Use descriptive but concise participant names, or use aliases: "participant A as Alice"
         - Use the correct Mermaid arrow syntax (8 types available):
           - -> solid line without arrow (rarely used)
           - --> dotted line without arrow (rarely used)
           - ->> solid line with arrowhead (most common for requests/calls)
           - -->> dotted line with arrowhead (most common for responses/returns)
           - ->x solid line with X at end (failed/error message)
           - -->x dotted line with X at end (failed/error response)
           - -) solid line with open arrow (async message, fire-and-forget)
           - --) dotted line with open arrow (async response)
           - Examples: A->>B: Request, B-->>A: Response, A->xB: Error, A-)B: Async event
         - Use +/- suffix for activation boxes: A->>+B: Start (activates B), B-->>-A: End (deactivates B)
         - Group related participants using "box": box GroupName ... end
         - Use structural elements for complex flows:
           - loop LoopText ... end (for iterations)
           - alt ConditionText ... else ... end (for conditionals)
           - opt OptionalText ... end (for optional flows)
           - par ParallelText ... and ... end (for parallel actions)
           - critical CriticalText ... option ... end (for critical regions)
           - break BreakText ... end (for breaking flows/exceptions)
         - Add notes for clarification: "Note over A,B: Description", "Note right of A: Detail"
         - Use autonumber directive to add sequence numbers to messages
         - NEVER use flowchart-style labels like A--|label|-->B. Always use a colon for labels: A->>B: My Label

4.  **Tables:**
    *   Use Markdown tables to summarize information such as:
        *   Key features or components and their descriptions.
        *   API endpoint parameters, types, and descriptions.
        *   Configuration options, their types, and default values.
        *   Data model fields, types, constraints, and descriptions.

5.  **Code Snippets (ENTIRELY OPTIONAL):**
    *   Include short, relevant code snippets (e.g., Python, Java, JavaScript, SQL, JSON, YAML) directly from the `[RELEVANT_SOURCE_FILES]` to illustrate key implementation details, data structures, or configurations.
    *   Ensure snippets are well-formatted within Markdown code blocks with appropriate language identifiers.

6.  **Source Citations (EXTREMELY IMPORTANT):**
    *   For EVERY piece of significant information, explanation, diagram, table entry, or code snippet, you MUST cite the specific source file(s) and relevant line numbers from which the information was derived.
    *   Place citations at the end of the paragraph, under the diagram/table, or after the code snippet.
    *   Use the EXACT format below, and ALWAYS use the FULL repository-relative path exactly as it appears in the "Relevant source files" list above — NEVER a bare filename (e.g. use `src/lightning/pytorch/loops/fit_loop.py`, not `fit_loop.py`):
        *   Range: `Sources: [src/full/path/file.ext:start_line-end_line]()`
        *   Single line: `Sources: [src/full/path/file.ext:line_number]()`
        *   Multiple files: `Sources: [src/full/path/a.ext:1-10](), [src/full/path/b.ext:5](), [src/full/path/c.ext]()` (omit line numbers when the whole file is relevant).
    *   The word `Sources:` MUST be placed BEFORE the opening bracket, never inside it (write `Sources: [path]()`, NOT `[Sources: path]()`).
    *   Leave the parentheses `()` EMPTY — they are resolved into real links automatically. Do not put a URL inside them.
    *   If an entire section is overwhelmingly based on one or two files, you can cite them under the section heading in addition to more specific citations within the section.
    *   IMPORTANT: You MUST cite AT LEAST 5 different source files throughout the wiki page to ensure comprehensive coverage.

7.  **Technical Accuracy:** All information must be derived SOLELY from the `[RELEVANT_SOURCE_FILES]`. Do not infer, invent, or use external knowledge about similar systems or common practices unless it's directly supported by the provided code. If information is not present in the provided files, do not include it or explicitly state its absence if crucial to the topic.

8.  **Clarity and Conciseness:** Use clear, professional, and concise technical language suitable for other developers working on or learning about the project. Avoid unnecessary jargon, but use correct technical terms where appropriate.

9.  **Conclusion/Summary:** End with a brief summary paragraph if appropriate for "{title}", reiterating the key aspects covered and their significance within the project.

IMPORTANT: Generate the content in {_language_name(language)} language.

Remember:
- Ground every claim in the provided source files.
- Prioritize accuracy and direct representation of the code's functionality and structure.
- Structure the document logically for easy understanding by other developers.
"""


_COMPREHENSIVE_STRUCTURE = """
Create a structured wiki with the following main sections:
- Overview (general information about the project)
- System Architecture (how the system is designed)
- Core Features (key functionality)
- Data Management/Flow: If applicable, how data is stored, processed, accessed, and managed (e.g., database schema, data pipelines, state management).
- Frontend Components (UI elements, if applicable.)
- Backend Systems (server-side components)
- Model Integration (AI model connections)
- Deployment/Infrastructure (how to deploy, what's the infrastructure like)
- Extensibility and Customization: If the project architecture supports it, explain how to extend or customize its functionality (e.g., plugins, theming, custom modules, hooks).

Each section should contain relevant pages. For example, the "Frontend Components" section might include pages for "Home Page", "Repository Wiki Page", "Ask Component", etc.

Return your analysis in the following XML format:

<wiki_structure>
  <title>[Overall title for the wiki]</title>
  <description>[Brief description of the repository]</description>
  <sections>
    <section id="section-1">
      <title>[Section title]</title>
      <pages>
        <page_ref>page-1</page_ref>
        <page_ref>page-2</page_ref>
      </pages>
      <subsections>
        <section_ref>section-2</section_ref>
      </subsections>
    </section>
    <!-- More sections as needed -->
  </sections>
  <pages>
    <page id="page-1">
      <title>[Page title]</title>
      <description>[Brief description of what this page will cover]</description>
      <importance>high|medium|low</importance>
      <relevant_files>
        <file_path>[Path to a relevant file]</file_path>
        <!-- More file paths as needed -->
      </relevant_files>
      <related_pages>
        <related>page-2</related>
        <!-- More related page IDs as needed -->
      </related_pages>
      <parent_section>section-1</parent_section>
    </page>
    <!-- More pages as needed -->
  </pages>
</wiki_structure>
"""

_CONCISE_STRUCTURE = """
Return your analysis in the following XML format:

<wiki_structure>
  <title>[Overall title for the wiki]</title>
  <description>[Brief description of the repository]</description>
  <pages>
    <page id="page-1">
      <title>[Page title]</title>
      <description>[Brief description of what this page will cover]</description>
      <importance>high|medium|low</importance>
      <relevant_files>
        <file_path>[Path to a relevant file]</file_path>
        <!-- More file paths as needed -->
      </relevant_files>
      <related_pages>
        <related>page-2</related>
        <!-- More related page IDs as needed -->
      </related_pages>
    </page>
    <!-- More pages as needed -->
  </pages>
</wiki_structure>
"""


if TYPE_CHECKING:
    from gh_puller.deepwiki import (  # noqa: F401 —— 仅运行时注解(forward ref)
        ChatCompletionRequest,
        ChatMessage,
        CodeMapRequest,
        WikiPage,
        WikiStructureModel,
        WikiTask,
    )

# ---------------------------------------------------------------------------
# 双路包装类:llm 路与 agent(cc/dsh)路对外 API 的统一入口。
# 分派:wiki 生成(结构/页面)按 target.generator 经 _wiki_pipeline() 选择;
#       chat / codemap 同开关经 _service_pipeline()(target 缺省走 env 默认)。
# 边界:语义属于单路生成协议的 helper(提示词组装/历史转写/检索上下文注入/
# agent 交付件路径/llm 一次问答生成链)收进对应 pipeline 类为 self._xxx();
# 共用构建件与低层支撑(共享提示词段、XML 解析/引用后处理、图索引、任务状态机、
# agent SDK/llm 端点通道)保持模块级。类方法体内一律以模块全局名引用
# cc_stream / dsh_stream / llm_stream / llm_complete / envs.*,保证调用时动态解析
# (测试 monkeypatch 与 envs 切换均生效,不得实例捕获或模块级快照)。
# ---------------------------------------------------------------------------


class WikiPipeline:
    """双路共同协议;基类默认实现即 llm 路语义(无交付文件、无续跑水合、占位不落盘)。"""

    def needs_structure_regenerate(self, task: WikiTask) -> bool:
        """结构是否需要强制重生成(cc 路:structure 交付文件缺失;llm 路恒 False)。"""
        return False

    async def hydrate_pages(self, task: WikiTask) -> None:
        """以已落盘交付文件水合 generated_pages(cc 路;llm 路无交付文件,no-op)。"""

    def write_error_page(self, task: WikiTask, page: WikiPage, content: str) -> None:
        """重试耗尽后的占位页持久化(cc 路写交付文件供续跑跳过;llm 路 no-op)。"""

    async def determine_structure(
        self, task: WikiTask, repo: Repo, file_tree: list[str], readme: str,
    ) -> WikiStructureModel:
        raise NotImplementedError

    async def generate_page(
        self, task: WikiTask, repo: Repo, page: WikiPage, file_links: str,
    ) -> str:
        raise NotImplementedError

    async def chat_stream(self, request: ChatCompletionRequest):
        raise NotImplementedError

    async def generate_codemap(self, request: CodeMapRequest):
        raise NotImplementedError


class AgentWikiPipeline(WikiPipeline):
    """cc(agent)路对外 API 包装:Claude Code agent 自读仓库代码,交付件 Write 落盘(文件为权威)。"""

    # deep_research 折叠版(cc/agent 路专用):一次回答完成整轮研究(agent 内部
    # 多轮工具调用);LLM 路仍走 LlmWikiPipeline 的 FIRST/INTERMEDIATE/FINAL 三模板(原版 5 轮协议)。
    # 标题字符串必须逐字匹配前端 Ask.tsx 的提取/完成判定正则(Research Plan /
    # Research Update {n} / Final Conclusion);禁 "## Next Steps"(它会截断 plan 提取
    # 并影响完成判定)与 "## Conclusion"/"## Summary"(完成判定的次选触发词)。
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

    def _proj_key(self, r: Any) -> str:
        """项目键 {type}_{owner}_{repo}_{digest}:digest = target 判等摘要
        (同一仓库/语言下不同 target 的交付文件并存,与成品缓存同规则)。"""
        return _sanitize_path_seg(f"{r.type}_{r.owner}_{r.repo}_{deepwiki._request_digest(r.target)}")

    def _agent_cache_dir(self, r: Any) -> Path:
        return Path(deepwiki._WIKI_CACHE_DIR) / deepwiki._AGENT_CACHE_DIRNAME / self._proj_key(r)

    def _agent_cache_structure_path(self, r: Any) -> Path:
        """cc 结构交付文件:{proj}-structure.md。"""
        return self._agent_cache_dir(r) / f"{self._proj_key(r)}-structure.md"

    def _agent_cache_page_path(self, r: Any, page_id: str) -> Path:
        """cc 页面交付文件:{proj}-<id>.md(id 形如 page-N 时直接采用,否则 {proj}-page_<id>;id 经安全化)。"""
        seg = _sanitize_path_seg(page_id)
        name = f"{self._proj_key(r)}-{seg}" if seg.startswith("page-") else f"{self._proj_key(r)}-page_{seg}"
        return self._agent_cache_dir(r) / f"{name}.md"

    def _render_natural_history(self, messages: list[ChatMessage]) -> tuple[str, list[dict]]:
        """agent 路对话历史:自然转写(无 <turn>/<conversation_history> 伪标签),含裁剪说明事件。"""
        history_parts: list[str] = []
        context: list[dict] = []
        if len(messages) > 1:
            last = messages[-1]
            if _estimate_tokens(last.content) > envs.CHAT_TOKEN_LIMIT_ESTIMATE:
                deepwiki._log(f"请求过大(估算 {_estimate_tokens(last.content)} tokens),省略对话历史")
                context.append({"type": "context/modify",
                                "data": {"target": "chat-history", "kind": "trim",
                                         "cause": "token-limit", "detail": "省略对话历史",
                                         "removed": {"n_turns": len(messages) - 1,
                                                     "est_tokens": _estimate_tokens(last.content)}}})
            else:
                for i in range(0, len(messages) - 1, 2):
                    user, assistant = messages[i], messages[i + 1]
                    if user.role == "user" and assistant.role == "assistant":
                        history_parts.append(f"User: {user.content}\nAssistant: {assistant.content}")
        if history_parts:
            return "Previous conversation:\n" + "\n\n".join(history_parts) + "\n\n", context
        return "", context

    def _codemap_note(self) -> str:
        """codemap 指引(仅 cc/agent 路用):先查图谱再构造,引用行号取自 Source 标记。"""
        return (
            "<note>Before answering, use the graphify_query tool to inspect the repository "
            "code graph (its result carries `Source: <file path> L<line>` markers). "
            "When filling citation.file_path / start_line / end_line, use those paths and line "
            "numbers, and make the 'snippet' a verbatim substring of the code shown in the result.</note>\n\n"
        )

    def needs_structure_regenerate(self, task: WikiTask) -> bool:
        """structure 交付文件被删即强制重生成(续跑失效)。"""
        return not self._agent_cache_structure_path(task.request).exists()

    async def determine_structure(
        self, task: WikiTask, repo: Repo, file_tree: list[str], readme: str,
    ) -> WikiStructureModel:
        """cc(agent)结构:交付文件已存在即跳过 agent(续跑);否则 agent 落盘 structure.md 后读回解析。"""
        r = task.request
        struct_path = self._agent_cache_structure_path(r)
        if struct_path.exists():
            content = await asyncio.to_thread(struct_path.read_text, encoding="utf-8")
        else:
            readme_path = _find_readme_path(file_tree)
            prompt = self._build_structure_prompt(
                r.owner, r.repo, "\n".join(file_tree), readme_path,
                os.path.abspath(repo.save_path), r.comprehensive, r.language,
                str(struct_path), generator=deepwiki._resolve_target(r.target)[0],
            )
            content = await deepwiki._agent_write_file(
                r.target, "", prompt, repo, struct_path, label="wiki:structure",
                run_id=task.repo_key,
            )
        return deepwiki.parse_wiki_structure(content, comprehensive=r.comprehensive)

    @staticmethod
    def _build_structure_prompt(
        owner: str, repo_name: str, file_tree: str, readme_path: str | None,
        repo_root: str, comprehensive: bool, language: str, out_path: str,
        generator: str = "cc",
    ) -> str:
        """cc(agent)结构提示词(现代 agent 风格):输入只给路径(文件树+README 路径),不内联内容;
        成品 XML 由 agent 用 Write 工具直接落盘 out_path,而不是作为 result 文本返回。"""
        structure_format = _COMPREHENSIVE_STRUCTURE if comprehensive else _CONCISE_STRUCTURE
        page_count = "8-12" if comprehensive else "4-6"
        kind = "comprehensive" if comprehensive else "concise"
        readme_line = (
            f"2. The README file of the project is at: {repo_root}/{readme_path}\n"
            "   Read it yourself with the Read tool.\n"
            if readme_path
            else "2. No README file was found in this repository; skip it.\n"
        )
        return f"""IMPORTANT: you are working INSIDE the repository (cwd = repository root at {repo_root}).
The file contents are NOT inlined in this prompt — read source files yourself with the
Read/Grep/Glob tools. All paths below are relative to the repository root.

Analyze this repository {owner}/{repo_name} and create a wiki structure for it.

1. The complete relative file tree of the project:
<file_tree>
{file_tree}
</file_tree>

{readme_line}
I want to create a wiki for this repository. Determine the most logical structure for a wiki based on the repository's content.

IMPORTANT: The wiki content will be generated in {_language_name(language)} language.

When designing the wiki structure, include pages that would benefit from visual diagrams, such as:
- Architecture overviews
- Data flow descriptions
- Component relationships
- Process workflows
- State machines
- Class hierarchies
{structure_format}
IMPORTANT FORMATTING INSTRUCTIONS:
- Return ONLY the valid XML structure specified above
- DO NOT wrap the XML in markdown code blocks (no ``` or ```xml)
- DO NOT include any explanation text before or after the XML
- Ensure the XML is properly formatted and valid
- Start directly with <wiki_structure> and end with </wiki_structure>

DELIVERABLE: Write the complete XML to the file {out_path} using the Write tool (create the file;
do not use Edit). Do NOT return the XML in your message text — the written file is the only deliverable.

IMPORTANT:
1. Create {page_count} pages that would make a {kind} wiki for this repository
2. Each page should focus on a specific aspect of the codebase (e.g., architecture, key features, setup)
3. The relevant_files should be actual files from the repository that would be used to generate that page
4. Do not inline file contents into this prompt — use your tools to read the files.
{deepwiki._agent_note(generator)}"""

    @staticmethod
    def _build_page_prompt(title: str, file_paths: list[str], out_path: str, language: str) -> str:
        """cc(agent)页面提示词(现代 agent 风格):只给相关文件相对路径,内容由 agent 自读;成品经 Write 落盘 out_path。"""
        paths = "\n".join(f"- [{p}]({p})" for p in file_paths)
        return (
            "IMPORTANT: you are working INSIDE the repository (cwd = repository root). "
            "The file contents are NOT inlined in this prompt — read the relevant source files "
            "yourself with the Read/Grep/Glob tools. All paths below are relative to the repository root.\n\n"
            + _build_page_prompt(title, paths, language)
            + f"\n\nDELIVERABLE: Write the complete generated Markdown page to `{out_path}` using the "
              "Write tool (create the file; do not use Edit). Do NOT return the page in your message "
              "text — the written file is the only deliverable."
        )

    async def generate_page(
        self, task: WikiTask, repo: Repo, page: WikiPage, file_links: str,
    ) -> str:
        """cc 路单页:交付文件已存在即读回(续跑,文件为权威);否则 agent 落盘 page_<id>.md。"""
        r = task.request
        out_path = self._agent_cache_page_path(r, page.id)
        if out_path.exists():
            return await asyncio.to_thread(out_path.read_text, encoding="utf-8")
        generator = deepwiki._resolve_target(r.target)[0]
        prompt = deepwiki._agent_note(generator) + self._build_page_prompt(
            page.title, list(page.filePaths), str(out_path), r.language
        )
        return await deepwiki._agent_write_file(
            r.target, "", prompt, repo, out_path, label=f"wiki:page:{page.id}",
            run_id=task.repo_key,
        )

    async def hydrate_pages(self, task: WikiTask) -> None:
        """cc 路径:从已落盘的页交付文件水合 generated_pages(文件为权威,覆盖 state 旧文本);
        无文件的页留给 _generate_pages(含每页完成即落盘的状态语义)。"""
        structure = task.wiki_structure
        if structure is None:
            return
        r = task.request
        ctx = deepwiki.RepoUrlContext(type=r.type, repo_url=r.repo_url, default_branch=task.default_branch)
        for page in structure.pages:
            out_path = self._agent_cache_page_path(r, page.id)
            if not out_path.exists():
                continue
            content = await asyncio.to_thread(out_path.read_text, encoding="utf-8")
            content = _strip_markdown_fences(content)
            content = deepwiki.post_process_wiki_content(content, list(page.filePaths), ctx)
            task.generated_pages[page.id] = page.model_copy(update={"content": content})
        task.pages_done = len(task.generated_pages)

    def write_error_page(self, task: WikiTask, page: WikiPage, content: str) -> None:
        """重试耗尽(cc 路):占位文本也落盘,续跑跳过占位页;用户删除该文件即可重试。"""
        try:
            self._agent_cache_page_path(task.request, page.id).write_text(content, encoding="utf-8")
        except OSError as e:  # noqa: BLE001 - 占位写入失败不阻断任务完成
            deepwiki._log(f"写入占位页文件失败: {page.id} - {e}")

    async def chat_stream(self, request: ChatCompletionRequest):
        """一次 chat 请求的流式应答(纯文本 chunk 序列,前后端协议同原 research_chat);
        现代 agent 模式:一次提问,agent 内部多轮工具调用完成,不做协议级轮转。"""
        if not request.messages:
            raise ValueError("No messages provided")
        last = request.messages[-1]
        if last.role != "user":
            raise ValueError("Last message must be from the user")

        # 注:未索引的前置校验已上移到端点层(_require_indexed),生成器内不再重复
        repo = Repo(request.repo_url, request.type, access_token=request.token)

        fmt = deepwiki._format_request_fmt(request)
        is_deep = last.mode == "deep_research"
        if is_deep:
            # 折叠:一次回答完成整轮研究(agent 内部多轮工具调用);continuation 回退
            # 保留作偏差兜底(首轮缺 Final Conclusion 时前端续跑轮为同一问题重跑)
            deepwiki._resolve_chat_continuation(last, request.messages)
            system = self._DEEP_RESEARCH_ONE_SHOT_PROMPT.format(**fmt)
        else:
            system = _SIMPLE_CHAT_SYSTEM_PROMPT.format(**fmt)

        # 对话历史自然转写(无 <turn> 伪标签);输入过大时省略历史。
        # 裁剪/注记作为监控上下文说明事件(context/modify|inject)伴跑,不改变折叠结果
        generator = deepwiki._resolve_target(request.target)[0]
        history, context = self._render_natural_history(request.messages)
        context.append({"type": "context/inject",
                        "data": {"target": "user-message", "phase": "prompt-assembly",
                                 "provenance": "deepwiki:note", "text": deepwiki._agent_note(generator)}})

        prompt = history + deepwiki._agent_note(generator) + f"<query>\n{last.content}\n</query>\n\nAssistant: "
        try:
            async for chunk in deepwiki._agent_stream(
                request.target, system, prompt, repo=repo, label=f"chat:{repo.name}",
                run_id=f"chat:{repo.name}", context=context,
            ):
                yield chunk
        except Exception as e:  # 执行期失败降级为可读错误文本(同原 stream_and_fallback 语义)
            deepwiki._log(f"chat agent 错误: {e}")
            yield f"\n\n(抱歉,本次请求处理失败: {e})"

    async def generate_codemap(self, request: CodeMapRequest):
        """两阶段 codemap 生成(骨架 → 指南/图),NDJSON 事件流;阶段失败语义与原相同。"""
        try:
            repo = Repo(request.repo_url, request.type, access_token=request.token)
        except Exception:
            repo = Repo(request.repo_url, request.type)

        yield _phase("analyzing", "start")
        if not deepwiki._index_ready(repo):
            yield _phase("analyzing", "done", chunk_count=0)
            yield _event(type="error", stage="analyzing", message=f"仓库尚未索引,请先 /repo/prepare: {repo.name}")
            return
        yield _phase("analyzing", "done", chunk_count=0)

        fmt = {
            "repo_type": request.type,
            "repo_url": request.repo_url,
            "repo_name": repo.name,
            "language_name": _language_name(request.language or "en"),
        }

        async def _run_json(prompt: str, attempts: int = 3) -> dict:
            """整收 + 解析 JSON,失败重试(每轮新 agent);system 恒用骨架提示词。"""
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    raw = await deepwiki._agent_text(
                        request.target, _CODEMAP_SKELETON_PROMPT.format(**fmt), prompt, repo=repo,
                        label="codemap:skeleton",
                        run_id=f"codemap:{repo.name}",
                        retry={"attempt": attempt, "prev_error": str(last_error)} if last_error else None,
                    )
                    return _extract_json(raw)
                except Exception as e:  # noqa: BLE001
                    last_error = e
                    deepwiki._log(f"codemap JSON 解析尝试 {attempt}/{attempts} 失败: {e}")
            raise ValueError(f"Model did not return valid JSON after {attempts} attempts: {last_error}")

        # 阶段 1:骨架
        yield _phase("initial_codemap", "start")
        skeleton_prompt = self._codemap_note() + f"<query>\n{request.question}\n</query>\n\nAssistant: "
        try:
            skeleton = deepwiki.CodeMap.model_validate(await _run_json(skeleton_prompt))
        except Exception as e:  # noqa: BLE001
            deepwiki._log(f"codemap 骨架失败: {e}")
            yield _event(type="error", stage="initial_codemap", message=str(e))
            return
        yield _phase("initial_codemap", "done", section_count=len(skeleton.sections))

        # 阶段 2:指南/图;i/骨架失败不致命 — 退化为骨架
        yield _phase("diagrams", "start")
        enrich_query = (
            f"{request.question}\n\n<SKELETON>\n{skeleton.model_dump_json()}\n</SKELETON>"
        )
        enrich_prompt = self._codemap_note() + f"<query>\n{enrich_query}\n</query>\n\nAssistant: "
        final = skeleton
        try:
            raw = await deepwiki._agent_text(
                request.target, _CODEMAP_ENRICH_PROMPT.format(**fmt), enrich_prompt, repo=repo,
                label="codemap:enrich", run_id=f"codemap:{repo.name}",
            )
            final = deepwiki.CodeMap.model_validate(_extract_json(raw))
            yield _phase("diagrams", "done")
        except Exception as e:  # noqa: BLE001
            deepwiki._log(f"codemap 指南/图失败,使用骨架: {e}")
            yield _phase("diagrams", "done", degraded=True)

        deepwiki._ground_citations(final, repo.save_path)
        yield _event(type="codemap", data=final.model_dump())
        yield _event(type="done")


class LlmWikiPipeline(WikiPipeline):
    """llm 路对外 API 包装:deepwiki-open 原式单次补全(内容内联进 prompt,无工具)。"""

    # 纯 LLM 路径单文件内联截断(字符)
    _FILE_INLINE_CAP = 8000

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
- NEVER respond with "Continue the research" as an answer - always provide a complete conclusion
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

    def _subgraph_hits(self, answer: str) -> dict[str, list[int]]:
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

    def _subgraph_src_blocks(
        self, save_path: str,
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
        (返回 ([], True));调用方据此跳过注入(提示词注"无检索增强"note)。
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

    def _format_subgraph_context(self, blocks: list[dict[str, Any]]) -> str:
        """代码窗 → 原版 _format_context 同式文本(chat/codemap 共用)。

        按文件分组:每组 `## File Path: {path}` 头 + 每窗 `[lines A-B]\n<code>`
        (窗间空行);文件段以原版同式(`"\\n\\n" + "-"*10 + "\\n\\n".join(parts)`)
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

    async def _graphify_context(self, repo: Repo, question: str) -> dict[str, Any]:
        """graphify.query + 子图 → 真实代码行窗;任何失败吞掉置 error(服务降级,不抛)。"""
        try:
            result = await asyncio.to_thread(
                graphify.query, question, graph_path=str(deepwiki._graph_path(repo))
            )
            answer = result.get("answer") or ""
        except Exception as exc:
            deepwiki._log(f"图谱查询失败,降级: {type(exc).__name__}: {exc}")
            return {"hits": {}, "blocks": [], "degraded": False, "error": f"{type(exc).__name__}: {exc}"}
        hits = self._subgraph_hits(answer)
        if not hits:
            return {"hits": {}, "blocks": [], "degraded": False, "error": None}
        blocks, degraded = self._subgraph_src_blocks(repo.save_path, hits)
        return {"hits": hits, "blocks": blocks, "degraded": degraded, "error": None}

    def _build_turn_history(self, messages: list[ChatMessage]) -> str:
        """LLM 路对话历史:恒拼 <turn> 成对序列(原版无裁剪;输入过大仅跳过检索上下文)。"""
        turns = ""
        for i in range(0, len(messages) - 1, 2):
            user, assistant = messages[i], messages[i + 1]
            if user.role == "user" and assistant.role == "assistant":
                turns += f"<turn>\n<user>{user.content}</user>\n<assistant>{assistant.content}</assistant>\n</turn>\n"
        return f"<conversation_history>\n{turns}</conversation_history>\n\n" if turns else ""

    def _build_service_prompt(
        self, system_prompt: str, query: str, *, conversation_history: str = "", context: str = "",
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

    def _is_token_limit_error(self, exc: Exception) -> bool:
        """原版 api/chat/__init__.py is_token_limit_error 的判断子串(大小写不敏感)。"""
        error_message = str(exc).lower()
        return any(k in error_message for k in (
            "maximum context length", "token limit", "too many tokens",
        ))

    async def _llm_stream_user(self, prompt: str, *, target, session_name: str, run_id: str,
                               context: list[dict] | None = None):
        """llm 路统一流式补全口(经 generate_stream 分派:model/url/api_key 由 target
        绑定,显式 target > env > provider 缺省;单条 user 消息,同原版 streamer)。"""
        async for chunk in deepwiki.generate_stream(
            "", target=target,
            options={"messages": [{"role": "user", "content": prompt}]},
            session_name=session_name, run_id=run_id, context=context,
        ):
            yield chunk

    async def _llm_complete_user(self, prompt: str, *, target, session_name: str, run_id: str,
                                 context: list[dict] | None = None) -> str:
        """llm 路统一整收补全口(经 generate_result 分派;单条 user 消息)。"""
        return await deepwiki.generate_result(
            "", target=target,
            options={"messages": [{"role": "user", "content": prompt}]},
            session_name=session_name, run_id=run_id, context=context,
        )

    async def _llm_research_chat(
        self, system: str, query: str, *, target, repo: Repo, session_name: str, run_id: str,
        conversation_history: str = "",
    ):
        """原版 research_chat 等价(LLM 路流式):见 _llm_research_stream 所述语义;
        失败语义与原版一致(错误文本进流,不抛出)。"""
        async for chunk in self._llm_research_stream(
            system, query, target=target, repo=repo, session_name=session_name, run_id=run_id,
            conversation_history=conversation_history,
        ):
            yield chunk

    async def _llm_research_stream(
        self, system: str, query: str, *, target, repo: Repo, session_name: str, run_id: str,
        conversation_history: str = "",
    ):
        """原版 research_chat 语义(LLM 路逐段对齐):

        - 最后一问估算超 CHAT_TOKEN_LIMIT_ESTIMATE(原 MAX_INPUT_TOKENS=7500)→ 跳过检索;
        - 检索上下文 = 图谱子图→真实代码窗(原 RAG 的适配点),经 prompt_builder 同式拼装
          (<START_OF_CONTEXT> 包裹;为空注"无检索增强"note);
        - stream_and_fallback:token 超限 → 简化提示词重试(去掉检索上下文)→ 致歉;
          其余异常 → "Error with openai API: {e}" 文本进流。
        """
        context: list[dict] = []
        context_text = ""
        if _estimate_tokens(query) > envs.CHAT_TOKEN_LIMIT_ESTIMATE:
            deepwiki._log(f"请求过大(估算 {_estimate_tokens(query)} tokens),跳过检索上下文(原版 MAX_INPUT_TOKENS 语义)")
            context.append({"type": "context/modify",
                            "data": {"target": "query", "kind": "trim",
                                     "cause": "token-limit", "detail": "输入过大,跳过检索"}})
        else:
            ctx = await self._graphify_context(repo, query)
            for b in ctx["blocks"]:
                context.append({"type": "context/inject",
                                "data": {"target": "query", "phase": "prompt-assembly",
                                         "provenance": f"deepwiki:graph:{b['path']}",
                                         "text": b["text"]}})
            if ctx["degraded"]:
                context.append({"type": "context/modify",
                                "data": {"target": "query", "kind": "degrade",
                                         "cause": "token-limit", "detail": "检索上下文超限"}})
            if ctx["error"]:
                context.append({"type": "context/modify",
                                "data": {"target": "query", "kind": "degrade",
                                         "cause": "graph-error",
                                         "detail": f"代码图谱不可用: {ctx['error']}"}})
            context_text = self._format_subgraph_context(ctx["blocks"])

        prompt = self._build_service_prompt(
            system, query, conversation_history=conversation_history, context=context_text
        )
        simplified = self._build_service_prompt(
            system, query, conversation_history=conversation_history, simplify=True
        )
        try:
            async for chunk in self._llm_stream_user(
                prompt, target=target, session_name=session_name, run_id=run_id, context=context,
            ):
                yield chunk
        except Exception as e:
            if self._is_token_limit_error(e):
                deepwiki._log("token 超限,简化为无检索上下文重试")
                try:
                    async for chunk in self._llm_stream_user(
                        simplified, target=target, session_name=session_name, run_id=run_id,
                        context=context,
                    ):
                        yield chunk
                except Exception as e2:  # noqa: BLE001 - 简化重试失败 → 致歉文本(原版同式)
                    deepwiki._log(f"简化重试失败: {e2}")
                    yield (
                        "\nI apologize, but your request is too large for me to process. "
                        "Please try a shorter query or break it into smaller parts."
                    )
            else:
                deepwiki._log(f"chat llm 错误: {e}")
                yield f"\nError with openai API: {e}"

    async def determine_structure(
        self, task: WikiTask, repo: Repo, file_tree: list[str], readme: str,
    ) -> WikiStructureModel:
        """llm 路结构:原版经 research_chat(结构提示词为查询,SIMPLE 角色模板 +
        检索上下文注入 + prompt_builder 拼装);内容错误时解析失败使任务 FAILED。"""
        r = task.request
        prompt = self._build_structure_prompt(
            r.owner, r.repo, "\n".join(file_tree), readme, r.comprehensive, r.language
        )
        system = _SIMPLE_CHAT_SYSTEM_PROMPT.format(**deepwiki._format_request_fmt(r))
        parts: list[str] = []
        async for chunk in self._llm_research_chat(
            system, prompt, target=r.target, repo=repo,
            session_name="wiki:structure", run_id=task.repo_key,
        ):
            parts.append(chunk)
        return deepwiki.parse_wiki_structure("".join(parts), comprehensive=r.comprehensive)

    @staticmethod
    def _build_structure_prompt(
        owner: str, repo_name: str, file_tree: str, readme: str,
        comprehensive: bool, language: str,
    ) -> str:
        """wiki 结构确定提示词(移植 determineWikiStructure)。"""
        structure_format = _COMPREHENSIVE_STRUCTURE if comprehensive else _CONCISE_STRUCTURE
        page_count = "8-12" if comprehensive else "4-6"
        kind = "comprehensive" if comprehensive else "concise"
        return f"""Analyze this GitHub repository {owner}/{repo_name} and create a wiki structure for it.

1. The complete file tree of the project:
<file_tree>
{file_tree}
</file_tree>

2. The README file of the project:
<readme>
{readme}
</readme>

I want to create a wiki for this repository. Determine the most logical structure for a wiki based on the repository's content.

IMPORTANT: The wiki content will be generated in {_language_name(language)} language.

When designing the wiki structure, include pages that would benefit from visual diagrams, such as:
- Architecture overviews
- Data flow descriptions
- Component relationships
- Process workflows
- State machines
- Class hierarchies
{structure_format}
IMPORTANT FORMATTING INSTRUCTIONS:
- Return ONLY the valid XML structure specified above
- DO NOT wrap the XML in markdown code blocks (no ``` or ```xml)
- DO NOT include any explanation text before or after the XML
- Ensure the XML is properly formatted and valid
- Start directly with <wiki_structure> and end with </wiki_structure>

IMPORTANT:
1. Create {page_count} pages that would make a {kind} wiki for this repository
2. Each page should focus on a specific aspect of the codebase (e.g., architecture, key features, setup)
3. The relevant_files should be actual files from the repository used to generate that page
4. Return ONLY valid XML with the structure specified above, with no markdown code block delimiters"""

    async def generate_page(
        self, task: WikiTask, repo: Repo, page: WikiPage, file_links: str,
    ) -> str:
        """llm 路单页:原版 _generate_page 同式——页面提示词(仅文件链接,不内联内容)
        作为查询经 research_chat 等价流(检索上下文注入;流错误为内容而非抛出,
        _generate_page_with_retry 只对校验/检索前置异常重试——与原版一致)。"""
        r = task.request
        prompt = _build_page_prompt(page.title, file_links, r.language)
        system = _SIMPLE_CHAT_SYSTEM_PROMPT.format(**deepwiki._format_request_fmt(r))
        parts: list[str] = []
        async for chunk in self._llm_research_chat(
            system, prompt, target=r.target, repo=repo,
            session_name=f"wiki:page:{page.id}", run_id=task.repo_key,
        ):
            parts.append(chunk)
        return "".join(parts)

    async def chat_stream(self, request: ChatCompletionRequest):
        """llm 路 chat:原版 research_chat 等价(模式/迭代选模板、原版历史拼接、
        输入过大跳过检索、prompt_builder 拼装、token 超限简化重试)。"""
        if not request.messages:
            raise ValueError("No messages provided")
        last = request.messages[-1]
        if last.role != "user":
            raise ValueError("Last message must be from the user")

        repo = Repo(request.repo_url, request.type, access_token=request.token)
        fmt = deepwiki._format_request_fmt(request)
        is_deep = last.mode == "deep_research"
        if is_deep:
            # 原版复刻:5 轮迭代由前端驱动,后端只按迭代号选模板 + 拼历史
            deepwiki._resolve_chat_continuation(last, request.messages)
            if request.research_iteration == 1:
                system = self._DEEP_RESEARCH_FIRST_ITERATION_PROMPT.format(**fmt)
            elif request.research_iteration >= 5:
                system = self._DEEP_RESEARCH_FINAL_ITERATION_PROMPT.format(**fmt)
            else:
                system = self._DEEP_RESEARCH_INTERMEDIATE_ITERATION_PROMPT.format(
                    **fmt, research_iteration=request.research_iteration
                )
        else:
            system = _SIMPLE_CHAT_SYSTEM_PROMPT.format(**fmt)

        history = self._build_turn_history(request.messages)
        async for chunk in self._llm_research_chat(
            system, last.content, target=request.target, repo=repo,
            session_name=f"chat:{repo.name}", run_id=f"chat:{repo.name}",
            conversation_history=history,
        ):
            yield chunk

    async def generate_codemap(self, request: CodeMapRequest):
        """llm 路 codemap(原版等价):analyzing 阶段完成检索(chunk_count=窗口数);
        双提示词经 prompt_builder(与 chat 同构);JSON 解析失败重试——骨架 3 次、
        富化 2 次(传输错误直接上抛);富化失败 degraded;引用接地两路共用。"""
        try:
            repo = Repo(request.repo_url, request.type, access_token=request.token)
        except Exception:
            repo = Repo(request.repo_url, request.type)

        # ---- 阶段 1a:analyzing(原版 RAG 检索;此处 = 图谱子图→真实代码窗) ----
        yield _phase("analyzing", "start")
        if not deepwiki._index_ready(repo):
            yield _phase("analyzing", "done", chunk_count=0)
            yield _event(type="error", stage="analyzing", message=f"仓库尚未索引,请先 /repo/prepare: {repo.name}")
            return
        ctx = await self._graphify_context(repo, request.question)
        yield _phase("analyzing", "done", chunk_count=len(ctx["blocks"]))

        fmt = {
            "repo_type": request.type,
            "repo_url": request.repo_url,
            "repo_name": repo.name,
            "language_name": _language_name(request.language or "en"),
        }
        context: list[dict] = []
        for b in ctx["blocks"]:
            context.append({"type": "context/inject",
                            "data": {"target": "codemap", "phase": "prompt-assembly",
                                     "provenance": f"deepwiki:graph:{b['path']}", "text": b["text"]}})
        if ctx["degraded"]:
            context.append({"type": "context/modify",
                            "data": {"target": "codemap", "kind": "degrade",
                                     "cause": "token-limit", "detail": "检索上下文超限"}})
        if ctx["error"]:
            context.append({"type": "context/modify",
                            "data": {"target": "codemap", "kind": "degrade",
                                     "cause": "graph-error",
                                     "detail": f"代码图谱不可用: {ctx['error']}"}})
        context_text = self._format_subgraph_context(ctx["blocks"])

        async def _run_llm_json(prompt: str, attempts: int, session_name: str) -> dict:
            """整收 + 解析 JSON;仅解析失败重试(原版 _generate_json 语义:
            传输异常直接上抛,由阶段 try 处理)。"""
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                raw = await self._llm_complete_user(
                    prompt, target=request.target,
                    session_name=session_name, run_id=f"codemap:{repo.name}",
                    context=context,
                )
                try:
                    return _extract_json(raw)
                except Exception as e:  # noqa: BLE001
                    last_error = e
                    deepwiki._log(f"codemap JSON 解析尝试 {attempt}/{attempts} 失败: {e}")
            raise ValueError(f"Model did not return valid JSON after {attempts} attempts: {last_error}")

        # ---- 阶段 1b:骨架 ----------------------------------------------------
        yield _phase("initial_codemap", "start")
        skeleton_prompt = self._build_service_prompt(
            _CODEMAP_SKELETON_PROMPT.format(**fmt), request.question, context=context_text
        )
        try:
            skeleton = deepwiki.CodeMap.model_validate(await _run_llm_json(skeleton_prompt, 3, "codemap:skeleton"))
        except Exception as e:  # noqa: BLE001
            deepwiki._log(f"codemap 骨架失败: {e}")
            yield _event(type="error", stage="initial_codemap", message=str(e))
            return
        yield _phase("initial_codemap", "done", section_count=len(skeleton.sections))

        # ---- 阶段 2:指南/图;失败不致命 — 退化为骨架 -------------------------
        yield _phase("diagrams", "start")
        enrich_query = (
            f"{request.question}\n\n<SKELETON>\n{skeleton.model_dump_json()}\n</SKELETON>"
        )
        enrich_prompt = self._build_service_prompt(
            _CODEMAP_ENRICH_PROMPT.format(**fmt), enrich_query, context=context_text
        )
        final = skeleton
        try:
            final = deepwiki.CodeMap.model_validate(
                await _run_llm_json(enrich_prompt, 2, "codemap:enrich")
            )
            yield _phase("diagrams", "done")
        except Exception as e:  # noqa: BLE001
            deepwiki._log(f"codemap 指南/图失败,使用骨架: {e}")
            yield _phase("diagrams", "done", degraded=True)

        deepwiki._ground_citations(final, repo.save_path)
        yield _event(type="codemap", data=final.model_dump())
        yield _event(type="done")


def _wiki_pipeline(target: "Mapping | None" = None) -> WikiPipeline:
    """按解析后的 target.generator 选路;调用时解析(测试 monkeypatch envs 生效)。

    agent 类后段(cc/dsh/codex)共用 AgentWikiPipeline —— 内部 _agent_stream/
    _agent_write_file 经 target 分派到 dispatcher,管线逻辑(结构/页面/缓存)后端无关;
    llm 走 LlmWikiPipeline(原式单次补全)。
    """
    gen = deepwiki._resolve_target(target)[0]
    return AgentWikiPipeline() if gen in ("cc", "dsh", "codex") else LlmWikiPipeline()


def _service_pipeline(target: "Mapping | None" = None) -> WikiPipeline:
    """chat/codemap 服务分派:与 wiki 同开关(按 target.generator),调用时解析(测试 monkeypatch envs 生效)。"""
    gen = deepwiki._resolve_target(target)[0]
    return AgentWikiPipeline() if gen in ("cc", "dsh", "codex") else LlmWikiPipeline()





