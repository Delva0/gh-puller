"""Shared native HTTP reads, response storage and recoverable views for Git-host APIs."""

import asyncio
import base64
import hashlib
import json
import re
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from functools import cached_property

import httpx
from jsonschema import ValidationError

from .registry import ToolInputError, ToolProvider, input_error
from .storage import ToolStorage
from .utils import json_pointer, replace_pointer, select_json


def page_error(exc):
    details = dict(getattr(exc, "details", {}))
    return {"type": type(exc).__name__, "message": str(exc), **details.pop("input_error", {}), **details}


def request_key(url, method, headers, payload=None):
    """Display options never enter the identity; preserve repeated parameters and header variants."""
    return json.dumps(
        [method, str(url), {k.lower(): v for k, v in headers.items()}, payload], sort_keys=True, separators=(",", ":"),
    )


async def collect_pages(
    read,
    request,
    *,
    max_pages,
    max_items,
    first_page=None,
    backwards=False,
    key=lambda request: json.dumps(request, sort_keys=True),
):
    """Collect bounded pages; adapters supply items, continuation, counts and any original evidence."""
    items, pages, seen = [], [], set()
    error, retry, incomplete, stop = None, None, False, "max_pages"
    for number in range(max_pages):
        retry = request
        try:
            if first_page is None:
                identity = key(request)
                if identity in seen:
                    raise ToolInputError(
                        "Pagination did not advance; repeated URL or cursor", code="pagination_stalled",
                    )
                seen.add(identity)
            page = first_page if first_page is not None else await read(request, max_items - len(items), number)
            first_page = None
            pages.append(page)
            extra = page["items"]
            if not isinstance(extra, list) or len(extra) > max_items - len(items):
                raise ToolInputError("A continuation must return a list within the remaining item budget.")
            if backwards:
                items[:0] = extra
            else:
                items.extend(extra)
            incomplete |= page.get("incomplete", False)
            request = page.get("next_request")
            error = page.get("error")
            if error:
                retry, stop = page.get("retry_request", retry), "page_error"
                break
            if request is None:
                stop = page.get("stop_reason") or ("source_incomplete" if incomplete else "exhausted")
                break
            if len(items) >= max_items:
                stop = "max_items"
                break
        except Exception as exc:
            error, stop = page_error(exc), "page_error"
            break
    stable, observed = {}, {}
    for name in {key for page in pages for key in page.get("counts", {})}:
        values = [page["counts"][name] for page in pages if name in page.get("counts", {})]
        distinct = list({json.dumps(value, sort_keys=True): value for value in values}.values())
        if len(distinct) > 1:
            observed[name] = distinct
        elif len(values) == len(pages):
            stable[name] = values[0]
    if observed and stop == "exhausted":
        stop = "source_changed"
    return {
        "items": items,
        "pages": pages,
        "stop_reason": stop,
        "complete": stop == "exhausted",
        "source_incomplete": incomplete,
        "next_request": request,
        "retry_request": retry if error else None,
        "counts": stable,
        "observed_counts": observed,
        **({"error": error} if error else {}),
    }


@dataclass
class ReadFlight:
    task: asyncio.Task
    readers: int = 0


@dataclass
class ReadCache:
    """One question's reads; pending requests are shared independently of snapshot reuse."""

    responses: dict = dataclass_field(default_factory=dict)
    pending: dict = dataclass_field(default_factory=dict)


