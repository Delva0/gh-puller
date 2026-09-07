"""Generate grounded codemaps as a two-phase NDJSON event stream.

Skeleton failures emit an error event; enrichment failures retain the usable skeleton.
Source snippets, rather than model-provided line numbers, anchor citations.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field

from ..utils import Repo, _event, _extract_json, _phase
from . import (
    utils,  # Keep helper patches visible at call time.
)
from .utils import log

# --- Wire models ---


@dataclass
class CodeMapCitation:
    file_path: str  # Repository-relative path of the source file
    start_line: int | None = None  # 1-based line range start
    end_line: int | None = None  # 1-based line range end
    snippet: str = ""  # Verbatim excerpt copied from the source (used to locate the range)


@dataclass
class CodeMapStep:
    id: str  # Human-facing id such as '1a', '1b', '2a'
    label: str  # Short title of the step
    code: str = ""  # Example code snippet illustrating the step
    citation: CodeMapCitation | None = None  # Where this step's code comes from


@dataclass
class CodeMapSection:
    id: str  # Section id such as '1', '2'
    title: str  # Section title
    guide: str = ""  # Prose guide for the section (filled in phase 2)
    diagram: str = ""  # Mermaid diagram source (filled in phase 2)
    steps: list[CodeMapStep] = field(default_factory=list)


@dataclass
class CodeMap:
    title: str  # Overall codemap title
    summary: str = ""  # Introductory summary
    sections: list[CodeMapSection] = field(default_factory=list)


def codemap_of(d: dict) -> CodeMap:
    """Build a codemap recursively while applying wire-format defaults.

    Args:
        d: Decoded codemap object with required title, section, and step fields.

    Returns:
        A codemap whose optional prose, code, and citation fields have stable defaults.

    Raises:
        ValueError: The decoded object does not satisfy the codemap structure.
    """
    try:
        return CodeMap(
            title=d["title"],
            summary=d.get("summary", ""),
            sections=[
                CodeMapSection(
                    id=s["id"],
                    title=s["title"],
                    guide=s.get("guide", ""),
                    diagram=s.get("diagram", ""),
                    steps=[
                        CodeMapStep(
                            id=t["id"],
                            label=t["label"],
                            code=t.get("code", ""),
                            citation=CodeMapCitation(**t["citation"]) if t.get("citation") else None,
                        )
                        for t in s.get("steps", [])
                    ],
                )
                for s in d.get("sections", [])
            ],
        )
    except (KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"无效 codemap 数据: {e}") from e

# Phase one produces a grounded codemap skeleton.
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

# Phase two adds prose guides and Mermaid diagrams.
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


# --- Citation grounding ---


def _locate_snippet(text: str, snippet: str) -> tuple[int, int] | None:
    """Locate a source snippet and return its one-based line range."""
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
    """Replace model-provided ranges with locations of real source snippets."""
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


# --- Generation ---


async def _codemap(
    *, generator: str | None = None, generator_config: dict | None = None, repo: Repo,
    question: str, language: str = "en",
):
    """Generate a skeleton, enrich it, and yield NDJSON events."""
    yield _phase("analyzing", "start")
    yield _phase("analyzing", "done", chunk_count=0)

    fmt = utils.prompt_fmt(repo, language=language)

    async def _run_json(prompt: str, attempts: int = 3) -> dict:
        """Collect and parse JSON, retrying each attempt in a fresh Agent session."""
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                adapter = utils.adapt_generator(
                    generator, generator_config=generator_config, repo=repo,
                    system_prompt=_CODEMAP_SKELETON_PROMPT.format(**fmt),
                )
                async with adapter.session(
                    session_name="codemap:skeleton", run_id=f"codemap:{repo.name}"):
                    raw = await adapter.result(prompt)
                return _extract_json(raw)
            except Exception as e:
                last_error = utils.failure(e)
                log(f"codemap JSON 解析尝试 {attempt}/{attempts} 失败: {last_error}")
        raise ValueError(f"Model did not return valid JSON after {attempts} attempts: {last_error}")

    # Phase one is required for a usable codemap.
    yield _phase("initial_codemap", "start")
    skeleton_prompt = f"{question}"
    try:
        skeleton = codemap_of(await _run_json(skeleton_prompt))
    except Exception as e:
        log(f"codemap 骨架失败: {e}")
        yield _event(type="error", stage="initial_codemap", message=str(e))
        return
    yield _phase("initial_codemap", "done", section_count=len(skeleton.sections))

    # Phase two is optional; enrichment failure degrades to the skeleton.
    yield _phase("diagrams", "start")
    enrich_query = (
        f"{question}\n\n<SKELETON>\n{json.dumps(dataclasses.asdict(skeleton))}\n</SKELETON>"
    )
    enrich_prompt = f"{enrich_query}"
    final = skeleton
    try:
        adapter = utils.adapt_generator(
            generator, generator_config=generator_config, system_prompt=_CODEMAP_ENRICH_PROMPT.format(**fmt), repo=repo,
        )
        async with adapter.session(session_name="codemap:enrich", run_id=f"codemap:{repo.name}"):
            raw = await adapter.result(enrich_prompt)
        final = codemap_of(_extract_json(raw))
        yield _phase("diagrams", "done")
    except Exception as e:
        err = utils.failure(e)
        log(f"codemap 指南/图失败,使用骨架: {err}")
        yield _phase("diagrams", "done", degraded=True)

    _ground_citations(final, repo.save_path)
    yield _event(type="codemap", data=dataclasses.asdict(final))
    yield _event(type="done")


async def generate_codemap(
    *, generator: str | None = None, generator_config: dict | None = None, repo: Repo,
    question: str, language: str = "en",
):
    """Stream a two-phase codemap generation.

    Args:
        generator: Registered Agent adapter name, or the default when omitted.
        generator_config: Adapter-specific construction options.
        repo: Repository to analyze and use for citation grounding.
        question: Usage question that the codemap should answer.
        language: Language code for human-readable output.

    Yields:
        NDJSON phase, codemap, error, and completion events.
    """
    async for ev in _codemap(
        generator=generator, generator_config=generator_config,
        repo=repo, question=question, language=language,
    ):
        yield ev
