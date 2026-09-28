"""Native GitHub HTTP transport, rate limits and original response evidence."""

import asyncio
import base64
import hashlib
import re
import time
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx
from graphql import (
    GraphQLError,
)

from .githost_api_utils import (
    APIReads,
    ResponseContent,
)
from .storage import ToolStorage
from .utils import retry_delay


class GitHubResponseContent(ResponseContent):
    """GitHub's native file envelopes and saved pagination collection shape."""

    collection_pointer = "/items"

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


API_ORIGIN = "https://api.github.com"


API_VERSION = "2026-03-10"


BACKEND = {
    "name": "GitHub",
    "origin": API_ORIGIN,
    "version": API_VERSION,
    "content_scope": "GitHub REST and read-only GraphQL on api.github.com",
    "methods": ["GET", "HEAD"],
    "graphql": "POST /graphql, query operations only",
    "tools": ["github", "github_graphql"],
}


MAX_LOG_BYTES = 50 * 1024 * 1024


SEARCH_FIELDS = ("total_count", "incomplete_results", "search_type", "lexical_fallback_reason", "truncated")


def search_status(data) -> dict:
    return {key: data[key] for key in SEARCH_FIELDS if key in data} if isinstance(data, dict) else {}


def result_metadata(metadata: dict, request: dict) -> dict:
    """Keep decision-making metadata visible; recover protocol details with full/raw or HEAD."""
    if request.get("view") in {"full", "raw"} or request.get("method") == "HEAD":
        return metadata
    hidden = {"request", "body_file", "body_bytes", "body_sha256", "header_items"}
    result = {key: value for key, value in metadata.items() if key not in hidden}
    if "headers" in result:
        actionable = {
            "etag",
            "last-modified",
            "content-range",
            "accept-ranges",
            "retry-after",
            "location",
            "www-authenticate",
            "x-github-sso",
            "x-github-deprecation",
            "deprecation",
            "sunset",
        }
        result["headers"] = {key: value for key, value in result["headers"].items() if key.lower() in actionable}
    return result


def rate_resource(url: httpx.URL, payload: dict | None) -> str:
    if payload is not None:
        return "graphql"
    if url.path == "/search/code":
        return "code_search"
    if url.path == "/search/issues" and url.params.get("search_type") == "semantic":
        return "semantic_search"
    return "search" if url.path.startswith("/search/") else "core"


class GitHubUnavailableError(RuntimeError):
    """The caller cannot initialize or continue its GitHub backend."""


class GitHubAPIError(RuntimeError):
    """A local request failure with any available response evidence attached."""

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = details


def api_url(path: str) -> httpx.URL:
    """Validate the credential origin, without an endpoint-family whitelist."""
    if path.startswith("//"):
        raise ValueError("Use a GitHub REST path or an https://api.github.com URL")
    url = httpx.URL(API_ORIGIN + path if path.startswith("/") else path)
    if (
        url.scheme != "https"
        or url.host != "api.github.com"
        or url.port not in {None, 443}
        or url.userinfo
        or url.fragment
    ):
        raise ValueError("Use a GitHub REST path or an https://api.github.com URL without credentials or fragment")
    return url


def request_headers(request: dict) -> dict:
    headers = {
        "accept": "application/vnd.github+json",
        "x-github-api-version": API_VERSION,
        "user-agent": "Graphub-v5/1.0",
    }
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
    }
    supplied = {}
    for key, value in request.get("headers", {}).items():
        name = key.lower()
        if name in reserved or name in supplied:
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


