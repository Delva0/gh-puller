"""GitCode REST and DSL tools."""

import asyncio
import copy
import inspect
import io
import json
import re
import zipfile
from dataclasses import dataclass
from functools import lru_cache, partial
from urllib.parse import quote, urljoin

import httpx
from graphql import (
    GraphQLArgument,
    GraphQLBoolean,
    GraphQLError,
    GraphQLField,
    GraphQLFloat,
    GraphQLID,
    GraphQLInt,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLString,
    Undefined,
    execute,
    get_named_type,
    is_leaf_type,
    is_list_type,
    parse,
    validate,
)
from graphql.execution.collect_fields import collect_sub_fields
from jsonschema import Draft202012Validator

from ..configuration import Credential, ToolConfig
from .gitcode_api import (
    API_ORIGIN,
    BACKEND,
    MAX_LOG_BYTES,
    STEP_LOG_PATH,
    GitCodeAPI,
    GitCodeAPIError,
    GitCodeResponseContent,
    api_url,
    compact_fields,
    native_catalog,
    readonly_post,
    request_headers,
    validate_native_body,
)
from .githost_api_utils import (
    APIProvider,
    ResponseContent,
    collect_pages,
    continue_view,
    request_key,
    validate_display,
    view_error,
)
from .githost_api_utils import response_view as api_response_view
from .githost_dsl import (
    JSON,
    OUTPUT,
    SCHEMA,
    DSLExtensions,
    Evidence,
    Field,
    Identity,
    Value,
    dsl_fields,
    execution_result,
    field_key,
    operation_context,
    schema_request,
)
from .registry import BATCH_OUTPUT, ToolInputError, input_error, tool, tool_definitions
from .utils import select_json, validate_http_credential


async def validate_gitcode_token(value, *, transport=None):
    return await validate_http_credential(value, url=API_ORIGIN + "/api/v5/user", transport=transport)


GITCODE_CONFIG = ToolConfig("gitcode", credentials={
    "gitcode_token": Credential(("GITCODE_TOKEN",), validator=validate_gitcode_token)})

API_DESCRIPTION = (
    "Read GitCode with a resource path, ci_logs, or saved result_id; choose one entry per request. "
    "Independent requests run concurrently. The tool handles API routing and authentication. "
    "Use GET/HEAD with native params, accept and headers; new paths fetch current data. "
    "POST with json is available only for /enterprises/ID/issues (native Issue filters), "
    "/repos/O/R/actions/workflows/validate (base64_content), and /video/status (request_id). "
    "Enterprise POST lists also support paginate.\n"
    "Search: /search/repositories uses q, owner, language and sort=last_push_at|stars_count|forks_count; "
    "/search/issues uses q, repo, state and sort=created_at|last_push_at; both use order=asc|desc. "
    "Use separate params, e.g. {'q':'timeout','repo':'OWNER/REPO'}, not GitHub q qualifiers. "
    "Repository search allows per_page<=20, issue search <=50, both page<=100. /search/users finds users. "
    "Verify repository, dates and content in hits; do not assume global search covers PRs or comments.\n"
    "Under /repos/OWNER/REPO: /issues filters title/body with search, plus state, labels, assignee, creator, "
    "since, created_after/before, updated_after/before, sort=created|updated and direction=asc|desc. "
    "/pulls lists PRs with state=all|open|closed|merged, author, reviewer, assignee, base, labels, since, "
    "created_after/before, updated_after/before and merged_after/before. "
    "Read /issues/N or /pulls/N and their /comments for details and conversations. "
    "/issues/N/pull_requests and /pulls/N/issues read explicit links; association alone does not prove a fix. "
    "PR /comments accepts comment_type=diff_comment|pr_comment; inspect discussion_id, resolved and "
    "diff_position for review threads. Under /pulls/N, /files.json reads diffs, /commits reads PR commits, "
    "/operate_logs and /modify_history read activity. Approval state is in PR details.\n"
    "Files/history under the same repo: /contents/PATH or /raw/PATH with ref reads a revision; "
    "/file_list with ref_name and file_name finds paths; /git/trees/SHA with recursive=1 and /git/blobs/SHA "
    "read Git objects. /commits, /commits/SHA, /compare/BASE...HEAD, /commit/SHA/diff and /commit/SHA/patch "
    "read history and changes. /releases/latest and /releases/tags/TAG read release notes; "
    "/branches, /tags, /releases, /labels, /milestones and /collaborators read metadata. "
    "Filename lookup is not code-content search.\n"
    "Discussions: /repos/OWNER/REPO/discuss or /orgs/ORG/discuss lists topics; append /N for a topic, "
    "/N/comment for comments, /N/comment/ID/reply for replies. "
    "/org/ORG/kanban/list lists boards; /org/ORG/kanban/ID/detail and /item_list read a board and its items. "
    "Other native reads include /user, /user/repos, /users/LOGIN, /orgs/ORG/repos, /orgs/ORG/members and "
    "/enterprises/ENTERPRISE resources (issues, pull_requests, labels, members, milestones). "
    "Endpoint permissions still apply.\n"
    "CI under /repos/OWNER/REPO/actions: /runs, /runs/R, /runs/R/jobs, /runs/R/jobs/J, /workflows and /artifacts. "
    "ci_logs={owner,repo,run_id} reads bounded job logs and extracts ZIP text; job_ids selects known jobs, "
    "max_jobs/max_pages bound work, tail_lines/max_chars_per_job bound excerpts. "
    "Follow resume_request and next_jobs_request for remaining jobs. "
    "For one step, use path .../runs/R/jobs/J/logs with params.step_id and optional offset, limit, sort=asc|desc; "
    "follow next_request for more server log records.\n"
    "paginate={} collects a list using native next links or page totals; optionally set items_pointer, "
    "max_pages and max_items. It returns an array suitable for fields. Follow collection.next_request or "
    "retry_request unchanged; a budget stop or missing pagination evidence is not completion. "
    "Without paginate, read one native response with your page/per_page parameters.\n"
    "Results preserve status, headers and a result_id for the complete saved response. "
    "Use fields={'number':'/number','title':'/title'} for concise lists; json_pointer selects a nested value. "
    "view='full' restores JSON fields, 'text' decodes base64 files, 'raw' reads original bytes. "
    "start_line/max_lines, tail_lines or find with context_lines read numbered text excerpts. "
    "Use continue_request unchanged for the next local excerpt; result_id makes no HTTP request. "
    "Check omissions, truncated and completion flags. Errors may include a recovery_request for saved data. "
    "External redirect_url can be read with web_fetch."
)