class APIReads:
    """Own reads per provider/session; platform adapters retain request and response semantics."""

    provider = "api"
    reuse_reads = False
    text_documents = False

    def __init__(self, client: httpx.AsyncClient, storage: ToolStorage, *, token: str, concurrency: int = 8):
        self.client, self.storage, self.token = client, storage, token
        self.limit = asyncio.Semaphore(concurrency)
        self.responses: dict[str, dict] = {}
        self._active_reads = ContextVar(f"{self.provider}_reads", default=None)
        self._reads = ReadCache()

    def _failure(self, exc):
        details = dict(getattr(exc, "details", {}))
        error = (
            input_error(exc)
            if isinstance(exc, (ValueError, TypeError, LookupError, ValidationError))
            else {"type": type(exc).__name__, "message": str(exc)}
        )
        error.update(details.pop("input_error", {}))
        if "error" in details:
            details["response_error"] = details["error"]
            if error.get("code") == "invalid_view":
                details["view_error"], error = error, details["error"]
        return {**details, "error": error, "fatal": False}

    @staticmethod
    def _metadata(metadata, request):
        return metadata

    @staticmethod
    def _cacheable(metadata: dict) -> bool:
        raise NotImplementedError("The platform must decide whether a response is reusable")

    def begin_query(self) -> None:
        self._reads = ReadCache()

    @contextmanager
    def read_scope(self):
        token = self._active_reads.set(self._reads)
        try:
            yield
        finally:
            self._active_reads.reset(token)

    def clear_context(self) -> None:
        self.responses.clear()
        self.begin_query()

    async def _job(self, call_id: str, arguments: dict, action) -> dict:
        arguments = arguments if isinstance(arguments, dict) else {"input": arguments}
        operation = self.storage.allocate(f"{self.provider}-api")
        self.storage.event(
            "io/queued", operation=operation, call_id=call_id, kind_name=f"{self.provider}-api", arguments=arguments,
        )
        try:
            async with self.limit:
                self.storage.event("io/start", operation=operation, call_id=call_id)
                result = await action(operation)
            self.storage.event(
                "io/end",
                operation=operation,
                call_id=call_id,
                status="failed" if "error" in result else "completed",
                **{k: result[k] for k in ("error",) if k in result},
            )
        except BaseException as exc:
            result = self._metadata(self._failure(exc), arguments)
            error = result["error"]
            self.storage.event("io/end", operation=operation, call_id=call_id, status="failed", error=error)
            self.storage.record(f"{operation}.result.json", {"operation": operation, **arguments, **result})
            if not isinstance(exc, Exception):
                raise
        else:
            self.storage.record(f"{operation}.result.json", {"operation": operation, **arguments, **result})
        return {"operation": operation, **arguments, **result}

    async def _request(
        self,
        operation: str,
        url: httpx.URL,
        method: str,
        headers: dict,
        payload: dict | None = None,
        *,
        reuse: bool | None = None,
    ) -> dict:
        cache = self._active_reads.get()
        if cache is None:
            return await self._fetch(operation, url, method, headers, payload)
        reuse = self.reuse_reads if reuse is None else reuse
        identity = request_key(url, method, headers, payload)
        if reuse and identity in cache.responses:
            metadata = cache.responses[identity]
            self.storage.event(f"{self.provider}/cache_hit", operation=operation,
                               source_result_id=metadata["result_id"])
            return metadata
        flight = cache.pending.get(identity)
        shared = flight is not None and not flight.task.done()
        if not shared:
            flight = ReadFlight(asyncio.create_task(self._fetch(operation, url, method, headers, payload)))
            cache.pending[identity] = flight
        flight.readers += 1
        try:
            metadata = await asyncio.shield(flight.task)
            if reuse and self._cacheable(metadata):
                cache.responses[identity] = metadata
            if shared:
                self.storage.event(
                    f"{self.provider}/request_shared", operation=operation, source_result_id=metadata["result_id"],
                )
            return metadata
        finally:
            flight.readers -= 1
            if not flight.readers:
                if cache.pending.get(identity) is flight:
                    cache.pending.pop(identity)
                if not flight.task.done():
                    flight.task.cancel()
                # Reap exceptions and cancellation even when the last reader leaves early.
                with suppress(asyncio.CancelledError, Exception):
                    await flight.task

    def _saved(self, result_id: str) -> dict:
        metadata = self.responses.get(result_id)
        if metadata is None:
            raise ValueError("Unknown result_id; use an ID returned in this conversation")
        return metadata

    def _body(self, metadata: dict) -> bytes:
        return (self.storage.root / metadata["body_file"]).read_bytes()

    def _derived(self, operation: str, kind: str, request: dict, data, **details) -> dict:
        """Save a derived document without inventing a native HTTP response status."""
        result_id = f"{operation}.{kind}"
        text = self.text_documents and isinstance(data, str)
        body = data.encode() if text else json.dumps(data, ensure_ascii=False).encode()
        metadata = {
            "result_id": result_id,
            "kind": kind,
            "request": request,
            "headers": {"content-type": "text/plain" if text else "application/json"},
            "body_file": self.storage.write(result_id + ".body", body),
            "body_bytes": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "created_at": datetime.now(UTC).isoformat(),
            **details,
        }
        self.responses[result_id] = metadata
        self.storage.record(result_id + ".response.json", metadata)
        self.storage.event(f"{self.provider}/response_saved", operation=operation, request=result_id, kind_name=kind)
        return metadata

    def _http_metadata(self, request_id, request, response):
        body = response.content
        return {
            "result_id": request_id,
            "request": request,
            "status": response.status_code,
            "api_url": str(response.url),
            "headers": dict(response.headers),
            "body_file": self.storage.write(f"{request_id}.body", body),
            "body_bytes": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "fetched_at": datetime.now(UTC).isoformat(),
            "pagination": {k: v["url"] for k, v in response.links.items()},
        }


