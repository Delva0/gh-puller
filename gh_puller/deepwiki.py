"""DeepWiki 兼容后端:前端契约沿用 deepwiki-open(前端见 apps/deepwiki-webui/web/),运行引擎替换为 Claude Code agent + graphify。

来源与协议:
- 前端契约与提示词来自 deepwiki-open(MIT License, Copyright (c) 2024 Sheing Ng)。
- 原后端 RAG(adalflow + FAISS chunk/embed 检索)整体切除:
  索引 = `graphify.extract(code_only=True)` 的纯本地 AST 建图(输出 <DEEPWIKI_ROOT>/graphify/<repo>/graph.json);
  检索 = `graphify.query()` 封装为 Claude Code agent 的 graphify_query 工具,由 agent 按需调用。
- chat / wiki 生成 / codemap 不再直接调 LLM API,统一走 claude_agent_sdk 的 Claude Code agent,
  提示词(chat / deep_research / codemap / wiki 页面与结构)全部为 deepwiki-open 原文。

已知简化(v1,详见 gh_puller/envs.py):
- repo 克隆用 git CLI(subprocess,不引 gitpython);远程 URL 的 token 注入沿用原后端三 host 方案。
- token 上限粗略估算(字符数/4,不引 tiktoken)。
- 语言仅 en/zh(与前端裁剪的 messages/{en,zh}.json 同步)。
- 模型仅 claude 单一 provider(由 CLAUDE_AGENT_MODEL 或 SDK 缺省)。
- 文件过滤为内嵌精简规则(与原 repo.json 全量规则有差异)。
- 聊天记忆由单次 agent 会话内承,无持久会话库。

wiki 生成进度中途落盘(deepwiki_taskstate_*,见文末任务状态机):结构确定后与每页
完成后各写一次,进程重启后同仓库再次提交即从落盘状态续跑(结构/已完成页不再重做)。

本模块为引擎+任务层(无 FastAPI 依赖,可被 apps/tui 等 CLI 复用):
HTTP 端点层(FastAPI app/SSE/WS)已迁至 apps/deepwiki-webui/server/app.py。
"""

import asyncio
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from . import envs, graphify
from .agent import cc_stream, llm_stream
from .utils import (
    Repo,
    RepoType,
    Task,
    TaskRegistry,
    TaskStatus,
    TaskSubmitResult,
    _estimate_tokens,
    _event,
    _extract_json,
    _find_readme_path,
    _phase,
    _sanitize_path_seg,
    _strip_markdown_fences,
    detect_default_branch,
    read_repo_file_tree,
)
from .utils import (
    _log as _utils_log,
)

# ---------------------------------------------------------------------------
# 日志与全局路径
# ---------------------------------------------------------------------------


# 进度日志走 stderr(同 graphify.py 约定);prefix 固定 [deepwiki]
_log = partial(_utils_log, prefix="deepwiki")


# wiki 缓存根目录(克隆根 repos 随 Repo 族已移至 utils._CLONE_ROOT)
_WIKI_CACHE_DIR = os.path.join(envs.DEEPWIKI_ROOT, "wikicache")
_WIKI_PREFIX = "deepwiki_cache_"
# 生成中途状态文件前缀(与 deepwiki_cache_ 区分,避免被 list_wiki_cache 当成成品扫描)
_WIKI_STATE_PREFIX = "deepwiki_taskstate_"
# 状态写锁:并发页生成器的落盘写串行化(asyncio 3.10+ 的 Lock 不再绑定 loop,模块级安全)
_state_write_lock = asyncio.Lock()
os.makedirs(_WIKI_CACHE_DIR, exist_ok=True)

# cc(agent)交付件目录名:wikicache/agent_cache/{proj}-{structure,page_<id>}.md
_AGENT_CACHE_DIRNAME = "agent_cache"
# 纯 LLM 路径单文件内联截断(字符)
_FILE_INLINE_CAP = 8000


def _proj_key(r: Any) -> str:
    """项目键 {type}_{owner}_{repo}(如 local_local_deepwiki-open,与 _wiki_cache_path 命名对齐)。"""
    return _sanitize_path_seg(f"{r.type}_{r.owner}_{r.repo}")


def _agent_cache_dir(r: Any) -> Path:
    return Path(_WIKI_CACHE_DIR) / _AGENT_CACHE_DIRNAME / _proj_key(r)


def _agent_cache_structure_path(r: Any) -> Path:
    """cc 结构交付文件:{proj}-structure.md。"""
    return _agent_cache_dir(r) / f"{_proj_key(r)}-structure.md"


def _agent_cache_page_path(r: Any, page_id: str) -> Path:
    """cc 页面交付文件:{proj}-<id>.md(id 形如 page-N 时直接采用,否则 {proj}-page_<id>;id 经安全化)。"""
    seg = _sanitize_path_seg(page_id)
    name = f"{_proj_key(r)}-{seg}" if seg.startswith("page-") else f"{_proj_key(r)}-page_{seg}"
    return _agent_cache_dir(r) / f"{name}.md"

# 页面/结构/图表生成所需的固定并发与重试(原 DEEPWIKI_* 缺省同式,见 envs.py;
# 页并发缺省 4:同时跑 4 个 agent 子进程,受 API 速限与机器内存约束)
# 统一从 envs 读取
_MAX_CONCURRENT_WIKI_TASKS = envs.MAX_CONCURRENT_WIKI_TASKS
_WIKI_PAGE_CONCURRENCY = max(1, envs.WIKI_PAGE_CONCURRENCY)
_WIKI_PAGE_RETRIES = max(0, envs.WIKI_PAGE_RETRIES)
_WIKI_TASK_TTL_SECONDS = envs.WIKI_TASK_TTL_SECONDS

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


# ---------------------------------------------------------------------------
# Pydantic 契约模型(与 deepwiki-open api/schemas 同形)
# ---------------------------------------------------------------------------

class RepoRequestBase(BaseModel):
    repo_url: str = Field(..., description="URL or local path of the repository")
    type: RepoType = Field("github", description="Repository type")
    token: str | None = Field(None, description="PAT for private repositories")
    provider: str = Field("claude", description="Model provider")
    model: str | None = Field(None, description="Model name for the provider")
    language: str = Field("en", description="Language for content generation")
    excluded_dirs: list[str] = Field(
        default_factory=list,
        description="List or newline-separated string of directories to exclude from processing",
    )
    excluded_files: list[str] = Field(
        default_factory=list,
        description="List or newline-separated string of file patterns to exclude from processing",
    )
    included_dirs: list[str] = Field(
        default_factory=list,
        description="List or newline-separated string of directories to include exclusively",
    )
    included_files: list[str] = Field(
        default_factory=list,
        description="List or newline-separated string of file patterns to include exclusively",
    )
    generator: str | None = Field(
        default=None,
        description=(
            "生成模式快照(cc/llm);由 _persist_state 落盘时以 envs.DEEPWIKI_GENERATOR 盖戳,"
            "续跑时据此校验,防止跨模式混用产物"
        ),
    )

    @field_validator(
        "excluded_dirs",
        "excluded_files",
        "included_dirs",
        "included_files",
        mode="before",
    )
    @classmethod
    def validate_path(cls, value: list[str] | str) -> list[str]:
        """list 或换行分隔字符串(原 schemas 同式,保留换行分隔兼容)。"""
        if isinstance(value, str):
            value = [p.strip() for p in value.split("\n") if p.strip()]
        return value


class ChatMessage(BaseModel):
    role: str  # 'user' or 'assistant'
    content: str
    mode: Literal["normal", "deep_research"] = Field(default="normal")


class ChatCompletionRequest(RepoRequestBase):
    messages: list[ChatMessage] = Field(..., description="List of chat messages")
    research_iteration: int = Field(
        default=1,
        ge=1,
        description="Current deep research iteration (1-based). Only used when the request is in deep_research mode.",
    )


class RepoPrepareRequest(RepoRequestBase):
    """POST /repo/prepare 的请求体(索引预热,无消息)。"""


class CodeMapCitation(BaseModel):
    file_path: str = Field(..., description="Repository-relative path of the source file")
    start_line: int | None = Field(None, description="1-based line range start")
    end_line: int | None = Field(None, description="1-based line range end")
    snippet: str = Field(
        "", description="Verbatim excerpt copied from the source (used to locate the range)"
    )


class CodeMapStep(BaseModel):
    id: str = Field(..., description="Human-facing id such as '1a', '1b', '2a'")
    label: str = Field(..., description="Short title of the step")
    code: str = Field("", description="Example code snippet illustrating the step")
    citation: CodeMapCitation | None = Field(None, description="Where this step's code comes from")