API_SCHEMA = {
    "type": "object",
    "properties": {
        "requests": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "GitCode resource path; the tool supplies the API prefix. Full native "
                            "API paths or api.gitcode.com URLs, including query strings, are also "
                            "accepted. Always makes a fresh request."
                        ),
                    },
                    "result_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Read a saved response instead of making an HTTP request. Choose "
                            "exactly one of path, ci_logs or result_id."
                        ),
                    },
                    "paginate": {
                        "type": "object",
                        "properties": {
                            "items_pointer": {
                                "type": "string",
                                "description": (
                                    "JSON Pointer to the array; '' for a root array. Omit to select"
                                    " a root array or one unambiguous standard list envelope."
                                ),
                            },
                            "max_pages": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 20,
                                "default": 5,
                            },
                            "max_items": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 2000,
                                "default": 200,
                            },
                            "page_result_id": {
                                "type": "string",
                                "minLength": 1,
                                "description": (
                                    "Saved first page from collection.next_request; avoids re-fetching it."
                                ),
                            },
                            "start_index": {
                                "type": "integer",
                                "minimum": 0,
                                "default": 0,
                            },
                        },
                        "additionalProperties": False,
                    },
                    "ci_logs": {
                        "type": "object",
                        "properties": {
                            "owner": {
                                "type": "string",
                                "minLength": 1,
                            },
                            "repo": {
                                "type": "string",
                                "minLength": 1,
                            },
                            "run_id": {
                                "type": ["string", "integer"],
                                "minLength": 1,
                                "minimum": 1,
                                "description": "workflow_run_id from /actions/runs; not run_number.",
                            },
                            "job_ids": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 2000,
                                "items": {
                                    "type": ["string", "integer"],
                                    "minLength": 1,
                                    "minimum": 1,
                                },
                                "description": ("Known jobs to read; omitting this lists the run's jobs first."),
                            },
                            "max_jobs": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 50,
                                "default": 10,
                            },
                            "max_pages": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 20,
                                "default": 5,
                            },
                            "tail_lines": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 1000,
                                "default": 200,
                            },
                            "max_chars_per_job": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 40000,
                                "default": 8000,
                            },
                        },
                        "required": ["owner", "repo", "run_id"],
                        "additionalProperties": False,
                    },
                    "method": {
                        "type": "string",
                        "enum": ["GET", "HEAD", "POST"],
                        "default": "GET",
                    },
                    "json": {
                        "type": "object",
                        "description": (
                            "Native JSON body for documented read-only POSTs: v8 enterprise Issue "
                            "query, workflow YAML validation, or v5 video status. Other POST paths "
                            "and writes are rejected."
                        ),
                    },
                    "params": {
                        "type": "object",
                        "additionalProperties": {
                            "type": ["string", "number", "boolean", "null", "array"],
                            "items": {
                                "type": ["string", "number", "boolean", "null"],
                            },
                        },
                        "description": (
                            "Native query parameters; no added defaults or query rewriting. Arrays "
                            "repeat the exact key, including [] when the endpoint requires that "
                            "spelling. These values replace matching keys already in path; null "
                            "encodes an empty value."
                        ),
                    },
                    "accept": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Native Accept header, e.g. application/json, text/plain or "
                            "application/octet-stream. Defaults to application/json."
                        ),
                    },
                    "headers": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "string",
                        },
                        "description": (
                            "Native headers, e.g. If-None-Match or Range. Authentication, host, "
                            "framing and method overrides are host-managed."
                        ),
                    },
                    "view": {
                        "type": "string",
                        "enum": ["compact", "full", "text", "raw"],
                        "default": "compact",
                        "description": (
                            "Local display only. compact preserves strings and trims repeated "
                            "identity/link fields with explicit omissions; full preserves every "
                            "field; text decodes base64 file/blob content; raw shows original "
                            "response bytes as UTF-8 or base64. Does not change the HTTP request."
                        ),
                    },
                    "json_pointer": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Select a field in the original JSON before display using RFC 6901, "
                            "e.g. /0/body, /head or /workflow_runs/0. Empty means the whole "
                            "response."
                        ),
                    },
                    "fields": {
                        "type": "object",
                        "minProperties": 1,
                        "additionalProperties": {
                            "type": "string",
                        },
                        "description": (
                            "Select JSON fields after json_pointer, e.g. {'number':'/number', "
                            "'title':'/title','author':'/user/login'}. Applies to each array item "
                            "or one object; missing fields are reported, not fabricated."
                        ),
                    },
                    "start_line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Read numbered text starting at this 1-based line.",
                    },
                    "end_line": {
                        "type": "integer",
                        "minimum": 1,
                    },
                    "max_lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2000,
                        "default": 200,
                    },
                    "tail_lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2000,
                        "description": "Numbered tail window; use instead of start_line/end_line.",
                    },
                    "context_lines": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "default": 3,
                        "description": ("Read numbered context around find in text or a JSON string pointer."),
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": (
                            "Character offset in the displayed representation; use next_offset with"
                            " the same result_id, view and json_pointer to continue."
                        ),
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 40000,
                        "default": 16000,
                    },
                    "find": {
                        "type": "string",
                        "description": (
                            "Find literal text locally: from offset in character views or "
                            "start_line in numbered views. Searches unshortened selected content."
                        ),
                    },
                },
                "oneOf": [
                    {
                        "required": ["path"],
                    },
                    {
                        "required": ["ci_logs"],
                    },
                    {
                        "required": ["result_id"],
                    },
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["requests"],
    "additionalProperties": False,
}

API_VALIDATOR = Draft202012Validator(API_SCHEMA["properties"]["requests"]["items"])

REQUEST_VALIDATOR = Draft202012Validator(API_SCHEMA["properties"]["requests"]["items"])


class GitCodeTool(APIProvider):
    api_type = GitCodeAPI

    @tool(description=API_DESCRIPTION, parameters=API_SCHEMA, returns=BATCH_OUTPUT, batch_parameter="requests",
          configuration=tuple(GITCODE_CONFIG.credentials))
    async def gitcode_api(self, call_id: str, requests: list[dict]) -> dict:
        """Read REST resources with their native methods and pagination."""

        async def one(request):
            async def action(operation):
                API_VALIDATOR.validate(request)
                metadata, resolved = await read_rest_resource(self.api, operation, request)
                return gitcode_view(self.api, metadata, resolved)

            return await self.api._job(call_id, request, action)

        with self.api.read_scope():
            return {"results": await asyncio.gather(*(one(request) for request in requests))}


def rest_request(request: dict) -> tuple[httpx.URL, str, dict, dict | None]:
    """Route only documented read POSTs; retain native GET and step-log compatibility."""
    requested = request["path"]
    if request.get("method") == "POST" and requested.startswith("/") and not requested.startswith("/api/"):
        # Only the allowlisted POST contract selects its version; GET keeps its established routing.
        candidates = [
            e
            for e in native_catalog().values()
            if e["method"] == "POST"
            and re.fullmatch(re.sub(r":\w+", "[^/]+", re.sub(r"^/api/v\d+", "", e["path"])), requested)
        ]
        if len(candidates) == 1:
            requested = candidates[0]["path"].split("/", 3)[0:3]
            requested = "/".join(requested) + request["path"]
    url = api_url(requested).copy_merge_params(request.get("params", {}))
    url = api_url(str(url))
    method, headers = request.get("method", "GET"), request_headers(request)
    raw_path = url.raw_path.partition(b"?")[0].decode("ascii")
    if not STEP_LOG_PATH.fullmatch(raw_path):
        if method == "POST":
            entry = readonly_post(raw_path)
            if entry is None:
                raise ToolInputError("POST is restricted to documented read-only queries.", path="/method")
            if url.query:
                raise ToolInputError("Supply native POST filters in json, not params.", path="/params")
            payload = request.get("json", {})
            validate_native_body(entry, payload)
            return url, method, headers | {"content-type": "application/json"}, payload
        if "json" in request:
            raise ToolInputError("json requires a documented read-only POST.", path="/json")
        return url, method, headers, None
    if "json" in request:
        raise ToolInputError("Step logs use GET with params.step_id.", path="/json")
    if method != "GET":
        raise ToolInputError("Step logs require GET with params.step_id.", path="/method")
    values = dict(url.params)
    unknown = set(values) - {"step_id", "offset", "limit", "sort"}
    if unknown or any(len(url.params.get_list(key)) != 1 for key in values):
        raise ToolInputError("Step logs accept step_id, offset, limit and sort once each.", path="/params")
    if not values.get("step_id"):
        raise ToolInputError("Supply the step's id from the job details.", path="/params/step_id")
    body = {"step_id": values["step_id"], "sort": values.get("sort", "asc")}
    for key, default, minimum in (("offset", 0, 0), ("limit", 200, 1)):
        value = str(values.get(key, default))
        if not re.fullmatch(r"\d+", value) or int(value) < minimum:
            raise ToolInputError(f"{key} must be an integer >= {minimum}.", path=f"/params/{key}")
        body[key] = int(value)
    if body["sort"] not in {"asc", "desc"}:
        raise ToolInputError("sort must be asc or desc.", path="/params/sort")
    return url.copy_with(query=None), "POST", headers | {"content-type": "application/json"}, body


async def read_rest_resource(api, operation: str, request: dict) -> tuple[dict, dict]:
    """Fetch complete native evidence before a tool chooses fields and display windows."""
    if "result_id" not in request:
        validate_display(request)
    if "result_id" in request:
        if any(key in request for key in ("method", "params", "json", "accept", "headers", "paginate")):
            raise ValueError("method, params, accept, headers and paginate require a new path request")
        metadata = api._saved(request["result_id"])
    elif "ci_logs" in request:
        if any(key in request for key in ("method", "params", "json", "accept", "headers", "paginate")):
            raise ValueError("ci_logs accepts its own options and local display options only")
        metadata = await read_ci_logs(api, operation, request)
    elif "paginate" in request:
        metadata = await collect_rest_pages(api, operation, request)
    else:
        metadata = await api._request(operation, *rest_request(request))
    return metadata, request