class APIProvider(ToolProvider):
    """A tool owns a read service; service state never depends on tool registration."""

    api_type = None

    def __init__(self, *args, **kwargs):
        self.api = self.api_type(*args, **kwargs)

    @property
    def storage(self):
        return self.api.storage

    @property
    def responses(self):
        return self.api.responses

    @property
    def unavailable(self):
        return getattr(self.api, "unavailable", None)

    def begin_query(self):
        self.api.begin_query()

    def clear_context(self):
        self.api.clear_context()

    # Retain the saved-evidence Python entry points used by recorded replays.
    def _saved(self, result_id):
        return self.api._saved(result_id)

    def _body(self, metadata):
        return self.api._body(metadata)


LINE_OPTIONS = {"start_line", "end_line", "max_lines", "tail_lines", "context_lines"}


TEXT_OPTIONS = LINE_OPTIONS | {"find", "offset", "max_chars"}


DISPLAY_OPTIONS = LINE_OPTIONS | {"view", "json_pointer", "fields", "offset", "max_chars", "find"}


class ResponseContent:
    """Complete response data; platforms define file envelopes and collection shapes."""

    collection_pointer = None

    def __init__(self, body: bytes, headers: dict):
        self.body = body
        self.media = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        self.is_json = bool(body) and (self.media == "application/json" or self.media.endswith("+json"))
        self.data = None
        if self.is_json:
            try:
                self.data = json.loads(body)
            except (ValueError, UnicodeError):
                self.is_json = False

    @staticmethod
    def is_file(data) -> bool:
        return False

    @staticmethod
    def decode_file(data) -> bytes:
        raise ValueError("This response is not a native file envelope")

    @cached_property
    def file_bytes(self) -> bytes:
        return self.decode_file(self.data)


def validate_display(request: dict) -> None:
    """Reject display combinations known to be invalid before downloading a response."""
    pointers = {"/json_pointer": request.get("json_pointer", "")}
    pointers.update({f"/fields/{key}": value for key, value in request.get("fields", {}).items()})
    for path, pointer in pointers.items():
        if (pointer and not pointer.startswith("/")) or re.search(r"~(?![01])", pointer):
            raise ToolInputError("Use a JSON Pointer beginning with /; escape ~ as ~0 and / as ~1.", path=path)
    if request.get("fields") and (request.get("view") in {"text", "raw"} or LINE_OPTIONS.intersection(request)):
        raise ToolInputError("fields requires a JSON compact/full view without line options.", path="/fields")
    if "tail_lines" in request and ("start_line" in request or "end_line" in request):
        raise ToolInputError("Use tail_lines or start_line/end_line, not both.", path="/tail_lines")
    if "end_line" in request and request["end_line"] < request.get("start_line", 1):
        raise ToolInputError("end_line must be at least start_line.", path="/end_line")


