"""Test GitHub client routing, quotas, and GraphQL traversal."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from gh_puller.github import (
    GitHubAPI,
    GitHubAPIError,
    RateQuota,
)
from tests.github._puller_support import (
    _T0,
    Clock,
    _closing_issue,
    _graphql_commit,
    _graphql_issue_comment,
    _graphql_pull,
    _graphql_reaction,
    _graphql_review,
    _graphql_review_comment,
    _quota_headers,
)


@pytest.mark.asyncio
async def test_rate_limit_waits_asynchronously(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock(_T0)
    progress = []
    responses = [
        (
            403,
            {
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(_T0.timestamp()) + 2),
                "x-ratelimit-resource": "core",
            },
            {"message": "rate limit exceeded"},
        ),
        (429, {"retry-after": "4"}, {"message": "secondary rate limit"}),
        (200, {}, {"ok": True}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        status, headers, body = responses.pop(0)
        return httpx.Response(status, headers=headers, json=body, request=request)

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(
        client=client,
        sleep=clock.sleep,
        now=clock,
        progress=progress.append,
    )
    try:
        assert await api.get_json("/resource") == {"ok": True}
    finally:
        await client.aclose()

    assert clock.sleeps == [3, 4]
    assert api.request_count == 3
    waits = [event for event in progress if event.wait_seconds is not None]
    assert [(event.detail, event.wait_seconds) for event in waits] == [
        ("primary_rate_limit", 3),
        ("secondary_rate_limit", 4),
    ]
    assert waits[0].quotas == (RateQuota("core", 5000, 0, _T0 + timedelta(seconds=2)),)
    assert "primary_rate_limit: resource=core status=403" in caplog.text
    assert "wait_source=reset wait=3.0s" in caplog.text
    assert "secondary_rate_limit: resource=core status=429" in caplog.text
    assert "wait_source=retry_after wait=4.0s" in caplog.text


@pytest.mark.asyncio
async def test_primary_limit_uses_the_conservative_jittered_reset() -> None:
    clock = Clock(_T0)
    progress = []
    responses = [
        (
            200,
            {
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "10",
                "x-ratelimit-reset": str(int(_T0.timestamp()) + 4),
                "x-ratelimit-resource": "core",
            },
        ),
        (
            403,
            {
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(_T0.timestamp()) + 2),
                "x-ratelimit-resource": "core",
            },
        ),
        (200, {}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        status, headers = responses.pop(0)
        return httpx.Response(status, headers=headers, json={"ok": True}, request=request)

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(client=client, sleep=clock.sleep, now=clock, progress=progress.append)
    try:
        await api.get_json("/quota-sample")
        await api.get_json("/limited")
    finally:
        await client.aclose()

    assert clock.sleeps == [5]
    assert progress[-1].quotas == (
        RateQuota("core", 5_000, 0, _T0 + timedelta(seconds=4)),
    )


@pytest.mark.asyncio
async def test_transient_failures_retry_in_place_until_the_page_succeeds() -> None:
    clock = Clock(_T0)
    progress = []
    observed_pages: list[int] = []
    requested: list[str] = []
    page_two_attempt = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal page_two_attempt
        requested.append(str(request.url))
        if request.url.params.get("page") is None:
            return httpx.Response(
                200,
                headers={"link": '<https://api.github.test/items?page=2>; rel="next"'},
                json=[{"id": 1}],
                request=request,
            )
        page_two_attempt += 1
        if page_two_attempt == 1:
            raise httpx.ConnectError("connection reset", request=request)
        if page_two_attempt <= 7:
            return httpx.Response(500, json={"message": "temporary"}, request=request)
        return httpx.Response(200, json=[{"id": 2}], request=request)

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client, sleep=clock.sleep, now=clock, progress=progress.append)
    try:
        items = await api.paginate("/items", page_observer=observed_pages.append)
    finally:
        await client.aclose()

    assert items == [{"id": 1}, {"id": 2}]
    assert observed_pages == [1, 1]
    assert sum("page=2" not in url for url in requested) == 1
    assert clock.sleeps == [1, 2, 4, 8, 16, 30, 30]
    assert api.request_count == 9
    waits = [event for event in progress if event.wait_seconds is not None]
    assert [event.wait_seconds for event in waits] == clock.sleeps
    assert {event.detail for event in waits} == {"transient_retry"}


@pytest.mark.asyncio
async def test_transient_retry_remains_cancellable() -> None:
    async def cancel(_: float) -> None:
        raise asyncio.CancelledError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "temporary"}, request=request)

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client, sleep=cancel)
    try:
        with pytest.raises(asyncio.CancelledError):
            await api.get_json("/resource")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_non_retryable_http_error_fails_immediately() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "invalid"}, request=request)

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client)
    try:
        with pytest.raises(GitHubAPIError, match="GitHub returned 422"):
            await api.get_json("/resource")
    finally:
        await client.aclose()

    assert api.request_count == 1


@pytest.mark.asyncio
async def test_graphql_repository_count_is_exact_and_authenticated() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "issues": {"totalCount": 17},
                        "pullRequests": {"totalCount": 23},
                    },
                },
            },
            request=request,
        )

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    credential = str(id(client))
    api = GitHubAPI(token=credential, client=client, graphql_url="/graphql")
    try:
        assert await api.repository_item_count("acme", "widgets") == 40
    finally:
        await client.aclose()

    body = json.loads(seen[0].content)
    assert seen[0].method == "POST"
    assert str(seen[0].url) == "https://api.github.test/graphql"
    assert seen[0].headers["authorization"] == f"Bearer {credential}"
    assert body["variables"] == {"owner": "acme", "repo": "widgets"}
    assert "issues(states: [OPEN, CLOSED])" in body["query"]
    assert "pullRequests(states: [OPEN, CLOSED, MERGED])" in body["query"]


@pytest.mark.asyncio
async def test_pull_detail_uses_graphql_when_its_capacity_is_higher() -> None:
    seen: list[str] = []
    raw = _graphql_pull()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path != "/graphql":
            return httpx.Response(
                200,
                headers=_quota_headers("core", 500),
                json={"seed": True},
                request=request,
            )
        query = json.loads(request.content)["query"]
        body = (
            {
                "data": {
                    "repository": {
                        "issues": {"totalCount": 1},
                        "pullRequests": {"totalCount": 2},
                    },
                },
            }
            if "RepositoryItemCount" in query
            else {"data": {"repository": {"pullRequest": raw}}}
        )
        return httpx.Response(
            200,
            headers=_quota_headers("graphql", 4_900),
            json=body,
            request=request,
        )

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(
        token=str(id(client)),
        client=client,
        graphql_url="/graphql",
        now=lambda: _T0,
    )
    try:
        await api.get_json("/seed-core")
        await api.repository_item_count("acme", "widgets")
        result = await api.pull_request(
            "acme",
            "widgets",
            7,
            previous=None,
            cache=None,
        )
    finally:
        await client.aclose()

    assert seen == ["/seed-core", "/graphql", "/graphql"]
    assert result.source == "graphql"
    assert result.raw == raw
    assert result.cache is None
    assert result.value["id"] == 700
    assert result.value["node_id"] == "pull-7"
    assert result.value["state"] == "closed"
    assert result.value["merged"] is True
    assert result.value["mergeable"] is None
    assert result.value["mergeable_state"] == "clean"
    assert result.value["base"]["sha"] == "a" * 40
    assert result.value["head"]["sha"] == "b" * 40
    assert result.value["merge_commit_sha"] == "c" * 40
    assert result.value["user"]["login"] == "alice"
    assert result.value["commits"] == 3


@pytest.mark.asyncio
async def test_pull_detail_uses_rest_when_its_capacity_is_higher() -> None:
    seen: list[str] = []
    rest = {"id": 700, "number": 7, "unknown": {"kept": True}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/graphql":
            return httpx.Response(
                200,
                headers=_quota_headers("graphql", 400),
                json={
                    "data": {
                        "repository": {
                            "issues": {"totalCount": 1},
                            "pullRequests": {"totalCount": 2},
                        },
                    },
                },
                request=request,
            )
        body = {"seed": True} if request.url.path == "/seed-core" else rest
        return httpx.Response(
            200,
            headers=_quota_headers("core", 4_900),
            json=body,
            request=request,
        )

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(
        token=str(id(client)),
        client=client,
        graphql_url="/graphql",
        now=lambda: _T0,
    )
    try:
        await api.repository_item_count("acme", "widgets")
        await api.get_json("/seed-core")
        result = await api.pull_request(
            "acme",
            "widgets",
            7,
            previous=None,
            cache=None,
        )
    finally:
        await client.aclose()

    assert seen == ["/graphql", "/seed-core", "/repos/acme/widgets/pulls/7"]
    assert result.source == "rest"
    assert result.value is result.raw
    assert result.value == rest


@pytest.mark.asyncio
async def test_pull_detail_falls_back_when_preferred_graphql_quota_exhausts() -> None:
    clock = Clock(_T0)
    seen: list[str] = []
    rest = {"id": 700, "number": 7}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/seed-core":
            return httpx.Response(
                200,
                headers=_quota_headers("core", 500),
                json={"seed": True},
                request=request,
            )
        if request.url.path == "/graphql":
            return httpx.Response(
                200,
                headers=_quota_headers("graphql", 0),
                json={"errors": [{"message": "API rate limit exceeded"}]},
                request=request,
            )
        return httpx.Response(
            200,
            headers=_quota_headers("core", 499),
            json=rest,
            request=request,
        )

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(
        token=str(id(client)),
        client=client,
        graphql_url="/graphql",
        sleep=clock.sleep,
        now=clock,
    )
    try:
        await api.get_json("/seed-core")
        result = await api.pull_request(
            "acme",
            "widgets",
            7,
            previous=None,
            cache=None,
        )
    finally:
        await client.aclose()

    assert seen == ["/seed-core", "/graphql", "/repos/acme/widgets/pulls/7"]
    assert result.source == "rest"
    assert result.value == rest
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_pull_detail_falls_back_after_graphql_operation_error() -> None:
    seen: list[str] = []
    rest = {"id": 700, "number": 7}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/seed-core":
            return httpx.Response(
                200,
                headers=_quota_headers("core", 500),
                json={"seed": True},
                request=request,
            )
        if request.url.path == "/graphql":
            return httpx.Response(
                200,
                headers=_quota_headers("graphql", 4_900),
                json={"errors": [{"message": "field is temporarily unavailable"}]},
                request=request,
            )
        return httpx.Response(200, json=rest, request=request)

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(
        token=str(id(client)),
        client=client,
        graphql_url="/graphql",
        now=lambda: _T0,
    )
    try:
        await api.get_json("/seed-core")
        result = await api.pull_request(
            "acme",
            "widgets",
            7,
            previous=None,
            cache=None,
        )
    finally:
        await client.aclose()

    assert seen == ["/seed-core", "/graphql", "/repos/acme/widgets/pulls/7"]
    assert result.source == "rest"


@pytest.mark.asyncio
async def test_pull_detail_falls_back_after_rest_operation_error() -> None:
    seen: list[str] = []
    raw = _graphql_pull()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/graphql":
            query = json.loads(request.content)["query"]
            body = (
                {
                    "data": {
                        "repository": {
                            "issues": {"totalCount": 1},
                            "pullRequests": {"totalCount": 2},
                        },
                    },
                }
                if "RepositoryItemCount" in query
                else {"data": {"repository": {"pullRequest": raw}}}
            )
            return httpx.Response(
                200,
                headers=_quota_headers("graphql", 400),
                json=body,
                request=request,
            )
        if request.url.path == "/seed-core":
            return httpx.Response(
                200,
                headers=_quota_headers("core", 4_900),
                json={"seed": True},
                request=request,
            )
        return httpx.Response(422, json={"message": "invalid"}, request=request)

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(
        token=str(id(client)),
        client=client,
        graphql_url="/graphql",
        now=lambda: _T0,
    )
    try:
        await api.repository_item_count("acme", "widgets")
        await api.get_json("/seed-core")
        result = await api.pull_request(
            "acme",
            "widgets",
            7,
            previous=None,
            cache=None,
        )
    finally:
        await client.aclose()

    assert seen == [
        "/graphql",
        "/seed-core",
        "/repos/acme/widgets/pulls/7",
        "/graphql",
    ]
    assert result.source == "graphql"


@pytest.mark.asyncio
async def test_dual_operation_waits_for_the_earlier_primary_reset() -> None:
    clock = Clock(_T0)
    seen: list[str] = []
    graphql_calls = 0

    def headers(resource: str, reset_seconds: int, remaining: int = 0) -> dict[str, str]:
        return {
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": str(remaining),
            "x-ratelimit-reset": str(int(_T0.timestamp()) + reset_seconds),
            "x-ratelimit-resource": resource,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal graphql_calls
        seen.append(request.url.path)
        if request.url.path == "/graphql":
            graphql_calls += 1
            query = json.loads(request.content)["query"]
            if "RepositoryItemCount" in query:
                body = {
                    "data": {
                        "repository": {
                            "issues": {"totalCount": 1},
                            "pullRequests": {"totalCount": 2},
                        },
                    },
                }
                remaining = 0
            else:
                body = {"data": {"repository": {"pullRequest": _graphql_pull()}}}
                remaining = 4_999
            return httpx.Response(
                200,
                headers=headers("graphql", 3, remaining),
                json=body,
                request=request,
            )
        return httpx.Response(
            200,
            headers=headers("core", 10),
            json={"seed": True},
            request=request,
        )

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(
        token=str(id(client)),
        client=client,
        graphql_url="/graphql",
        sleep=clock.sleep,
        now=clock,
    )
    try:
        await api.repository_item_count("acme", "widgets")
        await api.get_json("/seed-core")
        result = await api.pull_request(
            "acme",
            "widgets",
            7,
            previous=None,
            cache=None,
        )
    finally:
        await client.aclose()

    assert result.source == "graphql"
    assert graphql_calls == 2
    assert seen == ["/graphql", "/seed-core", "/graphql"]
    assert clock.sleeps == [4]


@pytest.mark.asyncio
async def test_pull_reviews_graphql_paginates_and_preserves_source() -> None:
    seen: list[dict[str, Any]] = []
    reviews = [_graphql_review(71), _graphql_review(72)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/graphql":
            return httpx.Response(
                200,
                headers=_quota_headers("core", 500),
                json={"seed": True},
                request=request,
            )
        body = json.loads(request.content)
        seen.append(body)
        cursor = body["variables"]["cursor"]
        nodes = reviews[:1] if cursor is None else reviews[1:]
        connection = {
            "totalCount": 2,
            "nodes": nodes,
            "pageInfo": {
                "hasNextPage": cursor is None,
                "endCursor": "next-review" if cursor is None else None,
            },
        }
        return httpx.Response(
            200,
            headers=_quota_headers("graphql", 4_900 - len(seen)),
            json={
                "data": {
                    "repository": {
                        "pullRequest": {"number": 7, "reviews": connection},
                    },
                },
            },
            request=request,
        )

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(
        token=str(id(client)),
        client=client,
        graphql_url="/graphql",
        now=lambda: _T0,
    )
    try:
        await api.get_json("/seed-core")
        result = await api.pull_reviews(
            "acme",
            "widgets",
            7,
            previous=None,
            cache=None,
        )
    finally:
        await client.aclose()

    assert [body["variables"]["cursor"] for body in seen] == [None, "next-review"]
    assert result.source == "graphql"
    assert result.raw == reviews
    assert [review["id"] for review in result.value] == [71, 72]
    assert result.value[0]["user"]["login"] == "reviewer-71"
    assert result.value[0]["commit_id"] == f"{71:040x}"
    assert result.value[0]["state"] == "APPROVED"


@pytest.mark.asyncio
async def test_pull_commits_graphql_paginates_without_rest_cap() -> None:
    seen: list[dict[str, Any]] = []
    paths: list[str] = []
    commits = [_graphql_commit(number) for number in range(1, 252)]

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path != "/graphql":
            return httpx.Response(
                200,
                headers=_quota_headers("core", 500),
                json={"seed": True},
                request=request,
            )
        body = json.loads(request.content)
        seen.append(body)
        cursor = body["variables"]["cursor"]
        offset = 0 if cursor is None else int(cursor.removeprefix("commit-"))
        nodes = commits[offset : offset + 100]
        next_offset = offset + len(nodes)
        has_next = next_offset < len(commits)
        connection = {
            "totalCount": len(commits),
            "nodes": nodes,
            "pageInfo": {
                "hasNextPage": has_next,
                "endCursor": f"commit-{next_offset}" if has_next else None,
            },
        }
        return httpx.Response(
            200,
            headers=_quota_headers("graphql", 4_900 - len(seen)),
            json={
                "data": {
                    "repository": {
                        "pullRequest": {"number": 7, "commits": connection},
                    },
                },
            },
            request=request,
        )

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(
        token=str(id(client)),
        client=client,
        graphql_url="/graphql",
        now=lambda: _T0,
    )
    try:
        await api.get_json("/seed-core")
        result = await api.pull_commits(
            "acme",
            "widgets",
            7,
            expected=251,
            base="a" * 40,
            head="b" * 40,
            previous=None,
            cache=None,
        )
    finally:
        await client.aclose()

    assert [body["variables"]["cursor"] for body in seen] == [
        None,
        "commit-100",
        "commit-200",
    ]
    assert result.source == "graphql"
    assert result.raw == commits
    assert len(result.value) == 251
    assert [commit["sha"] for commit in result.value[:2]] == [f"{1:040x}", f"{2:040x}"]
    assert result.value[0]["commit"]["message"] == "commit 1"
    assert result.value[0]["author"]["login"] == "committer-1"
    assert result.value[0]["parents"] == [
        {
            "sha": f"{0:040x}",
            "url": f"https://api.github.test/repos/acme/widgets/commits/{0:040x}",
        },
    ]
    assert paths == ["/seed-core", "/graphql", "/graphql", "/graphql"]


@pytest.mark.asyncio
async def test_pull_review_comments_graphql_closes_both_pagination_levels() -> None:
    seen: list[dict[str, Any]] = []
    comments = [_graphql_review_comment(number) for number in (81, 82, 83)]

    def connection(nodes: list[dict[str, Any]], total: int, cursor: str | None) -> dict[str, Any]:
        return {
            "totalCount": total,
            "nodes": nodes,
            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/graphql":
            return httpx.Response(
                200,
                headers=_quota_headers("core", 500),
                json={"seed": True},
                request=request,
            )
        body = json.loads(request.content)
        seen.append(body)
        variables = body["variables"]
        if "ReviewThreadComments" in body["query"]:
            data = {
                "node": {
                    "id": "thread-1",
                    "comments": connection([comments[1]], 2, None),
                },
            }
        elif variables["cursor"] is None:
            data = {
                "repository": {
                    "pullRequest": {
                        "number": 7,
                        "reviewThreads": connection(
                            [
                                {
                                    "id": "thread-1",
                                    "comments": connection(
                                        [comments[0]],
                                        2,
                                        "thread-1-comments",
                                    ),
                                },
                            ],
                            2,
                            "next-thread",
                        ),
                    },
                },
            }
        else:
            data = {
                "repository": {
                    "pullRequest": {
                        "number": 7,
                        "reviewThreads": connection(
                            [
                                {
                                    "id": "thread-2",
                                    "comments": connection([comments[2]], 1, None),
                                },
                            ],
                            2,
                            None,
                        ),
                    },
                },
            }
        return httpx.Response(
            200,
            headers=_quota_headers("graphql", 4_900 - len(seen)),
            json={"data": data},
            request=request,
        )

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(
        token=str(id(client)),
        client=client,
        graphql_url="/graphql",
        now=lambda: _T0,
    )
    try:
        await api.get_json("/seed-core")
        result = await api.pull_review_comments(
            "acme",
            "widgets",
            7,
            previous=None,
            cache=None,
        )
    finally:
        await client.aclose()

    assert [body["variables"] for body in seen] == [
        {"owner": "acme", "repo": "widgets", "number": 7, "cursor": None},
        {"id": "thread-1", "cursor": "thread-1-comments"},
        {"owner": "acme", "repo": "widgets", "number": 7, "cursor": "next-thread"},
    ]
    assert result.source == "graphql"
    assert result.raw == comments
    assert [comment["id"] for comment in result.value] == [81, 82, 83]
    assert result.value[0]["pull_request_review_id"] == 71
    assert result.value[0]["path"] == "src/example.py"
    assert result.value[0]["reactions"]["total_count"] == 1


@pytest.mark.asyncio
async def test_issue_comments_graphql_paginates_for_issue_or_pull() -> None:
    seen: list[dict[str, Any]] = []
    comments = [_graphql_issue_comment(81), _graphql_issue_comment(82)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/graphql":
            return httpx.Response(
                200,
                headers=_quota_headers("core", 500),
                json={"seed": True},
                request=request,
            )
        body = json.loads(request.content)
        seen.append(body)
        cursor = body["variables"]["cursor"]
        nodes = comments[:1] if cursor is None else comments[1:]
        connection = {
            "totalCount": 2,
            "nodes": nodes,
            "pageInfo": {
                "hasNextPage": cursor is None,
                "endCursor": "next-comment" if cursor is None else None,
            },
        }
        return httpx.Response(
            200,
            headers=_quota_headers("graphql", 4_900 - len(seen)),
            json={
                "data": {
                    "repository": {
                        "issueOrPullRequest": {
                            "__typename": "PullRequest",
                            "number": 7,
                            "comments": connection,
                        },
                    },
                },
            },
            request=request,
        )

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(
        token=str(id(client)),
        client=client,
        graphql_url="/graphql",
        now=lambda: _T0,
    )
    try:
        await api.get_json("/seed-core")
        result = await api.issue_comments(
            "acme",
            "widgets",
            7,
            previous=None,
            cache=None,
        )
    finally:
        await client.aclose()

    assert [body["variables"]["cursor"] for body in seen] == [None, "next-comment"]
    assert result.source == "graphql"
    assert result.raw == comments
    assert [comment["id"] for comment in result.value] == [81, 82]
    assert result.value[0]["body_html"] == "<p>comment 81</p>"
    assert result.value[0]["user"]["login"] == "commenter-81"
    assert result.value[0]["reactions"]["total_count"] == 1


@pytest.mark.asyncio
async def test_reactions_graphql_paginates_and_maps_content() -> None:
    seen: list[dict[str, Any]] = []
    reactions = [
        _graphql_reaction(91, "THUMBS_UP"),
        _graphql_reaction(92, "ROCKET"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/graphql":
            return httpx.Response(
                200,
                headers=_quota_headers("core", 500),
                json={"seed": True},
                request=request,
            )
        body = json.loads(request.content)
        seen.append(body)
        cursor = body["variables"]["cursor"]
        nodes = reactions[:1] if cursor is None else reactions[1:]
        connection = {
            "totalCount": 2,
            "nodes": nodes,
            "pageInfo": {
                "hasNextPage": cursor is None,
                "endCursor": "next-reaction" if cursor is None else None,
            },
        }
        return httpx.Response(
            200,
            headers=_quota_headers("graphql", 4_900 - len(seen)),
            json={
                "data": {
                    "node": {
                        "id": "comment-node-81",
                        "reactions": connection,
                    },
                },
            },
            request=request,
        )

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(
        token=str(id(client)),
        client=client,
        graphql_url="/graphql",
        now=lambda: _T0,
    )
    try:
        await api.get_json("/seed-core")
        result = await api.reactions(
            "/repos/acme/widgets/issues/comments/81/reactions",
            "comment-node-81",
            previous=None,
            cache=None,
        )
    finally:
        await client.aclose()

    assert [body["variables"]["cursor"] for body in seen] == [None, "next-reaction"]
    assert result.source == "graphql"
    assert result.raw == reactions
    assert [reaction["id"] for reaction in result.value] == [91, 92]
    assert [reaction["content"] for reaction in result.value] == ["+1", "rocket"]
    assert result.value[0]["user"]["login"] == "reactor-91"


@pytest.mark.asyncio
async def test_graphql_closing_issue_references_batch_and_paginate() -> None:
    seen: list[dict[str, Any]] = []
    issue_11 = _closing_issue(11)
    issue_12 = _closing_issue(12)
    issue_13 = _closing_issue(13, repository="other/project")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            repository = {
                "p0": {
                    "number": 7,
                    "closingIssuesReferences": {
                        "totalCount": 2,
                        "nodes": [issue_11],
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-7"},
                    },
                },
                "p1": {
                    "number": 9,
                    "closingIssuesReferences": {
                        "totalCount": 1,
                        "nodes": [issue_12],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                },
            }
        else:
            repository = {
                "p0": {
                    "number": 7,
                    "closingIssuesReferences": {
                        "totalCount": 2,
                        "nodes": [issue_13],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                },
            }
        return httpx.Response(200, json={"data": {"repository": repository}}, request=request)

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(token=str(id(client)), client=client, graphql_url="/graphql")
    try:
        result = await api.closing_issue_references("acme", "widgets", [7, 9])
    finally:
        await client.aclose()

    assert result == {7: [issue_11, issue_13], 9: [issue_12]}
    assert seen[0]["variables"] == {
        "owner": "acme",
        "repo": "widgets",
        "number0": 7,
        "cursor0": None,
        "number1": 9,
        "cursor1": None,
    }
    assert seen[1]["variables"] == {
        "owner": "acme",
        "repo": "widgets",
        "number0": 7,
        "cursor0": "cursor-7",
    }
    assert "p1: pullRequest(number: $number1)" in seen[0]["query"]
    assert "first: 100" in seen[0]["query"]
    assert "excludeUserLinked: false" in seen[0]["query"]
    assert api.request_count == 2


@pytest.mark.asyncio
async def test_graphql_closing_issue_references_accept_one_hundred_aliases() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        repository = {
            f"p{index}": {
                "number": number,
                "closingIssuesReferences": {
                    "totalCount": 0,
                    "nodes": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
            for index, number in enumerate(range(1, 101))
        }
        return httpx.Response(200, json={"data": {"repository": repository}}, request=request)

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(token=str(id(client)), client=client, graphql_url="/graphql")
    try:
        assert await api.closing_issue_references("acme", "widgets", list(range(1, 101))) == {
            number: [] for number in range(1, 101)
        }
        with pytest.raises(ValueError, match="at most 100"):
            await api.closing_issue_references("acme", "widgets", list(range(1, 102)))
    finally:
        await client.aclose()

    assert seen[0]["query"].count(": pullRequest(number:") == 100
    assert api.request_count == 1


@pytest.mark.asyncio
async def test_graphql_closing_issue_references_reject_incomplete_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "p0": {
                            "number": 7,
                            "closingIssuesReferences": {
                                "totalCount": 2,
                                "nodes": [_closing_issue(11)],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            },
                        },
                    },
                },
            },
            request=request,
        )

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(token=str(id(client)), client=client, graphql_url="/graphql")
    try:
        with pytest.raises(GitHubAPIError, match="advertised 2 closing issues, got 1"):
            await api.closing_issue_references("acme", "widgets", [7])
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_graphql_closing_issue_references_require_authentication() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected request: {request.url}")

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client, graphql_url="/graphql")
    try:
        with pytest.raises(GitHubAPIError, match="require GitHub authentication"):
            await api.closing_issue_references("acme", "widgets", [7])
    finally:
        await client.aclose()

    assert api.request_count == 0


@pytest.mark.asyncio
async def test_quota_progress_keeps_rest_and_graphql_buckets() -> None:
    progress = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/graphql":
            resource = "graphql"
            remaining = 4_997
            body = {
                "data": {
                    "repository": {
                        "issues": {"totalCount": 1},
                        "pullRequests": {"totalCount": 2},
                    },
                },
            }
        else:
            resource = "core"
            remaining = 4_321
            body = {"ok": True}
        return httpx.Response(
            200,
            headers={
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": str(remaining),
                "x-ratelimit-reset": str(int((_T0 + timedelta(hours=1)).timestamp())),
                "x-ratelimit-resource": resource,
            },
            json=body,
            request=request,
        )

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(token=str(id(client)), client=client, graphql_url="/graphql", progress=progress.append)
    try:
        await api.repository_item_count("acme", "widgets")
        await api.get_json("/rest")
    finally:
        await client.aclose()

    assert progress[-1].quotas == (
        RateQuota("core", 5_000, 4_321, _T0 + timedelta(hours=1)),
        RateQuota("graphql", 5_000, 4_997, _T0 + timedelta(hours=1)),
    )


@pytest.mark.asyncio
async def test_anonymous_repository_discovery_uses_no_graphql_quota() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected request: {request.url}")

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client, graphql_url="/graphql")
    try:
        assert await api.repository_item_count("acme", "widgets") is None
    finally:
        await client.aclose()

    assert api.request_count == 0


@pytest.mark.asyncio
async def test_graphql_rate_limit_waits_and_retries() -> None:
    clock = Clock(_T0)
    responses = [
        httpx.Response(
            200,
            headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(_T0.timestamp()) + 2),
            },
            json={"errors": [{"message": "API rate limit exceeded"}]},
        ),
        httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "issues": {"totalCount": 1},
                        "pullRequests": {"totalCount": 2},
                    },
                },
            },
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        response = responses.pop(0)
        response.request = request
        return response

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    credential = str(id(client))
    api = GitHubAPI(
        token=credential,
        client=client,
        graphql_url="/graphql",
        sleep=clock.sleep,
        now=clock,
    )
    try:
        assert await api.repository_item_count("acme", "widgets") == 3
    finally:
        await client.aclose()

    assert clock.sleeps == [3]
    assert api.request_count == 2


@pytest.mark.asyncio
async def test_successful_last_quota_response_gates_the_next_request() -> None:
    clock = Clock(_T0)
    progress = []
    responses = [
        (
            200,
            {
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(_T0.timestamp()) + 2),
                "x-ratelimit-resource": "core",
            },
        ),
        (200, {}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        status, headers = responses.pop(0)
        return httpx.Response(status, headers=headers, json={"ok": True}, request=request)

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client, sleep=clock.sleep, now=clock, progress=progress.append)
    try:
        await api.get_json("/first")
        await api.get_json("/second")
    finally:
        await client.aclose()

    assert clock.sleeps == [3]
    assert api.request_count == 2
    waits = [event for event in progress if event.wait_seconds is not None]
    assert len(waits) == 1
    assert waits[0].detail == "primary_rate_limit"
    assert waits[0].wait_seconds == 3
    assert waits[0].quotas[0].remaining == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exhausted", "next_path"),
    [("core", "/graphql"), ("graphql", "/rest")],
)
async def test_primary_quota_gate_does_not_block_the_other_resource(
    exhausted: str,
    next_path: str,
) -> None:
    clock = Clock(_T0)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        resource = "graphql" if request.url.path == "/graphql" else "core"
        remaining = 0 if resource == exhausted else 4_999
        body = (
            {
                "data": {
                    "repository": {
                        "issues": {"totalCount": 1},
                        "pullRequests": {"totalCount": 2},
                    },
                },
            }
            if resource == "graphql"
            else {"ok": True}
        )
        return httpx.Response(
            200,
            headers={
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": str(remaining),
                "x-ratelimit-reset": str(int((_T0 + timedelta(hours=1)).timestamp())),
                "x-ratelimit-resource": resource,
            },
            json=body,
            request=request,
        )

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(
        token=str(id(client)),
        client=client,
        graphql_url="/graphql",
        sleep=clock.sleep,
        now=clock,
    )
    try:
        if exhausted == "core":
            await api.get_json("/rest")
            assert await api.repository_item_count("acme", "widgets") == 3
        else:
            assert await api.repository_item_count("acme", "widgets") == 3
            await api.get_json("/rest")
    finally:
        await client.aclose()

    assert seen == (["/rest", next_path] if exhausted == "core" else ["/graphql", next_path])
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_concurrent_quota_responses_never_move_backward() -> None:
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    progress = []
    first_reset = int((_T0 + timedelta(hours=1)).timestamp())
    next_reset = int((_T0 + timedelta(hours=2)).timestamp())

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old-slow":
            slow_started.set()
            await release_slow.wait()
            reset, remaining = first_reset, 4_999
        elif request.url.path == "/old-fast":
            reset, remaining = first_reset, 4_998
        else:
            reset, remaining = next_reset, 4_999
        return httpx.Response(
            200,
            headers={
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": str(remaining),
                "x-ratelimit-reset": str(reset),
                "x-ratelimit-resource": "core",
            },
            json={"ok": True},
            request=request,
        )

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client, progress=progress.append)
    try:
        slow = asyncio.create_task(api.get_json("/old-slow"))
        await slow_started.wait()
        await api.get_json("/old-fast")
        assert progress[-1].quotas[0].remaining == 4_998
        await api.get_json("/new-fast")
        release_slow.set()
        await slow
    finally:
        await client.aclose()

    assert progress[-1].quotas == (RateQuota("core", 5_000, 4_999, _T0 + timedelta(hours=2)),)


@pytest.mark.asyncio
async def test_quota_reset_jitter_keeps_conservative_remaining() -> None:
    progress = []
    reset = int((_T0 + timedelta(hours=1)).timestamp())
    responses = [(3_325, reset), (4_447, reset + 2), (3_317, reset - 1)]

    def handler(request: httpx.Request) -> httpx.Response:
        remaining, response_reset = responses.pop(0)
        return httpx.Response(
            200,
            headers={
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": str(remaining),
                "x-ratelimit-reset": str(response_reset),
                "x-ratelimit-resource": "core",
            },
            json={"ok": True},
            request=request,
        )

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(client=client, progress=progress.append)
    try:
        await api.get_json("/first")
        await api.get_json("/later-reset")
        await api.get_json("/earlier-reset")
    finally:
        await client.aclose()

    assert progress[-1].quotas == (
        RateQuota("core", 5_000, 3_317, datetime.fromtimestamp(reset + 2, UTC)),
    )