DSL_DESCRIPTION = (
    "Read GitCode with the DSL (GraphQL syntax); query is the document text. Fields retain GitCode native meanings "
    "and spelling; use aliases, fragments and declared variables normally. Independent roots and nested "
    'resources can share a query. Example: {repo(owner:"O",repo:"R"){issues(search:"timeout",'
    'state:"all",first:20){totalCount nodes{number title '
    "comments_list(last:2){totalCount nodes{user{login} body}}}}}}. "
    "Paged collections use totalCount/nodes; unknown totals are null. Native envelope fields remain available. "
    "Nested reads inherit native identifiers. "
    'search_repositories/search_issues/search_users(q:"keywords") search globally; repository issues uses search. '
    "Identify repositories with full_name/web_url and namespace; owner.login can differ from the namespace. "
    'Repository pull_requests supports filters; org(org:"O"){pull_requests(search:"words"){nodes{number title}}} '
    "supports text search within the organization.\n"
    '{schema(fields:["pull_request"])} returns SDL text with arguments and output types; type:"PullRequest" selects '
    "another type. Omit fields for compact signatures; select fields for native meanings and sources. "
    "Standard __type/__schema also works. Fields from different native endpoints may be absent in a response. "
    "Page/per_page and native ordering remain available; first sets page size, limit collects up to 2000 "
    "items, e.g. issues(first:100,limit:120){nodes{number title}}. "
    "Issue/PR comments_list(last:N) reads the newest N comments "
    "chronologically. Pagination evidence is in "
    "extensions.pages at each selected nodes path. Native next_request objects work in get(request:...). "
    "Unknown completeness does not establish that no further items exist. "
    "contents(path:,ref:){text} decodes a file. "
    'file_list(file_name:"README.md") uses a full filename and returns path strings without subfields; '
    "omitting file_name lists paths recursively across the repository. "
    'get(request:{path:"/native/path",params:{...}}) exposes native reads as a JSON scalar with no sub-selection. '
    'DSL type coverage can differ from the REST API; schema(fields:["get"]) explains request options '
    "and local projection.\n"
    "Results use data/errors/extensions. Document errors prevent execution; execution errors identify paths. "
    'Full results are saved: saved(id:"RESULT_ID",at:"/data/path",fields:["title"]) reads '
    "exact JSON locally without a sub-selection. extensions.display supplies executable local reads "
    "for truncated displays."
)

DSL_SCHEMA = SCHEMA

__all__ = [
    "API_SCHEMA",
    "API_TOOL_DEFINITIONS",
    "BACKEND",
    "DSL_DESCRIPTION",
    "DSL_LANGUAGE",
    "DSL_TOOL_DEFINITIONS",
    "TOOL_DEFINITIONS",
    "GitCodeDSLTool",
    "GitCodeTool",
    "api_url",
    "native_catalog",
    "public_schema",
]


class GitCodeDSLTool(APIProvider):
    api_type = GitCodeAPI

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.api.reuse_reads = True
        self.adapter = GitCodeDSLAdapter(self.api, partial(read_api, self.api), partial(gitcode_content, self.api))

    def begin_query(self):
        self.api.begin_query()
        self.adapter.begin_query()

    def clear_context(self):
        self.api.responses.clear()
        self.begin_query()

    @tool(description=DSL_DESCRIPTION, parameters=DSL_SCHEMA, returns=OUTPUT,
          configuration=tuple(GITCODE_CONFIG.credentials))
    async def gitcode_dsl(self, call_id, query, variables=None, operation_name=None, max_chars=16000, refresh=False):
        """Execute a DSL query; resolvers call GitCode REST endpoints as needed."""
        schema = self.adapter.schema()
        try:
            document = parse(query)
        except GraphQLError as exc:
            return {"errors": [exc.formatted]}
        errors = validate(schema, document)
        if errors:
            return {"errors": self.adapter.validation_errors(document, errors)}
        context, errors = operation_context(self.adapter, call_id, query, document, variables, operation_name)
        if errors:
            return {"errors": errors}
        if refresh:
            self.begin_query()
        self.storage.event(
            "dsl/plan",
            call_id=call_id,
            provider=self.api.provider,
            language=self.adapter.language,
            operation=operation_name,
        )
        with self.api.read_scope(), self.adapter.evidence_scope():
            try:
                result = execute(
                    schema,
                    document,
                    context_value=context,
                    variable_values=variables,
                    operation_name=operation_name,
                    field_resolver=self.adapter.resolve,
                    type_resolver=getattr(self.adapter, "resolve_type", None),
                    execution_context_class=getattr(self.adapter, "execution_context_class", None),
                )
                if inspect.isawaitable(result):
                    result = await result
                return self.adapter.publish(context, execution_result(result, context), max_chars)
            finally:
                await context.close()


API_TOOL_DEFINITIONS = tool_definitions(GitCodeTool)
DSL_TOOL_DEFINITIONS = tool_definitions(GitCodeDSLTool)
TOOL_DEFINITIONS = API_TOOL_DEFINITIONS + DSL_TOOL_DEFINITIONS


IDENTIFIER = {"type": ["string", "integer"], "minLength": 1, "minimum": 1}


def response_view(body: bytes, headers: dict, request: dict) -> dict:
    if request.get("find") and request.get("view", "compact") == "compact":
        request = {**request, "view": "full"}
    return api_response_view(
        GitCodeResponseContent(body, headers),
        headers,
        request,
        octet_stream_text=True,
        raw_hint="the native /raw/PATH endpoint",
        compact_fields=compact_fields,
    )


def page_array(data, options: dict) -> tuple[list, str]:
    pointer = options.get("items_pointer")
    if pointer is None:
        envelopes = (
            "items",
            "content",
            "jobs",
            "workflow_runs",
            "workflows",
            "artifacts",
            "runners",
            "tree",
            "list",
            "data",
        )
        candidates = ["/" + key for key in envelopes if isinstance(data, dict) and isinstance(data.get(key), list)]
        if isinstance(data, list):
            pointer = ""
        elif len(candidates) == 1:
            pointer = candidates[0]
        else:
            raise ToolInputError(
                "Set paginate.items_pointer to the list to collect.",
                path="/paginate/items_pointer",
                choices=candidates,
            )
    array = select_json(data, pointer)
    if not isinstance(array, list):
        raise ToolInputError("paginate.items_pointer must select an array.", path="/paginate/items_pointer")
    return array, pointer


def next_page(metadata: dict, data, array: list) -> tuple[str | None, str]:
    """Use source pagination evidence; an absent contract never implies exhaustion."""
    url = api_url(metadata["api_url"])
    links, headers = metadata["pagination"], metadata["headers"]
    if links.get("next"):
        return str(api_url(urljoin(str(url), links["next"]))), "link"
    if "x-next-page" in headers:
        value = headers["x-next-page"]
        if not value.strip() or value == "0":
            return None, "exhausted"
        if not value.isdigit():
            raise ValueError("Invalid x-next-page response header")
        return str(url.copy_set_param("page", value)), "x-next-page"
    page = url.params.get("page", "1")
    total_pages = next((headers[key] for key in ("total_page", "total-page", "x-total-pages") if key in headers), None)
    if total_pages is None and isinstance(data, dict):
        total_pages = data.get("total_page", data.get("total_pages"))
    if total_pages is not None:
        if not str(page).isdigit() or not str(total_pages).isdigit() or int(page) < 1:
            raise ValueError("Invalid native page or total_page")
        if int(total_pages) == 0 and array:
            raise ValueError("Nonempty page with total_page=0")
        if int(page) >= int(total_pages):
            return None, "exhausted"
        if not array:
            raise ValueError("Empty page before the advertised last page")
        return str(url.copy_set_param("page", str(int(page) + 1))), "total_page"
    count = headers.get("total_count", headers.get("x-total-count"))
    if count is None and isinstance(data, dict):
        count = data.get("total_count")
    if count is not None and str(count).isdigit() and page == "1" and len(array) == int(count):
        return None, "exhausted"
    if links:  # A native Link pagination contract with no next relation.
        return None, "exhausted"
    return None, "pagination_unknown"


def gitcode_content(api, metadata: dict, *, enrich: bool = True) -> GitCodeResponseContent:
    return GitCodeResponseContent(api._body(metadata), metadata["headers"])


