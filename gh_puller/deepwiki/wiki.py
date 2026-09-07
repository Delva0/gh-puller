"""Generate, format, and persist DeepWiki structures and pages.

Generated files are authoritative for resumable work. This module also owns XML
recovery and source-link rendering; chat and codemap flows live in their own modules.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from .. import envs  # Read attributes at call time so reloads and patches stay visible.
from ..agent import RequestFailedError
from ..utils import Repo, TaskStatus, _sanitize_path_seg, _strip_markdown_fences
from . import (
    utils,  # Keep helper patches visible at call time.
)
from .utils import language_name, log

# --- Wire models ---


@dataclass
class WikiPage:
    id: str
    title: str
    content: str
    filePaths: list[str]  # Names mirror the persisted camelCase wire format.
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
    rootSections: list[str] | None = None


def wiki_structure_of(d: dict | None) -> WikiStructureModel | None:
    """Build a nested wiki structure from its persisted representation.

    Args:
        d: Decoded wiki data, or ``None`` to preserve an absent structure.

    Returns:
        A structure with stable defaults, or ``None`` when the input is ``None``.
    """
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

# --- Upstream prompts ---

def _build_page_prompt(title: str, file_links: str, language: str) -> str:
    """Build a page prompt from pre-rendered Markdown source links."""
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
"""  # noqa: E501 - Preserve the upstream prompt as a single literal.


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
"""  # noqa: E501 - Preserve the upstream structure template as one literal.

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


# --- Structure parsing ---


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
    """Recover complete page blocks when strict XML parsing yields no pages."""
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
            ),
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
            ),
        )
        referenced.update(subs)
    root_sections = [s.id for s in sections if s.id not in referenced]
    return sections, root_sections


def _first_group(pattern: str, text: str) -> str:
    m = re.search(pattern, text)
    return m.group(1).strip() if m else ""


def _sections_via_regex(xml_text: str) -> tuple[list[WikiSection], list[str]]:
    """Recover complete section blocks when strict XML parsing fails."""
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
            ),
        )
        referenced.update(subs)
    root_sections = [s.id for s in sections if s.id not in referenced]
    return sections, root_sections


def parse_wiki_structure(text: str, comprehensive: bool) -> WikiStructureModel:
    """Parse model XML with fence, truncation, ampersand, and regex recovery."""
    text = re.sub(r"^```(?:xml)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"```\s*$", "", text)

    match = re.search(r"<wiki_structure>[\s\S]*?</wiki_structure>", text)
    if match:
        xml_text = match.group(0)
    else:
        # Retain complete child blocks from a truncated outer element.
        open_match = re.search(r"<wiki_structure>[\s\S]*", text)
        if not open_match:
            raise ValueError("No valid <wiki_structure> XML found in response")
        log("响应疑似被截断(缺 </wiki_structure>),按完整块救取")
        xml_text = f"{open_match.group(0)}\n</wiki_structure>"

    xml_text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", xml_text)
    xml_text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", xml_text)

    root: ET.Element | None = None
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 - Model output is data, not a trusted XML document.
    except ET.ParseError as e:
        log(f"严格 XML 解析失败,用正则兜底: {e}")

    if root is not None:
        title = root.findtext("title") or ""
        description = root.findtext("description") or ""
        pages = [_page_from_element(el, i) for i, el in enumerate(root.iter("page"))]
    else:
        # The root title and description precede page-level elements.
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


# --- Source-link rendering ---


class RepoUrlContext:
    """Hold the repository fields required to build web source URLs."""

    def __init__(self, type: str, repo_url: str | None, default_branch: str):  # noqa: A002 - Mirrors repo.type.
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
    """Escape brackets so a path renders as ordinary Markdown link text."""
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
    """Render ``path[:start[-end]]`` for a supported remote host."""
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
    """Render canonical Markdown links for repository-relative files."""
    return "\n".join(f"- [{_escape_label(p)}]({generate_file_url(p, ctx)})" for p in file_paths)


def post_process_wiki_content(content: str, file_paths: list[str], ctx: RepoUrlContext) -> str:
    """Rebuild source details and resolve empty model citations to source URLs."""
    processed = content

    # Rebuild the source block from trusted repository paths.
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

    # Resolve known paths longest-first to avoid suffix collisions.
    if file_paths:
        alternation = "|".join(re.escape(p) for p in sorted(file_paths, key=len, reverse=True))
        citation_re = re.compile(r"\[(" + alternation + r")(?::(\d+)(?:-(\d+))?)?\]\(\)")

        def _repl_known(m: re.Match) -> str:
            link = _citation_link(m.group(1), m.group(2), m.group(3), ctx)
            return link if link is not None else m.group(0)

        processed = citation_re.sub(_repl_known, processed)

    # Resolve remaining path-shaped citations.
    def _repl_generic(m: re.Match) -> str:
        link = _citation_link(m.group(1), m.group(2), m.group(3), ctx)
        return link if link is not None else m.group(0)

    processed = _GENERIC_RE.sub(_repl_generic, processed)

    # Expand basename-only ``Sources:`` citations through the known path set.
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

    # Remove empty parentheses left after an already complete link.
    return _STRAY_PARENS_RE.sub(r"\1", processed)


def _finalize_page_content(content: str, page: WikiPage, ctx: RepoUrlContext) -> str:
    """Apply identical fence and citation cleanup to new and resumed pages."""
    return post_process_wiki_content(_strip_markdown_fences(content), list(page.filePaths), ctx)
# --- Persistence ---

# Resolve DEEPWIKI_ROOT at call time so refreshed environment snapshots remain visible.

_GENERATOR_CACHE_DIRNAME = "generator_cache"

_WIKI_PREFIX = "cache_"
_RESUME_STATE_PREFIX = "resume_"


def wiki_cache_dir() -> str:
    """Return the wiki cache root beside other DeepWiki artifacts."""
    return os.path.join(envs.DEEPWIKI_ROOT, "wiki")


def _project_seg(project_key: str) -> str:
    """Sanitize the canonical request-derived repository key for cache paths."""
    return _sanitize_path_seg(project_key)


def wiki_project_dir(owner: str, repo: str, repo_type: str) -> str:
    """Return the project cache directory below the current wiki cache root."""
    return os.path.join(wiki_cache_dir(), _project_seg(utils.repo_key_of(repo_type, owner, repo)))


def _wiki_cache_path(
    owner: str, repo: str, repo_type: str, language: str, digest: str = "",
) -> str:
    suffix = f"_{digest}" if digest else ""
    filename = f"{_WIKI_PREFIX}{repo_type}_{owner}_{repo}_{language}{suffix}.json"
    return os.path.join(wiki_project_dir(owner, repo, repo_type), filename)


def resume_state_path(
    owner: str, repo: str, repo_type: str, language: str, digest: str = "",
) -> str:
    suffix = f"_{digest}" if digest else ""
    filename = f"{_RESUME_STATE_PREFIX}{repo_type}_{owner}_{repo}_{language}{suffix}.json"
    return os.path.join(wiki_project_dir(owner, repo, repo_type), filename)


def wiki_cache_exists(owner: str, repo: str, repo_type: str, language: str, digest: str = "") -> bool:
    return os.path.exists(_wiki_cache_path(owner, repo, repo_type, language, digest))


async def read_wiki_cache(
    owner: str, repo: str, repo_type: str, language: str, digest: str = "",
) -> dict | None:
    """Read a finished wiki record, returning ``None`` for missing or invalid data."""
    if not wiki_cache_exists(owner, repo, repo_type, language, digest):
        return None
    path = _wiki_cache_path(owner, repo, repo_type, language, digest)
    try:
        text = await asyncio.to_thread(lambda: Path(path).read_text(encoding="utf-8"))
        data = json.loads(text)
    except Exception:
        log(f"读取 wiki 缓存失败: {path}")
        return None
    if not isinstance(data, dict):
        return None
    return data


async def save_wiki_cache(
    owner: str, repo: str, repo_type: str, language: str,
    wiki_cache: dict, digest: str = "",
) -> bool:
    path = _wiki_cache_path(owner, repo, repo_type, language, digest)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        await asyncio.to_thread(
            lambda: Path(path).write_text(json.dumps(wiki_cache), encoding="utf-8"),
        )
    except OSError:
        log(f"写 wiki 缓存失败: {path}")
        return False
    return True


async def save_generated_wiki(
    owner: str, repo: str, repo_type: str, repo_url: str,
    structure: WikiStructureModel, pages: dict[str, WikiPage], language: str = "en",
    generator: str | None = None, generator_config: dict | None = None,
) -> bool:
    """Persist one complete generation into the finished-wiki cache (cache-layer duty: identity + assembly + write).

    Identity via utils.generator_identity/digest (same equality, see utils);
    assembled as a plain dict (key set kept verbatim), public identity recorded,
    credentials never written (**token=None**). generator and generator_config
    are recorded as-given (whole dict; field layout is a read-side/wire concern —
    no type judgment / field splitting here), cache hit validated via
    cache_generator_matches.
    """
    generator_id, resolved = utils.resolve_generator(generator, generator_config)
    cache_record = {
        "wiki_structure": dataclasses.asdict(structure),
        "generated_pages": {pid: dataclasses.asdict(pg) for pid, pg in pages.items()},
        "repo_url": None,  # compatible for old cache
        "repo": {
            "owner": owner,
            "repo": repo,
            "type": repo_type,
            "token": None,  # Credentials must never enter persistent generation state.
            "localPath": None,
            "repoUrl": repo_url,
        },
        "generator": generator_id,
        "generator_config": resolved,
    }
    return await save_wiki_cache(
        owner=owner,
        repo=repo,
        repo_type=repo_type,
        language=language,
        digest=utils.generator_digest(generator, generator_config),
        wiki_cache=cache_record,
    )


async def delete_wiki_cache(
    owner: str, repo: str, repo_type: str, language: str, digest: str = "",
) -> bool:
    """Delete every finished, resume, and generator cache for one project.

    Args:
        owner: Repository owner used in the project key.
        repo: Repository name used in the project key.
        repo_type: Repository host type used in the project key.
        language: Compatibility field; deletion covers every project language.
        digest: Compatibility field; deletion covers every generator variant.

    Returns:
        Whether the project cache directory existed.
    """
    proj_dir = wiki_project_dir(owner, repo, repo_type)
    if not os.path.exists(proj_dir):  # noqa: ASYNC240 - Lightweight cache metadata check.
        return False
    shutil.rmtree(proj_dir, ignore_errors=True)
    return True


async def write_resume_state(
    owner: str, repo: str, repo_type: str, language: str,
    state: dict, digest: str = "",
) -> bool:
    """Atomically persist credential-free resume state for one generator variant.

    Args:
        owner: Repository owner used in the cache key.
        repo: Repository name used in the cache key.
        repo_type: Repository host type used in the cache key.
        language: Wiki language used in the cache key.
        state: Plain resume-state object assembled by the application.
        digest: Public generator identity suffix.

    Returns:
        Whether the state was written successfully.
    """
    path = resume_state_path(owner, repo, repo_type, language, digest)
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        await asyncio.to_thread(
            lambda: Path(tmp).write_text(json.dumps(state), encoding="utf-8"),
        )
        os.replace(tmp, path)
    except OSError as e:
        log(f"写续跑状态失败: {path} - {e}")
        with contextlib.suppress(OSError):
            os.remove(tmp)
        return False
    return True


async def read_resume_state(
    owner: str, repo: str, repo_type: str, language: str, digest: str = "",
) -> dict | None:
    """Read resume state, treating missing or malformed state as a fresh generation."""
    path = resume_state_path(owner, repo, repo_type, language, digest)
    if not os.path.exists(path):  # noqa: ASYNC240 - Lightweight cache metadata check.
        return None
    try:
        text = await asyncio.to_thread(lambda: Path(path).read_text(encoding="utf-8"))
        data = json.loads(text)
    except Exception as e:
        log(f"读取续跑状态失败: {path} :: {type(e).__name__}: {e}")
        return None
    if not isinstance(data, dict) or "request" not in data:
        return None
    return data


async def delete_resume_state(
    owner: str, repo: str, repo_type: str, language: str, digest: str = "",
) -> bool:
    path = resume_state_path(owner, repo, repo_type, language, digest)
    if not os.path.exists(path):  # noqa: ASYNC240 - Lightweight cache metadata check.
        return False
    os.remove(path)
    return True


async def list_wiki_cache() -> list[dict]:
    """Return completed-wiki summaries decoded from cache filenames."""
    if not os.path.exists(wiki_cache_dir()):  # noqa: ASYNC240 - Lightweight cache metadata check.
        return []
    entries: list[dict] = []
    for dirname in await asyncio.to_thread(os.listdir, wiki_cache_dir()):
        proj_dir = os.path.join(wiki_cache_dir(), dirname)
        if dirname.startswith(".") or not os.path.isdir(proj_dir):  # noqa: ASYNC240 - Skip non-project entries.
            continue
        for filename in await asyncio.to_thread(os.listdir, proj_dir):
            if not (filename.startswith(_WIKI_PREFIX) and filename.endswith(".json")):
                continue
            file_path = os.path.join(proj_dir, filename)
            try:
                stats = await asyncio.to_thread(os.stat, file_path)
                parts = os.path.splitext(filename)[0].removeprefix(_WIKI_PREFIX).split("_")
                # Legacy cache names omit the trailing eight-character generator digest.
                has_digest = len(parts) > 1 and len(parts[-1]) == 8 and re.fullmatch(r"[0-9a-f]+", parts[-1])
                language_idx = -2 if has_digest else -1
                owner = parts[1]
                repo = "_".join(parts[2:language_idx])
                entries.append(
                    {
                        "id": filename,
                        "owner": owner,
                        "repo": repo,
                        "repo_type": parts[0],
                        "language": parts[language_idx],
                        "status": TaskStatus.COMPLETED,
                        "digest": parts[-1] if has_digest else "",
                        "pages_done": 0,
                        "pages_total": 0,
                        "current_page_ids": [],
                        "error": None,
                        "submitted_at": int(stats.st_mtime * 1000),
                        "name": f"{owner}/{repo}",
                    },
                )
            except Exception:
                log(f"解析缓存文件失败: {file_path}")
    return entries


async def list_processed_projects() -> list[dict]:
    project_entries = [
        {
            "id": wiki["id"],
            "owner": wiki["owner"],
            "repo": wiki["repo"],
            "name": wiki["name"],
            "repo_type": wiki["repo_type"],
            "submittedAt": wiki["submitted_at"],
            "language": wiki["language"],
            "digest": wiki["digest"],
        }
        for wiki in await list_wiki_cache()
    ]
    project_entries.sort(key=lambda p: p["submittedAt"], reverse=True)
    return project_entries


def export_wiki(
    repo_url: str,
    pages: list[dict],
    format: Literal["json", "markdown"],  # noqa: A002 - Public wire contract uses this name.
    timestamp: datetime | None = None,
) -> str:
    """Render generated pages as a JSON or Markdown document.

    Args:
        repo_url: Repository identifier shown in export metadata.
        pages: Persisted page dictionaries in display order.
        format: Export representation.
        timestamp: Generation time, defaulting to local display time.

    Returns:
        Complete serialized export content.
    """
    dt = timestamp or datetime.now()  # noqa: DTZ005 - Exports intentionally use local display time.
    if format == "json":
        export_data = {
            "metadata": {
                "repository": repo_url,
                "generated_at": dt.isoformat(),
                "page_count": len(pages),
            },
            "pages": list(pages),
        }
        return json.dumps(export_data, indent=2)
    if format == "markdown":
        markdown = f"# Wiki Documentation for {repo_url}\n\n"
        markdown += f"Generated on: {dt.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        markdown += "## Table of Contents\n\n"
        for page in pages:
            markdown += f"- [{page['title']}](#{page['id']})\n"
        markdown += "\n"
        for page in pages:
            markdown += f"<a id='{page['id']}'></a>\n\n"
            markdown += f"## {page['title']}\n\n"
            if page.get("relatedPages"):
                related_titles = []
                for related_id in page["relatedPages"]:
                    related_page = next((p for p in pages if p["id"] == related_id), None)
                    if related_page:
                        related_titles.append(f"[{related_page['title']}](#{related_id})")
                if related_titles:
                    markdown += "### Related Pages\n\n"
                    markdown += "Related topics: " + ", ".join(related_titles) + "\n\n"
            markdown += f"{page['content']}\n\n"
            markdown += "---\n\n"
        return markdown
    raise ValueError(f"unsupported export format: {format!r}")

# --- Generator artifacts ---


def _proj_key(project_key: str, generator: str | None = None, generator_config: dict | None = None) -> str:
    """Append the generator identity digest to a safe project key."""
    return _sanitize_path_seg(f"{project_key}_{utils.generator_digest(generator, generator_config)}")


def _generator_cache_dir(
    project_key: str, generator: str | None = None, generator_config: dict | None = None,
) -> Path:
    """Return a project's flat generator-artifact directory."""
    return Path(wiki_cache_dir()) / _project_seg(project_key) / _GENERATOR_CACHE_DIRNAME


