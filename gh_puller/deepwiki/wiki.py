"""wiki 主线(wiki 结构/页面生成协议):双路包装类(WikiPipeline/AgentWikiPipeline/
LlmWikiPipeline)经 _wiki_pipeline 按 generator 挑选(见文件尾),加
本主线专用 helper:wiki 结构确定的提示词(_COMPREHENSIVE/_CONCISE_STRUCTURE)与
页面提示词(_build_page_prompt)、模型产出 XML 解析(parse_wiki_structure 容错链)、
交付内容引用渲染(RepoUrlContext 族 + post_process_wiki_content)与终态格式化
(_finalize_page_content)。

边界(按功能为主线):跨功能通用 helper(四路装配 adapter/llm 传输/检索簇/
research 协议/提示词共性常量/索引保障)在 utils,经本模块属性调用
(utils.xxx 调用时取 —— monkeypatch 活性);chat/codemap 属各自主线文件
(chat.py / codemap.py),本文件不含其入口。
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent import RequestFailedError
from ..utils import Repo, _find_readme_path, _sanitize_path_seg, _strip_markdown_fences
from . import utils  # 模块对象绑定:跨功能 helper 属性调用(monkeypatch 位点活性)
from .cache import _AGENT_CACHE_DIRNAME, _generator_digest, _wiki_cache_dir
from .utils import language_name, log

# ---------------------------------------------------------------------------
# 引擎契约 dataclass 族(wiki 主线;零 pydantic,字段名即序化键):
# wire/落盘 camelCase(filePaths/rootSections);wire 契约(出网校验)在
# apps/deepwiki-webui/server/schemas.py。
# ---------------------------------------------------------------------------


@dataclass
class WikiPage:
    id: str
    title: str
    content: str
    filePaths: list[str]  # 字段名即序化键(wire/落盘 camelCase)
    importance: str  # 'high' | 'medium' | 'low'
    relatedPages: list[str]


@dataclass
class WikiSection:
    id: str
    title: str
    pages: list[str]
    subsections: list[str] | None = None


@dataclass
class WikiStructureModel:
    id: str
    title: str
    description: str
    pages: list[WikiPage]
    sections: list[WikiSection] | None = None
    rootSections: list[str] | None = None  # 字段名即序化键


def wiki_structure_of(d: dict | None) -> WikiStructureModel | None:
    """dict → WikiStructureModel(嵌套 WikiPage/Section);缺失键按旧契约缺省兜底;None → None。"""
    if d is None:
        return None
    return WikiStructureModel(
        id=d["id"],
        title=d["title"],
        description=d.get("description", ""),
        pages=[WikiPage(**p) for p in d.get("pages", [])],
        sections=[
            WikiSection(
                id=s["id"],
                title=s["title"],
                pages=s.get("pages", []),
                subsections=s.get("subsections"),
            )
            for s in d.get("sections") or []
        ],
        rootSections=d.get("rootSections"),
    )



# ---------------------------------------------------------------------------
# wiki 提示词(原文移植自 deepwiki-open api/services/wiki/prompts.py)
# ---------------------------------------------------------------------------

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

IMPORTANT: Generate the content in {language_name(language)} language.

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
# 模型产出解析(移植 api/services/wiki/structure.py):wiki 结构 XML 容错链
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
        log("响应疑似被截断(缺 </wiki_structure>),按完整块救取")
        xml_text = f"{open_match.group(0)}\n</wiki_structure>"

    xml_text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", xml_text)
    xml_text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", xml_text)

    root: ET.Element | None = None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log(f"严格 XML 解析失败,用正则兜底: {e}")

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
        log("XML 解析无页面,用正则兜底")
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
# 引用渲染(仓库相对路径 → web URL 的交付终态格式化;只服务本主线的页面产出)
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


def render_file_links(file_paths: list[str], ctx: RepoUrlContext) -> str:
    """规范式文件链接行(带 _escape_label):llm 页 prompt 与 post_process 详情块共用。"""
    return "\n".join(f"- [{_escape_label(p)}]({generate_file_url(p, ctx)})" for p in file_paths)


def post_process_wiki_content(content: str, file_paths: list[str], ctx: RepoUrlContext) -> str:
    """后处理模型产出的 wiki markdown:重建 <details> 块、解析各种空括号引用为真实链接。"""
    processed = content

    # 1. 用已知文件列表重建 <details> 块
    if file_paths:
        links = render_file_links(file_paths, ctx)
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


def _finalize_page_content(content: str, page: WikiPage, ctx: RepoUrlContext) -> str:
    """页面交付的终态格式化(剥代码围栏 + 引用后处理);新鲜生成与续跑水合同一收口。"""
    return post_process_wiki_content(_strip_markdown_fences(content), list(page.filePaths), ctx)


# ---------------------------------------------------------------------------
# 双路包装类分发总则
# ---------------------------------------------------------------------------
# wiki 生成(结构/页面)按 generator 经 _wiki_pipeline() 选择;chat /
# codemap 同开关(generator 缺省走 env 默认),分派各在自己的功能模块内联 2 行。
# 边界:语义属于本主线的 helper(提示词组装/agent 交付件路径/交付内容终态
# 格式化)收进本文件(调用经 utils.xxx —— 测试 monkeypatch/即时解析)。
# 跨功能通用(四路装配/llm 传输/检索簇/research 协议/提示词共性常量/索引保障)
# 在 utils,一律 utils.xxx 属性调用;envs 同理(调用时取)。


class WikiPipeline:
    """双路共同协议;基类默认实现即 llm 路语义(无交付文件、无续跑水合、占位不落盘)。

    全部方法为散装参数(helper-funcs 思想,包内无 Request 概念):域聚类经 Repo
    对象携带(repo_url/repo_type/token),其余字段逐个 keyword 显式传入。
    """

    def needs_structure_regenerate(self, *, project_key: str, generator: str | None = None, generator_config: dict | None = None) -> bool:
        """结构是否需要强制重生成(cc 路:structure 交付文件缺失;llm 路恒 False)。"""
        return False

    async def hydrate_pages(
        self, *, project_key: str, generator: str | None = None, generator_config: dict | None = None, repo: Repo,
        structure: WikiStructureModel, default_branch: str,
    ) -> dict[str, WikiPage]:
        """从已落盘交付文件返回页快照(cc 路;llm 路无交付文件 → 空 dict)。

        以返回值交付,不触碰任何任务运行时字段(进度/去重/落盘均由 app 侧 runtime 负责)。
        """
        return {}

    def write_error_page(
        self, *, project_key: str, generator: str | None = None, generator_config: dict | None = None, page: WikiPage, content: str,
    ) -> None:
        """重试耗尽后的占位页持久化(cc 路写交付文件供续跑跳过;llm 路 no-op)。"""

    async def determine_structure(
        self, *, generator: str | None = None, generator_config: dict | None = None, repo: Repo, owner: str, repo_name: str,
        file_tree: list[str], readme: str, comprehensive: bool, language: str, run_id: str,
    ) -> WikiStructureModel:
        raise NotImplementedError

    async def generate_page(
        self, *, generator: str | None = None, generator_config: dict | None = None, repo: Repo, page: WikiPage,
        language: str, default_branch: str, run_id: str,
    ) -> str:
        """单页生成,返回**终态格式化**内容(围栏/引用后处理已收口)。"""
        raise NotImplementedError


class AgentWikiPipeline(WikiPipeline):
    """cc(agent)路对外 API 包装:Claude Code agent 自读仓库代码,交付件 Write 落盘(文件为权威)。"""

    def _proj_key(self, project_key: str, generator: str | None = None, generator_config: dict | None = None) -> str:
        """项目键 {repo_key}_{digest}:digest = generator 判等摘要
        (同一仓库/语言下不同 generator 的交付文件并存,与成品缓存同规则)。"""
        return _sanitize_path_seg(f"{project_key}_{_generator_digest(generator, generator_config)}")

    def _agent_cache_dir(self, project_key: str, generator: str | None = None, generator_config: dict | None = None) -> Path:
        return Path(_wiki_cache_dir()) / _AGENT_CACHE_DIRNAME / self._proj_key(project_key, generator, generator_config)

    def _agent_cache_structure_path(self, project_key: str, generator: str | None = None, generator_config: dict | None = None) -> Path:
        """cc 结构交付文件:{proj}-structure.md。"""
        proj = self._proj_key(project_key, generator, generator_config)
        return self._agent_cache_dir(project_key, generator, generator_config) / f"{proj}-structure.md"

    def _agent_cache_page_path(self, project_key: str, page_id: str, generator: str | None = None, generator_config: dict | None = None) -> Path:
        """cc 页面交付文件:{proj}-<id>.md(id 形如 page-N 时直接采用,否则 {proj}-page_<id>;id 经安全化)。"""
        proj = self._proj_key(project_key, generator, generator_config)
        seg = _sanitize_path_seg(page_id)
        name = f"{proj}-{seg}" if seg.startswith("page-") else f"{proj}-page_{seg}"
        return self._agent_cache_dir(project_key, generator, generator_config) / f"{name}.md"

    # ------------------------------------------------------------------
    # agent 交付通道(适配器构造统一经 utils.adapter;直呼 stream/result)
    # ------------------------------------------------------------------

    async def _deliver(
        self, adapter: Any, prompt: str, out_path: Path,
        label: str | None = None, *, run_id: str | None = None,
    ) -> str:
        """agent 交付件统一落盘口:提示词只给路径,agent 用自身工具读码并把成品写入
        out_path;产生以文件为准(流式文本仅作监控/错误检测),未产出文件即任务失败。

        adapter 为构造期注入 config 的实例(config 由 utils.adapter 装配);
        label 作为监控会话名(wiki:structure / wiki:page:<id>),run_id 关联任务级会话组。
        """
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)  # add_dirs 指向目录须先存在(agent Write 可直接落)
        try:
            async for _ in adapter.stream(prompt, session_name=label, run_id=run_id):
                pass
        except RequestFailedError as e:
            raise utils.failure(e) from e
        text = await asyncio.to_thread(out_path.read_text, encoding="utf-8")
        if not text.strip():
            raise RuntimeError(f"agent 未产出交付文件: {out_path}")
        return text

    def needs_structure_regenerate(self, *, project_key: str, generator: str | None = None, generator_config: dict | None = None) -> bool:
        """structure 交付文件被删即强制重生成(续跑失效)。"""
        return not self._agent_cache_structure_path(project_key, generator, generator_config).exists()

    async def determine_structure(
        self, *, generator: str | None = None, generator_config: dict | None = None, repo: Repo, owner: str, repo_name: str,
        file_tree: list[str], readme: str, comprehensive: bool, language: str, run_id: str,
    ) -> WikiStructureModel:
        """cc(agent)结构:交付文件已存在即跳过 agent(续跑);否则 agent 落盘 structure.md 后读回解析。"""
        struct_path = self._agent_cache_structure_path(run_id, generator, generator_config)
        if struct_path.exists():
            content = await asyncio.to_thread(struct_path.read_text, encoding="utf-8")
        else:
            adapter = utils.adapter(generator, generator_config=generator_config, repo=repo,
                                     agent_output_dir=str(struct_path.parent),
                                     agent_write_mode=True)
            readme_path = _find_readme_path(file_tree)
            prompt = self._build_structure_prompt(
                owner, repo_name, "\n".join(file_tree), readme_path,
                os.path.abspath(repo.save_path), comprehensive, language,
                str(struct_path), generator=adapter.generator,
            )
            content = await self._deliver(adapter, prompt, struct_path,
                                          label="wiki:structure", run_id=run_id)
        return parse_wiki_structure(content, comprehensive=comprehensive)

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

IMPORTANT: The wiki content will be generated in {language_name(language)} language.

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
{utils.agent_note(generator)}"""

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
        self, *, generator: str | None = None, generator_config: dict | None = None, repo: Repo, page: WikiPage,
        language: str, default_branch: str, run_id: str,
    ) -> str:
        """cc 路单页:交付文件已存在即读回(续跑,文件为权威);否则 agent 落盘 page_<id>.md;
        读回内容经终态格式化后返回(与续跑水合同式)。"""
        out_path = self._agent_cache_page_path(run_id, page.id, generator=generator, generator_config=generator_config)
        if out_path.exists():
            content = await asyncio.to_thread(out_path.read_text, encoding="utf-8")
        else:
            adapter = utils.adapter(generator, generator_config=generator_config, repo=repo,
                                     agent_output_dir=str(out_path.parent),
                                     agent_write_mode=True)
            prompt = utils.agent_note(adapter.generator) + self._build_page_prompt(
                page.title, list(page.filePaths), str(out_path), language
            )
            content = await self._deliver(adapter, prompt, out_path,
                                          label=f"wiki:page:{page.id}", run_id=run_id)
        ctx = RepoUrlContext(type=repo.repo_type, repo_url=repo.repo_url, default_branch=default_branch)
        return _finalize_page_content(content, page, ctx)

    async def hydrate_pages(
        self, *, project_key: str, generator: str | None = None, generator_config: dict | None = None, repo: Repo,
        structure: WikiStructureModel, default_branch: str,
    ) -> dict[str, WikiPage]:
        """cc 路径:从已落盘的页交付文件水合(文件为权威,覆盖 state 旧文本);
        返回页快照 dict(不触碰任务运行时字段;无文件的页留给调用方生成)。"""
        ctx = RepoUrlContext(type=repo.repo_type, repo_url=repo.repo_url, default_branch=default_branch)
        generated: dict[str, WikiPage] = {}
        for page in structure.pages:
            out_path = self._agent_cache_page_path(project_key, page.id, generator=generator, generator_config=generator_config)
            if not out_path.exists():
                continue
            content = await asyncio.to_thread(out_path.read_text, encoding="utf-8")
            generated[page.id] = dataclasses.replace(
                page, content=_finalize_page_content(content, page, ctx)
            )
        return generated

    def write_error_page(
        self, *, project_key: str, generator: str | None = None, generator_config: dict | None = None, page: WikiPage, content: str,
    ) -> None:
        """重试耗尽(cc 路):占位文本也落盘,续跑跳过占位页;用户删除该文件即可重试。"""
        try:
            out_path = self._agent_cache_page_path(project_key, page.id, generator=generator, generator_config=generator_config)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")
        except OSError as e:  # noqa: BLE001 - 占位写入失败不阻断任务完成
            log(f"写入占位页文件失败: {page.id} - {e}")