def gitcode_view(api, metadata: dict, request: dict) -> dict:
    try:
        view = response_view(api._body(metadata), metadata["headers"], request)
    except (ValueError, TypeError, LookupError) as exc:
        detail = view_error(metadata, request, gitcode_content(api, metadata), exc)
        raise GitCodeAPIError(detail["message"], **metadata, input_error=detail) from exc
    continuation = continue_view(view, request)
    if continuation is not None:
        continuation.update(result_id=metadata["result_id"], view=view["view"], json_pointer=view["json_pointer"])
    return {**metadata, "from_result_id": "result_id" in request, **view, "continue_request": continuation}


async def collect_rest_pages(api, operation: str, request: dict) -> dict:
    options = {"max_pages": 5, "max_items": 200, **request["paginate"]}

    async def read(current, remaining, number):
        page_options = current["paginate"]
        retry = {
            **current,
            "paginate": {k: v for k, v in page_options.items() if k not in {"page_result_id", "start_index"}},
        }
        page, metadata = {"items": [], "record": {}, "retry_request": retry}, None
        try:
            url, method, headers, payload = rest_request(current)
            post_list = method == "POST" and re.fullmatch(r"/api/v8/enterprises/[^/]+/issues", url.path)
            if method != "GET" and not post_list:
                raise ToolInputError(
                    "paginate collects GET lists or the enterprise Issue query; follow next_request for step logs.",
                    path="/paginate",
                )
            identity = {
                "method": method,
                "url": str(url),
                "headers": headers,
                **({"json": payload} if payload is not None else {}),
            }
            saved_id = page_options.get("page_result_id")
            if saved_id:
                metadata = api._saved(saved_id)
                if metadata.get("request") != identity:
                    raise ValueError("paginate.page_result_id must match the requested URL and headers")
            else:
                metadata = await api._request(f"{operation}.page-{number + 1}", url, method, headers, payload)
            record = page["record"] = {
                "result_id": metadata["result_id"],
                "status": metadata["status"],
                "from_result_id": bool(saved_id),
                "items_used": 0,
            }
            if metadata.get("error") or not 200 <= metadata["status"] < 300 or metadata.get("redirect_url"):
                page["error"] = metadata.get(
                    "error",
                    {"type": "PaginationError", "message": "Expected a successful JSON page"},
                )
                return page
            data = json.loads(api._body(metadata))
            page["incomplete"] = isinstance(data, dict) and any(
                data.get(k) is True for k in ("incomplete_results", "truncated")
            )
            count = metadata["headers"].get("total_count", metadata["headers"].get("x-total-count"))
            if count is None and isinstance(data, dict):
                count = data.get("total_count")
            if str(count).isdigit():
                page["counts"] = {"total_count": int(count)}
                record["total_count"] = int(count)
            array, pointer = page_array(data, options)
            record["items_pointer"] = pointer
            index = page_options.get("start_index", 0)
            if index > len(array):
                raise ValueError("paginate.start_index is beyond the saved page's array")
            take = min(len(array) - index, remaining)
            page["items"] = array[index : index + take]
            record.update(items_in_page=len(array), start_index=index, items_used=take)
            page_request = {k: v for k, v in current.items() if k != "params"} | {"path": metadata["api_url"]}
            if index + take < len(array):
                page["next_request"] = {
                    **page_request,
                    "paginate": {**options, "page_result_id": metadata["result_id"], "start_index": index + take},
                }
                return page
            pagination_meta = metadata
            if post_list:
                # The native page counter lives in the JSON body, not in the URL.
                pagination_meta = {**metadata, "api_url": str(url.copy_set_param("page", str(payload.get("page", 1))))}
            target, contract = next_page(pagination_meta, data, array)
            record["next_page_source"] = contract
            if target is not None:
                next_request = {
                    **page_request,
                    "path": target,
                    "paginate": {k: v for k, v in options.items() if k not in {"page_result_id", "start_index"}},
                }
                if post_list:
                    following = api_url(target)
                    if following.copy_with(query=None) != url.copy_with(query=None):
                        raise ToolInputError("Enterprise Issue pagination changed endpoint.")
                    next_request.update(
                        path=str(url.copy_with(query=None)),
                        json={**payload, "page": int(following.params["page"])},
                    )
                page["next_request"] = next_request
            elif contract != "exhausted":
                page["stop_reason"] = contract
        except Exception as exc:
            page["error"] = (metadata or {}).get("error") or input_error(exc)
            if isinstance(exc, GitCodeAPIError):
                page["error"].update(exc.details)
        return page

    collected = await collect_pages(
        read,
        {**request, "paginate": options},
        max_pages=options["max_pages"],
        max_items=options["max_items"],
        key=lambda r: request_key(*rest_request(r)),
    )
    items, pages = collected.pop("items"), collected.pop("pages")
    counts, error = collected.pop("counts"), collected.pop("error", None)
    collection = {**collected, **counts, "item_count": len(items), "pages": [p["record"] for p in pages if p["record"]]}
    if not collection["observed_counts"]:
        collection.pop("observed_counts")
    return api._derived(
        operation,
        "collection",
        request,
        items,
        collection=collection,
        source_result_ids=[p["result_id"] for p in collection["pages"]],
        **({"error": error, "fatal": False} if error else {}),
    )


def extract_log_text(api, operation: str, metadata: dict) -> dict:
    body, entries = api._body(metadata), []
    if zipfile.is_zipfile(io.BytesIO(body)):
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            files = [info for info in archive.infolist() if not info.is_dir()]
            if len(files) > 2000 or sum(info.file_size for info in files) > MAX_LOG_BYTES:
                raise ValueError("Log ZIP exceeds extraction budget; original archive retained")
            parts, used = [], 0
            for info in files:
                label = f"=== {info.filename} ===\n"
                remaining = MAX_LOG_BYTES - used - len(label.encode()) - 1
                if remaining < 0:
                    raise ValueError("Log ZIP exceeds extraction budget; original archive retained")
                with archive.open(info) as source:
                    data = source.read(remaining + 1)
                if len(data) > remaining:
                    raise ValueError("Log ZIP exceeds extraction budget; original archive retained")
                used += len(data) + len(label.encode()) + 1
                parts.append(label + data.decode("utf-8") + "\n")
                entries.append({"name": info.filename, "bytes": len(data)})
            text = "".join(parts)
    else:
        text = body.decode("utf-8")
    return api._derived(
        operation,
        "log-text",
        {"result_id": metadata["result_id"]},
        text,
        source_result_ids=[metadata["result_id"]],
        archive_entries=entries,
    )


