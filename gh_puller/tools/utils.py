"""Shared HTTP, credential validation, JSON Pointer and exact line-excerpt helpers."""

import math
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx


def retry_delay(value: str | None, *, default: float) -> float:
    """Parse Retry-After as seconds or an HTTP date, with a caller-selected fallback."""
    try:
        delay = float(value) if value is not None else default
    except ValueError:
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return default
    return max(0, delay) if math.isfinite(delay) else default


def select_json(data, pointer: str):
    if pointer and not pointer.startswith("/"):
        raise ValueError("json_pointer must be empty or start with /")
    for part in pointer.split("/")[1:]:
        if re.search(r"~(?![01])", part):
            raise ValueError("Invalid JSON Pointer escape")
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(data, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
            data = data[int(key)]
        elif isinstance(data, dict):
            data = data[key]
        else:
            raise ValueError("json_pointer does not identify a value in this response")
    return data


def json_pointer(parts):
    """Encode path components as an RFC 6901 pointer."""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def replace_pointer(value, at, replacement):
    if not at:
        return replacement
    head, tail = at.rsplit("/", 1)
    parent = select_json(value, head)
    tail = tail.replace("~1", "/").replace("~0", "~")
    parent[int(tail) if isinstance(parent, list) else tail] = replacement
    return value


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


async def validate_http_credential(value, *, url, header="Authorization", prefix="Bearer ", transport=None):
    """Probe an operator-declared provider endpoint using the tools' outbound network policy.

    Args:
        value: Credential to send in the declared header, never in the URL.
        url: Fixed provider endpoint declared by the tool module, not supplied by an end user.
        header: Provider authentication header.
        prefix: Header value prefix, empty for subscription-token APIs.
        transport: Optional HTTP transport for callers and tests.
    """
    try:
        async with (
            httpx.AsyncClient(transport=transport, timeout=15, follow_redirects=False) as client,
            client.stream("GET", url, headers={"Accept": "application/json", header: prefix + value}) as response,
        ):
            if response.status_code in {401, 403}:
                return {"valid": False, "reason": "API Key 验证失败"}
            response.raise_for_status()
            return {"valid": True, "reason": ""}
    except httpx.HTTPError:
        return {"valid": False, "reason": "连接测试失败，请检查凭据、额度和网络"}
