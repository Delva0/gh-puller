"""Independent HTTP header and JSON Pointer helpers."""

import math
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


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