async def read_ci_logs(api, operation: str, request: dict) -> dict:
    options = request["ci_logs"]
    prefix = "/repos/" + quote(options["owner"], safe="") + "/" + quote(options["repo"], safe="")
    prefix += "/actions/runs/" + quote(str(options["run_id"]), safe="")
    sources, scan, scan_error = [], {"complete": True, "next_request": None}, None
    if "job_ids" in options:
        jobs = [{"id": value} for value in dict.fromkeys(map(str, options["job_ids"]))]
    else:
        collected = await collect_rest_pages(
            api,
            operation + ".jobs",
            {
                "path": prefix + "/jobs",
                "paginate": {"items_pointer": "/jobs", "max_pages": options.get("max_pages", 5), "max_items": 2000},
            },
        )
        sources.append(collected["result_id"])
        scan, scan_error = collected["collection"], collected.get("error")
        jobs = json.loads(api._body(collected))
    selected, remaining = jobs[: options.get("max_jobs", 10)], jobs[options.get("max_jobs", 10) :]
    logs, failed = [], []
    for index, job in enumerate(selected):
        entry = {key: job[key] for key in ("id", "name", "status") if isinstance(job, dict) and key in job}
        entry["source_result_ids"] = []
        try:
            if (
                not isinstance(job, dict)
                or not isinstance(job.get("id"), (str, int))
                or isinstance(job["id"], bool)
                or not str(job["id"])
            ):
                raise TypeError("Job list entry has no usable id")
            path = prefix + "/jobs/" + quote(str(job["id"]), safe="") + "/download_log"
            metadata = await api._download_log(f"{operation}.job-{index + 1}", str(api_url(path)))
            entry["source_result_ids"] = [*metadata.get("source_result_ids", []), metadata["result_id"]]
            entry["result_id"] = metadata["result_id"]
            if "error" in metadata:
                entry["error"] = metadata["error"]
            else:
                extracted = extract_log_text(api, f"{operation}.job-{index + 1}", metadata)
                view = gitcode_view(
                    api,
                    extracted,
                    {
                        "view": "raw",
                        "tail_lines": options.get("tail_lines", 200),
                        "max_chars": options.get("max_chars_per_job", 8000),
                    },
                )
                entry.update(
                    {
                        key: value
                        for key, value in view.items()
                        if key
                        in {
                            "result_id",
                            "content",
                            "representation",
                            "start_line",
                            "end_line",
                            "total_lines",
                            "next_offset",
                            "display_complete",
                            "continue_request",
                            "archive_entries",
                        }
                    },
                )
                entry["source_result_ids"].append(extracted["result_id"])
        except Exception as exc:
            entry["error"] = {"type": type(exc).__name__, "message": str(exc)}
            if isinstance(exc, GitCodeAPIError):
                entry["source_result_ids"].extend(exc.details.get("source_result_ids", []))
                if exc.details.get("result_id"):
                    entry["result_id"] = exc.details["result_id"]
                    entry["source_result_ids"].append(entry["result_id"])
        if "error" in entry and "id" in entry:
            failed.append(entry["id"])
        sources.extend(entry["source_result_ids"])
        logs.append(entry)
    remaining_ids = [job["id"] for job in remaining if isinstance(job, dict) and "id" in job]
    report = {
        "run_id": options["run_id"],
        "scope": "job_ids" if "job_ids" in options else "run",
        "scan_complete": scan["complete"],
        "scan_stop_reason": scan.get("stop_reason"),
        "scan_error": scan_error,
        "matched_jobs": len(jobs),
        "logs_attempted": len(logs),
        "logs": logs,
        "remaining_job_ids": remaining_ids,
        "next_jobs_request": scan["next_request"],
        "retry_jobs_request": scan.get("retry_request"),
        "resume_request": {"ci_logs": {**options, "job_ids": remaining_ids}} if remaining_ids else None,
        "retry_request": {"ci_logs": {**options, "job_ids": failed}} if failed else None,
        "complete": scan["complete"] and not remaining and all("error" not in entry for entry in logs),
    }
    return api._derived(
        operation,
        "ci-logs",
        request,
        report,
        source_result_ids=sources,
        ci={
            key: report[key]
            for key in (
                "complete",
                "remaining_job_ids",
                "resume_request",
                "retry_request",
                "next_jobs_request",
                "retry_jobs_request",
                "scan_complete",
            )
        },
    )


async def read_api(api, call_id, request):
    """Read the REST resource selected by a DSL resolver."""

    async def action(operation):
        REQUEST_VALIDATOR.validate(request)
        metadata, resolved = await read_rest_resource(api, operation, request)
        return {**metadata, "resolved_request": resolved}

    return await api._job(call_id, request, action)


DSL_LANGUAGE = "gitcode-dsl-v2"


BOUNDS = {"first", "last", "limit"}


VIRTUAL = {"enterprise": "Enterprise", "enterprise_id": "EnterpriseID", "kanban": "Kanban"}


# Explicit native vocabularies, shared by validation and schema help. Do not infer
# closed enums from arbitrary prose or apply one endpoint's vocabulary to another.
ARGUMENT_CHOICES = {
    "issues": {"sort": ("created", "updated")},
    "pull_requests": {"sort": ("created", "updated")},
    "search_issues": {"sort": ("created_at", "last_push_at")},
    "search_repositories": {"sort": ("last_push_at", "stars_count", "forks_count")},
    "search_users": {"sort": ("joined_at",)},
    "pull_request_comments_list": {"comment_type": ("diff_comment", "pr_comment")},
    "discuss_comment": {"order": ("time_asc", "time_desc", "hot_desc")},
    "org_discuss_comment": {"order": ("time_asc", "time_desc", "hot_desc")},
}


IDENTIFIERS = {
    "Issue": {"number": "number"},
    "EnterpriseIssue": {"number": "id"},
    "PullRequest": {"number": "number"},
    "Discuss": {"number": "number"},
    "OrgDiscuss": {"number": "number"},
    "DiscussComment": {"comment_id": "id"},
    "OrgDiscussComment": {"comment_id": "id"},
    "IssueComment": {"comment_id": "id"},
    "PullComment": {"comment_id": "id"},
    "User": {"username": "login"},
    "Run": {"run_id": "workflow_run_id"},
    "Job": {"job_id": "id"},
    "Kanban": {"kanban_id": "id"},
    "RunnerGroup": {"runner_group_id": "id"},
    "Release": {"tag": "tag_name"},
    "Commit": {"ref": "sha", "sha": "sha"},
}


# Cross-endpoint equivalence is a GitCode contract, not inferred from identical JSON keys.
# List/search titles and links are native scalar fields. Bodies, roles, counts and relationships
# require their own endpoint evidence; a search excerpt cannot satisfy a detail body read.
OBJECT_READS = {"issue": "Issue", "pull_request": "PullRequest"}


REUSABLE_FIELDS = frozenset({"number", "title", "html_url", "url", "created_at"})


EMBEDDED = {"user": "User", "owner": "User", "repository": "Repo", "repo": "Repo"}


@lru_cache(maxsize=1)
def relations():
    result = {}
    for operation, entry in native_catalog().items():
        if entry["scope"] != "Query":
            result.setdefault(entry["scope"], {})[entry["field"]] = operation
    return result


def segment(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value) or str(value) in {".", ".."}:
        raise ToolInputError("Resource identifiers must be nonempty strings or integers.")
    return quote(str(value), safe="")


def require(args, allowed, required=()):
    unknown = args.keys() - allowed
    if unknown:
        raise ToolInputError(
            f"Unsupported arguments: {', '.join(sorted(unknown))}.",
            code="unsupported_argument",
            choices=sorted(allowed),
        )
    missing = set(required) - args.keys()
    if missing:
        raise ToolInputError(f"Supply {', '.join(sorted(missing))}.", code="missing_argument")


def scope_arguments(args, scope, entry):
    """Inherit native path identifiers from the parent resource."""
    args, context = dict(args), dict(scope)
    for parameter in ("owner", "repo", "org", "enterprise", "enterprise_id"):
        if parameter in args and parameter in scope and args[parameter] != scope[parameter]:
            raise ToolInputError(f"Nested {parameter} conflicts with its parent scope.")
    if entry["scope"] in {"Org", "Kanban"} and "owner" in entry["arguments"]:
        args.setdefault("owner", args.pop("org", context.get("org", context.get("owner"))))
    for name, definition in entry["arguments"].items():
        if definition["in"] == "path" and name not in args and name in context:
            args[name] = context[name]
    # Issue operation logs carry repo as a query parameter rather than a path parameter.
    if entry["path"] == "/api/v5/repos/:owner/issues/:number/operate_logs" and "repo" not in args:
        args["repo"] = context.get("repo")
    return args, context


@dataclass
class Endpoint:
    kind: str
    request: dict
    scope: dict
    backwards: bool = False


