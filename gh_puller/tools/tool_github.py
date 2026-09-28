"""GitHub REST, GraphQL and DSL tools."""

import asyncio
import inspect
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache, partial
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLArgument,
    GraphQLError,
    GraphQLIncludeDirective,
    GraphQLInt,
    GraphQLObjectType,
    GraphQLSkipDirective,
    InlineFragmentNode,
    NamedTypeNode,
    NameNode,
    NonNullTypeNode,
    OperationDefinitionNode,
    OperationType,
    TypeNameMetaFieldDef,
    build_schema,
    execute,
    extend_schema,
    get_named_type,
    get_operation_ast,
    is_abstract_type,
    is_leaf_type,
    parse,
    print_ast,
    print_type,
    validate,
    value_from_ast_untyped,
)
from graphql.execution.collect_fields import collect_fields
from graphql.execution.execute import ExecutionContext
from graphql.execution.values import get_argument_values, get_directive_values
from graphql.language import Visitor, visit
from jsonschema import Draft202012Validator

from ..configuration import Credential, ToolConfig
from .githost_api_utils import (
    DISPLAY_OPTIONS,
    APIProvider,
    collect_pages,
    continue_view,
    page_error,
    request_key,
    response_view,
    validate_display,
    view_error,
)
from .githost_dsl import (
    JSON,
    OUTPUT,
    SCHEMA,
    DSLExtensions,
    Evidence,
    Field,
    Identity,
    Object,
    Value,
    changed,
    dsl_fields,
    execution_result,
    field,
    literal,
    operation_context,
    query_document,
    selection,
)
from .githost_dsl import (
    key as response_key,
)
from .github_api import (
    API_ORIGIN,
    SEARCH_FIELDS,
    GitHubAPI,
    GitHubAPIError,
    GitHubResponseContent,
    api_url,
    compact_fields,
    request_headers,
    result_metadata,
    search_status,
)
from .registry import BATCH_OUTPUT, ToolInputError, tool, tool_definitions
from .utils import select_json, validate_http_credential


async def validate_github_token(value, *, transport=None):
    return await validate_http_credential(value, url=API_ORIGIN + "/user", transport=transport)


GITHUB_CONFIG = ToolConfig("github", credentials={
    "github_token": Credential(("GH_TOKEN", "GITHUB_TOKEN"), validator=validate_github_token)})

NATIVE_READ_FIELDS = {
    "result_id": {
        "type": "string",
        "minLength": 1,
        "description": ("Read a saved response instead of making an HTTP request. Accepts local display options only."),
    },
    "headers": {
        "type": "object",
        "additionalProperties": {
            "type": "string",
        },
        "description": (
            "Native headers, e.g. If-None-Match, Range, X-GitHub-Api-Version. Authentication, host,"
            " framing and method overrides are host-managed."
        ),
    },
    "start_line": {
        "type": "integer",
        "minimum": 1,
        "description": "Enable numbered text lines, starting at this 1-based line.",
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
        "description": "Numbered tail window; incompatible with start_line/end_line.",
    },
    "context_lines": {
        "type": "integer",
        "minimum": 0,
        "maximum": 100,
        "default": 3,
        "description": (
            "Enable numbered lines around the first literal find match. Use raw text, view=text for"
            " base64 files, or a JSON string pointer."
        ),
    },
    "offset": {
        "type": "integer",
        "minimum": 0,
        "default": 0,
        "description": (
            "Character offset in the displayed representation, valid only for the same result_id, "
            "view, json_pointer and fields. Follow continue_request unchanged."
        ),
    },
    "max_chars": {
        "type": "integer",
        "minimum": 1,
        "maximum": 40000,
        "default": 16000,
        "description": (
            "Local display window size; the saved response remains available through result_id and json_pointer."
        ),
    },
    "find": {
        "type": "string",
        "description": (
            "Find literal text locally: from offset in character views or start_line in numbered "
            "views; returns context around the first match. A null match_line/match_offset means no"
            " match in that view/range, not an empty resource."
        ),
    },
}

REST_DESCRIPTION = (
    "Read GitHub with REST path, ci_logs, or a saved result_id. Choose one entry per request. "
    "Independent requests run concurrently; errors affect only the failed item.\n"
    "Search: /search/issues for issues; /search/pulls for PRs; /search/repositories for repositories; "
    "/search/code for default-branch code; /search/commits for commit messages. "
    "Repository filters include org:, user:, language:, topic:, stars:. Code search uses legacy REST syntax. "
    "Put keywords and filters in params.q, e.g. 'repo:OWNER/REPO timeout'. "
    "Use repo:, author:, label:, in:title,body,comments, is:open, created:, updated: as needed. "
    "Search returns at most 1,000 hits; narrow the query if necessary.\n"
    "Search is lexical by default: use it for exact versions, errors, identifiers and PRs. "
    "Use params.search_type='semantic' for natural-language issue symptoms, conceptual similarity "
    "or missing vocabulary; "
    "keep scope filters and avoid OR paraphrase lists or quoted free text. "
    "For release problems, start with versioned release notes/docs and version search; "
    "expand only to fill evidence gaps. "
    "Verify version, hardware and configuration in the hits.\n"
    "REST supports GET/HEAD with params, accept and headers on any api.github.com path, including /users/*, /user, "
    "/orgs/*, /teams/*, /gists, /notifications, /app and /rate_limit. Endpoint permissions still apply. "
    "Use native Accept formats for raw files, diffs and patches; text-match fragments may come from comments. "
    "Under /repos/OWNER/REPO: /issues/N and /issues/N/comments read conversations; "
    "/pulls/N, /pulls/N/files and /pulls/N/comments read PRs; /pulls/N/checks reads the PR and "
    "one page each of its head's checks/status, with next requests for further pages. "
    "The repository /issues list includes both issues and PRs and uses different filters from search. "
    "Find an author's PRs with /search/pulls and author:LOGIN. "
    "/contents/PATH or /readme with params.ref reads a revision; /git/trees/REF with recursive=1 lists files; "
    "/commits, /compare/BASE...HEAD, /releases and /actions/runs read history, releases and CI. "
    "Under /issues/N: /timeline, /parent, /sub_issues, /dependencies/blocked_by and /dependencies/blocking "
    "read issue relationships; "
    "/issues/N/semantically_similar finds candidate duplicates. Mentions, closed issues and merged PRs do not prove "
    "release inclusion; similar issues need verification.\n"
    "paginate collects an array with items_pointer, max_pages and max_items. "
    "Follow collection.next_request to continue or retry_request for a failed page; "
    "partial page retries may repeat items. A budget stop is not completion. "
    "ci_logs={owner,repo,run_id} reads unsuccessful jobs' logs; max_jobs/max_pages bound work. "
    "Follow returned resume/next requests for remaining jobs or pages.\n"
    "Results preserve status, headers and a result_id for the full response. "
    "Use fields to select JSON fields, e.g. {'number':'/number','title':'/title'} after json_pointer='/items'. "
    "view='full' restores JSON fields; 'text' decodes file/blob content; 'raw' reads original bytes. "
    "start_line/max_lines, tail_lines or find with context_lines read numbered excerpts. "
    "Use continue_request unchanged for the next local excerpt; pagination.next is a separate API page. "
    "A new path fetches current data; result_id reads saved data. "
    "Check incomplete_results, truncated, omissions and completion flags. "
    "Errors may include recovery_request to inspect saved data without downloading again. "
    "External redirect_url can be read with web_fetch."
)

REST_FIELDS = {
    **NATIVE_READ_FIELDS,
    "path": {
        "type": "string",
        "minLength": 1,
        "description": (
            "GitHub read path or api.github.com URL, including its query string. Also accepts "
            "GitHub blob/raw code URLs with #L10-L30 line anchors. Always makes a fresh request."
        ),
    },
    "paginate": {
        "type": "object",
        "properties": {
            "items_pointer": {
                "type": "string",
                "description": "JSON Pointer to the array to collect; '' for a root array.",
            },
            "max_pages": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
            },
            "max_items": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2000,
            },
            "page_result_id": {
                "type": "string",
                "description": "Saved first page from next_request; avoids re-fetching it.",
            },
            "start_index": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Index within the first page's array, as returned in next_request.",
            },
        },
        "required": ["items_pointer", "max_pages", "max_items"],
        "additionalProperties": False,
    },
    "ci_logs": {
        "type": "object",
        "properties": {
            "owner": {
                "type": "string",
                "pattern": "^[A-Za-z0-9_.-]+$",
            },
            "repo": {
                "type": "string",
                "pattern": "^[A-Za-z0-9_.-]+$",
            },
            "run_id": {
                "type": "integer",
                "minimum": 1,
            },
            "job_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2000,
                "items": {
                    "type": "integer",
                    "minimum": 1,
                },
                "description": ("Optional known jobs, e.g. remaining_job_ids; skips listing the run's jobs."),
            },
            "max_pages": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 5,
            },
            "max_jobs": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 10,
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
        "enum": ["GET", "HEAD"],
        "default": "GET",
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
            "Query parameters, e.g. {'q':'repo:OWNER/REPO timeout'}. Arrays repeat the exact key: "
            "use {'repository_ids[]': [1, 2]} when required by the endpoint. These values replace "
            "matching keys already in path; null encodes an empty value."
        ),
    },
    "accept": {
        "type": "string",
        "minLength": 1,
        "description": (
            "Native Accept header, e.g. application/vnd.github.text-match+json, "
            "application/vnd.github.raw+json, application/vnd.github.diff, "
            "application/octet-stream. Defaults to application/vnd.github+json."
        ),
    },
    "view": {
        "type": "string",
        "enum": ["compact", "full", "text", "raw"],
        "description": (
            "Local display only. Defaults to text for base64 files/blobs, compact otherwise. "
            "compact preserves strings; repeated identity/link fields may be omitted with explicit "
            "omissions. It trims protocol metadata. full preserves every field and all metadata; "
            "text decodes base64 file/blob content; raw shows original response bytes as UTF-8 or "
            "base64. Does not change the HTTP request."
        ),
    },
    "json_pointer": {
        "type": "string",
        "description": (
            "Select a field in the original JSON before display using RFC 6901, e.g. /items/0/body "
            "or /head. Empty means the whole response; omitted with search or relation fields "
            "defaults to /items."
        ),
    },
    "fields": {
        "type": "object",
        "minProperties": 1,
        "additionalProperties": {
            "type": "string",
        },
        "description": (
            "Local projection after json_pointer: output-name -> relative JSON Pointer, e.g. "
            "{'title':'/title','author':'/user/login'}. Applies to each array item or one object. "
            "Missing values are omitted and reported, not fabricated."
        ),
    },
}