class CodeMapSection(BaseModel):
    id: str = Field(..., description="Section id such as '1', '2'")
    title: str = Field(..., description="Section title")
    guide: str = Field("", description="Prose guide for the section (filled in phase 2)")
    diagram: str = Field("", description="Mermaid diagram source (filled in phase 2)")
    steps: list[CodeMapStep] = Field(default_factory=list)


class CodeMap(BaseModel):
    title: str = Field(..., description="Overall codemap title")
    summary: str = Field("", description="Introductory summary")
    sections: list[CodeMapSection] = Field(default_factory=list)


class CodeMapRequest(RepoRequestBase):
    question: str = Field(..., description="The user's how-to / usage question")


class WikiTaskRequest(RepoRequestBase):
    owner: str
    repo: str
    comprehensive: bool = Field(True, description="Comprehensive vs concise wiki")

    @property
    def repo_key(self) -> str:
        return f"{self.type}_{self.owner}_{self.repo}"


# 通用化:直接别名(task_id/status/from_cache/resumed 语义同 utils.TaskSubmitResult)
WikiTaskSubmitResult = TaskSubmitResult


class RepoInfo(BaseModel):
    owner: str
    repo: str
    type: str
    token: str | None = None
    localPath: str | None = None
    repoUrl: str | None = None


class WikiPage(BaseModel):
    id: str
    title: str
    content: str
    filePaths: list[str]
    importance: str  # 'high' | 'medium' | 'low'
    relatedPages: list[str]


class WikiSection(BaseModel):
    id: str
    title: str
    pages: list[str]
    subsections: list[str] | None = None


class WikiStructureModel(BaseModel):
    id: str
    title: str
    description: str
    pages: list[WikiPage]
    sections: list[WikiSection] | None = None
    rootSections: list[str] | None = None


class WikiCacheData(BaseModel):
    wiki_structure: WikiStructureModel
    generated_pages: dict[str, WikiPage]
    repo_url: str | None = None  # compatible for old cache
    repo: RepoInfo | None = None
    provider: str | None = None
    model: str | None = None
    generator: str | None = None  # 生成模式快照(cc/llm);缓存命中时校验,防跨模式混用


class WikiTaskState(BaseModel):
    """生成中途落盘状态:同仓库再次提交时据此续跑(结构与已完成页不再重生成)。"""

    version: int = 1
    request: WikiTaskRequest  # 全量快照:续跑沿用首次输入(comprehensive/model/filters 等)
    status: TaskStatus  # 仅审计;恢复时按 wiki_structure 有无重新映射
    wiki_structure: WikiStructureModel | None = None
    generated_pages: dict[str, WikiPage] = Field(default_factory=dict)
    default_branch: str = "main"
    submitted_at: int  # 保留原始提交时间
    error: str | None = None


class WikiExportRequest(BaseModel):
    repo_url: str = Field(..., description="URL of the repository")
    pages: list[WikiPage] = Field(..., description="List of wiki pages to export")
    format: Literal["markdown", "json"] = Field(..., description="Export format")


class ProcessedProjectEntry(BaseModel):
    id: str  # Filename
    owner: str
    repo: str
    name: str  # owner/repo
    repo_type: str
    submittedAt: int
    language: str


class WikiTaskSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    owner: str
    repo: str
    repo_type: str
    language: str
    status: TaskStatus
    pages_done: int = Field(default=0, ge=0)
    pages_total: int = Field(default=0, ge=0)
    current_page_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    submitted_at: int = Field(..., ge=0, validation_alias="submittedAt")

    @computed_field
    @property
    def name(self) -> str:
        return f"{self.owner}/{self.repo}"


class WikiTaskStatus(WikiTaskSummary):
    wiki_structure: WikiStructureModel | None = None


class Model(BaseModel):
    id: str
    name: str


class Provider(BaseModel):
    id: str
    name: str
    models: list[Model] = Field(default_factory=list)
    supportsCustomModel: bool = Field(False, description="Whether this provider supports custom models")


class ModelConfig(BaseModel):
    providers: list[Provider] = Field(..., description="Available model providers")
    defaultProvider: str = Field(..., description="ID of the default provider")


class AuthorizationConfig(BaseModel):
    code: str = Field(..., description="Authorization code")





def _locate_snippet(text: str, snippet: str) -> tuple[int, int] | None:
    """在文本中定位 snippet 的 1-based 行号范围(LLM 给的行号不可靠,snippet 为权威)。"""
    snippet = snippet.strip("\n")
    if not snippet:
        return None
    pos = text.find(snippet)
    if pos != -1:
        start = text.count("\n", 0, pos) + 1
        return start, start + snippet.count("\n")
    first = next((ln.strip() for ln in snippet.splitlines() if ln.strip()), "")
    if first:
        idx = text.find(first)
        if idx != -1:
            start = text.count("\n", 0, idx) + 1
            return start, start + snippet.count("\n")
    return None


def _ground_citations(codemap: CodeMap, repo_dir: str) -> None:
    """用真实源码里的 snippet 位置覆盖每条引用的行号范围。"""
    file_cache: dict[str, str | None] = {}
    for section in codemap.sections:
        for step in section.steps:
            cit = step.citation
            if not cit or not cit.snippet or not cit.file_path:
                continue
            if cit.file_path not in file_cache:
                path = os.path.join(repo_dir, cit.file_path)
                try:
                    with open(path, encoding="utf-8") as f:
                        file_cache[cit.file_path] = f.read()
                except (OSError, UnicodeDecodeError):
                    file_cache[cit.file_path] = None
            text = file_cache[cit.file_path]
            if not text:
                continue
            loc = _locate_snippet(text, cit.snippet)
            if loc:
                cit.start_line, cit.end_line = loc


# ---------------------------------------------------------------------------
# graphify 对接:索引(extract)与 graphify_query 工具
# ---------------------------------------------------------------------------


def _graph_dir(repo: Repo) -> Path:
    """单仓库图产物根(extract 的 out_dir):graphify/{type}_{name},无日期层,路径稳定以支持已缓存即跳过。"""
    return Path(envs.DEEPWIKI_ROOT) / "graphify" / f"{repo.repo_type}_{repo.name}"


def _graph_path(repo: Repo) -> Path:
    """graph.json 规范路径(extract 的 out_dir 即最终目录,无 graphify-out 层)。"""
    return _graph_dir(repo) / "graph.json"


def _index_ready(repo: Repo) -> bool:
    """索引完成信号 = graph.json 已存在(复用 graphify._load_graph 的存在性语义)。"""
    return _graph_path(repo).exists()


async def _run_extract(repo: Repo, request: RepoRequestBase) -> dict:
    """graphify.extract 建图(code_only 纯本地 AST,无 key 可跑);失败返回错误态 dict,不抛。"""
    extra_excludes: list[str] | None = None
    if request.excluded_dirs or request.excluded_files:
        extra_excludes = [*request.excluded_dirs, *request.excluded_files]
    return await asyncio.to_thread(
        graphify.extract,
        path=repo.save_path,
        code_only=True,
        out_dir=_graph_dir(repo),
        extra_excludes=extra_excludes,
    )


def _graphify_server(repo: Repo):
    """进程内 MCP server:把 graphify.query 封装为 graphify_query 工具(闭包绑定图路径)。"""

    graph_path = _graph_path(repo)

    @tool(
        "graphify_query",
        "查询仓库代码图谱,返回与该问题相关的代码子图(函数/类/调用关系文本),"
        "并带 `Source: <文件路径> L<行号>` 形式的来源标记。适合获取代码结构与行号引用。",
        {"question": str},
    )
    async def graphify_query(args: dict) -> dict:
        try:
            question = (args.get("question") or "").strip()
            result = graphify.query(question, graph_path=graph_path)
            text = result.get("answer") or ""
        except Exception as exc:
            text = f"查询图谱失败: {type(exc).__name__}: {exc}"
        return {"content": [{"type": "text", "text": text.strip() or "(图谱无匹配结果)"}]}

    return create_sdk_mcp_server("graphify", tools=[graphify_query])


# ---------------------------------------------------------------------------
# Claude agent 封装(SDK 进程内 MCP 工具 + 流式/整收)
# ---------------------------------------------------------------------------


