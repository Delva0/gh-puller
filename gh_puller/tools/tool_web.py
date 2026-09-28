"""Generic web search and resource fetching, with separate search pacing and raw evidence."""

import asyncio
import base64
import bz2
import contextlib
import gzip
import io
import json
import lzma
import mimetypes
import re
import tarfile
import time
import zipfile
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin

import httpx
from bs4 import BeautifulSoup
from ddgs.ddgs import DDGS
from ddgs.engines.duckduckgo import Duckduckgo
from markdownify import MarkdownConverter
from PIL import Image
from pypdf import PdfReader

from ..configuration import Credential, ToolConfig, nonnegative_number, option, positive_integer
from .registry import BATCH_OUTPUT, ToolProvider, tool, tool_definitions
from .storage import ToolStorage
from .utils import page_excerpt, retry_delay, validate_http_credential

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_MEMBER_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 5000
MAX_IMAGE_PIXELS = 25_000_000
DEFAULT_SEARCH_INTERVAL = 2.0
SEARCH_BACKENDS = ("auto", "brave", "duckduckgo")
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


async def validate_brave_key(value, *, transport=None):
    return await validate_http_credential(value, url=BRAVE_SEARCH_URL + "?q=connection+test&count=1",
                                          header="X-Subscription-Token", prefix="", transport=transport)


WEB_CONFIG = ToolConfig("web", {
    "web_search_backend": option("brave", choices=SEARCH_BACKENDS),
    "web_search_concurrency": option(1, validator=positive_integer),
    "web_search_interval": option(DEFAULT_SEARCH_INTERVAL, description="Search interval in seconds.",
                                  validator=nonnegative_number),
}, {"brave_api_key": Credential(("BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY"), {"web_search_backend": "brave"},
                               validator=validate_brave_key, active_when={"web_search_backend": ("auto", "brave")})})

WEB_SEARCH_SCHEMA = {
    "type": "object", "properties": {
        "queries": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
            "type": "object", "properties": {
                "query": {"type": "string", "minLength": 1},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
                "region": {"type": "string", "default": "us-en",
                           "description": "Country-language, e.g. us-en, cn-zh, tw-tzh; wt-wt for all regions."},
                "timelimit": {"type": "string", "enum": ["d", "w", "m", "y"]},
                "page": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1},
            }, "required": ["query"], "additionalProperties": False,
        }},
    }, "required": ["queries"], "additionalProperties": False,
}
WEB_FETCH_SCHEMA = {
    "type": "object", "properties": {
        "requests": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
            "type": "object", "properties": {
                "url": {"type": "string", "minLength": 1, "description": "HTTP(S) resource URL."},
                "ref": {"type": "string", "minLength": 1,
                        "description": "Returned reference to an already downloaded resource; use instead of url."},
                "member": {"type": "string", "minLength": 1,
                           "description": "Exact file name from an archive listing."},
                "start_line": {"type": "integer", "minimum": 1, "default": 1},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 400, "default": 180},
                "find": {"type": "string", "description": "Find literal text in this resource or archive member."},
            }, "oneOf": [{"required": ["url"]}, {"required": ["ref"]}], "additionalProperties": False,
        }},
    }, "required": ["requests"], "additionalProperties": False,
}
WEB_SEARCH_DESCRIPTION = (
    "Search the web for relevant pages. Supports site:domain, quoted phrases "
    "and -excluded terms. Returns titles, URLs and snippets; use web_fetch to read resources. Searches "
    "share a separate concurrency and pacing limit; rate limits and service failures are reported "
    "explicitly."
)

WEB_FETCH_DESCRIPTION = (
    "Fetch HTTP(S) URLs and follow redirects. Converts HTML to Markdown while "
    "retaining image and attachment links; reads text, JSON, PDF text, ZIP/TAR archives and gzip/bzip2/xz "
    "files. Does not execute JavaScript. Archives return a file listing: use ref plus member to read a "
    "file. Use ref with start_line=next_line to continue downloaded text without another HTTP request; url "
    "always fetches anew. Images are supplied as native image input when the model supports it, otherwise "
    "only image metadata is returned. Other binary files are saved with metadata. Downloads are limited to "
    "50 MiB, individual archive files to 20 MiB; limits are explicit."
)