class LlmWikiPipeline(WikiPipeline):
    """llm 路对外 API 包装:deepwiki-open 原式单次补全(内容内联进 prompt,无工具)。"""

    async def determine_structure(
        self, *, generator: str | None = None, generator_config: dict | None = None, repo: Repo, owner: str, repo_name: str,
        file_tree: list[str], readme: str, comprehensive: bool, language: str, run_id: str,
    ) -> WikiStructureModel:
        """llm 路结构:原版经 research_chat(结构提示词为查询,SIMPLE 角色模板 +
        检索上下文注入 + prompt_builder 拼装);内容错误时解析失败 → 任务 FAILED。"""
        prompt = self._build_structure_prompt(
            owner, repo_name, "\n".join(file_tree), readme, comprehensive, language
        )
        system = utils._SIMPLE_CHAT_SYSTEM_PROMPT.format(**utils.prompt_fmt(repo, language=language))
        parts: list[str] = []
        async for chunk in utils.llm_research_chat(
            system, prompt, generator=generator, generator_config=generator_config, repo=repo,
            session_name="wiki:structure", run_id=run_id,
        ):
            parts.append(chunk)
        return parse_wiki_structure("".join(parts), comprehensive=comprehensive)

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

IMPORTANT: The wiki content will be generated in {language_name(language)} language.

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
        self, *, generator: str | None = None, generator_config: dict | None = None, repo: Repo, page: WikiPage,
        language: str, default_branch: str, run_id: str,
    ) -> str:
        """llm 路单页:原版同式——页面提示词(仅文件链接,不内联内容)
        作为查询经 research_chat 等价流(检索上下文注入;流错误为内容而非抛出,
        重试只覆盖校验/检索前置异常——与原版一致)。返回前经终态格式化(同 cc 路)。"""
        ctx = RepoUrlContext(type=repo.repo_type, repo_url=repo.repo_url, default_branch=default_branch)
        file_links = render_file_links(list(page.filePaths), ctx)
        prompt = _build_page_prompt(page.title, file_links, language)
        system = utils._SIMPLE_CHAT_SYSTEM_PROMPT.format(**utils.prompt_fmt(repo, language=language))
        parts: list[str] = []
        async for chunk in utils.llm_research_chat(
            system, prompt, generator=generator, generator_config=generator_config, repo=repo,
            session_name=f"wiki:page:{page.id}", run_id=run_id,
        ):
            parts.append(chunk)
        return _finalize_page_content("".join(parts), page, ctx)


# ---------------------------------------------------------------------------
# wiki 分派(chat/codemap 服务入口同开关,各自功能模块内联 2 行分派)
# ---------------------------------------------------------------------------


def _wiki_pipeline(generator: str | None = None, generator_config: dict | None = None) -> WikiPipeline:
    """按解析后的 generator 选路;调用时解析(测试 monkeypatch envs 生效)。

    agent 类后段(cc/dsh/codex)共用 AgentWikiPipeline —— 适配器构造统一经
    utils.adapter,管线逻辑(结构/页面/缓存)后端无关;llm 走 LlmWikiPipeline
    (原式单次补全)。chat/codemap 服务入口同开关共用本规则(分派在各自模块)。
    """
    gen = utils.resolve_generator(generator, generator_config)[0]
    return AgentWikiPipeline() if gen in ("cc", "dsh", "codex") else LlmWikiPipeline()