def _agent_options(
    system_prompt: str,
    repo: Repo | None,
    model: str | None = None,
    *,
    agent_output_dir: str | None = None,
    agent_write_mode: bool = False,
) -> ClaudeAgentOptions:
    """组装 agent 选项;repo 非空时挂 graphify 工具并把 cwd 固定到仓库根
    (SDK 缺省 = 进程 cwd,曾导致 agent 串到 gh-puller 并把 docs 写入其中);
    model 优先级:envs.CLAUDE_AGENT_MODEL > 请求 model > SDK 缺省。
    agent_write_mode(cc 交付件落盘)追加 agent_cache 写目录(add_dirs)与
    acceptEdits,工具集放开 Read/Grep/Glob/Write 供 agent 自读代码并落盘成品。"""
    kwargs: dict[str, Any] = {
        "system_prompt": system_prompt,
        "include_partial_messages": True,
    }
    model_name = envs.CLAUDE_AGENT_MODEL or model or ""
    if model_name:
        kwargs["model"] = model_name
    if repo is not None:
        kwargs["cwd"] = os.path.abspath(repo.save_path)
        kwargs["mcp_servers"] = {"graphify": _graphify_server(repo)}
        tools = ["graphify_query", "mcp__graphify__graphify_query"]
        if agent_write_mode:
            if agent_output_dir:
                kwargs["add_dirs"] = [os.path.abspath(agent_output_dir)]
            kwargs["permission_mode"] = "acceptEdits"
            tools = ["Read", "Grep", "Glob", "Write", *tools]
        kwargs["allowed_tools"] = tools
    return ClaudeAgentOptions(**kwargs)


async def _agent_stream(
    system_prompt: str, prompt: str, repo: Repo | None = None, model: str | None = None,
    label: str | None = None, *, run_id: str | None = None,
    context: list[dict] | None = None, retry: dict | None = None,
):
    """agent 流式应答:转发文本增量,语义与旧漏斗逐字节一致(见 agent.cc_stream);
    label 作为监控会话名(chat:/codemap:/wiki: 前缀区分用途);run_id 关联任务级
    会话组;context = 上下文注入/修改说明事件;retry = 重试元数据(见 events.py)。"""
    options = _agent_options(system_prompt, repo, model)
    async for chunk in cc_stream(options, prompt, session_name=label, run_id=run_id,
                                 context=context, retry=retry):
        yield chunk


def _agent_note() -> str:
    """注入到 user 消息的指引段(供 agent 知道用 graphify_query 工具获取带行号的代码上下文)。"""
    return (
        "<note>You may use the graphify_query tool to inspect this repository's code graph "
        "whenever you need code context or exact file/line references for citations. "
        "Its results mark sources as `Source: <file path> L<line number>`.</note>\n\n"
    )


async def _agent_text(
    system_prompt: str, prompt: str, repo: Repo | None = None, model: str | None = None,
    label: str | None = None, *, run_id: str | None = None,
    context: list[dict] | None = None, retry: dict | None = None,
) -> str:
    """agent 整收应答(用于 wiki 结构/页面、codemap 两阶段)。"""
    parts: list[str] = []
    async for chunk in _agent_stream(system_prompt, prompt, repo, model, label,
                                     run_id=run_id, context=context, retry=retry):
        parts.append(chunk)
    return "".join(parts)