class WebError(RuntimeError):
    """A fetch or search failure whose details can be returned without losing siblings."""

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = details


def web_url(value: str) -> httpx.URL:
    url = httpx.URL(value)
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
        raise ValueError("Use an HTTP(S) URL without embedded credentials")
    return url.copy_with(fragment=None)


class RecordedDuckDuckGo(Duckduckgo):
    """Keep DDGS extraction while recording HTTP failures that its default engine hides."""

    def __init__(self, storage: ToolStorage, operation: str):
        super().__init__(timeout=20)
        self.storage, self.operation = storage, operation
        self.failure = None

    def request(self, method, url, **kwargs):
        prefix = f"{self.operation}.http-1"
        self.storage.record(f"{prefix}.request.json", {"method": method, "url": url, **kwargs})
        self.storage.event("web/http_start", operation=self.operation, request=prefix, url=url, method=method)
        try:
            response = self.http_client.request(method, url, **kwargs)
            body_file = self.storage.write(f"{prefix}.body", response.content)
            self.storage.record(f"{prefix}.response.json", {"status": response.status_code, "body_file": body_file})
            self.storage.event("web/http_end", operation=self.operation, request=prefix, status=response.status_code)
            if response.status_code != 200:
                raise WebError(f"DuckDuckGo returned HTTP {response.status_code}", status=response.status_code,
                               body_file=body_file, rate_limited=response.status_code in {202, 429})
            if "anomaly.js" in response.text or "anomaly-modal" in response.text:
                raise WebError("DuckDuckGo returned a challenge page", status=200, body_file=body_file,
                               rate_limited=True)
        except Exception as exc:
            self.failure = exc
            self.storage.event("web/http_error", operation=self.operation, request=prefix,
                           error=type(exc).__name__, message=str(exc))
            raise
        else:
            return response.text