class GitHubAPI(APIReads):
    """Share HTTP concurrency and rate limits; snapshots never stand in for a fresh path request."""

    provider = "github"
    _metadata = staticmethod(result_metadata)

    def __init__(self, client: httpx.AsyncClient, storage: ToolStorage, *, token: str, concurrency: int = 8):
        super().__init__(client, storage, token=token, concurrency=concurrency)
        self.unavailable: str | None = None
        self.cooldowns: dict[str, float] = {}
        self.resource_names: dict[tuple[str, str], str] = {}

    def _failure(self, exc):
        result = super()._failure(exc)
        if isinstance(exc, GraphQLError):
            result["error"].update(
                code="invalid_query",
                path="/query",
                message=exc.message[:600],
                retryable=False,
                locations=[{"line": p.line, "column": p.column} for p in exc.locations or []],
            )
        return result

    @staticmethod
    def _cacheable(metadata: dict) -> bool:
        return 200 <= metadata.get("status", 0) < 300 and not any(
            key in metadata for key in ("error", "graphql_errors", "partial_data")
        )

    async def _wait(self, resource: str, operation: str) -> None:
        until = max(self.cooldowns.get(resource, 0), self.cooldowns.get("all", 0))
        delay = until - time.time()
        if delay > 65:
            raise GitHubAPIError(
                "GitHub requests are cooling down; wait until retry_at or use other resources",
                resource=resource,
                retry_at=until,
            )
        if delay > 0:
            self.storage.event("github/rate_wait", operation=operation, resource=resource, seconds=delay)
            await asyncio.sleep(delay)

    async def _fetch(
        self, operation: str, url: httpx.URL, method: str, headers: dict, payload: dict | None = None,
    ) -> dict:
        redirects, history, failures, retried = [], [], [], False
        for attempt in range(7):
            resource_key = (url.path, url.params.get("search_type", "lexical"))
            resource = self.resource_names.get(resource_key, rate_resource(url, payload))
            try:
                await self._wait(resource, operation)
            except GitHubAPIError as exc:
                if history:
                    exc.details.update(result_id=history[-1]["result_id"], previous_responses=list(history))
                raise
            url = api_url(str(url))
            request_id = f"{operation}.http-{attempt + 1}"
            request = {"method": method, "url": str(url), "headers": headers}
            if payload is not None:
                request["json"] = payload
            self.storage.record(f"{request_id}.request.json", request)
            self.storage.event("github/http_start", operation=operation, request=request_id, url=str(url))
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
                    "github/http_error",
                    operation=operation,
                    request=request_id,
                    error=type(exc).__name__,
                    message=str(exc),
                )
                failures.append(
                    {
                        "request_id": request_id,
                        "api_url": str(url),
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    },
                )
                if (
                    isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))
                    and not retried
                ):
                    retried = True
                    await asyncio.sleep(0.25)
                    continue
                if isinstance(exc, Exception) and history:
                    raise GitHubAPIError(
                        str(exc),
                        result_id=history[-1]["result_id"],
                        previous_responses=list(history),
                        failed_request=request,
                        failed_attempts=list(failures),
                        cause={"type": type(exc).__name__, "message": str(exc)},
                    ) from exc
                if isinstance(exc, httpx.RequestError):
                    raise GitHubAPIError(
                        str(exc),
                        failed_request=request,
                        failed_attempts=list(failures),
                        input_error={"code": "network_error", "retryable": True},
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
            if failures:
                metadata["failed_attempts"] = list(failures)
            try:
                data = response.json()
            except ValueError:
                data = None
            if url.path.startswith("/search/"):
                metadata.update(search_status(data))
            if response.headers.get("x-ratelimit-resource"):
                resource = response.headers["x-ratelimit-resource"]
                self.resource_names[resource_key] = resource
            graphql_errors = data.get("errors") if payload is not None and isinstance(data, dict) else None
            if graphql_errors:
                metadata.update(graphql_errors=graphql_errors, partial_data=data.get("data") is not None)
            exhausted = response.headers.get("x-ratelimit-remaining") == "0"
            if exhausted:
                try:
                    reset = float(response.headers["x-ratelimit-reset"]) + 1
                except (KeyError, ValueError):
                    reset = time.time() + 60
                self.cooldowns[resource] = max(self.cooldowns.get(resource, 0), reset)
            if response.is_error or graphql_errors:
                message = (
                    str(data.get("message", response.reason_phrase))
                    if isinstance(data, dict)
                    else response.reason_phrase
                )
                if graphql_errors and not response.is_error:
                    message = "GraphQL returned errors; inspect graphql_errors and any partial data"
                graphql_limited = isinstance(graphql_errors, list) and any(
                    isinstance(error, dict)
                    and (
                        error.get("type") == "RATE_LIMITED"
                        or (
                            isinstance(error.get("extensions"), dict)
                            and error["extensions"].get("code") == "RATE_LIMITED"
                        )
                    )
                    for error in graphql_errors
                )
                limited = (
                    response.status_code in {403, 429}
                    and (
                        exhausted
                        or response.status_code == 429
                        or "rate limit" in message.lower()
                        or "secondary" in message.lower()
                        or "retry-after" in response.headers
                    )
                ) or graphql_limited
                if limited:
                    scope = resource if exhausted else "all"
                    if not exhausted or "retry-after" in response.headers:
                        until = time.time() + retry_delay(response.headers.get("retry-after"), default=60)
                        self.cooldowns[scope] = max(self.cooldowns.get(scope, 0), until)
                    metadata["retry_at"] = max(self.cooldowns.get(resource, 0), self.cooldowns.get("all", 0))
                elif response.status_code in {500, 502, 503, 504} and "retry-after" in response.headers:
                    until = time.time() + retry_delay(response.headers["retry-after"], default=60)
                    self.cooldowns[resource] = max(self.cooldowns.get(resource, 0), until)
                    metadata["retry_at"] = self.cooldowns[resource]
                metadata.update(
                    error={
                        "type": "GitHubHTTPError" if response.is_error else "GitHubGraphQLError",
                        "message": message,
                        "code": "rate_limited"
                        if limited
                        else {
                            401: "authentication_required",
                            403: "access_denied",
                            404: "not_found_or_inaccessible",
                            422: "invalid_request",
                        }.get(
                            response.status_code,
                            "upstream_error"
                            if response.status_code >= 500
                            else "graphql_error"
                            if graphql_errors
                            else "http_error",
                        ),
                        "retryable": limited or response.status_code in {500, 502, 503, 504},
                    },
                    rate_limited=limited,
                    fatal=False,
                )
                if response.is_error and isinstance(data, dict):
                    metadata["error"].update({key: data[key] for key in ("errors", "documentation_url") if key in data})
            if response.has_redirect_location:
                try:
                    if payload is not None:
                        raise ValueError("GraphQL redirects are not followed; POST is restricted to /graphql")
                    target = httpx.URL(urljoin(str(url), response.headers["location"]))
                    if target.host == "api.github.com":
                        target = api_url(str(target))
                        if len(redirects) >= 5:
                            raise ValueError("Too many GitHub redirects")
                    elif target.scheme in {"http", "https"} and not target.userinfo:
                        metadata.update(redirect_url=str(target), note="Download this temporary URL with web_fetch.")
                    else:
                        raise ValueError("Invalid external redirect URL")
                except (ValueError, httpx.InvalidURL) as exc:
                    metadata["error"] = {"type": type(exc).__name__, "message": str(exc)}
            self.responses[request_id] = metadata
            self.storage.record(f"{request_id}.response.json",
                                {**metadata, "header_items": response.headers.multi_items()})
            self.storage.event(
                "github/http_end",
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
            if metadata.get("rate_limited") and not retried and metadata["retry_at"] - time.time() <= 65:
                retried = True
                continue
            if (
                response.status_code in {500, 502, 503, 504}
                and not retried
                and metadata.get("retry_at", 0) - time.time() <= 65
            ):
                retried = True
                if "retry_at" not in metadata:
                    await asyncio.sleep(0.25)
                continue
            return metadata
        raise GitHubAPIError("Too many GitHub redirects or retries", **metadata)

    async def _download_log(self, operation: str, value: str) -> dict:
        """Follow signed log URLs without any API/client credentials; retain partial downloads."""
        sources = []
        for attempt in range(6):
            url = httpx.URL(value)
            if url.scheme != "https" or not url.host or url.userinfo or url.fragment or url.port not in {None, 443}:
                raise ValueError("Log downloads require an HTTPS URL without credentials or fragment")
            result_id = f"{operation}.download-{attempt + 1}"
            # A fresh Request bypasses client-level cookies and headers; send(auth=None) disables client auth.
            outbound = httpx.Request("GET", url, headers={"User-Agent": "Graphub-v5/1.0", "Accept": "text/plain, */*"})
            request = {"method": "GET", "url": str(url), "headers": dict(outbound.headers)}
            self.storage.record(result_id + ".request.json", request)
            self.storage.event("github/http_start", operation=operation, request=result_id, url=str(url))
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
                        "type": "GitHubHTTPError",
                        "message": f"Expected log HTTP 200; got {response.status_code}",
                    }
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
                self.responses[result_id] = metadata
                self.storage.record(result_id + ".response.json", metadata)
                self.storage.event(
                    "github/response_saved", operation=operation, request=result_id, kind_name="ci_log_download",
                )
                self.storage.event(
                    "github/http_end",
                    operation=operation,
                    request=result_id,
                    status=metadata.get("status"),
                    download_complete=metadata["download_complete"],
                )
            sources.append(result_id)
            if "error" in metadata or response is None or not response.has_redirect_location:
                return metadata
            value = urljoin(str(url), response.headers["location"])
        raise GitHubAPIError("Too many log download redirects", result_id=result_id, source_result_ids=sources)
