"""Test GitHub REST transport pagination and conditional responses."""

from __future__ import annotations

import httpx
import pytest

from gh_puller.github import (
    GitHubAPI,
    GitHubAPIError,
)
from gh_puller.github.client import GitHubPage


@pytest.mark.asyncio
async def test_rest_page_exposes_the_opaque_next_cursor_without_following_it() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.params.get("cursor") == "opaque":
            return httpx.Response(200, json=[{"id": 2}], request=request)
        link = '<https://api.github.test/items?cursor=opaque>; rel="next"'
        return httpx.Response(
            200,
            headers={"link": link},
            json=[{"id": 1, "unknown": ["kept"]}],
            request=request,
        )

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client)
    try:
        page = await api.get_page("/items", params={"state": "all"})
        next_page = await api.get_page(page.next_url or "")
    finally:
        await client.aclose()

    assert page.items == [{"id": 1, "unknown": ["kept"]}]
    assert page.next_url == "https://api.github.test/items?cursor=opaque"
    assert next_page == GitHubPage([{"id": 2}], None)
    assert seen == [
        "https://api.github.test/items?state=all&per_page=100",
        "https://api.github.test/items?cursor=opaque",
    ]


@pytest.mark.asyncio
async def test_compare_commits_paginates_all_413_raw_objects() -> None:
    seen: list[str] = []
    total = 413

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        page_number = int(request.url.params.get("page", "1"))
        start = (page_number - 1) * 100
        stop = min(start + 100, total)
        headers = {}
        if stop < total:
            headers["link"] = (
                "<https://api.github.test/repos/acme/widgets/compare/base...head"
                f'?per_page=100&page={page_number + 1}>; rel="next"'
            )
        return httpx.Response(
            200,
            headers=headers,
            json={
                "total_commits": total,
                "commits": [
                    {"sha": f"{index:040x}", "unknown": {"index": index}}
                    for index in range(start, stop)
                ],
            },
            request=request,
        )

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(client=client)
    try:
        commits = await api.compare_commits("acme", "widgets", "base", "head")
    finally:
        await client.aclose()

    assert len(commits) == total
    assert len({commit["sha"] for commit in commits}) == total
    assert commits[-1]["unknown"] == {"index": total - 1}
    assert len(seen) == api.request_count == 5
    assert seen[0].endswith("/compare/base...head?per_page=100")
    assert seen[-1].endswith("/compare/base...head?per_page=100&page=5")


@pytest.mark.asyncio
async def test_compare_commits_rejects_a_total_that_changes_between_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200,
                json={"total_commits": 100, "commits": [{"sha": f"{100:040x}"}]},
                request=request,
            )
        return httpx.Response(
            200,
            headers={
                "link": (
                    "<https://api.github.test/repos/acme/widgets/compare/base...head"
                    '?per_page=100&page=2>; rel="next"'
                ),
            },
            json={
                "total_commits": 101,
                "commits": [{"sha": f"{index:040x}"} for index in range(100)],
            },
            request=request,
        )

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(client=client)
    try:
        with pytest.raises(GitHubAPIError, match="total changed"):
            await api.compare_commits("acme", "widgets", "base", "head")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_compare_commits_rejects_duplicate_commit_identities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total_commits": 2,
                "commits": [{"sha": "duplicate"}, {"sha": "duplicate"}],
            },
            request=request,
        )

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    api = GitHubAPI(client=client)
    try:
        with pytest.raises(GitHubAPIError, match="invalid commit identities"):
            await api.compare_commits("acme", "widgets", "base", "head")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rest_pagination_follows_link_header_and_preserves_fields() -> None:
    seen: list[str] = []
    page_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[{"id": 2, "unknown": ["b"]}], request=request)
        link = '<https://api.github.test/items?page=2>; rel="next"'
        return httpx.Response(200, headers={"link": link}, json=[{"id": 1, "unknown": ["a"]}], request=request)

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client)
    try:
        items = await api.paginate(
            "/items",
            params={"state": "all"},
            page_observer=page_sizes.append,
        )
    finally:
        await client.aclose()

    assert items == [{"id": 1, "unknown": ["a"]}, {"id": 2, "unknown": ["b"]}]
    assert "per_page=100" in seen[0]
    assert seen[1] == "https://api.github.test/items?page=2"
    assert page_sizes == [1, 1]


@pytest.mark.asyncio
async def test_conditional_json_reuses_exact_paired_response_on_304() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                200,
                headers={"etag": '"version-1"', "x-ratelimit-remaining": "4999"},
                json={"id": 1, "raw": {"kept": True}},
                request=request,
            )
        assert request.headers["if-none-match"] == '"version-1"'
        return httpx.Response(
            304,
            headers={"etag": '"version-1"', "x-ratelimit-remaining": "4999"},
            request=request,
        )

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client)
    try:
        first, cache = await api.get_json_cached(
            "/item",
            previous=None,
            cache=None,
        )
        second, updated = await api.get_json_cached(
            "/item",
            previous=first,
            cache=cache,
        )
    finally:
        await client.aclose()

    assert second is first
    assert updated == cache
    assert api.request_count == 2