async def _agent_write_file(
    system_prompt: str, prompt: str, repo: Repo, model: str | None, out_path: Path,
    label: str | None = None, *, run_id: str | None = None,
    context: list[dict] | None = None, retry: dict | None = None,
) -> str:
    """cc 交付件统一落盘口:提示词只给路径,agent 用自身工具读码并把成品写入 out_path;
    产生以文件为准(流式文本仅作监控/错误检测),未产出文件即任务失败。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)  # add_dirs 指向目录须先存在(agent Write 可直接落)
    options = _agent_options(
        system_prompt, repo, model,
        agent_output_dir=str(out_path.parent), agent_write_mode=True,
    )
    async for _ in cc_stream(options, prompt, session_name=label, run_id=run_id,
                             context=context, retry=retry):
        pass
    text = await asyncio.to_thread(out_path.read_text, encoding="utf-8")
    if not text.strip():
        raise RuntimeError(f"agent 未产出交付文件: {out_path}")
    return text


# ---------------------------------------------------------------------------
# 引用后处理(移植 api/services/wiki/content.py)
# ---------------------------------------------------------------------------


class RepoUrlContext:
    """把仓库相对路径转成 web URL 所需的一切(local/无 URL → 返回裸路径)。"""

    def __init__(self, type: str, repo_url: str | None, default_branch: str):
        self.type = type
        self.repo_url = repo_url
        self.default_branch = default_branch


def generate_file_url(file_path: str, ctx: RepoUrlContext) -> str:
    if ctx.type == "local" or not ctx.repo_url:
        return file_path
    if ctx.type == "github":
        return f"{ctx.repo_url}/blob/{ctx.default_branch}/{file_path}"
    if ctx.type == "gitlab":
        return f"{ctx.repo_url}/-/blob/{ctx.default_branch}/{file_path}"
    if ctx.type == "bitbucket":
        return f"{ctx.repo_url}/src/{ctx.default_branch}/{file_path}"
    return file_path


def _escape_label(s: str) -> str:
    """转义 '[' / ']' 使路径能作为 Markdown 链接普通文本渲染。"""
    return re.sub(r"([\[\]])", r"\\\1", s)


def _line_anchor(repo_type: str, start: str | None, end: str | None) -> str:
    if not start:
        return ""
    if repo_type == "github":
        return f"#L{start}-L{end}" if end else f"#L{start}"
    if repo_type == "gitlab":
        return f"#L{start}-{end}" if end else f"#L{start}"
    if repo_type == "bitbucket":
        return f"#lines-{start}:{end}" if end else f"#lines-{start}"
    return ""


def _citation_link(path: str, start: str | None, end: str | None, ctx: RepoUrlContext) -> str | None:
    """把 `path[:start[-end]]` 解析为 Markdown 链接;local/未知 host 返回 None。"""
    url = generate_file_url(path, ctx)
    if url == path:
        return None
    line_part = (f":{start}-{end}" if end else f":{start}") if start else ""
    anchor = _line_anchor(ctx.type, start, end)
    return f"[{_escape_label(path)}{line_part}]({url}{anchor})"


_DETAILS_RE = re.compile(
    r"<details>\s*<summary>\s*Relevant source files\s*</summary>[\s\S]*?</details>",
    re.IGNORECASE,
)
_GENERIC_RE = re.compile(r"\[([^\[\]\s()]+?\.[A-Za-z0-9]+)(?::(\d+)(?:-(\d+))?)?\]\(\)")
_PREFIXED_RE = re.compile(
    r"\[(Sources?|Source):\s*([^\[\]\s():]+?)(?::(\d+)(?:-(\d+))?)?\]\(\)",
    re.IGNORECASE,
)
_STRAY_PARENS_RE = re.compile(r"(\]\([^)\s]+\))\(\)")


def post_process_wiki_content(content: str, file_paths: list[str], ctx: RepoUrlContext) -> str:
    """后处理模型产出的 wiki markdown:重建 <details> 块、解析各种空括号引用为真实链接。"""
    processed = content

    # 1. 用已知文件列表重建 <details> 块
    if file_paths:
        links = "\n".join(
            f"- [{_escape_label(p)}]({generate_file_url(p, ctx)})" for p in file_paths
        )
        details_block = (
            "<details>\n"
            "<summary>Relevant source files</summary>\n\n"
            "The following files were used as context for generating this wiki page:\n\n"
            f"{links}\n"
            "</details>"
        )
        if _DETAILS_RE.search(processed):
            processed = _DETAILS_RE.sub(lambda _m: details_block, processed)
        else:
            processed = f"{details_block}\n\n{processed}"

    # 2. 按已知 filePaths 解析空引用(最长优先)
    if file_paths:
        alternation = "|".join(re.escape(p) for p in sorted(file_paths, key=len, reverse=True))
        citation_re = re.compile(r"\[(" + alternation + r")(?::(\d+)(?:-(\d+))?)?\]\(\)")

        def _repl_known(m: re.Match) -> str:
            link = _citation_link(m.group(1), m.group(2), m.group(3), ctx)
            return link if link is not None else m.group(0)

        processed = citation_re.sub(_repl_known, processed)

    # 3. 剩余形如文件路径的空引用
    def _repl_generic(m: re.Match) -> str:
        link = _citation_link(m.group(1), m.group(2), m.group(3), ctx)
        return link if link is not None else m.group(0)

    processed = _GENERIC_RE.sub(_repl_generic, processed)

    # 4. `[Sources: 裸文件名:行]()` 通过 basename 查回全路径
    if file_paths:
        by_basename: dict[str, str] = {}
        for p in file_paths:
            by_basename.setdefault(p.rsplit("/", 1)[-1], p)

        def _repl_prefixed(m: re.Match) -> str:
            prefix, token, start, end = m.group(1), m.group(2), m.group(3), m.group(4)
            full_path = token if "/" in token else by_basename.get(token)
            if not full_path:
                return m.group(0)
            link = _citation_link(full_path, start, end, ctx)
            if link is None:
                return m.group(0)
            return f"{prefix}: {link}"

        processed = _PREFIXED_RE.sub(_repl_prefixed, processed)

    # 5. 去掉完成链接后的冗余空 "()"
    processed = _STRAY_PARENS_RE.sub(r"\1", processed)
    return processed


# ---------------------------------------------------------------------------
# wiki 结构解析(移植 api/services/wiki/structure.py)
# ---------------------------------------------------------------------------


def _normalize_importance(value: str | None) -> str:
    v = (value or "").strip().lower()
    return v if v in ("high", "medium", "low") else "medium"


def _page_from_element(el: ET.Element, index: int) -> WikiPage:
    return WikiPage(
        id=el.get("id") or f"page-{index + 1}",
        title=(el.findtext("title") or "").strip(),
        content="",
        filePaths=[e.text.strip() for e in el.iter("file_path") if e.text and e.text.strip()],
        importance=_normalize_importance(el.findtext("importance")),
        relatedPages=[e.text.strip() for e in el.iter("related") if e.text and e.text.strip()],
    )


def _pages_via_regex(xml_text: str) -> list[WikiPage]:
    """严格 XML 解析失败或零页面时的正则兜底。"""
    pages: list[WikiPage] = []
    for i, block in enumerate(re.findall(r"<page\b[\s\S]*?</page>", xml_text)):
        pid = re.search(r'<page\s+id="([^"]+)"', block)
        title = re.search(r"<title>([\s\S]*?)</title>", block)
        importance = re.search(r"<importance>([\s\S]*?)</importance>", block)
        file_paths = [m.strip() for m in re.findall(r"<file_path>([\s\S]*?)</file_path>", block) if m.strip()]
        related = [m.strip() for m in re.findall(r"<related>([\s\S]*?)</related>", block) if m.strip()]
        pages.append(
            WikiPage(
                id=pid.group(1) if pid else f"page-{i + 1}",
                title=title.group(1).strip() if title else "",
                content="",
                filePaths=file_paths,
                importance=_normalize_importance(importance.group(1) if importance else None),
                relatedPages=related,
            )
        )
    return pages


def _parse_sections(root: ET.Element) -> tuple[list[WikiSection], list[str]]:
    sections: list[WikiSection] = []
    referenced: set[str] = set()
    for i, el in enumerate(root.iter("section")):
        sid = el.get("id") or f"section-{i + 1}"
        subs = [e.text.strip() for e in el.iter("section_ref") if e.text and e.text.strip()]
        sections.append(
            WikiSection(
                id=sid,
                title=(el.findtext("title") or "").strip(),
                pages=[e.text.strip() for e in el.iter("page_ref") if e.text and e.text.strip()],
                subsections=subs or None,
            )
        )
        referenced.update(subs)
    root_sections = [s.id for s in sections if s.id not in referenced]
    return sections, root_sections


def _first_group(pattern: str, text: str) -> str:
    m = re.search(pattern, text)
    return m.group(1).strip() if m else ""


def _sections_via_regex(xml_text: str) -> tuple[list[WikiSection], list[str]]:
    """严格解析失败时恢复完整 <section> 块(镜像 _parse_sections)。"""
    sections: list[WikiSection] = []
    referenced: set[str] = set()
    for i, block in enumerate(re.findall(r"<section\b[\s\S]*?</section>", xml_text)):
        sid = re.search(r'<section\s+id="([^"]+)"', block)
        title = re.search(r"<title>([\s\S]*?)</title>", block)
        page_refs = [m.strip() for m in re.findall(r"<page_ref>([\s\S]*?)</page_ref>", block) if m.strip()]
        subs = [m.strip() for m in re.findall(r"<section_ref>([\s\S]*?)</section_ref>", block) if m.strip()]
        sections.append(
            WikiSection(
                id=sid.group(1) if sid else f"section-{i + 1}",
                title=title.group(1).strip() if title else "",
                pages=page_refs,
                subsections=subs or None,
            )
        )
        referenced.update(subs)
    root_sections = [s.id for s in sections if s.id not in referenced]
    return sections, root_sections


def parse_wiki_structure(text: str, comprehensive: bool) -> WikiStructureModel:
    """解析模型产出的 XML 结构;容错:剥 markdown fence、转义裸 &、正则兜底;无 <wiki_structure> 抛 ValueError。"""
    text = re.sub(r"^```(?:xml)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"```\s*$", "", text)

    match = re.search(r"<wiki_structure>[\s\S]*?</wiki_structure>", text)
    if match:
        xml_text = match.group(0)
    else:
        # 截断响应:从开标签救取到文末(补合成闭合),让下方正则兜底恢复完整块
        open_match = re.search(r"<wiki_structure>[\s\S]*", text)
        if not open_match:
            raise ValueError("No valid <wiki_structure> XML found in response")
        _log("响应疑似被截断(缺 </wiki_structure>),按完整块救取")
        xml_text = f"{open_match.group(0)}\n</wiki_structure>"

    xml_text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", xml_text)
    xml_text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", xml_text)

    root: ET.Element | None = None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        _log(f"严格 XML 解析失败,用正则兜底: {e}")

    if root is not None:
        title = root.findtext("title") or ""
        description = root.findtext("description") or ""
        pages = [_page_from_element(el, i) for i, el in enumerate(root.iter("page"))]
    else:
        # 头版 <title>/<description> 最先出现;页面级同名标签在后面
        title = _first_group(r"<title>([\s\S]*?)</title>", xml_text)
        description = _first_group(r"<description>([\s\S]*?)</description>", xml_text)
        pages = []

    if not pages:
        _log("XML 解析无页面,用正则兜底")
        pages = _pages_via_regex(xml_text)

    sections: list[WikiSection] = []
    root_sections: list[str] = []
    if comprehensive:
        if root is not None:
            sections, root_sections = _parse_sections(root)
        else:
            sections, root_sections = _sections_via_regex(xml_text)

    return WikiStructureModel(
        id="wiki",
        title=title.strip(),
        description=description.strip(),
        pages=pages,
        sections=sections,
        rootSections=root_sections,
    )


# ---------------------------------------------------------------------------
# wiki 缓存与导出(移植 api/services/wiki/io.py;anyio IO → asyncio.to_thread + json)
# ---------------------------------------------------------------------------


def _wiki_cache_path(owner: str, repo: str, repo_type: str, language: str) -> str:
    filename = f"{_WIKI_PREFIX}{repo_type}_{owner}_{repo}_{language}.json"
    return os.path.join(_WIKI_CACHE_DIR, filename)


def wiki_cache_exists(owner: str, repo: str, repo_type: str, language: str) -> bool:
    return os.path.exists(_wiki_cache_path(owner, repo, repo_type, language))


async def read_wiki_cache(owner: str, repo: str, repo_type: str, language: str) -> WikiCacheData | None:
    if not wiki_cache_exists(owner, repo, repo_type, language):
        return None
    path = _wiki_cache_path(owner, repo, repo_type, language)
    try:
        text = await asyncio.to_thread(lambda: Path(path).read_text(encoding="utf-8"))
        return WikiCacheData.model_validate_json(text)
    except Exception:
        _log(f"读取 wiki 缓存失败: {path}")
        return None


async def save_wiki_cache(
    owner: str, repo: str, repo_type: str, language: str, wiki_cache: WikiCacheData
) -> bool:
    path = _wiki_cache_path(owner, repo, repo_type, language)
    try:
        await asyncio.to_thread(
            lambda: Path(path).write_text(wiki_cache.model_dump_json(), encoding="utf-8")
        )
        return True
    except OSError:
        _log(f"写 wiki 缓存失败: {path}")
        return False


async def delete_wiki_cache(owner: str, repo: str, repo_type: str, language: str) -> bool:
    path = _wiki_cache_path(owner, repo, repo_type, language)
    state_path = _wiki_state_path(owner, repo, repo_type, language)
    deleted = False
    if os.path.exists(path):
        os.remove(path)
        deleted = True
    if os.path.exists(state_path):  # 删除缓存同时清续跑状态,避免裸 state 无清理途径
        os.remove(state_path)
        deleted = True
    return deleted


def _wiki_state_path(owner: str, repo: str, repo_type: str, language: str) -> str:
    filename = f"{_WIKI_STATE_PREFIX}{repo_type}_{owner}_{repo}_{language}.json"
    return os.path.join(_WIKI_CACHE_DIR, filename)


async def write_wiki_task_state(state: WikiTaskState) -> bool:
    """原子写生成状态(先写 .tmp 再 os.replace,崩溃不产生半截文件)。"""
    path = _wiki_state_path(
        state.request.owner, state.request.repo, state.request.type, state.request.language
    )
    tmp = f"{path}.tmp"
    try:
        await asyncio.to_thread(
            lambda: Path(tmp).write_text(state.model_dump_json(), encoding="utf-8")
        )
        os.replace(tmp, path)
        return True
    except OSError as e:
        _log(f"写生成状态失败: {path} - {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


async def read_wiki_task_state(
    owner: str, repo: str, repo_type: str, language: str
) -> WikiTaskState | None:
    """读取生成状态;无文件或解析失败 → None(自动降级为全新生成)。"""
    path = _wiki_state_path(owner, repo, repo_type, language)
    if not os.path.exists(path):
        return None
    try:
        text = await asyncio.to_thread(lambda: Path(path).read_text(encoding="utf-8"))
        return WikiTaskState.model_validate_json(text)
    except Exception:
        _log(f"读取生成状态失败: {path}")
        return None


async def delete_wiki_task_state(owner: str, repo: str, repo_type: str, language: str) -> bool:
    path = _wiki_state_path(owner, repo, repo_type, language)
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True


async def _persist_state(task: "WikiTask") -> None:
    """把任务当前进度落盘(结构/已完成页/状态);并发写由模块锁串行。"""
    # 写盘即盖戳生成模式:续跑(load_resume)据此校验,跨模式切换后丢弃旧快照
    task.request.generator = envs.DEEPWIKI_GENERATOR
    state = WikiTaskState(
        request=task.request,
        status=task.status,
        wiki_structure=task.wiki_structure,
        generated_pages=dict(task.generated_pages),  # 浅拷贝快照:WikiPage 只整替换、不原地改
        default_branch=task.default_branch,
        submitted_at=task.submitted_at,
        error=task.error,
    )
    async with _state_write_lock:
        await write_wiki_task_state(state)


async def list_wiki_cache() -> list[WikiTaskSummary]:
    """扫描缓存目录,按文件名拆解为 (type, owner, repo, language) 摘要条目。"""
    if not os.path.exists(_WIKI_CACHE_DIR):
        return []
    entries: list[WikiTaskSummary] = []
    for filename in await asyncio.to_thread(os.listdir, _WIKI_CACHE_DIR):
        if not (filename.startswith(_WIKI_PREFIX) and filename.endswith(".json")):
            continue
        file_path = os.path.join(_WIKI_CACHE_DIR, filename)
        try:
            stats = await asyncio.to_thread(os.stat, file_path)
            repo_type, owner, *repo, language = (
                os.path.splitext(filename)[0].removeprefix(_WIKI_PREFIX).split("_")
            )
            entries.append(
                WikiTaskSummary(
                    id=filename,
                    owner=owner,
                    repo="_".join(repo),
                    repo_type=repo_type,
                    language=language,
                    submitted_at=int(stats.st_mtime * 1000),
                    status=TaskStatus.COMPLETED,
                )
            )
        except Exception:
            _log(f"解析缓存文件失败: {file_path}")
    return entries


async def list_processed_projects() -> list[ProcessedProjectEntry]:
    project_entries = [
        ProcessedProjectEntry(
            id=wiki.id,
            owner=wiki.owner,
            repo=wiki.repo,
            name=wiki.name,
            repo_type=wiki.repo_type,
            submittedAt=wiki.submitted_at,
            language=wiki.language,
        )
        for wiki in await list_wiki_cache()
    ]
    project_entries.sort(key=lambda p: p.submittedAt, reverse=True)
    return project_entries


def export_wiki(
    repo_url: str,
    pages: list[WikiPage],
    format: Literal["json", "markdown"],
    timestamp: datetime | None = None,
) -> str:
    """导出 wiki 为 markdown/json 字符串(与 io.py 同式)。"""
    dt = timestamp or datetime.now()
    if format == "json":
        export_data = {
            "metadata": {
                "repository": repo_url,
                "generated_at": dt.isoformat(),
                "page_count": len(pages),
            },
            "pages": [page.model_dump() for page in pages],
        }
        return json.dumps(export_data, indent=2)
    if format == "markdown":
        markdown = f"# Wiki Documentation for {repo_url}\n\n"
        markdown += f"Generated on: {dt.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        markdown += "## Table of Contents\n\n"
        for page in pages:
            markdown += f"- [{page.title}](#{page.id})\n"
        markdown += "\n"
        for page in pages:
            markdown += f"<a id='{page.id}'></a>\n\n"
            markdown += f"## {page.title}\n\n"
            if page.relatedPages:
                related_titles = []
                for related_id in page.relatedPages:
                    related_page = next((p for p in pages if p.id == related_id), None)
                    if related_page:
                        related_titles.append(f"[{related_page.title}](#{related_id})")
                if related_titles:
                    markdown += "### Related Pages\n\n"
                    markdown += "Related topics: " + ", ".join(related_titles) + "\n\n"
            markdown += f"{page.content}\n\n"
            markdown += "---\n\n"
        return markdown
    raise NotImplementedError(
        f"Exporting wiki to format {format} is not supported. Must be one of 'markdown' or 'json'."
    )


# ---------------------------------------------------------------------------
# chat 服务(agent 版;协议/错误语义与原 research.py 对齐)
# ---------------------------------------------------------------------------


class RepoNotIndexedError(ValueError):
    """chat/codemap 请求到达时仓库尚未建图。"""


def _require_indexed(request: RepoRequestBase) -> Repo:
    """前置校验:仓库必须已建图;返回 Repo 句柄(端点层调用,失败在进生成器前即抛)。"""
    repo = Repo(request.repo_url, request.type, access_token=request.token)
    if not _index_ready(repo):
        raise RepoNotIndexedError(
            f"仓库尚未索引: {repo.name}。请先通过 /repo/prepare 建立代码图谱。"
        )
    return repo


def _format_request_fmt(request: RepoRequestBase) -> dict:
    """提示词格式化用的公共字段。"""
    return {
        "repo_type": request.type,
        "repo_url": request.repo_url,
        "repo_name": Repo(request.repo_url, request.type).name,
        "language_name": _language_name(request.language or "en"),
    }


async def chat_stream(request: ChatCompletionRequest):
    """一次 chat 请求的流式应答(纯文本 chunk 序列,前后端协议同原 research_chat);恒 agent 路。"""
    async for chunk in _agent_pipeline().chat_stream(request):
        yield chunk



# ---------------------------------------------------------------------------
# codemap 服务(agent 版两阶段;NDJSON 事件协议同原)
# ---------------------------------------------------------------------------

def _codemap_note() -> str:
    """codemap 指引:先查图谱再构造,引用行号取自 Source 标记。"""
    return (
        "<note>Before answering, use the graphify_query tool to inspect the repository "
        "code graph (its result carries `Source: <file path> L<line>` markers). "
        "When filling citation.file_path / start_line / end_line, use those paths and line "
        "numbers, and make the 'snippet' a verbatim substring of the code shown in the result.</note>\n\n"
    )


async def generate_codemap(request: CodeMapRequest):
    """两阶段 codemap 生成(骨架 → 指南/图),NDJSON 事件流;阶段失败语义与原相同;恒 agent 路。"""
    async for ev in _agent_pipeline().generate_codemap(request):
        yield ev


# ---------------------------------------------------------------------------
# wiki 任务状态机(移植 api/services/wiki/tasks.py;RAG → agent + graphify)
# 状态机/去重/并发/TTL 基类在 utils.TaskRegistry,此处仅注入 wiki 特化钩子
# ---------------------------------------------------------------------------


class WikiTask(Task):
    """单个仓库生成任务的内存态状态(状态/错误/提交时间/运行时任务由 Task 基类承担)。"""

    request: WikiTaskRequest
    pages_done: int = 0
    current_page_ids: list[str] = Field(default_factory=list)
    generated_pages: dict[str, WikiPage] = Field(default_factory=dict)  # 完成页就地累积(续跑=已生成页)
    wiki_structure: WikiStructureModel | None = None
    default_branch: str = "main"  # 确定结构时设置,用于文件 URL

    @computed_field
    @property
    def pages_total(self) -> int:
        if self.wiki_structure is not None:
            return len(self.wiki_structure.pages)
        return 0

    @classmethod
    def from_wiki_request(cls, request: WikiTaskRequest) -> "WikiTask":
        return cls(request=request)

    @property
    def repo_key(self) -> str:
        return self.request.repo_key

    @property
    def key(self) -> str:
        return self.repo_key

    def to_status(self) -> WikiTaskStatus:
        r = self.request
        return WikiTaskStatus(
            id=self.repo_key,
            owner=r.owner,
            repo=r.repo,
            repo_type=r.type,
            language=r.language,
            status=self.status,
            pages_done=self.pages_done,
            pages_total=self.pages_total,
            current_page_ids=self.current_page_ids,
            wiki_structure=self.wiki_structure,
            error=self.error,
            submitted_at=self.submitted_at,
        )

    def to_summary(self) -> WikiTaskSummary:
        r = self.request
        return WikiTaskSummary(
            id=self.repo_key,
            owner=r.owner,
            repo=r.repo,
            repo_type=r.type,
            language=r.language,
            status=self.status,
            pages_done=self.pages_done,
            pages_total=self.pages_total,
            current_page_ids=self.current_page_ids,
            error=self.error,
            submitted_at=self.submitted_at,
        )


class WikiTaskRegistry(TaskRegistry):
    """wiki 专属提交语义(缓存胜/续跑/生成器执行)经钩子注入;TTL 读模块全局供测试 monkeypatch。"""

    async def run(self, task: WikiTask) -> None:
        await generate_repo_wiki(task)  # 调用时经模块全局解析(monkeypatch 生效)

    async def is_cached(self, task: WikiTask) -> bool:
        r = task.request
        if not wiki_cache_exists(owner=r.owner, repo=r.repo, repo_type=r.type, language=r.language):
            return False
        cache = await read_wiki_cache(r.owner, r.repo, r.type, r.language)
        if cache is None:
            return False
        if cache.generator != envs.DEEPWIKI_GENERATOR:
            _log(
                f"成品缓存生成模式不匹配({cache.generator!r} vs {envs.DEEPWIKI_GENERATOR!r}),"
                f"忽略并重新生成: {r.owner}/{r.repo}"
            )
            return False
        return True

    async def on_cache_hit(self, task: WikiTask) -> None:
        r = task.request
        await delete_wiki_task_state(r.owner, r.repo, r.type, r.language)

    async def load_resume(self, task: WikiTask) -> WikiTask | None:
        r = task.request
        state = await read_wiki_task_state(r.owner, r.repo, r.type, r.language)
        if state is None:
            return None
        # 快照含首次输入(含 generator):生成模式不同则丢弃旧进度并清状态,避免跨模式混用产物
        if state.request.generator != envs.DEEPWIKI_GENERATOR:
            _log(
                f"续跑状态生成模式不匹配({state.request.generator!r} vs {envs.DEEPWIKI_GENERATOR!r}),"
                f"丢弃旧快照: {r.owner}/{r.repo}"
            )
            await delete_wiki_task_state(r.owner, r.repo, r.type, r.language)
            return None
        return WikiTask(
            request=state.request,
            status=(
                TaskStatus.GENERATING
                if state.wiki_structure is not None
                else TaskStatus.DETERMINING_STRUCTURE
            ),
            pages_done=len(state.generated_pages),
            wiki_structure=state.wiki_structure,
            default_branch=state.default_branch,
            submitted_at=state.submitted_at,  # 保留原始提交时间
            generated_pages=state.generated_pages,
        )

    def _ttl_seconds(self) -> float:
        # call-time 读模块全局:tests monkeypatch deepwiki._WIKI_TASK_TTL_SECONDS
        return _WIKI_TASK_TTL_SECONDS


registry = WikiTaskRegistry(
    max_concurrent=_MAX_CONCURRENT_WIKI_TASKS,
    ttl_seconds=_WIKI_TASK_TTL_SECONDS,
)


# ---------------------------------------------------------------------------
# 双路包装类:llm 路与 cc(agent)路对外 API 的统一入口。
# 分派:wiki 生成(结构/页面)按 envs.DEEPWIKI_GENERATOR 经 _wiki_pipeline() 选择;
#       chat / codemap 恒 agent 路(经 _agent_pipeline(),不读 envs)。
# 共用构建件与低层支撑保持模块级 helper(共享提示词段、XML 解析/引用后处理、
# 图索引、任务状态机、agent/llm 流式通道);类方法体内一律以模块全局名引用
# cc_stream / llm_stream / _agent_write_file / envs.*,保证调用时动态解析
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

    def needs_structure_regenerate(self, task: WikiTask) -> bool:
        """structure 交付文件被删即强制重生成(续跑失效)。"""
        return not _agent_cache_structure_path(task.request).exists()

    async def determine_structure(
        self, task: WikiTask, repo: Repo, file_tree: list[str], readme: str,
    ) -> WikiStructureModel:
        """cc(agent)结构:交付文件已存在即跳过 agent(续跑);否则 agent 落盘 structure.md 后读回解析。"""
        r = task.request
        struct_path = _agent_cache_structure_path(r)
        if struct_path.exists():
            content = await asyncio.to_thread(struct_path.read_text, encoding="utf-8")
        else:
            readme_path = _find_readme_path(file_tree)
            prompt = self._build_structure_prompt(
                r.owner, r.repo, "\n".join(file_tree), readme_path,
                os.path.abspath(repo.save_path), r.comprehensive, r.language,
                str(struct_path),
            )
            content = await _agent_write_file(
                "", prompt, repo, r.model, struct_path, label="wiki:structure",
                run_id=task.repo_key,
            )
        return parse_wiki_structure(content, comprehensive=r.comprehensive)

    @staticmethod
    def _build_structure_prompt(
        owner: str, repo_name: str, file_tree: str, readme_path: str | None,
        repo_root: str, comprehensive: bool, language: str, out_path: str,
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
{_agent_note()}"""

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
        out_path = _agent_cache_page_path(r, page.id)
        if out_path.exists():
            return await asyncio.to_thread(out_path.read_text, encoding="utf-8")
        prompt = _agent_note() + self._build_page_prompt(page.title, list(page.filePaths), str(out_path), r.language)
        return await _agent_write_file(
            "", prompt, repo, r.model, out_path, label=f"wiki:page:{page.id}",
            run_id=task.repo_key,
        )

    async def hydrate_pages(self, task: WikiTask) -> None:
        """cc 路径:从已落盘的页交付文件水合 generated_pages(文件为权威,覆盖 state 旧文本);
        无文件的页留给 _generate_pages(含每页完成即落盘的状态语义)。"""
        structure = task.wiki_structure
        if structure is None:
            return
        r = task.request
        ctx = RepoUrlContext(type=r.type, repo_url=r.repo_url, default_branch=task.default_branch)
        for page in structure.pages:
            out_path = _agent_cache_page_path(r, page.id)
            if not out_path.exists():
                continue
            content = await asyncio.to_thread(out_path.read_text, encoding="utf-8")
            content = _strip_markdown_fences(content)
            content = post_process_wiki_content(content, list(page.filePaths), ctx)
            task.generated_pages[page.id] = page.model_copy(update={"content": content})
        task.pages_done = len(task.generated_pages)

    def write_error_page(self, task: WikiTask, page: WikiPage, content: str) -> None:
        """重试耗尽(cc 路):占位文本也落盘,续跑跳过占位页;用户删除该文件即可重试。"""
        try:
            _agent_cache_page_path(task.request, page.id).write_text(content, encoding="utf-8")
        except OSError as e:  # noqa: BLE001 - 占位写入失败不阻断任务完成
            _log(f"写入占位页文件失败: {page.id} - {e}")

    async def chat_stream(self, request: ChatCompletionRequest):
        """一次 chat 请求的流式应答(纯文本 chunk 序列,前后端协议同原 research_chat)。"""
        if not request.messages:
            raise ValueError("No messages provided")
        last = request.messages[-1]
        if last.role != "user":
            raise ValueError("Last message must be from the user")

        # 注:未索引的前置校验已上移到端点层(_require_indexed),生成器内不再重复
        repo = Repo(request.repo_url, request.type, access_token=request.token)

        fmt = _format_request_fmt(request)
        is_deep = last.mode == "deep_research"
        if is_deep:
            # continuation 回退:含 continue+research 时换回首个用户消息(移植 research.py)
            if "continue" in last.content.lower() and "research" in last.content.lower():
                for msg in request.messages:
                    if msg.role == "user" and "continue" not in msg.content.lower():
                        last.content = msg.content.strip()
                        break
            if request.research_iteration == 1:
                system = _DEEP_RESEARCH_FIRST_ITERATION_PROMPT.format(**fmt)
            elif request.research_iteration >= 5:
                system = _DEEP_RESEARCH_FINAL_ITERATION_PROMPT.format(**fmt)
            else:
                system = _DEEP_RESEARCH_INTERMEDIATE_ITERATION_PROMPT.format(
                    **fmt, research_iteration=request.research_iteration
                )
        else:
            system = _SIMPLE_CHAT_SYSTEM_PROMPT.format(**fmt)

        # 对话历史成对拼装;输入过大时省略历史(仿 prompt_builder 的简化路径)
        # 裁剪/注记作为监控上下文说明事件(context/modify|inject)伴跑,不改变折叠结果
        history = ""
        context: list[dict] = []
        if len(request.messages) > 1:
            if _estimate_tokens(last.content) > envs.CHAT_TOKEN_LIMIT_ESTIMATE:
                _log(f"请求过大(估算 {_estimate_tokens(last.content)} tokens),省略对话历史")
                context.append({"type": "context/modify",
                                "data": {"target": "chat-history", "kind": "trim",
                                         "cause": "token-limit", "detail": "省略对话历史",
                                         "removed": {"n_turns": len(request.messages) - 1,
                                                     "est_tokens": _estimate_tokens(last.content)}}})
            else:
                turns = ""
                for i in range(0, len(request.messages) - 1, 2):
                    user, assistant = request.messages[i], request.messages[i + 1]
                    if user.role == "user" and assistant.role == "assistant":
                        turns += f"<turn>\n<user>{user.content}</user>\n<assistant>{assistant.content}</assistant>\n</turn>\n"
                if turns:
                    history = f"<conversation_history>\n{turns}</conversation_history>\n\n"
        context.append({"type": "context/inject",
                        "data": {"target": "user-message", "phase": "prompt-assembly",
                                 "provenance": "deepwiki:note", "text": _agent_note()}})

        prompt = history + _agent_note() + f"<query>\n{last.content}\n</query>\n\nAssistant: "
        try:
            async for chunk in _agent_stream(
                system, prompt, repo=repo, model=request.model, label=f"chat:{repo.name}",
                run_id=f"chat:{repo.name}", context=context,
            ):
                yield chunk
        except Exception as e:  # 执行期失败降级为可读错误文本(同原 stream_and_fallback 语义)
            _log(f"chat agent 错误: {e}")
            yield f"\n\n(抱歉,本次请求处理失败: {e})"

    async def generate_codemap(self, request: CodeMapRequest):
        """两阶段 codemap 生成(骨架 → 指南/图),NDJSON 事件流;阶段失败语义与原相同。"""
        try:
            repo = Repo(request.repo_url, request.type, access_token=request.token)
        except Exception:
            repo = Repo(request.repo_url, request.type)

        yield _phase("analyzing", "start")
        if not _index_ready(repo):
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
                    raw = await _agent_text(
                        _CODEMAP_SKELETON_PROMPT.format(**fmt), prompt, repo=repo,
                        model=request.model, label="codemap:skeleton",
                        run_id=f"codemap:{repo.name}",
                        retry={"attempt": attempt, "prev_error": str(last_error)} if last_error else None,
                    )
                    return _extract_json(raw)
                except Exception as e:  # noqa: BLE001
                    last_error = e
                    _log(f"codemap JSON 解析尝试 {attempt}/{attempts} 失败: {e}")
            raise ValueError(f"Model did not return valid JSON after {attempts} attempts: {last_error}")

        # 阶段 1:骨架
        yield _phase("initial_codemap", "start")
        skeleton_prompt = _codemap_note() + f"<query>\n{request.question}\n</query>\n\nAssistant: "
        try:
            skeleton = CodeMap.model_validate(await _run_json(skeleton_prompt))
        except Exception as e:  # noqa: BLE001
            _log(f"codemap 骨架失败: {e}")
            yield _event(type="error", stage="initial_codemap", message=str(e))
            return
        yield _phase("initial_codemap", "done", section_count=len(skeleton.sections))

        # 阶段 2:指南/图;i/骨架失败不致命 — 退化为骨架
        yield _phase("diagrams", "start")
        enrich_query = (
            f"{request.question}\n\n<SKELETON>\n{skeleton.model_dump_json()}\n</SKELETON>"
        )
        enrich_prompt = _codemap_note() + f"<query>\n{enrich_query}\n</query>\n\nAssistant: "
        final = skeleton
        try:
            raw = await _agent_text(
                _CODEMAP_ENRICH_PROMPT.format(**fmt), enrich_prompt, repo=repo,
                model=request.model, label="codemap:enrich", run_id=f"codemap:{repo.name}",
            )
            final = CodeMap.model_validate(_extract_json(raw))
            yield _phase("diagrams", "done")
        except Exception as e:  # noqa: BLE001
            _log(f"codemap 指南/图失败,使用骨架: {e}")
            yield _phase("diagrams", "done", degraded=True)

        _ground_citations(final, repo.save_path)
        yield _event(type="codemap", data=final.model_dump())
        yield _event(type="done")