def plan(name, supplied, scope):
    catalog = native_catalog()
    if name in VIRTUAL and not scope:
        allowed = {"enterprise": {"enterprise"}, "enterprise_id": {"enterprise_id"}, "kanban": {"org", "kanban_id"}}[
            name
        ]
        require(supplied, allowed, allowed)
        for value in supplied.values():
            segment(value)
        if name == "kanban":
            return plan("kanban_detail", supplied, {})
        return Endpoint(VIRTUAL[name], {}, {**supplied, "kind": VIRTUAL[name]})
    operation = relations().get(scope.get("kind"), {}).get(name) if scope else name
    if operation not in catalog:
        raise ToolInputError(f"Unknown operation {name}.")
    entry = catalog[operation]
    args, context = scope_arguments(supplied, scope, entry)
    # limit is a native Step-log argument. Collection bounds never steal it from the endpoint.
    bounds = {k: args.pop(k) for k in BOUNDS if k in args and k not in entry["arguments"]}
    if len(bounds.keys() & {"first", "last"}) > 1:
        raise ToolInputError("Choose first or last, not both.")
    if any(type(v) is not int or not 1 <= v <= 2000 for v in bounds.values()):
        raise ToolInputError("first/last/limit must be integers from 1 to 2000.")
    require(args, set(entry["arguments"]))
    if operation == "job_logs":
        args = {"offset": 0, "limit": 200, "sort": "asc", **args}
    missing = [n for n, d in entry["arguments"].items() if d["required"] and n not in args]
    if missing:
        raise ToolInputError(f"Supply {', '.join(missing)} for {operation}.", code="missing_argument")
    path = entry["path"]
    params, body = {}, {}
    for parameter, value in args.items():
        definition = entry["arguments"][parameter]
        if definition["in"] == "path":
            path = re.sub(r":" + re.escape(parameter) + r"\b", segment(value), path)
            context[parameter] = value
        elif definition["in"] == "body":
            body[parameter] = value
        else:
            params[parameter] = value
    if "owner" in args and entry["scope"] in {"Org", "Kanban"}:
        context["org"] = args["owner"]
    comment_order = "order" if entry["scope"] in {"Issue", "EnterpriseIssue"} else "direction"
    backwards = "last" in bounds
    if backwards:
        if entry["field"] != "comments_list" or comment_order not in entry["arguments"]:
            raise ToolInputError("last applies to comments_list with native ordering; use native sort/page elsewhere.")
        if "page" in args or "order" in args or "direction" in args:
            raise ToolInputError("last already sets descending order and starts at page 1.")
        params[comment_order] = "desc"
    elif entry["field"] == "comments_list" and comment_order in entry["arguments"]:
        params.setdefault(comment_order, "asc")
    for n in ("order", "direction"):
        if n in params and n not in ARGUMENT_CHOICES.get(operation, {}) and params[n] not in {"asc", "desc"}:
            raise ToolInputError(f"{n} accepts native asc or desc.", path=f"/{n}", choices=["asc", "desc"])
    if "state" in params and entry["kind"] in {"Issue", "PullRequest"}:
        allowed = {"open", "closed", "all"} | ({"locked", "merged"} if entry["kind"] == "PullRequest" else set())
        if params["state"] not in allowed:
            raise ToolInputError("Use native lowercase state values.", choices=sorted(allowed))
    for argument, choices in ARGUMENT_CHOICES.get(operation, {}).items():
        if argument in args and args[argument] not in choices:
            raise ToolInputError(
                f"Invalid {argument} for {operation}. Use {', '.join(choices)}.",
                path=f"/{argument}",
                choices=list(choices),
            )
    for n in ("q", "search"):
        if n in params and not isinstance(params[n], str):
            raise ToolInputError(f"{n} must be keyword text.")
    request = {"path": path}
    if entry["method"] == "POST":
        if operation == "job_logs":
            request["params"] = body  # Native transport preserves its established GET-shaped step-log interface.
        else:
            request.update(method="POST", json=body)
    elif params:
        request["params"] = params
    if entry["paged"]:
        destination = body if entry["method"] == "POST" else params
        size = bounds.get("first", bounds.get("last", destination.get("per_page", 20)))
        maximum = {"search_repositories": 20, "search_issues": 50, "search_users": 50}.get(operation, 100)
        if "per_page" in args and (type(args["per_page"]) is not int or not 1 <= args["per_page"] <= maximum):
            raise ToolInputError(f"per_page must be an integer from 1 to {maximum}.")
        if "page" in args and (type(args["page"]) is not int or args["page"] < 1):
            raise ToolInputError("page must be a positive integer.")
        if "first" in bounds and "per_page" in args and size != args["per_page"]:
            raise ToolInputError("first and per_page must agree; limit controls collection size.")
        destination.setdefault("page", 1)
        destination["per_page"] = min(size, maximum)
        request["json" if entry["method"] == "POST" else "params"] = destination
        request["paginate"] = {
            "max_items": bounds.get("limit", size),
            "max_pages": 20 if "limit" in bounds or size > maximum else 1,
        }
    elif bounds:
        raise ToolInputError(
            f"{operation} has no documented page/per_page contract; select its native payload "
            "or use saved to bound a fetched array.",
        )
    context["kind"] = entry["kind"]
    return Endpoint(entry["kind"], request, context, backwards)


# Empty/native maps remain JSON scalars. No result-dependent field types or inferred selections.
SCALARS = {
    "str": GraphQLString,
    "string": GraphQLString,
    "int": GraphQLInt,
    "integer": GraphQLInt,
    "float": GraphQLFloat,
    "number": GraphQLFloat,
    "bool": GraphQLBoolean,
    "boolean": GraphQLBoolean,
}


WRAPPERS = {
    "org_kanban_list": "content",
    "org_runner_groups": "runner_groups",
    "repo_runs": "workflow_runs",
    "run_jobs": "jobs",
}


def envelope(entry):
    shape = entry["example_shape"]
    if not isinstance(shape, dict) or not entry["paged"]:
        return WRAPPERS.get(next((n for n, e in native_catalog().items() if e is entry), ""))
    names = [k for k, v in shape.items() if isinstance(v, list)]
    # A documented item can itself contain lists (labels/users). Only list envelopes count here.
    if len(names) == 1 and (
        set(shape) & {"total_count", "total", "all_count", "has_next_page", "page_count"}
        or names[0] in {"tree", "list", "data", "content"}
    ):
        return names[0]
    return None


def merge_shape(left, right):
    if left is None:
        return copy.deepcopy(right)
    if isinstance(left, dict) and isinstance(right, dict):
        return {k: merge_shape(left.get(k), right.get(k)) if k in right else v for k, v in left.items()} | {
            k: copy.deepcopy(v) for k, v in right.items() if k not in left
        }
    if isinstance(left, list) and isinstance(right, list):
        return [merge_shape(left[0] if left else None, right[0] if right else None)]
    return left if right is None or left == right else "json"


def typename(name):
    name = "".join(part[:1].upper() + part[1:] for part in name.split("_"))
    # These type labels are ours, not REST fields. Keep native resource scopes and spellings intact.
    names = {
        "OrgDiscuss": "OrganizationDiscussion",
        "Repo": "Repository",
        "Org": "Organization",
        "Discuss": "Discussion",
        "Run": "WorkflowRun",
    }
    for previous, public in names.items():
        if name.startswith(previous):
            suffix = name[len(previous) :]
            if not suffix or suffix[0].isupper():
                return public + suffix
    return name