def _generator_cache_structure_path(
    project_key: str, generator: str | None = None, generator_config: dict | None = None,
) -> Path:
    """Return the generator's structure artifact path."""
    proj = _proj_key(project_key, generator, generator_config)
    return _generator_cache_dir(project_key, generator, generator_config) / f"{proj}-structure.md"


def _generator_cache_page_path(
    project_key: str, page_id: str, generator: str | None = None, generator_config: dict | None = None,
) -> Path:
    """Return a safe page artifact path under the flat generator cache."""
    proj = _proj_key(project_key, generator, generator_config)
    seg = _sanitize_path_seg(page_id)
    name = f"{proj}-{seg}" if seg.startswith("page-") else f"{proj}-page_{seg}"
    return _generator_cache_dir(project_key, generator, generator_config) / f"{name}.md"


# --- Artifact capture ---


async def _produce_file(
    adapter: Any, prompt: str, out_path: Path,
    label: str | None = None, *, run_id: str | None = None,
) -> str:
    """Run an Agent and return its required file artifact.

    Streamed text is observational only. The call fails when the Agent does not write a
    non-empty artifact to ``out_path``.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    log(f"产物捕获开始 label={label} run_id={run_id} out={out_path.name}")
    try:
        async with adapter.session(session_name=label, run_id=run_id):
            async for _ in adapter.stream(prompt):
                pass
    except RequestFailedError as e:
        log(f"产物捕获失败 label={label} run_id={run_id} 耗时={time.time() - t0:.1f}s -> {e}")
        raise utils.failure(e) from e
    text = await asyncio.to_thread(out_path.read_text, encoding="utf-8")
    if not text.strip():
        raise RuntimeError(f"generator 未产出文件: {out_path}")
    log(f"产物捕获完成 label={label} run_id={run_id} 耗时={time.time() - t0:.1f}s")
    return text

def needs_structure_regenerate(
    *, project_key: str, generator: str | None = None, generator_config: dict | None = None,
) -> bool:
    """Return whether the selected generator lacks a structure artifact.

    Args:
        project_key: Canonical repository project key.
        generator: Registered Agent adapter name, or the default when omitted.
        generator_config: Adapter-specific construction options.

    Returns:
        Whether structure generation must run.
    """
    return not _generator_cache_structure_path(project_key, generator, generator_config).exists()


async def determine_structure(
    *, repo: Repo, owner: str, repo_name: str,
    generator: str | None = None, generator_config: dict | None = None,
    comprehensive: bool, language: str, run_id: str,
) -> WikiStructureModel:
    """Load or generate and parse a wiki structure artifact.

    Args:
        repo: Repository available to the Agent.
        owner: Repository owner included in the prompt.
        repo_name: Repository name included in the prompt.
        generator: Registered Agent adapter name, or the default when omitted.
        generator_config: Adapter-specific construction options.
        comprehensive: Whether to request the larger sectioned structure.
        language: Language code for human-readable output.
        run_id: Project generation id and monitoring correlation id.

    Returns:
        Parsed wiki structure.
    """
    struct_path = _generator_cache_structure_path(run_id, generator, generator_config)
    if struct_path.exists():
        content = await asyncio.to_thread(struct_path.read_text, encoding="utf-8")
    else:
        adapter = utils.adapt_generator(generator, generator_config=generator_config, repo=repo,
                                generator_cache_dir=str(struct_path.parent),
                                generator_cache_write_mode=True)
        prompt = _build_structure_prompt(
            owner, repo_name, os.path.abspath(repo.save_path), comprehensive, language,  # noqa: ASYNC240 - Path only.
            str(struct_path),
        )
        content = await _produce_file(adapter, prompt, struct_path,
                                      label="wiki:structure", run_id=run_id)
    return parse_wiki_structure(content, comprehensive=comprehensive)


def _build_structure_prompt(
    owner: str, repo_name: str, repo_root: str,
    comprehensive: bool, language: str, out_path: str,
) -> str:
    """Build a structure prompt that requires a directly written XML artifact."""
    structure_format = _COMPREHENSIVE_STRUCTURE if comprehensive else _CONCISE_STRUCTURE
    page_count = "8-12" if comprehensive else "4-6"
    kind = "comprehensive" if comprehensive else "concise"
    return f"""IMPORTANT: you are working INSIDE the repository (cwd = repository root at {repo_root}).