REST_SCHEMA = {
    "type": "object",
    "properties": {
        "requests": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": REST_FIELDS,
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

REST_VALIDATOR = Draft202012Validator(REST_SCHEMA["properties"]["requests"]["items"])


class GitHubRESTTool(APIProvider):
    api_type = GitHubAPI

    @tool(description=REST_DESCRIPTION, parameters=REST_SCHEMA, returns=BATCH_OUTPUT, batch_parameter="requests",
          configuration=tuple(GITHUB_CONFIG.credentials))
    async def github_rest(self, call_id: str, requests: list[dict]) -> dict:
        """Read REST resources with their native methods and pagination."""

        async def one(request):
            async def action(operation):
                REST_VALIDATOR.validate(request)
                metadata, resolved = await read_rest_resource(self.api, operation, request)
                return github_view(self.api, metadata, resolved)

            return await self.api._job(call_id, request, action)

        with self.api.read_scope():
            return {"results": await asyncio.gather(*(one(request) for request in requests))}

    async def github_api(self, call_id, requests):
        """Historical mixed-protocol Python replay entry; not another registered tool."""

        async def one(request):
            async def action(operation):
                REQUEST_VALIDATOR.validate(request)
                read = read_graphql_query if "query" in request or "query_from" in request else read_rest_resource
                metadata, resolved = await read(self.api, operation, request)
                return github_view(self.api, metadata, resolved)

            return await self.api._job(call_id, request, action)

        with self.api.read_scope():
            return {"results": await asyncio.gather(*(one(request) for request in requests))}


def rest_request(request):
    """Build one REST GET/HEAD request; the caller already chose the protocol."""
    if any(key in request for key in ("variables", "operation_name")):
        raise ValueError("variables and operation_name require query")
    method = request.get("method", "GET")
    if method not in {"GET", "HEAD"}:
        raise ValueError("REST supports only GET and HEAD; use query for read-only GraphQL")
    url = api_url(request["path"]).copy_merge_params(request.get("params", {}))
    if url.path.rstrip("/") == "/graphql":
        raise ValueError("Use query for /graphql so the document can be validated as read-only")
    return search_url(url), method, request_headers(request), None


async def read_rest_resource(api, operation, request):
    if "result_id" not in request:
        validate_display(request)
    if "path" in request and (link := code_link(request)):
        request = await resolve_code_link(api, operation, request, link)
        validate_display(request)
        metadata = await api._request(operation, *rest_request(request))
    elif "result_id" in request:
        if any(
            key in request
            for key in ("method", "params", "accept", "headers", "variables", "operation_name", "paginate")
        ):
            raise ValueError("result_id accepts local display options only")
        metadata = api._saved(request["result_id"])
    elif "ci_logs" in request:
        if any(
            key in request
            for key in ("method", "params", "accept", "headers", "variables", "operation_name", "paginate")
        ):
            raise ValueError("ci_logs accepts its own options and local display options only")
        metadata = await read_ci_logs(api, operation, request)
    elif RELATION_PATH.fullmatch(api_url(request["path"]).path):
        url, _, _, _ = rest_request(request)
        metadata = await read_relation(api, operation, request, url)
    elif "paginate" in request:
        if re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+/checks/?", api_url(request["path"]).path):
            raise ToolInputError("Follow each checks next_request for more pages.", path="/paginate")
        metadata = await collect_rest_pages(api, operation, request)
    else:
        url, method, headers, payload = rest_request(request)
        if re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+/checks/?", url.path):
            metadata = await read_pr_checks(api, operation, request, url, headers)
        else:
            metadata = await api._request(operation, url, method, headers, payload)
    if httpx.URL(metadata.get("api_url", "")).path.startswith("/search/"):
        metadata = {**metadata, **search_status(github_content(api, metadata).data)}
    return metadata, request


GRAPHQL_DESCRIPTION = (
    "Read GitHub with GraphQL query, query_from, or a saved result_id. Choose one entry per request. "
    "Independent requests run concurrently; errors affect only that item.\nGraphQL can fetch relevant "
    "bodies, comments and PR state together across multiple objects.\nSelect fields for what remains to "
    "be learned about each object; retain useful detail and conditions. Use aliases to combine objects "
    "and fragments for shared selections. Verify version, hardware and configuration in the evidence "
    "before applying a finding.\nFor known numbers, put issue/pullRequest aliases inside "
    "repository(owner,name). Example: A's description is known and its discussion is needed; B's "
    "environment and discussion both need checking. Fetch these together; adapt fields, objects and page"
    " sizes. Set query to:\n```graphql\nquery($owner:String!,$repo:String!,$a:Int!,$b:Int!){\n  "
    "repository(owner:$owner,name:$repo){\n    a:issue(number:$a){url comments(last:10){...Discussion}}"
    "\n    b:issue(number:$b){url body comments(last:10){...Discussion}}\n  }\n}\nfragment Discussion on "
    "IssueCommentConnection{\n  totalCount pageInfo{hasPreviousPage startCursor}\n  nodes{url "
    "author{login} authorAssociation body}\n}\n```\nSet variables to "
    '{"owner":"OWNER","repo":"REPO","a":1,"b":2}.\nSearch can also fetch needed evidence together; adapt '
    "q, fields and page sizes:\n"
    '{"query":"query($q:String!,$after:String){search(query:$q,type:ISSUE,first:10,after:$after){issueCount'
    " pageInfo{hasNextPage endCursor} nodes{... on Issue{number url title state body "
    "comments(last:10){totalCount pageInfo{hasPreviousPage startCursor} nodes{url author{login} "
    'authorAssociation body}}}}}}","variables":{"q":"repo:OWNER/REPO is:issue TERM","after":null}}\nRead '
    "already fetched fields locally with result_id; query fetches new data or refreshes it. Use "
    "result_id with view='full' to inspect the saved JSON structure. If a display window ends before the"
    " fields you need, select them directly with json_pointer/fields. To read the saved search's title "
    "list:\n"
    '{"result_id":"RESULT_ID","json_pointer":"/data/search/nodes","fields":{"number":"/number","title":"/title","state":"/state","url":"/url"}}'
    "\nFor A's saved comments use /data/repository/a/comments. fields maps output names to relative JSON "
    "Pointers in each selected array item or object. Local views can only select fields present in the "
    "saved response. field_projection omissions are intentional. Use continue_request unchanged for the "
    "next local excerpt (no HTTP); offsets do not carry over to a different view/selection.\nFor PR "
    "search use is:pr and ... on PullRequest. GraphQL search uses GitHub search qualifiers: ISSUE for "
    "exact search, ISSUE_SEMANTIC for meaning (issues only). For semantic search use natural language "
    "plus scope filters, avoiding OR paraphrase lists or quoted free text; select "
    "issueSearchType/lexicalFallbackReason to check fallback. Search is limited to 1,000 hits. Useful "
    "fields: issue.closedByPullRequestsReferences(includeClosedPrs:true), pullRequest.reviewThreads "
    "{isResolved isOutdated comments {...}}, repository.discussions, Commit.blame and ProjectsV2. "
    "Inspect uncertain fields/args via __type(name:TYPE). Only read-only queries are allowed; check "
    "graphql_errors/partial_data even on HTTP 200.\nRead enough evidence for the question; a page or "
    "recent-comment sample is not the full result. Select totalCount/issueCount and pageInfo; follow "
    "relevant pages and excerpts before drawing conclusions. query_from=RESULT_ID reuses the native "
    "query and bindings; variables overrides bindings. Reset cursors when changing filters. paginate "
    "collects items_pointer within max_pages/max_items; supply connection_pointer, cursor_variable bound"
    " to after, and pageInfo{hasNextPage endCursor}. An unchanged saved page is reused with paginate. "
    "collection.next_request continues API pagination, reusing any saved page remainder. Follow it "
    "unchanged, or retry_request on failure; partial retries may repeat items. Budget stops are not "
    "completion. Nested connections paginate separately; last:N uses before/startCursor to read earlier "
    "history.\ncompact preserves every selected JSON field and trims protocol metadata. String values are"
    " preserved within max_chars; Check incomplete_results, truncated, omissions and completion flags. "
    "text decodes files; raw exposes bytes; line options select numbered excerpts. Errors may supply "
    "recovery_request; external redirect_url can use web_fetch."
)

GRAPHQL_FIELDS = {
    **NATIVE_READ_FIELDS,
    "query": {
        "type": "string",
        "minLength": 1,
        "description": (
            "GitHub GraphQL document. Only query operations and fragments are allowed; "
            "mutations/subscriptions are rejected before HTTP."
        ),
    },
    "query_from": {
        "type": "string",
        "minLength": 1,
        "description": (
            "Reuse a saved GraphQL response's exact query, variables, operation and headers. "
            "Makes a fresh request unless paginate can reuse that page unchanged. variables "
            "overrides selected bindings."
        ),
    },
    "variables": {
        "type": "object",
        "additionalProperties": True,
        "description": "GraphQL variable bindings; with query_from, merge into saved bindings.",
    },
    "operation_name": {
        "type": "string",
        "minLength": 1,
        "description": "GraphQL operationName; required with multiple queries.",
    },
    "paginate": {
        "type": "object",
        "properties": {
            "items_pointer": {
                "type": "string",
                "description": "JSON Pointer to the array to collect; '' for a root array.",
            },
            "max_pages": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
            },
            "max_items": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2000,
            },
            "connection_pointer": {
                "type": "string",
                "description": (
                    "pointer to the connection containing pageInfo { hasNextPage endCursor }, e.g. "
                    "/data/repository/issues."
                ),
            },
            "cursor_variable": {
                "type": "string",
                "minLength": 1,
                "description": "declared variable used as that connection's after argument.",
            },
            "page_result_id": {
                "type": "string",
                "description": "Saved first page from next_request; avoids re-fetching it.",
            },
            "start_index": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Index within the first page's array, as returned in next_request.",
            },
        },
        "required": ["items_pointer", "max_pages", "max_items", "connection_pointer", "cursor_variable"],
        "additionalProperties": False,
    },
    "view": {
        "type": "string",
        "enum": ["compact", "full", "text", "raw"],
        "description": (
            "Local display only. Defaults to text for base64 files/blobs, compact otherwise. "
            "compact preserves GraphQL selections. It trims protocol metadata. full preserves every"
            " field and all metadata; text decodes base64 file/blob content; raw shows original "
            "response bytes as UTF-8 or base64. Does not change the HTTP request."
        ),
    },
    "json_pointer": {
        "type": "string",
        "description": (
            "Select a field in the original JSON before display using RFC 6901, e.g. "
            "/data/search/nodes or /data/repository/a/comments. Empty means the whole response."
        ),
    },
    "fields": {
        "type": "object",
        "minProperties": 1,
        "additionalProperties": {
            "type": "string",
        },
        "description": (
            "Local projection after json_pointer: output-name -> relative JSON Pointer, e.g. "
            "{'title':'/title','author':'/author/login'}. Applies to each array item or one object."
            " Missing values are omitted and reported, not fabricated."
        ),
    },
}