def view_error(metadata: dict, request: dict, content: ResponseContent, exc: Exception) -> dict:
    """Explain a failed selection and recover locally using the saved response's actual shape."""
    path = (
        "/json_pointer"
        if isinstance(exc, LookupError) or "json_pointer" in str(exc)
        else ("/fields" if "fields" in str(exc) else "/view")
    )
    recovery = {"result_id": metadata["result_id"], "view": "full"}
    if "max_chars" in request:
        recovery["max_chars"] = request["max_chars"]
    detail = {"code": "invalid_view", "path": path, "message": str(exc), "retryable": False}
    data = content.data
    if content.is_json:
        detail["response_shape"] = (
            {"type": "array"}
            if isinstance(data, list)
            else {"type": "object", "keys": list(data)}
            if isinstance(data, dict)
            else {"type": "scalar"}
        )
    if metadata.get("kind") == "collection" and not metadata.get("error"):
        pointer = content.collection_pointer
        items = None
        if pointer is not None:
            with suppress(ValueError, TypeError, LookupError):
                items = select_json(data, pointer)
        if isinstance(items, list):
            detail["message"] += (
                f" This saved pagination collection exposes its items at json_pointer "
                f"{json.dumps(pointer)}; select fields relative to each item."
            )
            detail["response_shape"]["items_pointer"] = pointer
            recovery["json_pointer"] = pointer
            if request.get("fields"):
                try:
                    omissions = []
                    project_json(select_json(data, pointer), request["fields"], pointer, omissions)
                    if not any(o["reason"] == "missing_projection_field" for o in omissions):
                        recovery["fields"] = request["fields"]
                except (ValueError, TypeError, LookupError):
                    pass  # A second invalid projection must not prevent access to the original evidence.
    detail["recovery_request"] = recovery
    return detail


def project_json(data, fields: dict, pointer: str, omissions: list, *, compact_fields=None):
    """Project objects without conflating absent fields and explicit null values."""
    for source in fields.values():
        if (source and not source.startswith("/")) or re.search(r"~(?![01])", source):
            raise ValueError("fields values must be valid JSON Pointers relative to each object")
    omissions.append({"pointer": pointer, "reason": "field_projection", "fields": fields})

    def one(item, at):
        if not isinstance(item, dict):
            raise TypeError("fields requires an object or array of objects after json_pointer")
        projected = {}
        for name, source in fields.items():
            try:
                value = select_json(item, source)
            except (LookupError, ValueError):
                omissions.append({"pointer": at + source, "reason": "missing_projection_field", "field": name})
                continue
            projected[name] = (
                compact_json(value, at + source, omissions, omit_fields=compact_fields) if compact_fields else value
            )
        return projected

    if isinstance(data, list):
        return [one(item, f"{pointer}/{index}") for index, item in enumerate(data)]
    return one(data, pointer)


