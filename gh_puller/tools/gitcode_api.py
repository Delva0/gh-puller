"""Native GitCode HTTP transport, rate limits and original response evidence."""

import asyncio
import base64
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin

import httpx
from jsonschema import Draft202012Validator

from .githost_api_utils import (
    APIReads,
    ResponseContent,
)
from .storage import ToolStorage
from .utils import retry_delay


class GitCodeResponseContent(ResponseContent):
    """GitCode's native file envelopes and saved pagination collection shape."""

    collection_pointer = ""

    @staticmethod
    def is_file(data) -> bool:
        return isinstance(data, dict) and data.get("encoding") == "base64" and isinstance(data.get("content"), str)

    @staticmethod
    def decode_file(data) -> bytes:
        return base64.b64decode("".join(data["content"].split()), validate=True)


def compact_fields(data: dict, role: str) -> list[str]:
    identity = None
    if role in {"user", "owner", "assignee", "sender", "author", "committer"} and "login" in data:
        identity = {"login", "id", "type", "url", "html_url", "name", "email", "date"}
    elif role in {"repo", "repository"} and "full_name" in data:
        identity = {
            "id",
            "name",
            "full_name",
            "description",
            "private",
            "fork",
            "url",
            "html_url",
            "default_branch",
            "archived",
            "disabled",
            "visibility",
        }
    return [
        key
        for key, value in data.items()
        if key in {"node_id", "avatar_url", "gravatar_id"}
        or (identity is not None and key not in identity)
        or (key.endswith("_url") and isinstance(value, str) and "{" in value)
    ]


API_ORIGIN = "https://api.gitcode.com"


DEFAULT_API_PREFIX = "/api/v5"


# Fixed resource mappings from the published REST catalog. Shared enterprise labels/member paths keep
# the default contract; explicit native API paths/URLs can select either published contract.
RESOURCE_PREFIXES = (
    (re.compile(r"^/(?:repos/[^/]+/[^/]+|orgs/[^/]+)/actions(?:/|$)"), "/api/v8"),
    (re.compile(r"^/org/[^/]+/enterprise/?$"), "/api/v8"),
    (re.compile(r"^/enterprise/[^/]+/customized_roles/?$"), "/api/v8"),
    (re.compile(r"^/enterprises/[^/]+/(?:groups/projects|issue_extend_field|milestones(?:/[^/]+)?)/?$"), "/api/v8"),
)


BACKEND = {
    "name": "GitCode",
    "origin": API_ORIGIN,
    "versions": ["v5", "v8"],
    "content_scope": "GitCode REST on api.gitcode.com",
    "methods": ["GET", "HEAD", "POST"],
    "post_scope": "Documented read-only queries only",
    "tool": "gitcode_api",
}


MAX_LOG_BYTES = 50 * 1024 * 1024


STEP_LOG_PATH = re.compile(r"^/api/v8/repos/[^/]+/[^/]+/actions/runs/[^/]+/jobs/[^/]+/logs/?$")


class GitCodeAPIError(RuntimeError):
    """A local request failure with any available response evidence attached."""

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = details


def api_url(path: str) -> httpx.URL:
    """Supply a resource's API prefix; explicit native paths and URLs retain their selected contract."""
    if path.startswith("//"):
        raise ValueError("Use a GitCode resource path or a complete GitCode API URL")
    url = httpx.URL(API_ORIGIN + path if path.startswith("/") else path)
    if (
        url.scheme != "https"
        or url.host not in {"api.gitcode.com", "gitcode.com"}
        or url.port not in {None, 443}
        or url.userinfo
        or url.fragment
        or any(part in {".", ".."} for part in url.path.split("/"))
    ):
        raise ValueError("Use a GitCode resource path or API URL without credentials or fragment")
    if not url.path.startswith("/api/"):
        if not path.startswith("/"):
            raise ValueError("Use a complete GitCode API URL, not a website URL")
        # Match encoded path segments so a namespace or filename containing %2F remains intact.
        resource = url.raw_path.partition(b"?")[0].decode("ascii")
        prefix = next((value for pattern, value in RESOURCE_PREFIXES if pattern.match(resource)), DEFAULT_API_PREFIX)
        url = url.copy_with(raw_path=prefix.encode("ascii") + url.raw_path)
    if any(
        key.casefold().replace("-", "_") in {"access_token", "private_token", "token", "authorization"}
        for key in url.params
    ):
        raise ValueError("Authentication parameters are supplied by the host, not tool arguments")
    return url.copy_with(host="api.gitcode.com")