class WebTools(ToolProvider):
    """Share fetch connections, serialize/pause searches, and keep explicit downloadable references."""

    def __init__(self, client: httpx.AsyncClient, storage: ToolStorage, *, concurrency: int = 8,
                 search_concurrency: int = 1, search_interval: float = DEFAULT_SEARCH_INTERVAL,
                 is_llm_multi_modal: bool = True, search_backend: str = "auto", brave_api_key: str = ""):
        if concurrency < 1 or search_concurrency < 1 or search_interval < 0:
            raise ValueError("Concurrency must be positive and search interval nonnegative")
        if search_backend not in SEARCH_BACKENDS:
            raise ValueError(f"Unknown web search backend: {search_backend}")
        self.search_backend = (("brave" if brave_api_key else "duckduckgo")
                               if search_backend == "auto" else search_backend)
        if self.search_backend == "brave" and not brave_api_key:
            raise ValueError("Brave search requires BRAVE_SEARCH_API_KEY (or BRAVE_API_KEY)")
        self.brave_api_key = brave_api_key
        self.search_name = "Brave" if self.search_backend == "brave" else "DuckDuckGo"
        self.client, self.storage = client, storage
        self.fetch_limit = asyncio.Semaphore(concurrency)
        self.search_limit = asyncio.Semaphore(search_concurrency)
        self.search_concurrency = search_concurrency
        self.search_interval = search_interval
        self.search_pacing = asyncio.Lock()
        self.next_search = self.search_cooldown = 0.0
        self.is_llm_multi_modal = is_llm_multi_modal
        self.resources: dict[str, dict] = {}

    def metadata(self):
        return {"search": "brave-api" if self.search_backend == "brave" else "ddgs",
                "engine": self.search_backend, "fetch": "httpx", "authenticated": self.search_backend == "brave",
                "search_concurrency": self.search_concurrency, "search_interval_seconds": self.search_interval}

    def clear_context(self) -> None:
        self.resources.clear()
        self.storage.event("web/cleared")

    def remember_resource(self, resource):
        self.resources[resource["ref"]] = resource
        self.storage.event("web/resource_saved", resource=resource)

    def search_deadline(self, kind, until):
        """Observe wall-clock deadlines so pacing survives a different process clock."""
        remaining = max(0, until - time.time())
        if kind == "web/search_scheduled":
            self.next_search = time.monotonic() + remaining
        else:
            self.search_cooldown = time.monotonic() + remaining
        self.storage.event(kind, backend=self.search_backend, until=until)

    def load_events(self, events):
        resources, deadlines = {}, {}
        for event in events:
            kind, data = event["type"], event["data"]
            if kind == "web/cleared":
                resources.clear()
            elif kind == "web/resource_saved":
                resource = data["resource"]
                if not self.storage.path(resource["body_file"]).is_file():
                    raise ValueError("Observed web resource file is missing")
                resources[resource["ref"]] = resource
            elif kind in {"web/search_scheduled", "web/search_limited"} and data["backend"] == self.search_backend:
                deadlines[kind] = data["until"]
        for resource in resources.values():
            self.remember_resource(dict(resource))
        for kind, until in deadlines.items():
            self.search_deadline(kind, until)

    async def _job(self, call_id, kind, arguments, limit, action):
        operation = self.storage.allocate(kind)
        self.storage.event("io/queued", operation=operation, call_id=call_id, kind_name=kind, arguments=arguments)
        try:
            async with limit:
                self.storage.event("io/start", operation=operation, call_id=call_id)
                result = await action(operation)
            self.storage.event("io/end", operation=operation, call_id=call_id, status="completed")
        except BaseException as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            if isinstance(exc, WebError):
                error.update(exc.details)
            self.storage.event("io/end", operation=operation, call_id=call_id, status="failed", error=error)
            result = {"error": error}
            self.storage.record(f"{operation}.result.json", {"operation": operation, **arguments, **result})
            if not isinstance(exc, Exception):
                raise
        else:
            self.storage.record(f"{operation}.result.json", {"operation": operation, **arguments, **result})
        return {"operation": operation, **arguments, **result}

    @staticmethod
    async def _thread(function, *args):
        # A cancelled coroutine must not release its permit while DDGS still issues HTTP in a thread.
        work = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await work
            raise

    def _search(self, operation, query):
        engine = RecordedDuckDuckGo(self.storage, operation)
        search = DDGS(timeout=20)
        # Pinned DDGS 9.16.0: replace only this instance's engine to preserve raw HTTP evidence.
        search._engines_cache[Duckduckgo] = engine
        try:
            return search.text(backend="duckduckgo", **query)
        except Exception:
            if engine.failure:
                raise engine.failure from None
            raise

    async def _search_brave(self, operation, query):
        if len(query["query"]) > 600 or len(query["query"].split()) > 75:
            raise WebError("Brave query exceeds 600 characters or 75 words", retryable=False)
        country, separator, language = query["region"].partition("-")
        if not separator:
            raise WebError("region must be country-language, e.g. us-en, cn-zh or wt-wt", retryable=False)
        params = {"q": query["query"], "count": query["max_results"], "offset": query["page"] - 1,
                  "country": {"wt": "ALL", "uk": "GB"}.get(country.lower(), country.upper()),
                  "result_filter": "web", "text_decorations": "false", "spellcheck": "false",
                  "extra_snippets": "true"}
        if language != "wt":
            params["search_lang"] = {"zh": "zh-hans", "tzh": "zh-hant"}.get(language, language)
        if query.get("timelimit"):
            params["freshness"] = "p" + query["timelimit"]
        prefix = f"{operation}.http-1"
        self.storage.record(f"{prefix}.request.json", {"method": "GET", "url": BRAVE_SEARCH_URL, "params": params,
                                                "header_names": ["Accept", "X-Subscription-Token"]})
        self.storage.event("web/http_start", operation=operation, request=prefix, url=BRAVE_SEARCH_URL, method="GET")
        try:
            response = await self.client.get(BRAVE_SEARCH_URL, params=params, follow_redirects=False,
                headers={"Accept": "application/json", "X-Subscription-Token": self.brave_api_key})
        except Exception as exc:
            self.storage.event("web/http_error", operation=operation, request=prefix,
                           error=type(exc).__name__, message=str(exc))
            raise
        body_file = self.storage.write(f"{prefix}.body", response.content)
        headers = {k: v for k, v in response.headers.items()
                   if k in {"content-type", "retry-after"} or k.startswith("x-ratelimit-")}
        self.storage.record(f"{prefix}.response.json", {"status": response.status_code, "headers": headers,
                                                 "body_file": body_file})
        self.storage.event("web/http_end", operation=operation, request=prefix, status=response.status_code)
        if response.status_code != 200:
            details = {"status": response.status_code, "body_file": body_file,
                       "rate_limited": response.status_code == 429,
                       "retryable": response.status_code == 429 or response.status_code >= 500}
            try:
                error = response.json().get("error", {})
                if isinstance(error, dict):
                    details.update({f"service_{key}": error[key] for key in ("code", "detail") if key in error})
            except (ValueError, AttributeError):
                pass
            if response.status_code == 429:
                delay = headers.get("retry-after")
                if delay is None:
                    remaining = headers.get("x-ratelimit-remaining", "").split(",")
                    resets = headers.get("x-ratelimit-reset", "").split(",")
                    limits = headers.get("x-ratelimit-limit", "").split(",")
                    exhausted = [retry_delay(reset.strip(), default=60)
                                 for index, (left, reset) in enumerate(zip(remaining, resets, strict=False))
                                 if left.strip() == "0" and reset.strip()
                                 and (index >= len(limits) or limits[index].strip() != "0")]
                    delay = str(max(exhausted)) if exhausted else None
                details["retry_after"] = retry_delay(delay, default=60)
                details["cooldown_source"] = "response_headers" if delay is not None else "local_default"
            raise WebError(f"Brave returned HTTP {response.status_code}", **details)
        try:
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("type") != "search":
                raise ValueError("Expected a Brave search response")
            results = (payload.get("web") or {}).get("results", [])
            items = [{"title": item["title"], "href": item["url"], "body": item.get("description", ""),
                      **({"extra_snippets": item["extra_snippets"]} if item.get("extra_snippets") else {})}
                     for item in results]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise WebError("Invalid Brave search response", status=200, body_file=body_file) from exc
        return {"items": items, "query_info": payload.get("query", {})}

    @tool(description=WEB_SEARCH_DESCRIPTION, parameters=WEB_SEARCH_SCHEMA, returns=BATCH_OUTPUT,
          batch_parameter="queries", configuration=(*WEB_CONFIG.defaults, *WEB_CONFIG.credentials))
    async def web_search(self, call_id: str, queries: list[dict]) -> dict:
        async def one(query):
            async def action(operation):
                async with self.search_pacing:
                    if self.search_cooldown > time.monotonic():
                        raise WebError(f"{self.search_name} is cooling down after a rate limit; no request sent",
                                       rate_limited=True, retry_after=round(self.search_cooldown - time.monotonic(), 1))
                    delay = self.next_search - time.monotonic()
                    if delay > 0:
                        self.storage.event("web/search_wait", operation=operation, seconds=delay)
                        await asyncio.sleep(delay)
                    self.search_deadline("web/search_scheduled", time.time() + self.search_interval)
                arguments = {"max_results": 10, "region": "us-en", "page": 1, **query}
                try:
                    if self.search_backend == "brave":
                        result = await self._search_brave(operation, arguments)
                    else:
                        result = {"items": await self._thread(self._search, operation, arguments)}
                except WebError as exc:
                    if exc.details.get("rate_limited"):
                        delay = exc.details.setdefault("retry_after", 60)
                        self.search_deadline("web/search_limited", time.time() + delay)
                    raise
                items = result["items"]
                raw_file = self.storage.write(f"{operation}.search-results.json", items)
                return {"backend": self.search_backend, **result, "returned_count": len(items),
                        "raw_file": raw_file}
            return await self._job(call_id, "web-search", query, self.search_limit, action)
        return {"results": await asyncio.gather(*(one(query) for query in queries))}

    async def _download(self, operation, value):
        url = web_url(value)
        for attempt in range(6):
            prefix = f"{operation}.http-{attempt + 1}"
            headers = {"User-Agent": "Graphub-v5-web/1.0", "Accept": "*/*"}
            self.storage.record(f"{prefix}.request.json", {"method": "GET", "url": str(url), "headers": headers})
            self.storage.event("web/http_start", operation=operation, request=prefix, url=str(url), method="GET")
            body_name = f"{prefix}.body"
            body_file = str(self.storage.path(body_name).relative_to(self.storage.root))
            metadata = {"url": str(url), "body_file": body_file, "download_complete": False, "size": 0}
            try:
                async with self.client.stream("GET", url, headers=headers, follow_redirects=False) as response:
                    metadata.update(status=response.status_code, headers={k: v for k, v in response.headers.items()
                        if k in {"content-type", "content-length", "content-disposition", "location", "retry-after"}})
                    with self.storage.binary(body_name, media_type=response.headers.get(
                            "content-type", "application/octet-stream")) as output:
                        async for chunk in response.aiter_bytes():
                            remaining = MAX_DOWNLOAD_BYTES - metadata["size"]
                            output.write(chunk[:remaining])
                            metadata["size"] += min(len(chunk), remaining)
                            if len(chunk) > remaining:
                                raise WebError("Download exceeds 50 MiB; partial body retained", **metadata)
                    metadata["download_complete"] = True
                    if response.is_redirect:
                        url = web_url(urljoin(str(url), response.headers["location"]))
                        continue
                    if response.status_code != 200:
                        raise WebError(f"HTTP {response.status_code}", **metadata)
                    return metadata
            except BaseException as exc:
                self.storage.event("web/http_error", operation=operation, request=prefix,
                               error=type(exc).__name__, message=str(exc))
                raise
            finally:
                self.storage.record(f"{prefix}.response.json", metadata)
                self.storage.event("web/http_end", operation=operation, request=prefix,
                               status=metadata.get("status"), size=metadata["size"],
                               download_complete=metadata["download_complete"])
        raise WebError("Too many redirects", **metadata)

    def _html(self, body, url, operation):
        soup = BeautifulSoup(body, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        base = urljoin(url, soup.base.get("href", "")) if soup.base else url
        for tag in soup(["script", "style", "noscript", "iframe"]):
            tag.decompose()
        assets = []
        for tag in soup.find_all(["a", "img"]):
            attr = "href" if tag.name == "a" else "src"
            value = tag.get(attr) or tag.get("data-src", "")
            if value.startswith("data:"):
                # Keep inline assets accessible without leaking their base64 into Markdown/tool text.
                header, separator, payload = value.partition(",")
                if separator:
                    try:
                        raw = (base64.b64decode(payload, validate=True) if ";base64" in header
                               else unquote(payload).encode())
                        ref = f"{operation}-inline-{len(assets) + 1}"
                        mime = header[5:].split(";", 1)[0] or "application/octet-stream"
                        filename = self.storage.write(f"{ref}.body", raw, operation=operation, media_type=mime)
                        self.remember_resource({"ref": ref, "url": url, "body_file": filename,
                                                "headers": {"content-type": mime}, "size": len(raw)})
                        assets.append({"ref": ref, "media_type": mime})
                        tag[attr] = f"resource:{ref}"
                    except ValueError:
                        tag.attrs.pop(attr, None)
                else:
                    tag.attrs.pop(attr, None)
            elif value:
                absolute = urljoin(base, value)
                if absolute.startswith(("http://", "https://", "mailto:")):
                    tag[attr] = absolute
                else:
                    tag.attrs.pop(attr, None)
        root = soup.find("main") or soup.body or soup
        text = MarkdownConverter(
            heading_style="ATX", keep_inline_images_in=["td", "th", "a", "p", "h1", "h2"],
        ).convert_soup(root)
        return {"format": "markdown", "title": title, "text": text, "inline_resources": assets}

    def _image(self, body, operation):
        with Image.open(io.BytesIO(body)) as picture:
            width, height = picture.size
            if width * height > MAX_IMAGE_PIXELS:
                raise WebError("Image exceeds the 25 megapixel decode limit", width=width, height=height)
            frames = getattr(picture, "n_frames", 1)
            metadata = {"width": width, "height": height, "frames": frames, "format": picture.format}
            if not self.is_llm_multi_modal:
                return {"format": "image", "image": metadata, "image_input": False,
                        "note": "Model image input is disabled; only metadata is supplied, no base64 text."}
            if picture.format in {"JPEG", "PNG", "WEBP"} and frames == 1:
                picture.load()
                data, mime = body, Image.MIME[picture.format]
            else:
                picture.seek(0)
                output = io.BytesIO()
                picture.convert("RGBA").save(output, format="PNG")
                data, mime = output.getvalue(), "image/png"
            if len(data) > MAX_MEMBER_BYTES:
                raise WebError("Image input exceeds 20 MiB; original download retained")
        filename = self.storage.write(f"{operation}.image{mimetypes.guess_extension(mime) or '.png'}", data,
                                  media_type=mime)
        return {"format": "image", "image": {**metadata, "media_type": mime, "image_file": filename},
                "image_input": True, "note": "Native image input attached; animated/multipage images use frame 1."
                if frames > 1 else "Native image input attached."}

    def _archive(self, body, member):
        stream = io.BytesIO(body)
        if zipfile.is_zipfile(stream):
            with zipfile.ZipFile(stream) as archive:
                files = [item for item in archive.infolist() if not item.is_dir()]
                if len(files) > MAX_ARCHIVE_MEMBERS:
                    raise WebError("Archive exceeds 5,000 files")
                if member is None:
                    return [{"name": item.filename, "size": item.file_size} for item in files], None
                info = archive.getinfo(member)
                if info.file_size > MAX_MEMBER_BYTES:
                    raise WebError("Archive member exceeds 20 MiB", member=member)
                with archive.open(info) as source:
                    data = source.read(MAX_MEMBER_BYTES + 1)
        else:
            stream.seek(0)
            with tarfile.open(fileobj=stream, mode="r:*") as archive:
                files = []
                expanded_size = 0
                for item in archive:
                    if len(files) >= MAX_ARCHIVE_MEMBERS:
                        raise WebError("Archive exceeds 5,000 entries")
                    files.append(item)
                    expanded_size += item.size
                    if expanded_size > 100 * 1024 * 1024:
                        raise WebError("TAR contents exceed the 100 MiB expanded-size limit")
                if member is None:
                    return [{"name": item.name, "size": item.size, "readable": item.isfile()}
                            for item in files if not item.isdir()], None
                info = archive.getmember(member)
                if not info.isfile() or info.size > MAX_MEMBER_BYTES:
                    raise WebError("Archive member is not a regular file or exceeds 20 MiB", member=member)
                with archive.extractfile(info) as source:
                    data = source.read(MAX_MEMBER_BYTES + 1)
        if len(data) > MAX_MEMBER_BYTES:
            raise WebError("Archive member exceeds 20 MiB", member=member)
        return None, data

    def _render(self, resource, operation, member=None):
        body = self.storage.read(resource["body_file"])
        mime = resource.get("headers", {}).get("content-type", "").split(";", 1)[0].lower()
        name = unquote(httpx.URL(resource["url"]).path)
        compressed = body.startswith((b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00"))
        is_archive = zipfile.is_zipfile(io.BytesIO(body)) or mime in {
            "application/x-tar", "application/zip", "application/x-zip-compressed",
        } or (len(body) > 262 and body[257:262] == b"ustar") \
            or name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz"))
        if is_archive or member is not None:
            entries, content = self._archive(body, member)
            if entries is not None:
                listing = "\n".join(
                    f"{json.dumps(item['name'], ensure_ascii=False)}\t{item['size']} bytes"
                    + (" [not a regular file]" if item.get("readable") is False else "") for item in entries)
                return {"format": "archive", "member_count": len(entries), "text": listing,
                        "note": "Use ref and the exact member name to read a file; no filesystem extraction."}
            name, body = member, content
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self.storage.record(f"{operation}.member.body", body)
        elif compressed:
            opener = (gzip.open if body.startswith(b"\x1f\x8b")
                      else bz2.open if body.startswith(b"BZh") else lzma.open)
            with opener(io.BytesIO(body), "rb") as source:
                body = source.read(MAX_MEMBER_BYTES + 1)
            if len(body) > MAX_MEMBER_BYTES:
                raise WebError("Decompressed resource exceeds 20 MiB")
            self.storage.record(f"{operation}.decompressed.body", body)
            name = str(PurePosixPath(name).with_suffix(""))
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if len(body) > 262 and body[257:262] == b"ustar":
                entries, _ = self._archive(body, None)
                return {"format": "archive", "member_count": len(entries),
                        "text": "\n".join(f"{json.dumps(item['name'])}\t{item['size']} bytes" for item in entries)}
        if body.startswith(b"%PDF-") or mime == "application/pdf":
            pdf = PdfReader(io.BytesIO(body))
            pages = [page.extract_text() or "" for page in pdf.pages]
            return {"format": "pdf", "pages": len(pages),
                    "pages_without_text": [i + 1 for i, text in enumerate(pages) if not text.strip()],
                    "note": "PDF text extraction; scanned images are not OCRed.",
                    "text": "\n\n".join(f"# Page {i + 1}\n\n{text}" for i, text in enumerate(pages))}
        image_magic = body.startswith((b"\x89PNG\r\n", b"\xff\xd8\xff", b"GIF8", b"BM", b"II*\x00", b"MM\x00*")) \
            or (body[:4] == b"RIFF" and body[8:12] == b"WEBP")
        if image_magic or (mime.startswith("image/") and mime != "image/svg+xml"):
            return self._image(body, operation)
        if (mime in {"text/html", "application/xhtml+xml"}
                or body.lstrip().lower().startswith((b"<!doctype html", b"<html"))):
            return self._html(body, resource["url"], operation)
        if body.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = body.decode("utf-16")
        else:
            charset = re.search(r"charset=[\"']?([^;\s\"']+)", resource.get("headers", {}).get("content-type", ""))
            try:
                text = body.decode(charset.group(1) if charset else "utf-8-sig")
            except (UnicodeError, LookupError):
                if mime.startswith("text/"):
                    return {"format": "text", "text": body.decode("utf-8", errors="replace"),
                            "note": "Invalid text encoding replaced; original bytes retained."}
                return {"format": "binary", "note": "Downloaded binary resource; no text decoder for this format."}
        controls = any(ord(char) < 9 or 13 < ord(char) < 32 for char in text[:4096])
        # ANSI escape is normal in CI logs.
        if "\x00" in text or (controls and not mime.startswith("text/") and "\x1b[" not in text):
            return {"format": "binary", "note": "Downloaded binary resource; no text decoder for this format."}
        return {"format": "text", "text": text}

    @tool(description=WEB_FETCH_DESCRIPTION, parameters=WEB_FETCH_SCHEMA, returns=BATCH_OUTPUT,
          batch_parameter="requests")
    async def web_fetch(self, call_id: str, requests: list[dict]) -> dict:
        async def one(request):
            async def action(operation):
                if "url" in request:
                    resource = {"ref": operation, **await self._download(operation, request["url"])}
                    self.remember_resource(resource)
                else:
                    if request["ref"] not in self.resources:
                        raise ValueError("Unknown resource ref in this conversation; fetch its URL again")
                    resource = self.resources[request["ref"]]
                parsed = await self._thread(self._render, resource, operation, request.get("member"))
                result = {**resource, **parsed}
                if "text" in parsed:
                    # Bound physical lines for minified HTML/JSON/logs while retaining the exact full text artifact.
                    result["text_file"] = self.storage.write(f"{operation}.text.md", parsed["text"])
                    result["text"] = "\n".join(line[i:i + 2000] for line in parsed["text"].splitlines()
                                               for i in range(0, max(1, len(line)), 2000))
                    result["long_lines_wrapped_at"] = 2000
                    return page_excerpt(result, **{
                        k: request[k] for k in ("start_line", "max_lines", "find") if k in request
                    })
                return result
            return await self._job(call_id, "web-fetch", request, self.fetch_limit, action)
        return {"results": await asyncio.gather(*(one(request) for request in requests))}

    def image_parts(self, result: dict, call_id: str) -> tuple[list[dict], list[dict]]:
        """Native image input and lightweight observation metadata; never base64 in a text tool result."""
        parts, observations = [], []
        if self.is_llm_multi_modal:
            for item in result.get("results", []):
                if item.get("image_input"):
                    picture = item["image"]
                    label = f"Image from web_fetch call {call_id}: {item['url']}"
                    if item.get("member"):
                        label += f" (archive member {item['member']})"
                    data = base64.b64encode((self.storage.root / picture["image_file"]).read_bytes()).decode("ascii")
                    parts.extend([{"type": "text", "text": label}, {"type": "image_url", "image_url": {
                        "url": f"data:{picture['media_type']};base64,{data}", "detail": "auto",
                    }}])
                    observations.extend([{"type": "input_text", "text": label},
                                         {"type": "input_image", "image_url": picture["image_file"],
                                          "media_type": picture["media_type"]}])
        return parts, observations


WEB_TOOL_DEFINITIONS = tool_definitions(WebTools)
