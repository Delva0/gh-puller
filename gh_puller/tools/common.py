"""Expose runtime tool storage and exact line excerpts for the search tools."""

import re

from .storage import ToolStorage

__all__ = ["ToolStorage", "page_excerpt"]


def page_excerpt(document: dict, start_line: int = 1, max_lines: int = 180, find: str = "") -> dict:
    """Return exact contiguous source lines; pagination and text continuation are independent."""
    lines = document["text"].splitlines()
    matches = [i + 1 for i, line in enumerate(lines) if find and find.casefold() in line.casefold()]
    if matches:
        start_line = max(1, matches[0] - 8)
    start = min(start_line - 1, len(lines))
    selected, chars = [], 0
    for index in range(start, min(start + max_lines, len(lines))):
        # Preserve individual source lines, including long Markdown table rows.
        if selected and chars + len(lines[index]) > 26000:
            break
        selected.append(f"L{index + 1}: {lines[index]}")
        chars += len(lines[index])
    end = start + len(selected)
    return {key: value for key, value in document.items() if key != "text"} | {
        "total_lines": len(lines), "start_line": start + 1, "end_line": end,
        "next_line": end + 1 if end < len(lines) else None,
        "headings": [{"line": i + 1, "text": line} for i, line in enumerate(lines) if re.match(r"^#{1,6} ", line)],
        "find": find, "matches": matches, "content": "\n".join(selected),
    }