class GitCodeSchema:
    def __init__(self):
        self.catalog = {
            name: {**entry, "example_shape": merge_shape(entry["example_shape"], entry.get("observed_shape"))}
            for name, entry in native_catalog().items()
        }
        self.shapes, self.types, self.outputs = {}, {}, {}
        for name, entry in self.catalog.items():
            shape = entry["example_shape"]
            wrap = WRAPPERS.get(name) or envelope(entry)
            item = shape.get(wrap) if wrap else shape
            if isinstance(item, list):
                item = item[0] if item else {}
            if entry["kind"] != "Native" and isinstance(item, dict):
                self.shapes[entry["kind"]] = merge_shape(self.shapes.get(entry["kind"]), item)
        for kind in set(relations()) | set(VIRTUAL.values()):
            self.shapes.setdefault(kind, {})
        for kind, shape in self.shapes.items():
            self.object_type(kind, shape, kind=kind)
        roots = {name: self.operation(name) for name in self.catalog}
        roots["pull_requests_count"] = self.count_field()
        for name, kind in VIRTUAL.items():
            if name == "kanban":
                continue  # kanban_detail is the native entry point.
            roots[name] = GraphQLField(
                self.types[typename(kind)],
                args={name: GraphQLArgument(GraphQLNonNull(GraphQLID))},
                extensions={"scope": name},
            )
        roots.update(dsl_fields())
        self.schema = GraphQLSchema(GraphQLObjectType("Query", roots), types=list(self.types.values()))
        for operation, entry in self.catalog.items():
            output = self.output(operation)
            wrap = WRAPPERS.get(operation) or envelope(entry)
            if get_named_type(output).extensions.get("connection") and not wrap:
                output = output.fields["nodes"].type
            self.annotate(output, native_catalog()[operation]["example_shape"], operation)
            if "observed_shape" in entry:
                self.annotate(output, entry["observed_shape"], operation, evidence=entry["observed_evidence"])
        for type_ in self.types.values():
            for definition in type_.fields.values():
                meanings = {}
                for source in definition.extensions.get("native_sources", []):
                    if source.get("description"):
                        meanings.setdefault(source["description"], []).append(source["operation"])
                descriptions = [definition.description] if definition.description else []
                descriptions.extend(
                    f"{text}\nNative description ({', '.join(sorted(set(operations)))})."
                    for text, operations in meanings.items()
                )
                definition.description = "\n".join(descriptions) or None

    def annotate(self, type_, shape, operation, path=(), *, evidence=None):
        """Track each native source separately even when several endpoints share an object type."""
        if isinstance(shape, list):
            shape = shape[0] if shape else {}
        type_ = get_named_type(type_)
        if not isinstance(type_, GraphQLObjectType) or not isinstance(shape, dict):
            return
        for name, child in shape.items():
            definition = type_.fields.get(name)
            if definition is None or definition.extensions.get("operation"):
                continue
            field_path = (*path, name)
            native = ".".join(field_path)
            source = {"operation": operation, "field": native}
            if evidence:
                source["observed_evidence"] = evidence
            description = self.catalog[operation].get("response_fields", {}).get(native)
            if description:
                source["description"] = description
            definition.extensions.setdefault("native_sources", []).append(source)
            self.annotate(definition.type, child, operation, field_path, evidence=evidence)

    def shape_type(self, name, shape, *, embedded=None):
        if isinstance(shape, list):
            return GraphQLList(self.shape_type(name, shape[0] if shape else {}, embedded=embedded))
        if isinstance(shape, dict):
            if embedded in EMBEDDED and shape:
                return self.types.get(typename(EMBEDDED[embedded])) or self.object_type(
                    EMBEDDED[embedded],
                    self.shapes.get(EMBEDDED[embedded], shape),
                    kind=EMBEDDED[embedded],
                )
            return self.object_type(name, shape) if shape else JSON
        return SCALARS.get(shape, JSON) if isinstance(shape, str) else JSON

    def object_type(self, name, shape, *, kind=None):
        name = typename(name)
        if name in self.types:
            return self.types[name]

        def fields():
            result = {}
            for key, value in shape.items():
                if not re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*", key) or key.startswith("__"):
                    continue
                type_ = self.shape_type(name + typename(key), value, embedded=key)
                if (key == "id" or key.endswith("_id")) and not isinstance(value, (list, dict)):
                    type_ = GraphQLID
                result[key] = GraphQLField(type_)
            for key, operation in relations().get(kind, {}).items():
                if key not in result:
                    result[key] = self.operation(operation, nested=True)
            if kind == "Repo":
                result["pull_requests_count"] = self.count_field(nested=True)
                for key, description in {
                    "full_name": "Native repository path including its namespace; use this to identify the repository.",
                    "web_url": "Native web URL for the repository.",
                    "namespace": "Native repository namespace; path/type distinguishes organization and user spaces.",
                    "owner": "Native owner user metadata. owner.login can differ from the namespace in full_name.",
                }.items():
                    if key in result:
                        result[key].description = description
            # Text is decoded explicitly, without altering native content/encoding fields.
            if "encoding" in shape and "content" in shape:
                result["text"] = GraphQLField(
                    GraphQLString,
                    description="Decoded UTF-8 file content.",
                    extensions={"decode": True},
                )
            return result or {"raw": GraphQLField(JSON, extensions={"raw": True})}

        self.types[name] = GraphQLObjectType(name, fields, extensions={"kind": kind or name})
        return self.types[name]

    def output(self, operation):
        if operation in self.outputs:
            return self.outputs[operation]
        if operation == "repo_languages":
            return JSON  # Arbitrary language names are map keys, not schema fields.
        entry = self.catalog[operation]
        shape = entry["example_shape"]
        wrap = WRAPPERS.get(operation) or envelope(entry)
        if wrap:
            name = typename(operation) + "Result"
            native_shape = copy.deepcopy(shape)
            result = self.object_type(name, native_shape)
            if entry["kind"] != "Native":
                # Install a typed item relation on the fixed native envelope.
                result.fields[wrap] = GraphQLField(GraphQLList(self.types[typename(entry["kind"])]))
        elif entry["kind"] != "Native":
            result = self.types[typename(entry["kind"])]
            if isinstance(shape, list) or entry["paged"] or operation == "issue_pull_requests":
                result = GraphQLList(result)
        else:
            result = self.shape_type(typename(operation) + "Result", shape)
            if entry["paged"] and not is_list_type(result):
                result = GraphQLList(result)
        if entry["paged"]:
            nodes = result.fields[wrap].type if wrap else result
            name = typename(operation) + "Connection" if wrap else get_named_type(nodes).name + "Connection"
            if name not in self.types:
                self.types[name] = GraphQLObjectType(
                    name,
                    {
                        **(result.fields if wrap else {}),
                        "totalCount": GraphQLField(
                            GraphQLInt,
                            description="Native total for these filters, independent of page size. Null when absent "
                            "or inconsistent across pages. Count-only reads use the native count endpoint when "
                            "available, otherwise one item page; they do not scan the collection.",
                        ),
                        "nodes": GraphQLField(nodes, description="The requested page or bounded collection of items."),
                    },
                    extensions={"connection": True},
                )
            result = self.types[name]
        self.outputs[operation] = result
        return result

    def operation(self, name, nested=False):
        entry = self.catalog[name]
        inherited = {"owner", "repo", "org", "enterprise", "enterprise_id"}
        inherited |= set(IDENTIFIERS.get(entry["scope"], {}))
        args = {}
        for key, item in entry["arguments"].items():
            if key == "only_count":
                continue
            if nested and key in inherited:
                continue
            raw_type = item["type"].lower()
            type_ = SCALARS.get(raw_type, JSON)
            if item["in"] == "path" and (key.endswith("_id") or key in {"id", "number"}):
                type_ = GraphQLID
            default = (
                {"offset": 0, "limit": 200, "sort": "asc"}.get(key, Undefined) if name == "job_logs" else Undefined
            )
            if item["required"] and default is Undefined:
                type_ = GraphQLNonNull(type_)
            description = item["description"]
            choices = ARGUMENT_CHOICES.get(name, {}).get(key)
            if choices:
                description += "\nAllowed native values: " + ", ".join(choices) + "."
            args[key] = GraphQLArgument(type_, default_value=default, description=description)
        if entry["paged"]:
            for key in ("first", "limit"):
                args.setdefault(
                    key,
                    GraphQLArgument(
                        GraphQLInt,
                        description=(
                            "Page size, default 20."
                            if key == "first"
                            else "Collect at most this many items (maximum 2000), e.g. "
                            "issues(first:100,limit:120){nodes{number title}}."
                        ),
                    ),
                )
            if entry["field"] == "comments_list" and {"order", "direction"} & entry["arguments"].keys():
                args["last"] = GraphQLArgument(
                    GraphQLInt,
                    description=(
                        "Fetch the latest N comments, displayed chronologically; excludes page/order/direction."
                    ),
                )
        description = entry["title"] + "\n" + entry["docs"]
        description += f"\nNative source: {entry['method']} {entry['path']}."
        if not entry["paged"] and is_list_type(self.output(name)):
            description += " No documented native pagination; first/last/limit are not arguments here."
        if name == "file_list":
            description += (
                "\nReturns path strings recursively across the repository, not directory objects. "
                "Filter with a full file_name (e.g. README.md); select this list without subfields."
            )
        if entry["paged"]:
            description += (
                "\nSelect totalCount for the native total matching these filters (null if unknown), "
                "and nodes { ... } for item fields. The connection shape is fixed."
            )
        return GraphQLField(
            self.output(name),
            args=args,
            description=description,
            extensions={"operation": name, "nested": nested},
        )

    def count_field(self, nested=False):
        args = {
            k: v
            for k, v in self.operation("pull_requests", nested).args.items()
            if k not in {"first", "last", "limit", "page", "per_page"}
        }
        return GraphQLField(
            GraphQLInt,
            args=args,
            description="Native pull_requests only_count=true: the count for these filters.",
            extensions={"operation": "pull_requests", "count": True},
        )


@lru_cache(maxsize=1)
def public_schema():
    return GitCodeSchema().schema


@dataclass
class DSLValue:
    data: dict
    kind: str
    scope: dict
    operation: str
    source: str | None = None
    at: str = ""