@pytest.mark.asyncio
async def test_conditional_single_page_collection_reuses_then_reads_change() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(200, headers={"etag": '"a"'}, json=[{"id": 1}], request=request)
        if len(seen) == 2:
            assert request.headers["if-none-match"] == '"a"'
            return httpx.Response(304, headers={"etag": '"a"'}, request=request)
        assert request.headers["if-none-match"] == '"a"'
        return httpx.Response(
            200,
            headers={"etag": '"b"'},
            json=[{"id": 1}, {"id": 2}],
            request=request,
        )

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client)
    try:
        first, cache = await api.paginate_cached(
            "/items",
            previous=None,
            cache=None,
        )
        reused, cache = await api.paginate_cached(
            "/items",
            previous=first,
            cache=cache,
        )
        changed, cache = await api.paginate_cached(
            "/items",
            previous=reused,
            cache=cache,
        )
    finally:
        await client.aclose()

    assert reused is first
    assert changed == [{"id": 1}, {"id": 2}]
    assert cache is not None
    assert cache["size"] == 2
    assert api.request_count == 3


@pytest.mark.asyncio
async def test_conditional_multi_page_collection_reuses_every_validated_page() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = int(request.url.params.get("page", "1"))
        etag = f'"page-{page}"'
        conditional = request.headers.get("if-none-match")
        if conditional is not None:
            assert conditional == etag
            return httpx.Response(304, headers={"etag": etag}, request=request)
        if page == 1:
            link = '<https://api.github.test/items?per_page=100&page=2>; rel="next"'
            return httpx.Response(
                200,
                headers={"etag": etag, "link": link},
                json=[{"id": item_id} for item_id in range(1, 101)],
                request=request,
            )
        return httpx.Response(
            200,
            headers={"etag": etag},
            json=[{"id": 101}],
            request=request,
        )

    observed_pages: list[int] = []
    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client)
    try:
        first, cache = await api.paginate_cached(
            "/items",
            previous=None,
            cache=None,
            page_observer=observed_pages.append,
        )
        reused, updated = await api.paginate_cached(
            "/items",
            previous=first,
            cache=cache,
            page_observer=observed_pages.append,
        )
    finally:
        await client.aclose()

    assert reused is first
    assert updated == cache
    assert observed_pages == [100, 1, 100, 1]
    assert len(seen) == 4


@pytest.mark.asyncio
async def test_changed_later_page_forces_a_fresh_complete_pagination() -> None:
    seen: list[httpx.Request] = []

    def page_one(request: httpx.Request) -> httpx.Response:
        link = '<https://api.github.test/items?per_page=100&page=2>; rel="next"'
        return httpx.Response(
            200,
            headers={"etag": '"page-1"', "link": link},
            json=[{"id": item_id} for item_id in range(1, 101)],
            request=request,
        )

    def page_two(request: httpx.Request, *, changed: bool) -> httpx.Response:
        item = {"id": 101}
        if changed:
            item["body"] = "changed"
        return httpx.Response(
            200,
            headers={"etag": '"page-2b"' if changed else '"page-2a"'},
            json=[item],
            request=request,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        invocation = len(seen)
        if invocation == 1:
            return page_one(request)
        if invocation == 2:
            return page_two(request, changed=False)
        if invocation == 3:
            assert request.headers["if-none-match"] == '"page-1"'
            return httpx.Response(304, headers={"etag": '"page-1"'}, request=request)
        if invocation == 4:
            assert request.headers["if-none-match"] == '"page-2a"'
            return page_two(request, changed=True)
        if invocation == 5:
            assert "if-none-match" not in request.headers
            return page_one(request)
        assert invocation == 6
        assert "if-none-match" not in request.headers
        return page_two(request, changed=True)

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client)
    try:
        first, cache = await api.paginate_cached(
            "/items",
            previous=None,
            cache=None,
        )
        changed, updated = await api.paginate_cached(
            "/items",
            previous=first,
            cache=cache,
        )
    finally:
        await client.aclose()

    assert changed[-1] == {"id": 101, "body": "changed"}
    assert updated is not None
    assert len(seen) == 6


@pytest.mark.asyncio
async def test_full_page_collection_probes_growth_by_unconditional_pagination() -> None:
    invocation = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal invocation
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200,
                headers={"etag": '"second-page"'},
                json=[{"id": 101}],
                request=request,
            )
        invocation += 1
        assert "if-none-match" not in request.headers
        headers = {"etag": '"first-page"'}
        if invocation == 2:
            headers["link"] = '<https://api.github.test/items?per_page=100&page=2>; rel="next"'
        return httpx.Response(
            200,
            headers=headers,
            json=[{"id": item_id} for item_id in range(1, 101)],
            request=request,
        )

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client)
    try:
        first, cache = await api.paginate_cached(
            "/items",
            previous=None,
            cache=None,
        )
        second, updated = await api.paginate_cached(
            "/items",
            previous=first,
            cache=cache,
        )
    finally:
        await client.aclose()

    assert len(first) == 100
    assert cache is None
    assert len(second) == 101
    assert updated is not None