def request_headers(request: dict) -> dict:
    headers = {"accept": "application/json", "user-agent": "Graphub-v5/1.0"}
    # These affect credentials, routing, framing, or the read-only HTTP method, rather than REST options.
    reserved = {
        "authorization",
        "proxy-authorization",
        "cookie",
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "trailer",
        "upgrade",
        "x-http-method-override",
        "x-method-override",
        "x-http-method",
        "private-token",
        "access-token",
        "token",
        "x-auth-token",
        "x-access-token",
    }
    supplied = {}
    for key, value in request.get("headers", {}).items():
        name = key.lower()
        if name.replace("_", "-") in reserved or name in supplied:
            raise ValueError(f"Header {key!r} is host-managed or duplicated")
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) or any(c in value for c in "\r\n\x00"):
            raise ValueError("Invalid HTTP header")
        supplied[name] = value
    if "accept" in request:
        if "accept" in supplied and supplied["accept"] != request["accept"]:
            raise ValueError("Specify the same Accept value, or use only accept or headers.Accept")
        if any(c in request["accept"] for c in "\r\n\x00"):
            raise ValueError("Invalid Accept header")
        supplied["accept"] = request["accept"]
    return headers | supplied


@lru_cache(maxsize=1)
def native_catalog():
    return json.loads(Path(__file__).with_name("gitcode_api_catalog.json").read_text())["operations"]


def readonly_post(path):
    return next(
        (
            entry
            for entry in native_catalog().values()
            if entry["method"] == "POST" and re.fullmatch(re.sub(r":\w+", "[^/]+", entry["path"]), path.rstrip("/"))
        ),
        None,
    )