class GitCodeDSLAdapter(DSLExtensions):
    """Resolve the DSL schema through GitCode REST endpoints."""

    language = DSL_LANGUAGE
    schema = staticmethod(public_schema)

    @staticmethod
    def identity(kind, scope):
        if kind not in OBJECT_READS.values() or not all(k in scope for k in ("owner", "repo", "number")):
            return None
        return Identity(kind, (scope["owner"], scope["repo"], str(scope["number"])))

    def remember(self, data, kind, scope, operation, source, at):
        identity = self.identity(kind, scope)
        if identity is None or source is None:
            return
        index = self.evidence
        obj = index.object(identity)
        for name, value in data.items():
            if isinstance(value, (dict, list)) or (operation not in OBJECT_READS and name not in REUSABLE_FIELDS):
                continue
            obj.fields[field_key(name)] = Value(value, frozenset({Evidence(source, at).child(name)}))

    def wrap(self, data, type_, scope, operation, source=None, at=""):
        if is_leaf_type(get_named_type(type_)):
            return data
        if isinstance(data, list):
            return [
                self.wrap(v, get_named_type(type_), scope, operation, source, f"{at}/{i}") for i, v in enumerate(data)
            ]
        if not isinstance(data, dict) or not isinstance(get_named_type(type_), GraphQLObjectType):
            return data
        kind = get_named_type(type_).extensions.get("kind", get_named_type(type_).name)
        scoped = dict(scope)
        for target, native in IDENTIFIERS.get(kind, {}).items():
            if native in data:
                scoped[target] = data[native]
        repo = data if kind == "Repo" else data.get("repository")
        if isinstance(repo, dict) and isinstance(repo.get("full_name"), str) and "/" in repo["full_name"]:
            scoped["owner"], scoped["repo"] = repo["full_name"].split("/", 1)
        scoped["kind"] = kind
        self.remember(data, kind, scoped, operation, source, at)
        return DSLValue(data, kind, scoped, operation, source, at)

    async def fetch(self, context, info, operation, args, scope):
        entry = native_catalog()[operation]
        nested_name = entry["field"] if scope else operation
        only_count = info.parent_type.fields[info.field_name].extensions.get("count")
        if only_count:
            args = {**args, "only_count": True}
        endpoint = plan(nested_name, args, scope)
        type_ = get_named_type(info.return_type)
        selected = (
            collect_sub_fields(info.schema, context.fragments, context.variables, type_, info.field_nodes)
            if isinstance(type_, GraphQLObjectType)
            else {}
        )
        identity = self.identity(endpoint.kind, endpoint.scope)
        if identity and operation in OBJECT_READS and selected:
            index = self.evidence
            obj = index.objects.get((identity.kind, identity.key))
            values, origins = {}, set()
            for nodes in selected.values():
                name = nodes[0].name.value
                if name == "__typename":
                    continue
                if obj is None or nodes[0].selection_set or nodes[0].arguments:
                    break
                value, complete, sources = index.read(Field(field_key(name), name), obj)
                if not complete:
                    break
                values[name] = value
                origins.update(sources)
            else:
                context.sources.extend(origins)
                context.reused = True
                return self.wrap(values, info.return_type, endpoint.scope, operation)
        only_relations = selected and all(
            nodes[0].name.value == "__typename" or type_.fields[nodes[0].name.value].extensions.get("operation")
            for nodes in selected.values()
        )
        if only_relations and not is_list_type(info.return_type):
            return self.wrap({}, info.return_type, endpoint.scope, operation)
        wrap = WRAPPERS.get(operation) or envelope(entry)
        connection = type_.extensions.get("connection")
        count_selection = connection and all(
            nodes[0].name.value in {"totalCount", "__typename"} for nodes in selected.values()
        )
        if count_selection and "only_count" in entry["arguments"]:
            endpoint = plan(
                nested_name,
                {
                    **{k: v for k, v in args.items() if k not in BOUNDS | {"page", "per_page"}},
                    "only_count": True,
                },
                scope,
            )
            only_count = True
        request = copy.deepcopy(endpoint.request)
        if only_count:
            request.pop("paginate", None)
        elif count_selection:
            # Keep filters (including v8 POST bodies), but only fetch the total's native evidence.
            request["json" if entry["method"] == "POST" else "params"].update(page=1, per_page=1)
            request["paginate"] = {"max_pages": 1, "max_items": 1}
        if wrap and "paginate" in request:
            request["paginate"]["items_pointer"] = "/" + wrap
        result = await context.read(request, info.path.as_list())
        content = self.content(result)
        if only_count:
            # Native PR counts contain every state even when the request filters by state.
            # Other filters apply to these buckets; omitted state means all, open means opened.
            state = request["params"].get("state", "all")
            key = "opened" if state == "open" else state
            total = content.data.get(key, content.data.get("total_count"))
            total = int(total) if str(total).isdigit() else None
            return {"totalCount": total} if connection else total
        if content.is_json:
            data = content.data
        else:
            try:
                data = content.body.decode("utf-8")
            except UnicodeDecodeError:
                data = {"result_id": result["result_id"], "binary": True, "size_bytes": len(content.body)}
        collection = result.get("collection")
        if collection:
            # Completeness describes selected nodes, never a count-only query. Raw collection
            # evidence (including the header probe) remains saved under source_result_ids.
            paths = (
                [
                    [*info.path.as_list(), alias]
                    for alias, nodes in selected.items()
                    if nodes[0].name.value in {"nodes", wrap}
                ]
                if connection
                else [info.path.as_list()]
            )
            context.pages.extend({"path": path, **collection} for path in paths)
            if wrap and not connection and isinstance(data, list):
                original = self.api._saved(collection["pages"][0]["result_id"])
                original = ResponseContent(self.api._body(original), original["headers"]).data
                data = {**original, wrap: data}
        elif result.get("pagination"):
            context.pages.append({"path": info.path.as_list(), "pagination": result["pagination"]})
        if result.get("redirect_url"):
            data = {"redirect_url": result["redirect_url"]}
        if connection:
            native, origin, items, at = {}, result, data, ""
            if wrap:
                origin = self.api._saved(collection["pages"][0]["result_id"]) if collection else result
                native = self.content(origin).data
                if not collection:
                    items, at = native[wrap], f"/{wrap}"
            nodes = self.wrap(items, type_.fields["nodes"].type, endpoint.scope, operation, result["result_id"], at)
            if endpoint.backwards:
                # Reverse presentation after wrapping so native evidence pointers retain their indices.
                nodes.reverse()
            payload = {**native, "totalCount": (collection or {}).get("total_count"), "nodes": nodes}
            if wrap:
                payload[wrap] = nodes
            return DSLValue(payload, type_.name, endpoint.scope, operation, origin["result_id"])
        return self.wrap(data, info.return_type, endpoint.scope, operation, result["result_id"])

    async def resolve(self, source, info, **args):
        context = info.context
        definition = info.parent_type.fields[info.field_name]
        options = definition.extensions
        if info.parent_type is info.schema.query_type:
            if info.field_name in dsl_fields():
                return await self.resolve_dsl_field(context, info, args)
            if options.get("scope"):
                kind = VIRTUAL[info.field_name]
                return DSLValue({}, kind, {**args, "kind": kind}, "")
            return await self.fetch(context, info, options["operation"], args, {})
        if not isinstance(source, DSLValue):
            return source.get(info.field_name) if isinstance(source, dict) else None
        if options.get("operation"):
            return await self.fetch(context, info, options["operation"], args, source.scope)
        if options.get("raw"):
            return source.data
        if options.get("decode"):
            if source.data.get("encoding") == "base64":
                import base64

                return base64.b64decode(source.data["content"]).decode("utf-8")
            return source.data.get("content")
        name = info.field_name
        if name not in source.data:
            raise GraphQLError(
                f"Native field {info.parent_type.name}.{name} is absent from the {source.operation} response.",
                extensions={
                    "code": "native_field_unavailable",
                    "result_id": source.source,
                    "operation": source.operation,
                    "catalog_sources": sorted({s["operation"] for s in options.get("native_sources", [])}),
                    "schema_request": schema_request(info.parent_type.name, [name]),
                },
            )
        return self.wrap(
            source.data[name],
            info.return_type,
            source.scope,
            source.operation,
            source.source,
            source.at + "/" + name,
        )
