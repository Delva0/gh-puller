"""Normalize native compact graph tables for vllm-kb consumers."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from vllm_kb_adapter.upstream import structured_content


def normalize_result(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    """Return a CallToolResult whose text and structured forms agree.

    Args:
        tool: Checklist tool whose native result is being adapted.
        result: Upstream CallToolResult envelope.
    """
    if result.get("isError"):
        return result
    data = structured_content(result)
    if data is None:
        return result
    normalized = deepcopy(data)
    if tool in {"search_graph", "search_code"}:
        normalized = _normalize_search(normalized)
    elif tool == "trace_path":
        normalized = _normalize_trace(normalized)
    elif tool == "query_graph" and _is_table(normalized):
        normalized["rows"] = _table_rows(normalized)
    elif tool == "get_architecture":
        normalized = _normalize_architecture(normalized)
    envelope = dict(result)
    text = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    envelope["content"] = [{"type": "text", "text": text}]
    envelope["structuredContent"] = normalized
    envelope["isError"] = False
    return envelope


def _normalize_search(data: dict[str, Any]) -> dict[str, Any]:
    if _is_table(data):
        data["rows"] = _table_rows(data)
    elif isinstance(data.get("groups"), list):
        data["rows"] = _flatten_grouped_table(data)
    for key in ("semantic_results", "raw_matches"):
        nested = data.get(key)
        if not isinstance(nested, dict):
            continue
        if _is_table(nested):
            nested["rows"] = _table_rows(nested)
        elif isinstance(nested.get("groups"), list):
            nested["rows"] = _flatten_grouped_table(nested)
    return data


def _normalize_trace(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("callers", "callees", "impacted"):
        value = data.get(key)
        if isinstance(value, dict):
            data[key] = _flatten_grouped_table(value)
    if "next_cursor" in data and "next" not in data:
        data["next"] = data["next_cursor"]
    return data


def _normalize_architecture(data: dict[str, Any]) -> dict[str, Any]:
    for key, value in data.items():
        if isinstance(value, dict) and _is_table(value):
            data[key] = _table_rows(value)
    return data


def _is_table(value: dict[str, Any]) -> bool:
    columns = value.get("cols") or value.get("columns")
    return isinstance(columns, list) and isinstance(value.get("rows"), list)


def _table_rows(value: dict[str, Any]) -> list[dict[str, Any]]:
    columns = value.get("cols") or value.get("columns") or []
    rows = value.get("rows") or []
    return [dict(zip(columns, row, strict=False)) for row in rows if isinstance(row, list)]


def _flatten_grouped_table(value: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _table_rows(value) if _is_table(value) else []
    columns = value.get("cols") or value.get("columns") or []
    groups = value.get("groups")
    if not isinstance(groups, list):
        return rows
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("rows"), list):
            continue
        prefix = group.get("qn_prefix")
        file_path = group.get("file") or group.get("file_path")
        for raw in group["rows"]:
            if not isinstance(raw, list):
                continue
            row = dict(zip(columns, raw, strict=False))
            name = row.get("name")
            if isinstance(prefix, str) and isinstance(name, str):
                row["qn"] = f"{prefix}.{name}" if prefix else name
            if isinstance(file_path, str) and "file" not in row:
                row["file"] = file_path
            rows.append(row)
    return rows