GRAPHQL_SCHEMA = {
    "type": "object",
    "properties": {
        "requests": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": GRAPHQL_FIELDS,
                "oneOf": [
                    {
                        "required": ["query"],
                    },
                    {
                        "required": ["query_from"],
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

GRAPHQL_VALIDATOR = Draft202012Validator(GRAPHQL_SCHEMA["properties"]["requests"]["items"])


class GitHubGraphQLTool(APIProvider):
    api_type = GitHubAPI

    @tool(description=GRAPHQL_DESCRIPTION, parameters=GRAPHQL_SCHEMA, returns=BATCH_OUTPUT, batch_parameter="requests",
          configuration=tuple(GITHUB_CONFIG.credentials))
    async def github_graphql(self, call_id: str, requests: list[dict]) -> dict:
        """Send a read-only document to POST /graphql, or inspect a saved response."""

        async def one(request):
            async def action(operation):
                GRAPHQL_VALIDATOR.validate(request)
                metadata, resolved = await read_graphql_query(self.api, operation, request)
                return github_view(self.api, metadata, resolved)

            return await self.api._job(call_id, request, action)

        with self.api.read_scope():
            return {"results": await asyncio.gather(*(one(request) for request in requests))}


def graphql_request(request):
    """Build a read-only POST to GitHub's GraphQL endpoint."""
    headers = request_headers(request)
    if any(key in request for key in ("method", "params", "accept")):
        raise ValueError("GraphQL query uses its own POST body; omit method, params and accept")
    headers["content-type"] = headers["accept"] = "application/json"
    return api_url("/graphql"), "POST", headers, graphql_payload(request)


async def read_graphql_query(api, operation, request):
    if "result_id" in request:
        if any(
            key in request
            for key in ("method", "params", "accept", "headers", "variables", "operation_name", "paginate")
        ):
            raise ValueError("result_id accepts local display options only")
        return api._saved(request["result_id"]), request
    validate_display(request)
    resolved = restore_graphql_query(api, request)
    if "paginate" in resolved:
        metadata = await collect_graphql_pages(api, operation, resolved)
    else:
        url, method, headers, body = graphql_request(resolved)
        metadata = await api._request(operation, url, method, headers, body)
    return metadata, request


# The native get(request:) extension and historical Python entry accept either protocol.
NATIVE_GET_FIELDS = {
    **REST_FIELDS,
    **GRAPHQL_FIELDS,
    "query": {
        "type": "string",
        "minLength": 1,
        "description": (
            "GitHub GraphQL document, sent to /graphql. Only query operations and fragments are "
            "allowed; mutations/subscriptions are rejected before HTTP."
        ),
    },
    "paginate": {
        "type": "object",
        "properties": {
            "items_pointer": {
                "type": "string",
                "description": "JSON Pointer to the array to collect; '' for a root array.",
            },
            "max_pages": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
            },
            "max_items": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2000,
            },
            "connection_pointer": {
                "type": "string",
                "description": (
                    "GraphQL only: pointer to the connection containing pageInfo { hasNextPage "
                    "endCursor }, e.g. /data/repository/issues."
                ),
            },
            "cursor_variable": {
                "type": "string",
                "minLength": 1,
                "description": ("GraphQL only: declared variable used as that connection's after argument."),
            },
            "page_result_id": {
                "type": "string",
                "description": "Saved first page from next_request; avoids re-fetching it.",
            },
            "start_index": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Index within the first page's array, as returned in next_request.",
            },
        },
        "required": ["items_pointer", "max_pages", "max_items"],
        "additionalProperties": False,
    },
    "view": {
        "type": "string",
        "enum": ["compact", "full", "text", "raw"],
        "description": (
            "Local display only. Defaults to text for base64 files/blobs, compact otherwise. "
            "compact preserves GraphQL selections and REST strings; repeated REST identity/link "
            "fields may be omitted with explicit omissions. It trims protocol metadata. full "
            "preserves every field and all metadata; text decodes base64 file/blob content; raw "
            "shows original response bytes as UTF-8 or base64. Does not change the HTTP request."
        ),
    },
    "json_pointer": {
        "type": "string",
        "description": (
            "Select a field in the original JSON before display using RFC 6901, e.g. /items/0/body "
            "or /head. Empty means the whole response; omitted with search or relation fields "
            "defaults to /items."
        ),
    },
    "fields": {
        "type": "object",
        "minProperties": 1,
        "additionalProperties": {
            "type": "string",
        },
        "description": (
            "Local projection after json_pointer: output-name -> relative JSON Pointer, e.g. "
            "{'title':'/title','author':'/user/login'}. Applies to each array item or one object. "
            "Missing values are omitted and reported, not fabricated."
        ),
    },
}

API_SCHEMA = {
    "type": "object",
    "properties": {
        "requests": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": NATIVE_GET_FIELDS,
                "oneOf": [
                    {
                        "required": ["path"],
                    },
                    {
                        "required": ["query"],
                    },
                    {
                        "required": ["query_from"],
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

REQUEST_VALIDATOR = Draft202012Validator(API_SCHEMA["properties"]["requests"]["items"])

DSL_DESCRIPTION = (
    "Read GitHub with the DSL (GraphQL syntax); query is the document text. Use GitHub fields, connections, "
    "aliases, fragments and declared variables. Select only evidence needed for the question; independent "
    "roots and nested resources can share one query. Example (newest matching issues): "
    '{search(query:"repo:O/R words is:issue sort:created-desc",type:ISSUE,first:20){issueCount '
    "nodes{... on Issue{number title comments(last:1){totalCount nodes{author{login} url}}}}}}. "
    "PRs use is:pr and ... on PullRequest. ISSUE_SEMANTIC supports symptom-based Issue search; retain "
    "explicit repo/date/state qualifiers. Read pageInfo and use after/before for further pages.\n"
    "The schema adds searchCode/searchCommits(query:,first:,page:){totalCount nodes{...}}, "
    "Repository.file(path:,ref:){name text}, and PullRequestChangedFile.patch. "
    "On connections, first/last sets page size (1..100); limit is a separate argument (1..2000), e.g. "
    "comments(first:100,limit:120){nodes{body}} collects up to 120 comments automatically. "
    'schema(fields:["repository"]) discovers root arguments and output types; type:"Release" selects '
    "another type. Omit fields for compact signatures; select fields for full descriptions. "
    "Standard __type/__schema also works. "
    'The root field get(request:{path:"/native/path",params:{...}}) exposes native transport options. '
    "Its return type is the JSON scalar, so select it without subfields. DSL type coverage can differ "
    'from the native API; schema(fields:["get"]) explains request options and local projection.\n'
    "Results use data/errors/extensions. Document errors prevent execution; execution errors identify paths. "
    "Full results are saved: "
    'saved(id:"RESULT_ID",at:"/data/path",fields:["title"]) returns local JSON without a sub-selection. '
    "extensions.display supplies executable local reads for truncated displays. "
    "A partial page does not establish exhaustive coverage; "
    "closed issues or merged PRs alone do not prove release inclusion."
)

DSL_SCHEMA = SCHEMA

__all__ = [
    "API_SCHEMA",
    "DSL_DESCRIPTION",
    "DSL_LANGUAGE",
    "DSL_TOOL_DEFINITIONS",
    "GRAPHQL_SCHEMA",
    "GRAPHQL_TOOL_DEFINITIONS",
    "REST_DESCRIPTION",
    "REST_SCHEMA",
    "REST_TOOL_DEFINITIONS",
    "SCHEMA_SOURCE",
    "TOOL_DEFINITIONS",
    "GitHubDSLTool",
    "GitHubGraphQLTool",
    "GitHubRESTTool",
    "public_schema",
]


class GitHubDSLTool(APIProvider):
    api_type = GitHubAPI

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.api.reuse_reads = True
        self.adapter = GitHubDSLAdapter(self.api, partial(read_api, self.api), partial(github_content, self.api))

    def begin_query(self):
        self.api.begin_query()
        self.adapter.begin_query()

    def clear_context(self):
        self.api.responses.clear()
        self.begin_query()

    @tool(description=DSL_DESCRIPTION, parameters=DSL_SCHEMA, returns=OUTPUT,
          configuration=tuple(GITHUB_CONFIG.credentials))
    async def github_dsl(self, call_id, query, variables=None, operation_name=None, max_chars=16000, refresh=False):
        """Execute a DSL query; resolvers call GitHub REST and GraphQL APIs as needed."""
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


REST_TOOL_DEFINITIONS = tool_definitions(GitHubRESTTool)
GRAPHQL_TOOL_DEFINITIONS = tool_definitions(GitHubGraphQLTool)
DSL_TOOL_DEFINITIONS = tool_definitions(GitHubDSLTool)
TOOL_DEFINITIONS = REST_TOOL_DEFINITIONS + GRAPHQL_TOOL_DEFINITIONS + DSL_TOOL_DEFINITIONS


PR_FILES = re.compile(r"/repos/([^/]+/[^/]+)/pulls/\d+/files/?")


@dataclass(frozen=True)
class CodeLink:
    repo: str
    tail: str
    first_ref: str
    ref: str | None
    file_path: str | None
    start_line: int | None
    end_line: int | None
    namespace: str | None = None

    def request(self, original: dict, ref: str, file_path: str) -> dict:
        if not file_path or any(part in {"", ".", ".."} or "\x00" in part for part in file_path.split("/")):
            raise ToolInputError("The code URL needs a file path without empty, . or .. components.", path="/path")
        request = {
            **original,
            "path": f"/repos/{self.repo}/contents/{quote(file_path, safe='/')}",
            "params": {**original.get("params", {}), "ref": ref},
        }
        if self.start_line and not {"start_line", "end_line", "tail_lines"}.intersection(original):
            request.update(start_line=self.start_line, end_line=self.end_line)
        return request


def code_link(request: dict) -> CodeLink | None:
    """Decode URL segments separately so an escaped slash can belong to a ref."""
    url = httpx.URL(request.get("path", ""))
    if url.host not in {"github.com", "raw.githubusercontent.com"}:
        return None
    if url.scheme != "https" or url.userinfo or url.port not in {None, 443}:
        raise ToolInputError("Use an HTTPS GitHub code URL without credentials or a custom port.", path="/path")
    parts = [unquote(part) for part in url.raw_path.split(b"?", 1)[0].decode("ascii").strip("/").split("/")]
    if url.host == "github.com":
        if len(parts) < 5 or parts[2] not in {"blob", "raw"}:
            raise ToolInputError("Use a GitHub blob/raw code URL, or a GitHub API path.", path="/path")
        parts = parts[:2] + parts[3:]
    if len(parts) < 4 or not all(
        re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part not in {".", ".."} for part in parts[:2]
    ):
        raise ToolInputError("The code URL needs a repository, revision and file path.", path="/path")
    start = end = None
    if url.fragment:
        match = re.fullmatch(r"L([1-9]\d*)(?:-L([1-9]\d*))?", url.fragment)
        if not match or (match[2] and int(match[2]) < int(match[1])):
            raise ToolInputError("Use a code line anchor such as #L10 or #L10-L30.", path="/path")
        start, end = int(match[1]), int(match[2] or match[1])
    namespace = None
    if len(parts) >= 6 and parts[2] == "refs" and parts[3] in {"heads", "tags"}:
        namespace, parts = parts[3], parts[:2] + parts[4:]
    tail, first = "/".join(parts[2:]), parts[2]
    ref, file_path = None, None
    if "ref" in request.get("params", {}):
        ref = request["params"]["ref"]
        if not isinstance(ref, str) or not ref or not tail.startswith(ref + "/"):
            raise ToolInputError("params.ref must identify the revision prefix in the code URL.", path="/params/ref")
        file_path = tail[len(ref) + 1 :]
    elif re.fullmatch(r"[0-9a-fA-F]{7,40}", first) or "/" in first:
        ref, file_path = first, "/".join(parts[3:])
    if namespace and ref is not None:
        ref = f"refs/{namespace}/{ref}"
    if "paginate" in request:
        raise ToolInputError("Code links read one file; omit paginate.", path="/paginate")
    return CodeLink("/".join(parts[:2]), tail, first, ref, file_path, start, end, namespace)


def link_revision(link: CodeLink, refs: list[dict]) -> tuple[str, str]:
    """Use the longest matching branch/tag name, never guess where a slash belongs."""
    matches = []
    for item in refs:
        name = item.get("ref", "")
        if not isinstance(name, str) or not name.startswith(("refs/heads/", "refs/tags/")):
            continue
        if link.namespace and not name.startswith(f"refs/{link.namespace}/"):
            continue
        short = name.split("/", 2)[2]
        if link.tail.startswith(short + "/"):
            matches.append((short, name, item.get("object", {})))
    if not matches:
        raise ToolInputError(
            "No branch or tag matches this code URL; use a commit URL or supply params.ref.",
            code="not_found_or_inaccessible",
            path="/path",
        )
    longest = max(len(item[0]) for item in matches)
    matches = [item for item in matches if len(item[0]) == longest]
    if len(matches) != 1:
        raise ToolInputError(
            "Both a branch and a tag match this URL; use a commit permalink or an API path "
            "with an explicit refs/heads/... or refs/tags/... ref.",
            path="/path",
        )
    short, name, obj = matches[0]
    ref = obj.get("sha") if obj.get("type") == "commit" else name
    if not isinstance(ref, str) or not ref:
        raise ValueError("GitHub returned a reference without a revision")
    return ref, link.tail[len(short) + 1 :]


def file_read_request(item: dict, repo: str) -> dict | None:
    """Keep the server's pinned path/ref; deleted files are read by their old blob SHA."""
    sha = item.get("sha", "")
    if item.get("status") == "removed" and isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        return {"path": f"/repos/{repo}/git/blobs/{sha}"}
    value = item.get("contents_url")
    if not isinstance(value, str):
        return None
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        return None
    if (
        url.scheme != "https"
        or url.host != "api.github.com"
        or url.port not in {None, 443}
        or url.userinfo
        or url.fragment
        or not re.fullmatch(r"/repos/[^/]+/[^/]+/contents/.+", url.path)
        or len(url.params.get_list("ref")) != 1
        or not url.params["ref"]
    ):
        return None
    return {"path": str(url)}


def pr_file_reads(data, source: str):
    """Attach pinned read targets to complete PR file data without changing the native response."""
    match = PR_FILES.fullmatch(httpx.URL(source).path)
    if not match:
        return data
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return data
    files = []
    for item in items:
        if isinstance(item, dict) and (read := file_read_request(item, match[1])):
            enriched = {**item, "read_request": read}
            files.append({"read_request": read, **enriched})
        else:
            files.append(item)
    return {**data, "items": files} if isinstance(data, dict) else files


RELATION_PATH = re.compile(
    r"/repos/([^/]+)/([^/]+)/(pulls/([1-9]\d*)/review_threads|"
    r"issues/([1-9]\d*)/closing_pull_requests)/?",
)


PAGE_INFO = "totalCount pageInfo {hasNextPage endCursor}"


COMMENT_FIELDS = """id body url path line originalLine startLine originalStartLine
                    createdAt updatedAt author {login}"""


THREAD_FIELDS = "id isResolved isOutdated path line"


REVIEW_THREADS = (
    """query ReviewThreads($owner:String!,$repo:String!,$number:Int!,$first:Int!,
                                       $after:String,$comments:Int!) {
  repository(owner:$owner,name:$repo) {subject:pullRequest(number:$number) {
    number title url state
    items:reviewThreads(first:$first,after:$after) {
      """
    + PAGE_INFO
    + """ nodes {"""
    + THREAD_FIELDS
    + """
        comments(first:$comments) {"""
    + PAGE_INFO
    + " nodes {"
    + COMMENT_FIELDS
    + """}}}
    }
  }}
}"""
)


THREAD_COMMENTS = (
    """query ThreadComments($id:ID!,$first:Int!,$after:String) {
  subject:node(id:$id) {... on PullRequestReviewThread {
    """
    + THREAD_FIELDS
    + """
    pullRequest {number url repository {nameWithOwner}}
    items:comments(first:$first,after:$after) {"""
    + PAGE_INFO
    + " nodes {"
    + COMMENT_FIELDS
    + """}}
  }}
}"""
)


CLOSING_PULLS = (
    """query ClosingPullRequests($owner:String!,$repo:String!,$number:Int!,$first:Int!,$after:String) {
  repository(owner:$owner,name:$repo) {subject:issue(number:$number) {
    number title url state closedAt
    items:closedByPullRequestsReferences(first:$first,after:$after,includeClosedPrs:true) {
      """
    + PAGE_INFO
    + """
      nodes {number title url state merged mergedAt baseRefName headRefName repository {nameWithOwner}}
    }
  }}
}"""
)


@dataclass
class RelationPlan:
    kind: str
    graphql_request: dict
    request: dict
    pointer: str
    repository: str
    number: int


def relation_plan(request: dict, url: httpx.URL) -> RelationPlan:
    match = RELATION_PATH.fullmatch(url.path)
    owner, repo, _, pr, issue = match.groups()
    for key in ("paginate", "accept"):
        if key in request:
            raise ToolInputError(
                "This GraphQL read uses JSON and one page; follow connection.next_request.",
                path=f"/{key}",
            )
    if request.get("method", "GET") != "GET":
        raise ToolInputError("This GraphQL read supports GET paths only.", path="/method")
    allowed = {"per_page", "after"} | ({"comments_per_page", "thread_id"} if pr else set())
    params = dict(url.params)
    for key in url.params:
        if key not in allowed or len(url.params.get_list(key)) != 1:
            raise ToolInputError(f"Use only {', '.join(sorted(allowed))}, once each.", path=f"/params/{key}")

    def size(key, default):
        value = params.get(key, str(default))
        if not re.fullmatch(r"[1-9]\d*", value) or not 1 <= int(value) <= 100:
            raise ToolInputError("Use an integer from 1 to 100.", path=f"/params/{key}")
        return int(value)

    first = size("per_page", 30)
    if "after" in params and not params["after"]:
        raise ToolInputError("Use a nonempty cursor or omit after.", path="/params/after")
    variables = {"first": first, "after": params.get("after")}
    if "thread_id" in params:
        if not params["thread_id"]:
            raise ToolInputError("Use the returned thread ID.", path="/params/thread_id")
        if "comments_per_page" in params:
            raise ToolInputError("Use per_page when reading a thread's comments.", path="/params/comments_per_page")
        query, kind, pointer = THREAD_COMMENTS, "review-comments", "/data/subject"
        variables["id"] = params["thread_id"]
    else:
        query, kind, pointer = (
            (REVIEW_THREADS if pr else CLOSING_PULLS),
            ("review-threads" if pr else "closing-pull-requests"),
            "/data/repository/subject",
        )
        variables.update(owner=owner, repo=repo, number=int(pr or issue))
        if pr:
            variables["comments"] = size("comments_per_page", 5)
    graphql_request = {"query": query, "variables": variables}
    if "headers" in request:
        graphql_request["headers"] = request["headers"]
    current = {**request, "path": url.path.rstrip("/"), "params": params}
    return RelationPlan(kind, graphql_request, current, pointer, f"{owner}/{repo}", int(pr or issue))


def next_page(connection: dict, request: dict) -> dict | None:
    if not isinstance(connection, dict):
        raise TypeError("Missing connection data")
    info = connection.get("pageInfo")
    if not isinstance(info, dict) or not isinstance(info.get("hasNextPage"), bool):
        raise TypeError("Missing connection.pageInfo.hasNextPage")
    if not info["hasNextPage"]:
        return None
    cursor = info.get("endCursor")
    if not isinstance(cursor, str) or not cursor or cursor == request.get("params", {}).get("after"):
        raise ValueError("Connection has another page but no advancing endCursor")
    return {**request, "params": {**request.get("params", {}), "after": cursor}}


def relation_page(plan: RelationPlan, data: dict) -> tuple[dict, dict]:
    subject = select_json(data, plan.pointer)
    if subject is None:
        raise LookupError("Requested Issue, PR or thread was not found or is inaccessible")
    if not isinstance(subject, dict):
        raise TypeError("Expected an Issue, PR or thread object")
    if plan.kind == "review-comments":
        pr = subject.get("pullRequest", {})
        if not isinstance(pr, dict) or not isinstance(pr.get("repository"), dict):
            raise TypeError("Missing thread's pull request identity")
        if (
            pr.get("number") != plan.number
            or pr.get("repository", {}).get("nameWithOwner", "").casefold() != plan.repository.casefold()
        ):
            raise ValueError("Thread does not belong to the requested repository and PR")
    connection = subject.get("items")
    if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
        raise TypeError("Missing requested connection.nodes")
    continuation = next_page(connection, plan.request)
    pending = []
    for thread in connection["nodes"] if plan.kind == "review-threads" else []:
        if not isinstance(thread, dict):
            raise TypeError("Missing review thread data")
        # Comment pagination is independent of thread pagination. Keep it outside the projected body too.
        child = {key: value for key, value in plan.request.items() if key not in DISPLAY_OPTIONS | {"params"}}
        child["params"] = {"thread_id": thread["id"], "per_page": plan.graphql_request["variables"]["comments"]}
        child_next = next_page(thread["comments"], child)
        if child_next:
            pending.append({"thread_id": thread["id"], "next_request": child_next})
    summary = {
        "page_info": connection["pageInfo"],
        "complete": not continuation and not pending,
        "next_request": continuation,
    }
    if "totalCount" in connection:
        summary["total_count"] = connection["totalCount"]
    if plan.kind == "review-threads":
        summary["pending_comments"] = pending
    report = {"subject": {key: value for key, value in subject.items() if key != "items"}, "items": connection["nodes"]}
    return report, summary


def graphql_payload(request: dict) -> dict:
    """Parse every operation before sending a document to GitHub's query endpoint."""
    document = parse(request["query"], max_tokens=20000)
    for definition in document.definitions:
        if isinstance(definition, OperationDefinitionNode):
            if definition.operation != OperationType.QUERY:
                raise ToolInputError(
                    "Only GraphQL query operations are allowed; mutations and subscriptions are disabled",
                    code="read_only",
                    path="/query",
                )
        elif not isinstance(definition, FragmentDefinitionNode):
            raise TypeError("Only executable GraphQL queries and fragments are allowed")
    operation = get_operation_ast(document, request.get("operation_name"))
    if operation is None:
        names = [
            node.name.value for node in document.definitions if isinstance(node, OperationDefinitionNode) and node.name
        ]
        raise ToolInputError(
            "Select one GraphQL query; operation_name must identify it when multiple queries are present",
            path="/operation_name",
            choices=names,
        )
    variables = request.get("variables", {})
    for variable in operation.variable_definitions:
        name = variable.variable.name.value
        missing = (name in variables and variables[name] is None) or (
            name not in variables and variable.default_value is None
        )
        if isinstance(variable.type, NonNullTypeNode) and missing:
            raise ToolInputError(
                f"Supply a non-null value for ${name}.",
                code="missing_variable",
                path=f"/variables/{name}",
            )
    payload = {"query": request["query"], "variables": request.get("variables", {})}
    if "operation_name" in request:
        payload["operationName"] = request["operation_name"]
    cursor = request.get("paginate", {}).get("cursor_variable")
    if cursor and cursor not in {v.variable.name.value for v in operation.variable_definitions}:
        raise ValueError("paginate.cursor_variable must be declared by the selected GraphQL query")
    return payload


def search_type_filters(query: str) -> tuple[set[str], set[str]]:
    """Recognize explicit type qualifiers, excluding quoted free text."""
    # Quoted text such as '"is:pr"' is not a qualifier. Parentheses may delimit boolean terms.
    tokens = re.findall(r'(?:[^\s"()]|"(?:\\.|[^"\\])*")+', query)
    positive, negative = set(), set()
    for token in tokens:
        match = re.fullmatch(r'(-?)(?:is|type):"?(issue|pr|pull-request)"?', token, re.IGNORECASE)
        if match:
            kind = "issue" if match[2].lower() == "issue" else "pr"
            (negative if match[1] else positive).add(kind)
    return positive, negative


def search_url(url: httpx.URL) -> httpx.URL:
    """Compile convenient search routes while preserving explicit native query scopes."""
    route = url.path.rstrip("/")
    if route not in {"/search/issues", "/search/pulls"}:
        return url
    queries = url.params.get_list("q")
    if len(queries) != 1 or not queries[0].strip():
        raise ToolInputError("Supply one non-empty search query in params.q.", code="missing_query", path="/params/q")
    query = queries[0]
    positive, negative = search_type_filters(query)
    target = "pr" if route == "/search/pulls" else "issue"
    if route == "/search/pulls" and ("issue" in positive or "pr" in negative):
        raise ToolInputError(
            "This query's type filter conflicts with PR search. Use /search/issues for issues "
            "or correct the query's type filter.",
            code="conflicting_search_type",
            path="/params/q",
        )
    if not positive:
        if target in negative:
            raise ToolInputError(
                "The query excludes the resource being searched. Select the other search route "
                "or correct the type filter.",
                code="conflicting_search_type",
                path="/params/q",
            )
        query = f"is:{'pull-request' if target == 'pr' else 'issue'} {query}"
    if route == "/search/pulls":
        url = url.copy_with(path="/search/issues")
    return url.copy_set_param("q", query) if query != queries[0] else url


def github_content(api, metadata: dict, *, enrich: bool = True) -> GitHubResponseContent:
    """Expose complete data and lazy file decoding before any language-specific selection or display."""
    content = GitHubResponseContent(api._body(metadata), metadata["headers"])
    if enrich and content.is_json:
        source = metadata.get("api_url") or metadata["request"].get("path", "")
        content.data = pr_file_reads(content.data, source)
    return content


def github_view(api, metadata: dict, request: dict) -> dict:
    try:
        source = metadata.get("api_url") or metadata["request"].get("path", "")
        search = httpx.URL(source).path.startswith("/search/")
        content = github_content(api, metadata, enrich=request.get("view", "compact") == "compact")
        # Older saved responses also expose search controls, independent of projection or truncation.
        if search:
            with suppress(ValueError, UnicodeError):
                metadata = {**metadata, **search_status(content.data)}
        item_view = search or metadata.get("kind") in {"review-threads", "review-comments", "closing-pull-requests"}
        graphql = httpx.URL(source).path == "/graphql" or (
            metadata.get("kind") == "collection" and "query" in metadata["request"]
        )
        view = response_view(
            content,
            metadata["headers"],
            request,
            default_items_key="items" if item_view else "",
            default_file_text=True,
            compact_fields=None if graphql else compact_fields,
        )
    except (ValueError, TypeError, LookupError) as exc:
        detail = view_error(metadata, request, github_content(api, metadata, enrich=False), exc)
        raise GitHubAPIError(detail["message"], **metadata, input_error=detail) from exc
    continuation = continue_view(view, request)
    if continuation is not None:
        continuation.update(view=view["view"], result_id=metadata["result_id"], json_pointer=view["json_pointer"])
    return {
        **result_metadata(metadata, request),
        "from_result_id": "result_id" in request,
        **view,
        "continue_request": continuation,
    }


def restore_graphql_query(api, request: dict) -> dict:
    """Restore a GraphQL query, validating overrides before deciding whether its page is reusable."""
    if "query_from" not in request:
        return request
    saved = api._saved(request["query_from"])
    source = saved.get("request", {})
    payload = source.get("json", {})
    if (
        source.get("method") != "POST"
        or source.get("url") != API_ORIGIN + "/graphql"
        or not isinstance(payload.get("query"), str)
    ):
        raise ToolInputError(
            "query_from requires a GraphQL response ID, including a collection page ID.",
            path="/query_from",
        )
    resolved = {k: v for k, v in request.items() if k != "query_from"}
    resolved.update(
        query=payload["query"],
        variables={**payload.get("variables", {}), **request.get("variables", {})},
        headers={**source.get("headers", {}), **{k.lower(): v for k, v in request.get("headers", {}).items()}},
    )
    # Validate the supplied headers independently so case folding cannot hide duplicate names.
    request_headers(request)
    if "operationName" in payload and "operation_name" not in resolved:
        resolved["operation_name"] = payload["operationName"]
    url, method, headers, body = graphql_request(resolved)
    expected = {"method": method, "url": str(url), "headers": headers, "json": body}
    if "paginate" in resolved and "page_result_id" not in resolved["paginate"] and source == expected:
        resolved["paginate"] = {**resolved["paginate"], "page_result_id": saved["result_id"]}
    return resolved


def graphql_continuation(request: dict | None, metadata: dict | None) -> dict | None:
    """Keep next requests small; fresh retries intentionally retain their GraphQL query."""
    if request is None or metadata is None or "query" not in request:
        return request
    source = metadata.get("request", {})
    _, _, headers, payload = graphql_request(request)
    original = source.get("json", {})
    if (
        payload["query"] != original.get("query")
        or headers != source.get("headers")
        or payload.get("operationName") != original.get("operationName")
    ):
        return request
    result = {k: v for k, v in request.items() if k not in {"query", "variables", "operation_name", "headers"}}
    result["query_from"] = metadata["result_id"]
    changes = {
        k: v
        for k, v in payload["variables"].items()
        if k not in original.get("variables", {}) or v != original["variables"][k]
    }
    if changes:
        result["variables"] = changes
    return result


async def resolve_code_link(api, operation: str, request: dict, link: CodeLink) -> dict:
    if link.ref is not None:
        return link.request(request, link.ref, link.file_path)
    # Validate native options before resolution and keep display/download headers off ref lookups.
    _, _, headers, _ = rest_request(link.request(request, link.first_ref, "_"))
    refs, sources = [], []
    for namespace in (link.namespace,) if link.namespace else ("heads", "tags"):
        lookup = {
            "path": f"/repos/{link.repo}/git/matching-refs/{namespace}/{quote(link.first_ref, safe='')}",
            "headers": {"x-github-api-version": headers["x-github-api-version"]},
        }
        metadata = await api._request(f"{operation}.ref-{namespace}", *rest_request(lookup))
        sources.append(metadata["result_id"])
        if metadata["status"] != 200 or "error" in metadata:
            raise GitHubAPIError("Could not resolve the code URL's revision.", **metadata, source_result_ids=sources)
        try:
            data = json.loads(api._body(metadata))
            if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
                raise ValueError("Expected a list of Git references")
        except (ValueError, UnicodeError) as exc:
            raise GitHubAPIError(str(exc), **metadata, source_result_ids=sources) from exc
        refs.extend(data)
    ref, file_path = link_revision(link, refs)
    return link.request(request, ref, file_path)


async def read_relation(api, operation: str, request: dict, url: httpx.URL) -> dict:
    plan = relation_plan(request, url)
    metadata = await api._request(operation + ".source", *graphql_request(plan.graphql_request))
    if metadata["status"] != 200 or ("error" in metadata and not metadata.get("partial_data")):
        return metadata
    try:
        report, connection = relation_page(plan, json.loads(api._body(metadata)))
    except (ValueError, TypeError, LookupError) as exc:
        if "error" in metadata:
            return metadata
        raise GitHubAPIError(
            str(exc),
            **metadata,
            input_error={
                "code": "not_found_or_inaccessible" if isinstance(exc, LookupError) else "unexpected_response",
                "retryable": False,
                "recovery_request": {"result_id": metadata["result_id"], "view": "full"},
            },
        ) from exc
    if "error" in metadata:
        connection.update(complete=False, retry_request=plan.request)
    details = {
        key: metadata[key]
        for key in ("error", "graphql_errors", "partial_data", "rate_limit", "retry_at", "rate_limited", "fatal")
        if key in metadata
    }
    return api._derived(
        operation,
        plan.kind,
        request,
        report,
        connection=connection,
        source_result_ids=[metadata["result_id"]],
        source_status=metadata["status"],
        **details,
    )


async def read_pr_checks(api, operation: str, request: dict, url: httpx.URL, headers: dict) -> dict:
    """Read a PR's head once, then its check runs and combined status; retain each source."""
    if request.get("method", "GET") != "GET" or "paginate" in request:
        raise ToolInputError(
            "PR checks uses GET; follow each returned next_request for more pages.",
            code="invalid_checks_options",
            path="/paginate" if "paginate" in request else "/method",
        )
    root, number = url.path.rsplit("/pulls/", 1)
    pr_request = {"path": f"{root}/pulls/{number.split('/')[0]}"}
    pr = await api._request(operation + ".pr", api_url(pr_request["path"]), "GET", headers)
    if "error" in pr or pr["status"] != 200:
        return pr
    try:
        data = json.loads(api._body(pr))
    except (ValueError, UnicodeError) as exc:
        raise GitHubAPIError("Expected a JSON PR response.", **pr) from exc
    head = data.get("head") if isinstance(data, dict) else None
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
        raise GitHubAPIError("PR response has no valid head SHA.", **pr)
    report, sources, complete, errors = {"head_sha": sha, "pull_request": data}, [pr["result_id"]], True, []
    for key, suffix in (("check_runs", "check-runs"), ("status", "status")):
        target = api_url(f"{root}/commits/{sha}/{suffix}").copy_with(query=url.query)
        child = {"path": str(target), **{k: v for k, v in request.items() if k in {"accept", "headers"}}}
        try:
            metadata = await api._request(operation + "." + key, target, "GET", headers)
            sources.append(metadata["result_id"])
            entry = {k: metadata[k] for k in ("result_id", "status")}
            try:
                entry["data"] = json.loads(api._body(metadata))
            except (ValueError, UnicodeError):
                entry["recovery_request"] = {"result_id": metadata["result_id"], "view": "raw"}
                entry["error"] = {"type": "UnexpectedResponse", "message": "Expected a JSON response."}
                errors.append(entry["error"])
            if "error" in metadata:
                entry["error"] = metadata["error"]
                errors.append(entry["error"])
            elif metadata["status"] != 200:
                entry["error"] = {"type": "UnexpectedResponse", "message": f"Expected 200; got {metadata['status']}"}
                errors.append(entry["error"])
            elif "error" not in entry:
                array_key = "check_runs" if key == "check_runs" else "statuses"
                payload = entry["data"]
                if not isinstance(payload, dict) or not isinstance(payload.get(array_key), list):
                    entry["error"] = {
                        "type": "UnexpectedResponse",
                        "message": f"Expected an object with a {array_key} array.",
                    }
                    errors.append(entry["error"])
                elif payload.get("incomplete_results") or payload.get("truncated"):
                    complete = False
            next_url = metadata["pagination"].get("next")
            entry["next_request"] = {**child, "path": next_url} if next_url else None
            complete = complete and not next_url
        except Exception as exc:
            entry = {"error": {"type": type(exc).__name__, "message": str(exc)}, "retry_request": child}
            errors.append(entry["error"])
        report[key] = entry
    report["complete"] = complete and not errors
    return api._derived(
        operation,
        "pr-checks",
        request,
        report,
        source_result_ids=sources,
        checks={"complete": report["complete"], "head_sha": sha},
        **(
            {
                "error": {"type": "IncompleteChecks", "message": "Some check/status reads failed.", "errors": errors},
                "fatal": False,
            }
            if errors
            else {}
        ),
    )


async def read_ci_logs(api, operation: str, request: dict) -> dict:
    options = request["ci_logs"]
    prefix = f"/repos/{options['owner']}/{options['repo']}/actions"
    sources, scan, jobs = [], {"complete": True, "next_request": None}, []
    scan_error = None
    if "job_ids" in options:
        jobs = [{"id": job_id} for job_id in dict.fromkeys(options["job_ids"])]
    else:
        listing = {
            "path": f"{prefix}/runs/{options['run_id']}/jobs",
            "params": {"per_page": 100, "filter": "latest"},
            "paginate": {"items_pointer": "/jobs", "max_pages": options.get("max_pages", 5), "max_items": 2000},
        }
        collected = await collect_rest_pages(api, operation + ".jobs", listing)
        sources.append(collected["result_id"])
        scan = collected["collection"]
        scan_error = collected.get("error")
        all_jobs = json.loads(api._body(collected))["items"]
        failures = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
        jobs = [job for job in all_jobs if isinstance(job, dict) and job.get("conclusion") in failures]
    selected = jobs[: options.get("max_jobs", 10)]
    remaining = [job["id"] for job in jobs[len(selected) :]]
    logs = []
    for index, job in enumerate(selected):
        entry = {key: job[key] for key in ("id", "name", "conclusion", "html_url") if key in job}
        entry["source_result_ids"] = []
        metadata = None
        try:
            metadata = await api._request(
                f"{operation}.job-{index + 1}",
                api_url(f"{prefix}/jobs/{job['id']}/logs"),
                "GET",
                request_headers({}),
            )
            entry["source_result_ids"].append(metadata["result_id"])
            if "redirect_url" in metadata and "error" not in metadata:
                metadata = await api._download_log(f"{operation}.job-{index + 1}", metadata["redirect_url"])
                entry["source_result_ids"].extend(metadata.get("source_result_ids", []))
                entry["source_result_ids"].append(metadata["result_id"])
            entry["result_id"] = metadata["result_id"]
            if "error" in metadata:
                entry["error"] = metadata["error"]
            elif metadata.get("status") != 200:
                entry["error"] = {
                    "type": "GitHubHTTPError",
                    "message": f"Expected log HTTP 200; got {metadata.get('status')}",
                }
            else:
                view = response_view(
                    api._body(metadata),
                    metadata["headers"],
                    {
                        "view": "raw",
                        "tail_lines": options.get("tail_lines", 200),
                        "max_chars": options.get("max_chars_per_job", 8000),
                    },
                )
                entry.update(view)
                entry["continue_request"] = (
                    {
                        "result_id": metadata["result_id"],
                        "view": "raw",
                        "tail_lines": options.get("tail_lines", 200),
                        "max_chars": options.get("max_chars_per_job", 8000),
                        "offset": view["next_offset"],
                    }
                    if view["next_offset"] is not None
                    else None
                )
        except Exception as exc:
            entry["error"] = {"type": type(exc).__name__, "message": str(exc)}
            if isinstance(exc, GitHubAPIError):
                entry["error"].update(exc.details)
        logs.append(entry)
    report = {
        "run_id": options["run_id"],
        "scope": "job_ids" if "job_ids" in options else "run",
        "scan_complete": scan["complete"],
        "scan_stop_reason": scan.get("stop_reason"),
        "scan_error": scan_error,
        "matched_jobs": len(jobs),
        "logs_attempted": len(logs),
        "logs": logs,
        "remaining_job_ids": remaining,
        "next_jobs_request": scan["next_request"],
        "retry_jobs_request": scan.get("retry_request"),
        "resume_request": {"ci_logs": {**options, "job_ids": remaining}} if remaining else None,
        "complete": scan["complete"] and not remaining and all("error" not in entry for entry in logs),
    }
    summary = {
        key: report[key]
        for key in (
            "complete",
            "scope",
            "scan_complete",
            "scan_stop_reason",
            "scan_error",
            "matched_jobs",
            "logs_attempted",
            "remaining_job_ids",
            "next_jobs_request",
            "retry_jobs_request",
            "resume_request",
        )
    }
    return api._derived(operation, "ci-logs", request, report, source_result_ids=sources, ci=summary)


async def collect_rest_pages(api, operation: str, request: dict) -> dict:
    options = request["paginate"]
    _url, method, _headers, _payload = rest_request(request)
    if method == "HEAD":
        raise ValueError("paginate requires REST GET or a GraphQL query")
    if any(key in options for key in ("connection_pointer", "cursor_variable")):
        raise ValueError("connection_pointer and cursor_variable require GraphQL query")
    metadata = None

    async def read(current, remaining, number):
        nonlocal metadata
        page_options = current["paginate"]
        retry = {
            **current,
            "paginate": {k: v for k, v in page_options.items() if k not in {"page_result_id", "start_index"}},
        }
        page = {"items": [], "record": {}, "retry_request": retry}
        metadata = None
        try:
            url, method, headers, payload = rest_request(current)
            saved_id = page_options.get("page_result_id")
            if saved_id:
                metadata = api._saved(saved_id)
                expected = {"method": method, "url": str(url), "headers": headers}
                if metadata.get("request") != expected:
                    raise ValueError("paginate.page_result_id must match the requested URL, headers and query body")
            else:
                metadata = await api._request(f"{operation}.page-{number + 1}", url, method, headers, payload)
            record = page["record"] = {
                "result_id": metadata["result_id"],
                "status": metadata["status"],
                "from_result_id": bool(saved_id),
                "items_used": 0,
            }
            if metadata.get("redirect_url") or metadata["status"] != 200:
                page["error"] = metadata.get(
                    "error",
                    {"type": "PaginationError", "message": "Expected a JSON 200 page"},
                )
                return page
            data = github_content(api, metadata, enrich=False).data
            if url.path.startswith("/search/"):
                record.update(search_status(data))
            page["incomplete"] = isinstance(data, dict) and any(
                data.get(k) is True for k in ("incomplete_results", "truncated")
            )
            array = select_json(data, options["items_pointer"])
            if not isinstance(array, list):
                raise TypeError("paginate.items_pointer must select an array")
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
            if metadata.get("error"):
                page["error"] = metadata["error"]
                return page
            page["counts"] = {k: record[k] for k in SEARCH_FIELDS if k in record}
            if "next_request" in page:
                return page
            next_options = {k: v for k, v in options.items() if k not in {"page_result_id", "start_index"}}
            if next_url := metadata["pagination"].get("next"):
                page["next_request"] = {k: v for k, v in current.items() if k != "params"} | {
                    "path": str(api_url(next_url)),
                    "paginate": next_options,
                }
        except Exception as exc:
            page["error"] = (metadata or {}).get("error") or page_error(exc)
        return page

    collected = await collect_pages(
        read,
        request,
        max_pages=options["max_pages"],
        max_items=options["max_items"],
        key=lambda r: request_key(*rest_request(r)),
    )
    items, pages = (collected.pop("items"), collected.pop("pages"))
    counts, error = (collected.pop("counts"), collected.pop("error", None))
    collection = {**collected, "item_count": len(items), "pages": [p["record"] for p in pages if p["record"]]}
    if not collection["observed_counts"]:
        collection.pop("observed_counts")
    controls = {k: counts[k] for k in SEARCH_FIELDS if k in counts}
    for key in ("incomplete_results", "truncated"):
        if any(p["record"].get(key) is True for p in pages):
            controls[key] = True
    return api._derived(
        operation,
        "collection",
        request,
        {"items": items},
        collection=collection,
        **controls,
        **{"error": error, "fatal": False} if error else {},
    )


async def collect_graphql_pages(api, operation: str, request: dict) -> dict:
    options = request["paginate"]
    graphql_request(request)
    if not all(key in options for key in ("connection_pointer", "cursor_variable")):
        raise ValueError("GraphQL pagination requires connection_pointer and cursor_variable")
    metadata = None

    async def read(current, remaining, number):
        nonlocal metadata
        page_options = current["paginate"]
        retry = {
            **current,
            "paginate": {k: v for k, v in page_options.items() if k not in {"page_result_id", "start_index"}},
        }
        page = {"items": [], "record": {}, "retry_request": retry}
        metadata = None
        try:
            url, method, headers, payload = graphql_request(current)
            saved_id = page_options.get("page_result_id")
            if saved_id:
                metadata = api._saved(saved_id)
                expected = {"method": method, "url": str(url), "headers": headers}
                expected["json"] = payload
                if metadata.get("request") != expected:
                    raise ValueError("paginate.page_result_id must match the requested URL, headers and query body")
            else:
                metadata = await api._request(f"{operation}.page-{number + 1}", url, method, headers, payload)
            record = page["record"] = {
                "result_id": metadata["result_id"],
                "status": metadata["status"],
                "from_result_id": bool(saved_id),
                "items_used": 0,
            }
            if metadata.get("redirect_url") or metadata["status"] != 200:
                page["error"] = metadata.get(
                    "error",
                    {"type": "PaginationError", "message": "Expected a JSON 200 page"},
                )
                return page
            data = github_content(api, metadata, enrich=False).data
            page["incomplete"] = isinstance(data, dict) and any(
                data.get(k) is True for k in ("incomplete_results", "truncated")
            )
            array = select_json(data, options["items_pointer"])
            if not isinstance(array, list):
                raise TypeError("paginate.items_pointer must select an array")
            index = page_options.get("start_index", 0)
            if index > len(array):
                raise ValueError("paginate.start_index is beyond the saved page's array")
            take = min(len(array) - index, remaining)
            page["items"] = array[index : index + take]
            record.update(items_in_page=len(array), start_index=index, items_used=take)
            page_request = current
            if index + take < len(array):
                page["next_request"] = {
                    **page_request,
                    "paginate": {**options, "page_result_id": metadata["result_id"], "start_index": index + take},
                }
            if metadata.get("error"):
                page["error"] = metadata["error"]
                return page
            connection = select_json(data, options["connection_pointer"])
            record["connection_counts"] = {
                k: connection[k]
                for k in ("totalCount", "issueCount")
                if isinstance(connection, dict) and k in connection
            }
            page["counts"] = record["connection_counts"]
            if "next_request" in page:
                return page
            next_options = {k: v for k, v in options.items() if k not in {"page_result_id", "start_index"}}
            info = connection.get("pageInfo") if isinstance(connection, dict) else None
            if not isinstance(info, dict) or not isinstance(info.get("hasNextPage"), bool):
                raise ValueError("GraphQL pagination requires pageInfo.hasNextPage and endCursor")  # noqa: TRY004
            if info["hasNextPage"]:
                cursor = info.get("endCursor")
                if not isinstance(cursor, str) or not cursor:
                    raise ValueError("GraphQL hasNextPage is true but endCursor is missing")
                page["next_request"] = {
                    **current,
                    "paginate": next_options,
                    "variables": {**current.get("variables", {}), options["cursor_variable"]: cursor},
                }
        except Exception as exc:
            page["error"] = (metadata or {}).get("error") or page_error(exc)
        return page

    collected = await collect_pages(
        read,
        request,
        max_pages=options["max_pages"],
        max_items=options["max_items"],
        key=lambda r: request_key(*graphql_request(r)),
    )
    items, pages = (collected.pop("items"), collected.pop("pages"))
    counts, error = (collected.pop("counts"), collected.pop("error", None))
    collection = {**collected, "item_count": len(items), "pages": [p["record"] for p in pages if p["record"]]}
    collection["next_request"] = graphql_continuation(collection["next_request"], metadata)
    if not collection["observed_counts"]:
        collection.pop("observed_counts")
    collection["connection"] = {
        "pointer": options["connection_pointer"],
        "items_pointer": options["items_pointer"],
        **{k: counts[k] for k in ("totalCount", "issueCount") if k in counts},
    }
    return api._derived(
        operation,
        "collection",
        request,
        {"items": items},
        collection=collection,
        **{"error": error, "fatal": False} if error else {},
    )


async def read_api(api, call_id, request):
    """REST and GraphQL reads for DSL resolvers and get(request:)."""

    async def action(operation):
        REQUEST_VALIDATOR.validate(request)
        read = read_graphql_query if "query" in request or "query_from" in request else read_rest_resource
        metadata, resolved = await read(api, operation, request)
        return {**metadata, "resolved_request": resolved}

    return await api._job(call_id, request, action)


DSL_LANGUAGE = "github-dsl-v1"


SCHEMA_SOURCE = json.loads(Path(__file__).with_name("github_schema.meta.json").read_text())


@lru_cache(maxsize=1)
def public_schema():
    """The GitHub GraphQL schema used by transport tests and the DSL compiler."""
    return build_schema(Path(__file__).with_name("github_schema.graphql").read_text())


@lru_cache(maxsize=1)
def query_schema():
    schema = extend_schema(
        public_schema(),
        parse(
            """
        scalar JSON
        type GitHubFile {
            name: String path: String sha: String size: Int type: String
            content: String encoding: String text: String
            url: String html_url: String download_url: String entries: [GitHubFile!]
        }
        type CodeHit { name: String path: String url: URI repository: Repository textMatches: JSON }
        type CodeSearchResult { totalCount: Int incomplete: Boolean nodes: [CodeHit!] }
        type CommitSearchResult { totalCount: Int incomplete: Boolean nodes: [Commit!] }
        extend type Query {
            searchCode(query: String!, first: Int = 20, page: Int = 1, limit: Int): CodeSearchResult
            searchCommits(query: String!, first: Int = 20, page: Int = 1, limit: Int,
                          sort: String, order: String): CommitSearchResult
        }
        extend type Repository { file(path: String!, ref: String): GitHubFile }
        extend type PullRequestChangedFile { patch: String }
    """
            + print_type(GraphQLObjectType("Query", dsl_fields())).replace("type Query", "extend type Query", 1),
        ),
    )
    opaque = schema.get_type("JSON")
    opaque.serialize, opaque.parse_value, opaque.parse_literal = JSON.serialize, JSON.parse_value, JSON.parse_literal
    # A declared schema argument controls collection, without changing first/last or response types.
    for type_ in schema.type_map.values():
        for definition in getattr(type_, "fields", {}).values():
            target = get_named_type(definition.type)
            if (
                "first" in getattr(definition, "args", {})
                and {"nodes", "pageInfo"} <= getattr(target, "fields", {}).keys()
            ):
                definition.args["limit"] = GraphQLArgument(
                    GraphQLInt,
                    description=(
                        "Collect at most N nodes/edges (1..2000). Pass alongside first/last, e.g. "
                        "comments(first:100,limit:120){nodes{body}}; later pages are automatic."
                    ),
                )
    schema.mutation_type = None
    schema.subscription_type = None
    return schema


class GraphQLDocument:
    def __init__(self, context):
        self.context, self.schema = context, public_schema()
        names = set()

        class Names(Visitor):
            def enter_field(self, node, *_):
                names.add(response_key(node))

        visit(context.document, Names())
        self.prefix = "_graphub_"
        while any(name.startswith(self.prefix) for name in names):
            self.prefix += "_"

    def expand(self, selections):
        for node in selections.selections if selections else ():
            skip = get_directive_values(GraphQLSkipDirective, node, self.context.variables)
            include = get_directive_values(GraphQLIncludeDirective, node, self.context.variables)
            if (skip and skip["if"]) or (include and not include["if"]):
                continue
            if isinstance(node, FragmentSpreadNode):
                fragment = self.context.fragments[node.name.value]
                yield InlineFragmentNode(type_condition=fragment.type_condition, selection_set=fragment.selection_set)
            else:
                yield node

    def compile(self, node, parent):
        name = node.name.value
        definition = TypeNameMetaFieldDef if name == "__typename" else parent.fields.get(name)
        if definition is None:
            return None  # A DSL resolver owns this field.
        public = getattr(query_schema().get_type(parent.name), "fields", {}).get(name, definition)
        args = get_argument_values(public, node, self.context.variables)
        limit = args.pop("limit", None)
        if limit is not None and not 1 <= limit <= 2000:
            raise GraphQLError("limit must be between 1 and 2000.", nodes=node)
        target = get_named_type(definition.type)
        connection = {"pageInfo", "nodes"} <= getattr(target, "fields", {}).keys()
        if connection and "first" in definition.args:
            if args.get("first") is not None and args.get("last") is not None:
                raise GraphQLError("Choose first or last.", nodes=node)
            if args.get("first") is None and args.get("last") is None:
                args["first"] = min(limit or 20, 100)
            for bound in ("first", "last"):
                if args.get(bound) is not None and not 1 <= args[bound] <= 100:
                    raise GraphQLError(
                        f"{bound} must be between 1 and 100. To collect 120 items, use "
                        f"{bound}:100,limit:120 as arguments on the same connection.",
                        nodes=node,
                    )
                if limit is not None and args.get(bound) is not None:
                    args[bound] = min(args[bound], limit)
        children = []
        for child in self.expand(node.selection_set):
            if isinstance(child, InlineFragmentNode):
                kind = self.schema.get_type(child.type_condition.name.value) if child.type_condition else target
                rewritten = self.children(child.selection_set, kind) + self.probes(kind, {})
                if rewritten:
                    children.append(changed(child, selection_set=selection(*rewritten), directives=()))
            else:
                compiled = self.compile(child, target)
                if compiled:
                    children.append(compiled)
        if node.selection_set is not None:
            children.extend(self.probes(target, args))
        return changed(
            node,
            arguments=tuple(literal(n, v, definition.args[n].type) for n, v in args.items()),
            selection_set=selection(*children) if node.selection_set is not None else None,
            directives=(),
        )

    def children(self, selections, parent):
        result = []
        for node in self.expand(selections):
            if isinstance(node, InlineFragmentNode):
                target = self.schema.get_type(node.type_condition.name.value) if node.type_condition else parent
                children = self.children(node.selection_set, target) + self.probes(target, {})
                if children:
                    result.append(changed(node, selection_set=selection(*children), directives=()))
            else:
                value = self.compile(node, parent)
                if value:
                    result.append(value)
        return result

    def probes(self, target, args):
        prefix = self.prefix
        probes = [field("__typename", alias=prefix + "type")]
        if target.name == "Node" or any(t.name == "Node" for t in getattr(target, "interfaces", ())):
            probes.append(field("id", alias=prefix + "id"))
        elif is_abstract_type(target) and any(
            any(t.name == "Node" for t in getattr(k, "interfaces", ())) for k in self.schema.get_possible_types(target)
        ):
            probes.append(
                InlineFragmentNode(
                    type_condition=NamedTypeNode(name=NameNode(value="Node")),
                    selection_set=selection(field("id", alias=prefix + "id")),
                ),
            )
        if target.name in {"Issue", "PullRequest"}:
            probes.extend(
                [
                    field("number", alias=prefix + "number"),
                    field("repository", alias=prefix + "repo", children=selection(field("nameWithOwner"))),
                ],
            )
        if target.name == "Repository":
            probes.append(field("nameWithOwner", alias=prefix + "fullName"))
        if target.name == "PullRequestChangedFile":
            probes.append(field("path", alias=prefix + "path"))
        if {"pageInfo", "nodes"} <= getattr(target, "fields", {}).keys():
            probes.append(
                field(
                    "pageInfo",
                    alias=prefix + "page",
                    children=selection(
                        *(field(n) for n in ("hasNextPage", "endCursor", "hasPreviousPage", "startCursor")),
                    ),
                ),
            )
            count = (
                "totalCount"
                if "totalCount" in target.fields
                else str(args.get("type", "ISSUE")).removesuffix("_SEMANTIC").lower() + "Count"
            )
            if count in target.fields:
                probes.append(field(count, alias=prefix + "count"))
        return probes


def signature(node):
    return node.name.value, json.dumps(
        {a.name.value: value_from_ast_untyped(a.value) for a in node.arguments or ()},
        sort_keys=True,
        separators=(",", ":"),
    )


class GraphQLEvidence:
    """Bind GitHub GraphQL selections/identities to the shared evidence index."""

    def __init__(self, schema, index, prefix):
        self.schema, self.index, self.prefix = schema, index, prefix

    def selected(self, node, parent="Query", on=(), conditions=()):
        definition = getattr(self.schema.get_type(parent), "fields", {}).get(node.name.value)
        target = get_named_type(definition.type) if definition else self.schema.get_type(parent)
        children = None if node.selection_set is None else self.selections(node.selection_set, target.name)
        return Field(
            signature(node),
            response_key(node),
            target.name,
            children,
            on,
            atomic="pageInfo" in getattr(target, "fields", {}) and "nodes" in target.fields,
            probe=response_key(node) in {self.prefix + "id", self.prefix + "type"},
            origin=(node, conditions),
        )

    def selections(self, selections, parent, on=(), conditions=()):
        result = []
        for node in selections.selections:
            if isinstance(node, FieldNode):
                result.append(self.selected(node, parent, on, conditions))
            elif isinstance(node, InlineFragmentNode):
                target = self.schema.get_type(node.type_condition.name.value) if node.type_condition else None
                names = (
                    tuple(t.name for t in self.schema.get_possible_types(target))
                    if target and is_abstract_type(target)
                    else (target.name,)
                    if target
                    else on
                )
                if on:
                    names = tuple(n for n in names if n in on)
                    if not names:
                        continue
                result.extend(
                    self.selections(
                        node.selection_set,
                        target.name if target else parent,
                        names,
                        conditions + ((target.name,) if target else ()),
                    ),
                )
        return tuple(result)

    def resolve(self, selected, parent):
        name, encoded = selected.key
        args = json.loads(encoded)
        obj = None
        if parent is self.index.root and name == "repository":
            scope = f"{args.get('owner')}/{args.get('name')}".lower()
            if any(address[0] == scope for address in self.index.addresses):
                obj = Object("Repository", attributes={"scope": scope})
        elif parent is self.index.root and name == "node":
            obj = self.index.addresses.get(("node", args.get("id")))
        elif parent.typename == "Repository" and name in {"issue", "pullRequest"}:
            obj = self.index.addresses.get((parent.attributes.get("scope", ""), name, args.get("number")))
        if obj is not None:
            parent.fields[selected.key] = Value(obj)
            return parent.fields[selected.key]
        if selected.output == self.prefix + "fullName" and parent.typename == "Repository":
            return Value(parent.attributes.get("scope"))
        if selected.output in {self.prefix + "id", self.prefix + "type", self.prefix + "repo", self.prefix + "number"}:
            return Value(
                parent.typename
                if selected.output == self.prefix + "type"
                else parent.identity
                if selected.output == self.prefix + "id"
                else None,
            )
        return None

    def identify(self, kind, data):
        actual, identity = data.get(self.prefix + "type", kind), data.get(self.prefix + "id")
        addresses = [("node", identity)] if identity is not None else []
        repository = (data.get(self.prefix + "repo") or {}).get("nameWithOwner")
        number = data.get(self.prefix + "number")
        if repository and number and actual in {"Issue", "PullRequest"}:
            addresses.append((repository.lower(), "issue" if actual == "Issue" else "pullRequest", number))
        return Identity(actual, identity, tuple(addresses))

    def read(self, node):
        return self.index.read(self.selected(node), resolve=self.resolve)

    def missing(self, node):
        selected = self.selected(node)
        missing = self.index.missing(selected, resolve=self.resolve)
        if missing is selected:
            return node
        return None if missing is None else self.restore(missing)

    def restore(self, selected):
        original, _conditions = selected.origin
        if selected.children is None:
            return original
        children = []
        for child in selected.children:
            node = self.restore(child)
            for condition in reversed(child.origin[1]):
                node = InlineFragmentNode(
                    type_condition=NamedTypeNode(name=NameNode(value=condition)),
                    selection_set=selection(node),
                )
            children.append(node)
        return changed(original, selection_set=selection(*children))

    def write(self, node, data, source, *, at=None):
        entry = self.index.write(
            self.selected(node),
            data,
            Evidence(source, at or "/data/" + response_key(node)),
            self.identify,
        )
        if node.name.value == "repository" and isinstance(entry.data, Object):
            args = json.loads(signature(node)[1])
            entry.data.attributes["scope"] = f"{args.get('owner')}/{args.get('name')}".lower()

    def overlay(self):
        return GraphQLEvidence(self.schema, self.index.overlay(), self.prefix)


@dataclass
class DSLValue:
    data: dict
    kind: str
    scope: dict
    prefix: str
    from_graphql: bool = True


class GraphQLIntValue(int):
    """An integer already serialized by GitHub, whose Int outputs can exceed 32 bits."""


class GitHubDSLExecutionContext(ExecutionContext):
    @staticmethod
    def complete_leaf_value(return_type, result):
        if return_type.name == "Int" and isinstance(result, GraphQLIntValue):
            return int(result)
        return ExecutionContext.complete_leaf_value(return_type, result)


class GitHubDSLAdapter(DSLExtensions):
    """Compile DSL selections into GitHub GraphQL batches and resolve REST-backed DSL fields."""

    language = DSL_LANGUAGE
    schema = staticmethod(query_schema)
    execution_context_class = GitHubDSLExecutionContext

    @staticmethod
    def resolve_type(value, _info, _abstract):
        return value.kind if isinstance(value, DSLValue) else value.get("__typename")

    def compiler(self, context):
        if "compiler" not in context.state:
            context.state["compiler"] = GraphQLDocument(context)
        return context.state["compiler"]

    def wrap(self, data, type_, prefix, *, scope=None, from_graphql=True):
        if isinstance(data, list):
            return [
                self.wrap(
                    item,
                    type_,
                    prefix,
                    scope=scope,
                    from_graphql=from_graphql,
                )
                for item in data
            ]
        target = get_named_type(type_)
        if is_leaf_type(target):
            if from_graphql and target.name == "Int" and type(data) is int:
                return GraphQLIntValue(data)
            return data
        if not isinstance(data, dict):
            return data
        kind = data.get(prefix + "type", get_named_type(type_).name)
        scoped = dict(scope or {})
        repo = data.get(prefix + "fullName") or (data.get(prefix + "repo") or {}).get("nameWithOwner")
        if repo:
            scoped["repo"] = repo
        if kind in {"Issue", "PullRequest"} and prefix + "number" in data:
            scoped["number"] = data[prefix + "number"]
        return DSLValue(data, kind, scoped, prefix, from_graphql)

    async def load(self, context, node, path):
        future = asyncio.get_running_loop().create_future()
        queue = context.state.setdefault("graphql_queue", [])
        queue.append((node, future, path))
        if not context.state.get("flushing"):
            context.state["flushing"] = True
            context.task(self.flush(context))
        return await future

    async def flush(self, context):
        await asyncio.sleep(0)  # Collect sibling resolvers in the same DSL execution wave.
        queue = context.state.pop("graphql_queue", [])
        context.state["flushing"] = False
        try:
            compiler = self.compiler(context)
            cache = GraphQLEvidence(public_schema(), self.evidence, compiler.prefix)

            # Cache identity ignores response aliases, but retains field arguments and type conditions.
            class Unalias(Visitor):
                def enter_field(self, node, *_):
                    return changed(node, alias=None)

            identity = print_ast(visit(parse(query_document(*(entry[0] for entry in queue))), Unalias()))
            async with self.lock("graph:" + identity):
                pending = [cache.missing(node) for node, _, _ in queue]
                requested = [node for node in pending if node is not None]
                result, body = None, {"data": {}}
                if requested:
                    result = await context.read({"query": query_document(*requested)}, partial=True)
                    if not result.get("result_id"):
                        raise GraphQLError(result.get("error", {}).get("message", "GraphQL request failed."))
                    body = self.content(result).data
                    if not isinstance(body, dict):
                        raise GraphQLError("Expected a GraphQL response object.")
                    if result.get("error") and not body.get("errors"):
                        raise GraphQLError(
                            result["error"]["message"],
                            extensions={**result["error"], "result_id": result["result_id"]},
                        )
                for (node, future, path), missing in zip(queue, pending, strict=True):
                    if future.cancelled():
                        continue
                    errors = [
                        e for e in body.get("errors", []) if not e.get("path") or e["path"][0] == response_key(node)
                    ]
                    target = cache.overlay() if errors else cache
                    raw = (body.get("data") or {}).get(response_key(node))
                    if missing is not None and response_key(node) in (body.get("data") or {}):
                        target.write(missing, raw, result["result_id"])
                    value, complete, sources = target.read(node)
                    if missing is not None and not complete and not errors:
                        if print_ast(missing) != print_ast(node):
                            refill = await context.read({"query": query_document(node)}, partial=True)
                            full = self.content(refill).data
                            errors = full.get("errors", [])
                            value = (full.get("data") or {}).get(response_key(node))
                            sources = {refill["result_id"]}
                            if not errors:
                                cache.write(node, value, refill["result_id"])
                        else:
                            value = raw
                    if missing is None:
                        context.reused = True
                    context.sources.extend(sources)
                    mapped = [self.graphql_error(error, path, response_key(node)) for error in errors]
                    context.errors.extend(mapped)
                    if value is None and errors:
                        future.set_exception(mapped[0])
                    else:
                        future.set_result(value)
        except asyncio.CancelledError:
            for _, future, _ in queue:
                future.cancel()
            raise
        except Exception as exc:
            for _, future, _ in queue:
                if not future.done():
                    future.set_exception(exc)

    @staticmethod
    def graphql_error(error, path, root):
        location = error.get("path") or [root]
        return GraphQLError(
            error.get("message", "GitHub GraphQL read failed."),
            path=[*path, *location[1:]],
            extensions={**(error.get("extensions") or {}), **({"type": error["type"]} if "type" in error else {})},
        )

    def node_for(self, context, info, args=None):
        node = info.field_nodes[0]
        if node.selection_set:
            node = changed(
                node,
                selection_set=selection(*(child for n in info.field_nodes for child in n.selection_set.selections)),
            )
        if args is not None:
            definition = info.parent_type.fields[info.field_name]
            node = changed(node, arguments=tuple(literal(n, v, definition.args[n].type) for n, v in args.items()))
        return node

    def focus(self, context, source, info, args=None):
        compiler = self.compiler(context)
        child = compiler.compile(self.node_for(context, info, args), public_schema().get_type(info.parent_type.name))
        alias = "q" + str(context.state.setdefault("sequence", 0))
        context.state["sequence"] += 1
        if source is None:
            return changed(child, alias=NameNode(value=alias)), []
        identity = source.data.get(source.prefix + "id")
        if identity is None:
            raise GraphQLError("This GitHub object has no addressable node ID for an additional read.")
        parent = field(
            "node",
            alias=alias,
            arguments=(literal("id", identity, public_schema().query_type.fields["node"].args["id"].type),),
            children=selection(
                InlineFragmentNode(
                    type_condition=NamedTypeNode(name=NameNode(value=source.kind)),
                    selection_set=selection(child, *compiler.probes(public_schema().get_type(source.kind), {})),
                ),
                *compiler.probes(public_schema().get_type("Node"), {}),
            ),
        )
        return parent, [response_key(child)]

    async def graphql(self, context, info, source=None, args=None):
        node, extract = self.focus(context, source, info, args)
        data = await self.load(
            context,
            node,
            info.path.as_list()[: -len(extract)] if extract else info.path.as_list(),
        )
        for part in extract:
            data = data.get(part) if isinstance(data, dict) else None
        scope = source.scope if source else {}
        if info.field_name == "repository":
            values = (
                args
                if args is not None
                else get_argument_values(
                    info.parent_type.fields[info.field_name],
                    info.field_nodes[0],
                    context.variables,
                )
            )
            scope = {"repo": values["owner"] + "/" + values["name"]}
        return self.wrap(
            data,
            info.return_type,
            self.compiler(context).prefix,
            scope=scope,
        )

    async def collect(self, context, source, info, args, value):
        if not isinstance(value, DSLValue) or value.prefix + "page" not in value.data:
            return value
        prefix, data = value.prefix, value.data
        page = data[prefix + "page"] or {}
        record = {
            "path": info.path.as_list(),
            "pageInfo": dict(page),
            "count": data.get(prefix + "count"),
            "complete": not page.get("hasNextPage", True) and not page.get("hasPreviousPage", True),
        }
        context.pages.append(record)
        limit = args.get("limit")
        if not limit:
            return value
        selected = collect_fields(
            info.schema,
            context.fragments,
            context.variables,
            get_named_type(info.return_type),
            self.node_for(context, info).selection_set,
        )
        arrays = [alias for alias, nodes in selected.items() if nodes[0].name.value in {"nodes", "edges"}]
        if not arrays:
            return value
        backwards = args.get("last") is not None
        direction, cursor_key = ("hasPreviousPage", "startCursor") if backwards else ("hasNextPage", "endCursor")
        count = len(data.get(arrays[0]) or [])
        seen = set()
        while count < limit and page.get(direction):
            cursor = page.get(cursor_key)
            if not cursor or cursor in seen:
                record["error"] = {"code": "pagination_stalled", "message": "Pagination did not advance."}
                break
            seen.add(cursor)
            options = {k: v for k, v in args.items() if k not in {"first", "last", "before", "after", "limit"}}
            bound = "last" if backwards else "first"
            options.update(
                {bound: min(100, limit - count), "before" if backwards else "after": cursor},
            )
            error_start = len(context.errors)
            try:
                following = await self.graphql(context, info, source, options)
            except GraphQLError as exc:
                record["error"] = exc.formatted
                break
            if not isinstance(following, DSLValue):
                record["error"] = {"message": "Continuation returned no connection."}
                break
            incoming = following.data
            connection_path = info.path.as_list()
            extra_count = len(incoming.get(arrays[0]) or [])
            for index, error in enumerate(context.errors):
                path = error.path or []
                at = len(connection_path)
                if (
                    path[:at] == connection_path
                    and len(path) > at + 1
                    and path[at] in arrays
                    and isinstance(path[at + 1], int)
                    and ((backwards and index < error_start) or (not backwards and index >= error_start))
                ):
                    path[at + 1] += extra_count if backwards else count
            for alias in arrays:
                extra = incoming.get(alias) or []
                data[alias] = extra + (data.get(alias) or []) if backwards else (data.get(alias) or []) + extra
            count = len(data.get(arrays[0]) or [])
            page = incoming[prefix + "page"] or {}
            previous = data[prefix + "page"]
            merged_page = {
                **previous,
                **{
                    k: v
                    for k, v in page.items()
                    if k in ({"hasPreviousPage", "startCursor"} if backwards else {"hasNextPage", "endCursor"})
                },
            }
            data[prefix + "page"] = merged_page
            # Keep every explicitly selected pageInfo alias in the same connection shape.
            for alias, nodes in selected.items():
                if nodes[0].name.value == "pageInfo":
                    children = selection(*(child for node in nodes for child in node.selection_set.selections))
                    page_fields = collect_fields(
                        info.schema,
                        context.fragments,
                        context.variables,
                        info.schema.get_type("PageInfo"),
                        children,
                    )
                    for output, fields in page_fields.items():
                        name = fields[0].name.value
                        if name in merged_page:
                            data[alias][output] = merged_page[name]
            if incoming.get(prefix + "count") != record["count"]:
                record.setdefault("observed_counts", [record["count"]]).append(incoming.get(prefix + "count"))
        record.update(
            returned=count,
            pageInfo=data[prefix + "page"],
            complete=not record.get("error")
            and not record.get("observed_counts")
            and not data[prefix + "page"].get("hasNextPage", True)
            and not data[prefix + "page"].get("hasPreviousPage", True),
        )
        if record.get("observed_counts"):
            record["observed_counts"] = list(dict.fromkeys(record["observed_counts"]))
            record["count"] = None
        return value

    async def rest(self, context, source, info, args):
        name = info.field_name
        if name == "file":
            repo = source.scope.get("repo")
            path = f"/repos/{repo}/contents/{quote(args['path'], safe='/')}"
            result = await context.read(
                {"path": path, "params": {"ref": args["ref"]} if args.get("ref") else {}},
                info.path.as_list(),
            )
            content = self.content(result)
            data = content.data
            if isinstance(data, list):
                data = {"path": args["path"], "type": "dir", "entries": data}
            elif content.is_file(data):
                data = {**data, "text": content.file_bytes.decode("utf-8")}
            return self.wrap(data, info.return_type, self.compiler(context).prefix, from_graphql=False)
        if name == "patch":
            repo, number = source.scope.get("repo"), source.scope.get("number")
            result = await context.read(
                {
                    "path": f"/repos/{repo}/pulls/{number}/files",
                    "params": {"per_page": 100},
                    "paginate": {"items_pointer": "", "max_pages": 20, "max_items": 2000},
                },
                info.path.as_list(),
            )
            values = self.content(result).data
            if isinstance(values, dict):
                values = values["items"]
            path = source.data.get(source.prefix + "path", source.data.get("path"))
            match = next((v for v in values if v.get("filename") == path), None)
            if match is None:
                raise GraphQLError(
                    f"No REST changed-file evidence for {path}.",
                    extensions={"result_id": result["result_id"]},
                )
            return match.get("patch")
        route = "code" if name == "searchCode" else "commits"
        request = {
            "path": "/search/" + route,
            "params": {
                "q": args["query"],
                "per_page": args["first"],
                "page": args["page"],
                **{k: args[k] for k in ("sort", "order") if args.get(k)},
            },
        }
        if args.get("limit"):
            request["paginate"] = {"max_items": args["limit"], "max_pages": 20, "items_pointer": "/items"}
        result = await context.read(request, info.path.as_list())
        data = self.content(result).data
        if isinstance(data, list):
            collection = result.get("collection", {})
            items, total = data, collection.get("total_count", result.get("total_count"))
        else:
            items, total = data["items"], data.get("total_count", result.get("total_count"))
        prefix = self.compiler(context).prefix
        normalized = []
        for item in items:
            repo = item.get("repository", {})
            repository = {
                **repo,
                "id": repo.get("node_id"),
                prefix + "id": repo.get("node_id"),
                prefix + "type": "Repository",
                "nameWithOwner": repo.get("full_name"),
                prefix + "fullName": repo.get("full_name"),
                "url": repo.get("html_url"),
            }
            if route == "code":
                normalized.append(
                    {
                        "name": item.get("name"),
                        "path": item["path"],
                        "url": item.get("html_url"),
                        "repository": repository,
                        "textMatches": item.get("text_matches"),
                    },
                )
            else:
                normalized.append(
                    {
                        prefix + "type": "Commit",
                        prefix + "id": item.get("node_id"),
                        "id": item.get("node_id"),
                        "oid": item["sha"],
                        "url": item.get("html_url"),
                        "message": item.get("commit", {}).get("message"),
                        "repository": repository,
                    },
                )
        context.pages.append(
            {
                "path": info.path.as_list(),
                **{k: result[k] for k in ("collection", "pagination", "incomplete_results") if k in result},
            },
        )
        return self.wrap(
            {"totalCount": total, "incomplete": result.get("incomplete_results", False), "nodes": normalized},
            info.return_type,
            prefix,
            from_graphql=False,
        )

    async def resolve(self, source, info, **args):
        context = info.context
        if info.parent_type is info.schema.query_type:
            if info.field_name in dsl_fields():
                return await self.resolve_dsl_field(context, info, args)
            if info.field_name in {"searchCode", "searchCommits"}:
                return await self.rest(context, source, info, args)
            return await self.collect(context, None, info, args, await self.graphql(context, info, args=args))
        if not isinstance(source, DSLValue):
            return source.get(info.field_name) if isinstance(source, dict) else None
        if (info.parent_type.name, info.field_name) in {("Repository", "file"), ("PullRequestChangedFile", "patch")}:
            return await self.rest(context, source, info, args)
        alias = info.path.key if source.from_graphql else info.field_name
        if (
            alias not in source.data
            and not source.from_graphql
            and source.data.get(source.prefix + "id")
            and info.parent_type.name in public_schema().type_map
        ):
            return await self.collect(context, source, info, args, await self.graphql(context, info, source, args))
        value = self.wrap(
            source.data.get(alias),
            info.return_type,
            source.prefix,
            scope=source.scope,
            from_graphql=source.from_graphql,
        )
        return await self.collect(context, source, info, args, value)