class LlmWikiPipeline(WikiPipeline):
    """llm 路对外 API 包装:deepwiki-open 原式单次补全(内容内联进 prompt,无工具)。"""

    async def determine_structure(
        self, task: WikiTask, repo: Repo, file_tree: list[str], readme: str,
    ) -> WikiStructureModel:
        """llm 路结构:文件树+README 全文 → 单次流式补全(无工具)→ XML(与 deepwiki-open 同式)。"""
        r = task.request
        prompt = self._build_structure_prompt(
            r.owner, r.repo, "\n".join(file_tree), readme, r.comprehensive, r.language
        )
        parts: list[str] = []
        async for chunk in llm_stream(
            url=envs.DEEPWIKI_LLM_URL,
            payload={"model": envs.DEEPWIKI_LLM_MODEL,
                     "messages": [{"role": "user", "content": prompt}]},
            api_key=envs.DEEPWIKI_LLM_API_KEY or None,
            session_name="wiki:structure", run_id=task.repo_key,
        ):
            parts.append(chunk)
        return parse_wiki_structure("".join(parts), comprehensive=r.comprehensive)

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
3. The relevant_files should be actual files from the repository that would be used to generate that page
4. Return ONLY valid XML with the structure specified above, with no markdown code block delimiters"""

    @staticmethod
    def _build_page_prompt(
        title: str, file_links: str, content_blocks: list[tuple[str, str]], degraded: bool, language: str,
    ) -> str:
        """纯 LLM 页面提示词(deepwiki-open 同式 + 内联相关文件内容近似其 RAG 注入;超限降级仅链接)。"""
        prompt = _build_page_prompt(title, file_links, language)
        if content_blocks:
            inline = "\n\n".join(f'<file path="{p}">\n{text}\n</file>' for p, text in content_blocks)
            extra = f"\n\nFILE CONTENT (injected context for citation-accurate writing):\n{inline}"
        elif degraded:
            extra = "\n\n<note>输入超限,仅提供文件链接,请依代码检索。</note>"
        else:
            extra = ""
        return prompt + extra

    async def generate_page(
        self, task: WikiTask, repo: Repo, page: WikiPage, file_links: str,
    ) -> str:
        """llm 路单页:内联相关文件内容 → 单次流式补全(无工具调用)。"""
        r = task.request
        blocks, degraded = self._inline_page_files(repo.save_path, list(page.filePaths))
        prompt = self._build_page_prompt(page.title, file_links, blocks, degraded, r.language)
        # 内联文件块(str)计入注入说明;整体降级计入 modify —— 供轨迹还原"为什么这个 prompt"
        context: list[dict] = []
        for p, text in blocks:
            context.append({"type": "context/inject",
                            "data": {"target": "user-message", "phase": "page-prompt",
                                     "provenance": f"deepwiki:inline:{p}", "text": text}})
        if degraded:
            context.append({"type": "context/modify",
                            "data": {"target": "user-message", "kind": "degrade",
                                     "cause": "token-limit",
                                     "detail": "输入超限,仅提供文件链接,请依代码检索"}})
        parts: list[str] = []
        async for chunk in llm_stream(
            url=envs.DEEPWIKI_LLM_URL,
            payload={"model": envs.DEEPWIKI_LLM_MODEL,
                     "messages": [{"role": "user", "content": prompt}]},
            api_key=envs.DEEPWIKI_LLM_API_KEY or None,
            session_name=f"wiki:page:{page.id}", run_id=task.repo_key, context=context,
        ):
            parts.append(chunk)
        return "".join(parts)

    def _inline_page_files(
        self, save_path: str, file_paths: list[str],
    ) -> tuple[list[tuple[str, str]], bool]:
        """内联页面相关文件内容(纯 LLM 路径,近似 RAG 注入;单文件截断 8k 字符)。
        累计超过 CHAT_TOKEN_LIMIT_ESTIMATE(字符/4 近似)即整体降级:返回 ([], True),
        页面提示词将只提供文件链接(与 deepwiki-open 的 MAX_INPUT_TOKENS 降级同式)。"""
        limit_chars = envs.CHAT_TOKEN_LIMIT_ESTIMATE * 4
        total = 0
        blocks: list[tuple[str, str]] = []
        for p in file_paths:
            try:
                text = Path(save_path, p).read_text(encoding="utf-8")
            except OSError:
                continue
            if len(text) > _FILE_INLINE_CAP:
                text = text[:_FILE_INLINE_CAP]
            if total + len(text) > limit_chars:
                return [], True
            total += len(text)
            blocks.append((p, text))
        return blocks, False


def _wiki_pipeline() -> WikiPipeline:
    """按 envs.DEEPWIKI_GENERATOR 选路;调用时读 envs(测试 monkeypatch 生效)。"""
    return AgentWikiPipeline() if envs.DEEPWIKI_GENERATOR == "cc" else LlmWikiPipeline()


def _agent_pipeline() -> AgentWikiPipeline:
    """恒 agent 路(chat/codemap 不走 generator 分派)。"""
    return AgentWikiPipeline()




async def generate_repo_wiki(task: WikiTask) -> None:
    """驱动一个任务走完状态机(索引 → 结构 → 页面 → 缓存),失败置 FAILED。

    进度中途落盘(deepwiki_taskstate_*):结构确定后与每页完成后各写一次,
    失败/取消也尽力写;同仓库再次提交时从落盘状态续跑(见 TaskRegistry.submit)。
    """
    r = task.request
    try:
        await _persist_state(task)  # 入口即落盘:中断于索引/结构阶段的也能续跑
        pipeline = _wiki_pipeline()
        repo = Repo(r.repo_url, r.type, access_token=r.token)
        # 索引:只建一次(v1 无增量;已存在即跳过)
        if not _index_ready(repo):
            task.status = TaskStatus.INDEXING
            _log(f"索引中: {task.repo_key}")
            if not repo.downloaded and not repo.is_local:
                await asyncio.to_thread(repo.download)
            result = await _run_extract(repo, r)
            if result.get("error"):
                raise RuntimeError(result["error"])

        if task.wiki_structure is None or pipeline.needs_structure_regenerate(task):
            # 续跑:结构已落盘(cc 下以交付文件为准,被删则强制重生成)则跳过 agent 调用
            task.status = TaskStatus.DETERMINING_STRUCTURE
            task.wiki_structure = await _determine_structure(task)
            await _persist_state(task)

        task.status = TaskStatus.GENERATING
        await pipeline.hydrate_pages(task)  # cc 以文件为权威覆盖落盘 state 旧文本;llm no-op
        pages = await _generate_pages(task, task.wiki_structure)

        if not await _save(task, pages):
            raise RuntimeError("写 wiki 缓存失败")  # 不删状态:再提交仅重试写缓存
        await delete_wiki_task_state(r.owner, r.repo, r.type, r.language)
        task.status = TaskStatus.COMPLETED
        _log(f"wiki 任务完成: {task.repo_key}")
    except asyncio.CancelledError:  # Ctrl+C/停机:尽力持久化一次后重新抛出
        await _persist_state(task)
        raise
    except Exception as e:
        task.status = TaskStatus.FAILED
        task.error = str(e)
        await _persist_state(task)  # FAILED 也落盘,后续提交可续跑
        _log(f"wiki 任务失败: {task.repo_key} - {e}")


async def _determine_structure(task: WikiTask) -> WikiStructureModel:
    """确定 wiki 结构(按 DEEPWIKI_GENERATOR 分派 cc/llm);失败上抛使任务 FAILED。"""
    r = task.request
    repo = Repo(r.repo_url, r.type, access_token=r.token)
    if not repo.is_local and not repo.downloaded:
        await asyncio.to_thread(repo.download)

    task.default_branch = await asyncio.to_thread(detect_default_branch, repo.save_path)
    file_tree, readme = await asyncio.to_thread(
        read_repo_file_tree,
        repo.save_path,
        r.included_files,
        r.included_dirs,
        r.excluded_files,
        r.excluded_dirs,
    )
    return await _wiki_pipeline().determine_structure(task, repo, file_tree, readme)


async def _generate_page(task: WikiTask, page: WikiPage) -> WikiPage:
    """生成单个页面:cc 落盘读回 / llm 单次补全 → 剥 fence → 引用后处理。"""
    r = task.request
    repo = Repo(r.repo_url, r.type, access_token=r.token)
    ctx = RepoUrlContext(type=r.type, repo_url=r.repo_url, default_branch=task.default_branch)
    file_links = "\n".join(f"- [{p}]({generate_file_url(p, ctx)})" for p in page.filePaths)
    content = await _wiki_pipeline().generate_page(task, repo, page, file_links)
    content = _strip_markdown_fences(content)
    content = post_process_wiki_content(content, list(page.filePaths), ctx)
    return page.model_copy(update={"content": content})


async def _generate_page_with_retry(task: WikiTask, page: WikiPage) -> WikiPage:
    last_error: Exception | None = None
    for attempt in range(_WIKI_PAGE_RETRIES + 1):
        try:
            return await _generate_page(task, page)
        except Exception as e:  # noqa: BLE001 - 瞬时/永久错误统一由重试预算兜底
            last_error = e
            _log(f"页面 {page.id} 生成失败(尝试 {attempt + 1}/{_WIKI_PAGE_RETRIES + 1}): {e}")
    # 重试耗尽:回退错误占位页,保证整个 wiki 仍能完成
    content = f"Error generating content: {last_error}"
    _wiki_pipeline().write_error_page(task, page, content)
    return page.model_copy(update={"content": content})


def _pending_pages(structure: WikiStructureModel, done: dict[str, WikiPage]) -> list[WikiPage]:
    """按结构顺序返回尚未生成的页面(done: 已完成页 id → 页)。"""
    return [p for p in structure.pages if p.id not in done]


async def _generate_pages(task: WikiTask, structure: WikiStructureModel) -> dict[str, WikiPage]:
    """有界并发 + 每页重试地生成所有页面;续跑跳过已落盘的页,每页完成后立即落盘。"""
    sema = asyncio.Semaphore(_WIKI_PAGE_CONCURRENCY)
    task.pages_done = len(task.generated_pages)  # 续跑:从恢复的完成数起步
    pending = _pending_pages(structure, task.generated_pages)

    async def one(page: WikiPage) -> None:
        async with sema:
            task.current_page_ids.append(page.id)
            try:
                task.generated_pages[page.id] = await _generate_page_with_retry(task, page)
            finally:
                try:
                    task.current_page_ids.remove(page.id)
                except ValueError:
                    pass
                task.pages_done += 1
            await _persist_state(task)  # 每页完成即落盘(锁内串行写)

    await asyncio.gather(*(one(page) for page in pending))
    return task.generated_pages


async def _save(task: WikiTask, pages: dict[str, WikiPage]) -> bool:
    assert task.wiki_structure is not None
    return await save_wiki_cache(
        owner=task.request.owner,
        repo=task.request.repo,
        repo_type=task.request.type,
        language=task.request.language,
        wiki_cache=WikiCacheData(
            wiki_structure=task.wiki_structure,
            generated_pages=pages,
            generator=envs.DEEPWIKI_GENERATOR,  # 成品缓存记模式,cache 命中时校验
            repo=RepoInfo(
                owner=task.request.owner,
                repo=task.request.repo,
                type=task.request.type,
                token=None,  # 缓存文件不落 token
                repoUrl=task.request.repo_url,
            ),
            provider=task.request.provider,
            model=task.request.model,
        ),
    )


