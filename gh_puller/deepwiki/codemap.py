"""codemap 主线:两阶段 codemap 生成(骨架 → 指南/图),NDJSON 事件流;阶段失败
语义与原相同(骨架失败 error 事件;指南/图失败退化为骨架)。

入口 generate_codemap 按 choice.generator 内联分派(cc/dsh/codex →
_agent_codemap;llm → _llm_codemap,分派规则与 wiki._wiki_pipeline 同);本主线
专用 helper:骨架/富化提示词(带 JSON 输出格式与引用接地规则)、codemap 引用
接地(_ground_citations/_locate_snippet —— snippet 为权威,LLM 给的行号不可靠)。
跨功能通用 helper(四路装配/检索簇/提示词共性)在 utils。
"""

from __future__ import annotations

import os

from .models import CodeMap

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


# ---------------------------------------------------------------------------
# codemap 引用接地:模型产出的 snippet 在真实源码里重新定位行号(权威覆盖)
# ---------------------------------------------------------------------------


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
    """用真实源码里的 snippet 位置覆盖每条引用的行号范围(codemap 接地)。"""
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