def validate_native_body(entry, body):
    properties, required = {}, []
    for name, definition in entry["arguments"].items():
        if definition["in"] != "body":
            continue
        kind = definition["type"]
        item = {"type": kind.removesuffix("[]")}
        properties[name] = {"type": "array", "items": item} if kind.endswith("[]") else item
        if definition["required"]:
            required.append(name)
    if "custom_fields" in properties:
        properties["custom_fields"]["items"] = {
            "type": "object",
            "properties": {
                "field_name": {"type": "string"},
                "values": {"type": "array", "items": {"type": "string"}},
                "operation": {"enum": ["search", "greater_equal", "less_equal", "between", "in"]},
            },
            "required": ["operation"],
            "additionalProperties": False,
        }
    for key, minimum, maximum in (("page", 1, None), ("per_page", 1, 100)):
        if key in properties:
            properties[key].update(minimum=minimum, **({"maximum": maximum} if maximum else {}))
    Draft202012Validator(
        {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
    ).validate(body)


class GitCodeAPI(APIReads):
    """Share HTTP concurrency and rate limits; snapshots never stand in for a fresh path request."""

    provider = "gitcode"
    text_documents = True

    def cooldown_until(self, until):
        self.cooldown = max(self.cooldown, until)
        self.event("rate_limited", until=self.cooldown)

    def load_events(self, events):
        super().load_events(events)
        for event in events:
            if event["type"] == "gitcode/rate_limited" and event["data"].get("tool") == self.scope:
                self.cooldown_until(event["data"]["until"])

    def __init__(self, client: httpx.AsyncClient, storage: ToolStorage, *, token: str, concurrency: int = 8):
        super().__init__(client, storage, token=token, concurrency=concurrency)
        self.cooldown = 0.0

    @staticmethod
    def _cacheable(metadata: dict) -> bool:
        return 200 <= metadata.get("status", 0) < 300 and not any(key in metadata for key in ("error",))

    async def _wait(self, operation: str) -> None:
        delay = self.cooldown - time.time()
        if delay > 60:
            raise GitCodeAPIError("GitCode quota exhausted; wait until retry_at", retry_at=self.cooldown)
        if delay > 0:
            self.storage.event("gitcode/rate_wait", operation=operation, seconds=delay)
            await asyncio.sleep(delay)

    async def _fetch(
        self, operation: str, url: httpx.URL, method: str, headers: dict, payload: dict | None = None,
    ) -> dict:
        redirects, history, retried = [], [], False
        for attempt in range(7):
            try:
                await self._wait(operation)
            except GitCodeAPIError as exc:
                if history:
                    exc.details.update(result_id=history[-1]["result_id"], previous_responses=list(history))
                raise
            url = api_url(str(url))
            raw_path = url.raw_path.partition(b"?")[0].decode("ascii")
            if method == "POST" and (readonly_post(raw_path) is None or payload is None):
                raise GitCodeAPIError("POST is restricted to documented read-only queries", previous_responses=history)
            request_id = f"{operation}.http-{attempt + 1}"
            request = {"method": method, "url": str(url), "headers": headers}
            if payload is not None:
                request["json"] = payload
            self.storage.record(f"{request_id}.request.json", request)
            self.storage.event("gitcode/http_start", operation=operation, request=request_id, url=str(url))
            try:
                auth = {"authorization": f"Bearer {self.token}"} if self.token else {}
                response = await self.client.request(
                    method,
                    url,
                    headers=headers | auth,
                    auth=None,
                    follow_redirects=False,
                    **({"json": payload} if payload is not None else {}),
                )
            except BaseException as exc:
                self.storage.event(
                    "gitcode/http_error",
                    operation=operation,
                    request=request_id,
                    error=type(exc).__name__,
                    message=str(exc),
                )
                if isinstance(exc, Exception) and history:
                    raise GitCodeAPIError(
                        str(exc),
                        result_id=history[-1]["result_id"],
                        previous_responses=list(history),
                        failed_request=request,
                        cause={"type": type(exc).__name__, "message": str(exc)},
                    ) from exc
                raise
            metadata = self._http_metadata(request_id, request, response)
            metadata["rate_limit"] = {
                k.removeprefix("x-ratelimit-"): v for k, v in response.headers.items() if k.startswith("x-ratelimit-")
            }
            if redirects:
                metadata["redirects"] = list(redirects)
            if history:
                metadata["previous_responses"] = list(history)
            if response.is_error:
                try:
                    data = response.json()
                except ValueError:
                    data = None
                detail = data if isinstance(data, dict) else {}
                message = str(detail.get("error_message") or detail.get("message") or response.reason_phrase)
                limited = response.status_code == 429 or (
                    response.status_code == 403 and "retry-after" in response.headers
                )
                if limited:
                    until = time.time() + retry_delay(response.headers.get("retry-after"), default=60)
                    self.cooldown_until(until)
                    metadata["retry_at"] = self.cooldown
                metadata.update(
                    error={
                        "type": "GitCodeHTTPError",
                        "message": message,
                        **{k: detail[k] for k in ("error_code", "error_code_name") if k in detail},
                    },
                    rate_limited=limited,
                    fatal=False,
                )
            if response.has_redirect_location:
                try:
                    if payload is not None:
                        raise ValueError("Read-only POST redirects are not followed")
                    target = httpx.URL(urljoin(str(url), response.headers["location"]))
                    if target.host in {"api.gitcode.com", "gitcode.com"}:
                        target = api_url(str(target))
                        if len(redirects) >= 5:
                            raise ValueError("Too many GitCode redirects")
                    elif target.scheme in {"http", "https"} and not target.userinfo:
                        metadata.update(redirect_url=str(target), note="Download this temporary URL with web_fetch.")
                    else:
                        raise ValueError("Invalid external redirect URL")
                except (ValueError, httpx.InvalidURL) as exc:
                    metadata["error"] = {"type": type(exc).__name__, "message": str(exc)}
            if payload is not None and STEP_LOG_PATH.fullmatch(raw_path) and response.status_code == 200:
                try:
                    data = response.json()
                    metadata["next_request"] = None
                    more = data["has_more"]
                    if not isinstance(more, bool):
                        raise TypeError("Step logs require a boolean has_more")
                    if more:
                        cursor = data["end_offset" if payload["sort"] == "asc" else "start_offset"]
                        if (
                            isinstance(cursor, bool)
                            or not isinstance(cursor, int)
                            or cursor < 0
                            or cursor == payload["offset"]
                            or (payload["sort"] == "asc" and cursor < payload["offset"])
                            or (payload["sort"] == "desc" and payload["offset"] > 0 and cursor > payload["offset"])
                        ):
                            raise ValueError("Step-log cursor did not advance")
                        metadata["next_request"] = {
                            "path": re.sub(r"^/api/v\d+", "", raw_path),
                            "params": {**payload, "offset": cursor},
                        }
                    metadata["log_complete"] = not more
                except (ValueError, KeyError, TypeError) as exc:
                    metadata.update(error={"type": "LogPaginationError", "message": str(exc)}, log_complete=False)
            self.remember_response(metadata)
            self.storage.record(f"{request_id}.response.json",
                                {**metadata, "header_items": response.headers.multi_items()})
            self.storage.event(
                "gitcode/http_end",
                operation=operation,
                request=request_id,
                status=response.status_code,
                rate_limit=metadata["rate_limit"],
            )
            history.append({k: metadata[k] for k in ("result_id", "status", "api_url")})
            if response.has_redirect_location and "error" not in metadata and "redirect_url" not in metadata:
                url = target
                redirects.append(
                    {k: metadata[k] for k in ("result_id", "status", "api_url")} | {"location": str(target)},
                )
                continue
            if metadata.get("rate_limited") and not retried and metadata["retry_at"] - time.time() <= 60:
                retried = True
                continue
            return metadata
        raise GitCodeAPIError("Too many GitCode redirects or retries", **metadata)

    async def _download_log(self, operation: str, value: str) -> dict:
        """Bound downloads and attach credentials only to the GitCode API origin."""
        sources = []
        for attempt in range(6):
            url = httpx.URL(value)
            if url.scheme != "https" or not url.host or url.userinfo or url.fragment or url.port not in {None, 443}:
                raise GitCodeAPIError("Invalid log download URL", source_result_ids=sources)
            headers = {"User-Agent": "Graphub-v5/1.0", "Accept": "text/plain, application/zip, */*"}
            auth = {}
            if url.host in {"api.gitcode.com", "gitcode.com"}:
                url = api_url(str(url))
                await self._wait(operation)
                if self.token:
                    auth = {"Authorization": "Bearer " + self.token}
            result_id = f"{operation}.download-{attempt + 1}"
            request = {"method": "GET", "url": str(url), "headers": headers}
            self.storage.record(result_id + ".request.json", request)
            self.storage.event("gitcode/http_start", operation=operation, request=result_id, url=str(url))
            body_name = result_id + ".body"
            metadata = {
                "result_id": result_id,
                "kind": "ci_log_download",
                "request": request,
                "api_url": str(url),
                "headers": {},
                "body_file": str(self.storage.path(body_name).relative_to(self.storage.root)),
                "body_bytes": 0,
                "download_complete": False,
                "fetched_at": datetime.now(UTC).isoformat(),
                "source_result_ids": list(sources),
            }
            response = None
            try:
                # Fresh requests bypass client-level cookies, authorization and headers on signed URLs.
                outbound = httpx.Request("GET", url, headers=headers | auth)
                response = await self.client.send(outbound, auth=None, follow_redirects=False, stream=True)
                metadata.update(status=response.status_code, headers=dict(response.headers))
                with self.storage.binary(body_name) as output:
                    async for chunk in response.aiter_bytes():
                        remaining = MAX_LOG_BYTES - metadata["body_bytes"]
                        output.write(chunk[:remaining])
                        metadata["body_bytes"] += min(len(chunk), remaining)
                        if len(chunk) > remaining:
                            raise ValueError("Log download exceeds 50 MiB; partial body retained")
                metadata["download_complete"] = True
                if not response.has_redirect_location and response.status_code != 200:
                    metadata["error"] = {
                        "type": "GitCodeHTTPError",
                        "message": f"Expected log HTTP 200; got {response.status_code}",
                    }
                    if response.status_code == 429 or (
                        response.status_code == 403 and "retry-after" in response.headers
                    ):
                        until = time.time() + retry_delay(response.headers.get("retry-after"), default=60)
                        self.cooldown_until(until)
                        metadata.update(rate_limited=True, retry_at=self.cooldown)
            except BaseException as exc:
                metadata["error"] = {"type": type(exc).__name__, "message": str(exc)}
                if not (self.storage.root / metadata["body_file"]).exists():
                    self.storage.write(body_name, b"")
                if not isinstance(exc, Exception):
                    raise
            finally:
                if response is not None:
                    await response.aclose()
                metadata["body_sha256"] = hashlib.sha256(self._body(metadata)).hexdigest()
                self.remember_response(metadata)
                self.storage.record(result_id + ".response.json", metadata)
                self.storage.event(
                    "gitcode/http_end",
                    operation=operation,
                    request=result_id,
                    status=metadata.get("status"),
                    download_complete=metadata["download_complete"],
                )
            sources.append(result_id)
            if "error" in metadata or response is None or not response.has_redirect_location:
                return metadata
            value = urljoin(str(url), response.headers["location"])
        raise GitCodeAPIError("Too many log download redirects", result_id=result_id, source_result_ids=sources)