def line_window(text: str, request: dict) -> tuple[str, dict]:
    """Number physical text lines; character continuation never drops an oversized line."""
    lines = text.splitlines(keepends=True)
    total = len(lines)
    start, end = request.get("start_line", 1), request.get("end_line", total)
    count, context = request.get("max_lines", 200), request.get("context_lines", 3)
    if start < 1 or ("end_line" in request and end < start) or not 1 <= count <= 2000 or not 0 <= context <= 100:
        raise ValueError("Invalid line range, max_lines or context_lines")
    if "tail_lines" in request:
        if "start_line" in request or "end_line" in request or not 1 <= request["tail_lines"] <= 2000:
            raise ValueError("tail_lines must be 1..2000 and cannot be combined with start_line/end_line")
        start, count = max(1, total - request["tail_lines"] + 1), request["tail_lines"]
    end = min(end, total)
    match = None
    if request.get("find"):
        match = next((i + 1 for i in range(start - 1, end) if request["find"] in lines[i]), None)
        if match is None:
            return "", {
                "total_lines": total,
                "start_line": start,
                "end_line": None,
                "next_line": None,
                "match_line": None,
                "line_window_complete": False,
            }
        start = max(start, match - min(context, (count - 1) // 2))
        end = min(end, match + context)
    end = min(end, start + count - 1)
    excerpt = "".join(f"{index + 1}: {lines[index]}" for index in range(start - 1, end))
    return excerpt, {
        "total_lines": total,
        "start_line": start,
        "end_line": end if excerpt else None,
        "next_line": end + 1 if end < total and excerpt else None,
        "match_line": match,
        "line_window_complete": start == 1 and end == total,
    }


def compact_json(data, pointer: str, omissions: list, *, omit_fields, role: str = ""):
    """Preserve string values; trim repeated identities and links with an explicit audit trail."""
    if isinstance(data, dict):
        removed = omit_fields(data, role)
        if removed:
            omissions.append({"pointer": pointer, "reason": "compact_fields", "keys": removed})
        return {
            key: compact_json(
                value,
                pointer + "/" + key.replace("~", "~0").replace("/", "~1"),
                omissions,
                omit_fields=omit_fields,
                role=key,
            )
            for key, value in data.items()
            if key not in removed
        }
    if isinstance(data, list):
        return [
            compact_json(item, f"{pointer}/{index}", omissions, omit_fields=omit_fields)
            for index, item in enumerate(data)
        ]
    return data


def structured_window(value, budget, *, path=()):
    """Preview complete list items and small fields before windowing large values."""

    def size(v):
        return len(json.dumps(v, ensure_ascii=False))

    if size(value) <= budget:
        return value, []
    if isinstance(value, str):
        low, high = 0, len(value)
        while low < high:
            middle = (low + high + 1) // 2
            if size(value[:middle]) <= max(2, budget):
                low = middle
            else:
                high = middle - 1
        return value[:low], [{"path": json_pointer(path), "offset": low, "total_chars": len(value)}]
    if not isinstance(value, (dict, list)):
        return value, []
    available, pending = max(0, budget - 2), []
    if isinstance(value, list):
        result = []
        for index, child in enumerate(value):
            allowance = available - (2 if result else 0)
            if size(child) <= allowance:
                result.append(child)
                available = allowance - size(child)
                continue
            # A large first object can still expose its small fields and a text window.
            # Never turn many complete paths/IDs into tiny prefixes or empty strings.
            if not result and isinstance(child, (dict, list)) and allowance >= 2:
                excerpt, windows = structured_window(child, allowance, path=(*path, index))
                if excerpt:
                    result.append(excerpt)
                    pending.extend(windows)
            following = len(result)
            if not result:
                # A single item larger than the entire window is read at its own pointer,
                # rather than a skip:0 continuation which would repeat the same empty page.
                pending.append({"path": json_pointer((*path, index))})
                following = index + 1
            if following < len(value):
                pending.append({"path": json_pointer(path), "skip": following, "total_items": len(value)})
            break
        return result, pending

    kept = {}
    # Preserve short sibling fields even when a preceding body is very long.
    for name, child in sorted(value.items(), key=lambda item: size(item[1])):
        allowance = available - size(name) - 2 - (2 if kept else 0)
        if size(child) <= allowance:
            kept[name] = child
        elif allowance >= 2 and (isinstance(child, (dict, list)) or size(child) > budget):
            excerpt, windows = structured_window(child, allowance, path=(*path, name))
            kept[name] = excerpt
            pending.extend(windows)
        else:
            pending.append({"path": json_pointer((*path, name))})
            continue
        available = allowance - size(kept[name])
    return {name: kept[name] for name in value if name in kept}, pending


def continue_view(view: dict, request: dict) -> dict | None:
    """Describe the next local text window independently of the caller's saved-result syntax."""
    following = {k: v for k, v in request.items() if k in DISPLAY_OPTIONS}
    if view["next_offset"] is not None:
        return {**following, "offset": view["next_offset"]}
    if view.get("next_line") and not any(k in request for k in ("find", "tail_lines", "end_line")):
        following.pop("offset", None)
        return {**following, "start_line": view["next_line"]}
    return None


def selection_window(data, request, field_windows):
    """Apply selected text windows before a typed JSON preview, retaining every local continuation."""
    value = deepcopy(data) if field_windows else data
    pending, text_windows, applied = [], [], {}
    for original in field_windows:
        base = request.get("json_pointer", "")
        if base and not original["path"].startswith(base + "/"):
            continue
        window = {**original, "path": original["path"][len(base) :]}
        try:
            text = select_json(value, window["path"])
        except (LookupError, ValueError):
            continue
        if not isinstance(text, str):
            continue
        part = response_view(text.encode(), {"content-type": "text/plain"}, window["options"])
        applied[window["path"]] = {**window["options"], "offset": part["offset"]}
        text_windows.append(
            {
                "path": window["path"],
                **{
                    k: part[k]
                    for k in ("display_complete", "total_lines", "start_line", "end_line", "match_line", "match_offset")
                    if k in part
                },
            },
        )
        value = replace_pointer(value, window["path"], part.get("content", part.get("data")))
        if following := continue_view(part, window["options"]):
            pending.append(
                {
                    "path": window["path"],
                    "options": following,
                    **{k: following[k] for k in ("offset", "start_line") if k in following},
                    **({"total_chars": len(text)} if "offset" in following else {}),
                },
            )
    preview, cropped = structured_window(value, request.get("max_chars", 16000))
    for crop in cropped:
        if crop["path"] in applied:
            pending = [w for w in pending if w["path"] != crop["path"]]
            crop["options"] = applied[crop["path"]]
            crop["offset"] = crop.get("offset", 0) + crop["options"]["offset"]
    pending.extend(cropped)
    return {
        "data": preview,
        "display_complete": not pending and all(w["display_complete"] for w in text_windows),
        "total_chars": len(json.dumps(data, ensure_ascii=False)),
        "offset": 0,
        "end_offset": None,
        "next_offset": None,
        **({"windows": pending} if pending else {}),
        **({"text_windows": text_windows} if text_windows else {}),
    }


def response_view(
    body: bytes | ResponseContent,
    headers: dict,
    request: dict,
    *,
    octet_stream_text: bool = False,
    raw_hint: str = "request the native raw Accept type",
    default_items_key: str = "",
    default_file_text: bool = False,
    compact_fields=None,
    structured: bool = False,
    plain_json_strings: bool = False,
    field_windows=(),
) -> dict:
    """Default field projection to the named array only when json_pointer is omitted."""
    view, pointer = request.get("view", "compact"), request.get("json_pointer", "")
    if view not in {"compact", "full", "text", "raw"}:
        raise ValueError("view must be compact, full, text or raw")
    offset, max_chars = request.get("offset", 0), request.get("max_chars", 16000)
    if offset < 0 or not 1 <= max_chars <= 40000:
        raise ValueError("offset must be nonnegative and max_chars between 1 and 40000")
    # Preserve JSON media semantics, while raw always exposes the original bytes, including JSON whitespace.
    content = body if isinstance(body, ResponseContent) else ResponseContent(body, headers)
    body, media = content.body, content.media
    is_json = view != "raw" and content.is_json
    data, omissions = content.data if is_json else None, []
    numbered = bool(LINE_OPTIONS.intersection(request))
    if pointer and not is_json:
        raise ValueError("json_pointer requires a JSON response")
    if is_json:
        if (
            default_items_key
            and request.get("fields")
            and "json_pointer" not in request
            and isinstance(data, dict)
            and isinstance(data.get(default_items_key), list)
        ):
            pointer = "/" + default_items_key.replace("~", "~0").replace("/", "~1")
        data = select_json(data, pointer)
    if (
        default_file_text
        and "view" not in request
        and not pointer
        and not request.get("fields")
        and content.is_file(data)
    ):
        view = "text"
    if request.get("fields") and (not is_json or numbered or view in {"text", "raw"}):
        raise ValueError("fields requires a JSON compact/full view without line options")
    if plain_json_strings and is_json and isinstance(data, str):
        body, is_json = data.encode("utf-8"), False
    if view == "text" and is_json:
        if not content.is_file(data):
            raise ValueError(f"view='text' requires base64 file/blob content; use view='full' or {raw_hint}")
        body = content.file_bytes if data is content.data else content.decode_file(data)
        is_json = False
    if numbered and is_json:
        if not isinstance(data, str):
            raise ValueError("Line options require text, a JSON string pointer, or view='text' for base64 files")
        body, is_json = data.encode("utf-8"), False
    if is_json:
        compactor = compact_fields if view == "compact" else None
        if request.get("fields"):
            data = project_json(data, request["fields"], pointer, omissions, compact_fields=compactor)
        elif compactor:
            data = compact_json(data, pointer, omissions, omit_fields=compactor)
        if structured and isinstance(data, (dict, list)) and not (set(request) & (TEXT_OPTIONS - {"max_chars"})):
            preview = selection_window(data, request, field_windows)
            return {
                "view": view,
                "json_pointer": pointer,
                "representation": "json",
                **preview,
                "display_complete": preview["display_complete"] and not omissions,
                "omission_count": len(omissions),
                "omissions": omissions[:32],
            }
        text, encoding = json.dumps(data, ensure_ascii=False, indent=2), "json"
    else:
        # Never replace undecodable bytes or strip trailing newlines. Binary chunks concatenate to valid base64.
        binary_media = {"application/zip", "application/gzip", "application/pdf"}
        if not octet_stream_text:
            binary_media.add("application/octet-stream")
        try:
            text, encoding = body.decode("utf-8"), "utf-8"
            if "\x00" in text or (not numbered and media in binary_media):
                raise UnicodeError
        except UnicodeError:
            text, encoding = base64.b64encode(body).decode("ascii"), "base64"
    line_info = {}
    if numbered:
        if encoding == "base64":
            raise ValueError("Line options require UTF-8 text, not binary data")
        text, line_info = line_window(text, request)
    find = request.get("find", "")
    match = text.find(find, offset) if find and not numbered else -1
    if match >= 0:
        offset = max(offset, match - min(200, max_chars // 4))
    offset = min(offset, len(text))
    end = min(offset + max_chars, len(text))
    result = {
        "view": view,
        "json_pointer": pointer,
        "representation": encoding,
        "display_complete": offset == 0
        and end == len(text)
        and not omissions
        and line_info.get("line_window_complete", True),
        "omission_count": len(omissions),
        "omissions": omissions[:32],
        "total_chars": len(text),
        "offset": offset,
        "end_offset": end,
        "next_offset": end if end < len(text) else None,
        **line_info,
    }
    if numbered and end < len(text):
        result["next_line"] = None  # Finish this numbered window with next_offset first.
    if find and not numbered:
        result.update(find=find, match_offset=match if match >= 0 else None)
    if is_json and offset == 0 and end == len(text) and not find:
        result["data"] = data
    else:
        result["content"] = text[offset:end]
    return result