Analyze this repository {owner}/{repo_name} and create a wiki structure for it.

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
"""  # noqa: E501 - Preserve the upstream prompt as a single literal.

def _generator_cache_page_prompt(title: str, file_paths: list[str], out_path: str, language: str) -> str:
    """Build a page prompt that requires a directly written Markdown artifact."""
    paths = "\n".join(f"- [{p}]({p})" for p in file_paths)
    return (
        "IMPORTANT: you are working INSIDE the repository (cwd = repository root). "
        "All paths below are relative to the repository root.\n\n"
        + _build_page_prompt(title, paths, language)
        + f"\n\nDELIVERABLE: Write the complete generated Markdown page to `{out_path}` using the "
          "Write tool (create the file; do not use Edit). Do NOT return the page in your message "
          "text — the written file is the only deliverable."
    )


async def generate_page(
    *, generator: str | None = None, generator_config: dict | None = None, repo: Repo, page: WikiPage,
    language: str, default_branch: str, run_id: str,
) -> str:
    """Load or generate one page artifact and apply final source-link formatting.

    Args:
        generator: Registered Agent adapter name, or the default when omitted.
        generator_config: Adapter-specific construction options.
        repo: Repository available to the Agent and used for source URLs.
        page: Page metadata and relevant source paths.
        language: Language code for human-readable output.
        default_branch: Branch used in remote source URLs.
        run_id: Project generation id and monitoring correlation id.

    Returns:
        Finalized Markdown page content.
    """
    out_path = _generator_cache_page_path(run_id, page.id, generator=generator, generator_config=generator_config)
    if out_path.exists():
        content = await asyncio.to_thread(out_path.read_text, encoding="utf-8")
    else:
        adapter = utils.adapt_generator(generator, generator_config=generator_config, repo=repo,
                                generator_cache_dir=str(out_path.parent),
                                generator_cache_write_mode=True)
        prompt = _generator_cache_page_prompt(
            page.title, list(page.filePaths), str(out_path), language,
        )
        content = await _produce_file(adapter, prompt, out_path,
                                      label=f"wiki:page:{page.id}", run_id=run_id)
    ctx = RepoUrlContext(type=repo.repo_type, repo_url=repo.repo_url, default_branch=default_branch)
    return _finalize_page_content(content, page, ctx)


async def hydrate_pages(
    *, project_key: str, generator: str | None = None, generator_config: dict | None = None, repo: Repo,
    structure: WikiStructureModel, default_branch: str,
) -> dict[str, WikiPage]:
    """Hydrate page snapshots from authoritative generator artifacts.

    Args:
        project_key: Canonical repository project key.
        generator: Registered Agent adapter name, or the default when omitted.
        generator_config: Adapter-specific construction options.
        repo: Repository used to build source URLs.
        structure: Wiki structure whose pages may have artifacts.
        default_branch: Branch used in remote source URLs.

    Returns:
        Pages with existing artifacts; missing pages remain for the caller to generate.
    """
    ctx = RepoUrlContext(type=repo.repo_type, repo_url=repo.repo_url, default_branch=default_branch)
    generated: dict[str, WikiPage] = {}
    for page in structure.pages:
        out_path = _generator_cache_page_path(
            project_key, page.id, generator=generator, generator_config=generator_config)
        if not out_path.exists():
            continue
        content = await asyncio.to_thread(out_path.read_text, encoding="utf-8")
        generated[page.id] = dataclasses.replace(
            page, content=_finalize_page_content(content, page, ctx),
        )
    return generated


def write_error_page(
    *, project_key: str, page: WikiPage, content: str,
    generator: str | None = None, generator_config: dict | None = None,
) -> None:
    """Persist an exhausted-retry placeholder so resume skips the failed page.

    Args:
        project_key: Canonical repository project key.
        page: Failed page whose id determines the artifact name.
        content: Placeholder content to persist.
        generator: Registered Agent adapter name, or the default when omitted.
        generator_config: Adapter-specific construction options.
    """
    try:
        out_path = _generator_cache_page_path(
            project_key, page.id, generator=generator, generator_config=generator_config)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
    except OSError as e:
        log(f"写入占位页文件失败: {page.id} - {e}")
