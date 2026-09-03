"""GitHub 拉取器的观测水位、原始响应保留、恢复与 SQLite 发布测试。"""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit

import aiosqlite
import httpx
import pytest

from gh_puller.github import (
    ArchivedHead,
    ArchivedRun,
    ArchivedVersion,
    ConsoleProgress,
    GitHubAPI,
    GitHubAPIError,
    GitHubPullConfig,
    GitHubPuller,
    GitStoreError,
    IncompleteGitHubDataError,
    PullProgress,
    RateQuota,
    iter_heads,
    iter_runs,
    iter_versions,
)
from gh_puller.github.client import GitHubPage, GitHubResource
from gh_puller.github.store import SQLiteArchive

if TYPE_CHECKING:
    from pathlib import Path

_BASE = "/repos/acme/widgets"
_T0 = datetime(2026, 8, 1, 12, tzinfo=UTC)


@dataclass
class Clock:
    current: datetime
    sleeps: list[float] = field(default_factory=list)
    on_sleep: Any = None

    def __call__(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)
        if self.on_sleep:
            self.on_sleep()


class FakeAPI:
    def __init__(self) -> None:
        self.catalog: list[dict[str, Any]] = []
        self.json: dict[str, Any] = {}
        self.pages: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.request_count = 0
        self.fail_once: set[str] = set()
        self.failed: set[str] = set()
        self.on_request: Any = None
        self.reported_count: int | None = None
        self.count_available = True
        self.closing: dict[int, list[dict[str, Any]]] = {}
        self.comparisons: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.catalog_accepts: list[str | None] = []

    async def close(self) -> None:
        pass

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str | None = None,
    ) -> Any:
        self._called("json", path, params)
        if path in self.fail_once and path not in self.failed:
            self.failed.add(path)
            raise RuntimeError(f"injected failure: {path}")
        if path.endswith("/requested_reviewers"):
            return deepcopy(self.json.get(path, {"users": [], "teams": []}))
        value = deepcopy(self.json[path])
        if re.fullmatch(rf"{re.escape(_BASE)}/pulls/\d+", path):
            value.setdefault("requested_reviewers", [])
            value.setdefault("requested_teams", [])
        return value

    async def get_json_cached(
        self,
        path: str,
        *,
        previous: Any | None,
        cache: dict[str, Any] | None,
        params: dict[str, Any] | None = None,
        accept: str | None = None,
    ) -> tuple[Any, dict[str, Any] | None]:
        return await self.get_json(path, params=params, accept=accept), None

    async def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        page_observer: Any = None,
    ) -> list[dict[str, Any]]:
        items = self._items(path, params)
        self._called("page", path, params, requests=max(1, math.ceil(len(items) / 100)))
        if path in self.fail_once and path not in self.failed:
            self.failed.add(path)
            raise RuntimeError(f"injected failure: {path}")
        if page_observer is not None:
            if items:
                for offset in range(0, len(items), 100):
                    page_observer(min(100, len(items) - offset))
            else:
                page_observer(0)
        return items

    async def paginate_cached(
        self,
        path: str,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
        params: dict[str, Any] | None = None,
        page_observer: Any = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        return await self.paginate(path, params=params, page_observer=page_observer), None

    async def get_page(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str | None = None,
    ) -> GitHubPage:
        self.catalog_accepts.append(accept)
        parsed = urlsplit(path)
        route = parsed.path
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        offset = int(query.pop("__offset", 0))
        query.pop("per_page", None)
        effective = deepcopy(params) if params is not None else query
        items = self._items(route, effective)
        self._called("page", route, effective)
        if route in self.fail_once and route not in self.failed:
            self.failed.add(route)
            raise RuntimeError(f"injected failure: {route}")
        page = items[offset : offset + 100]
        next_url = None
        if offset + 100 < len(items):
            next_query = dict(effective or {}) | {
                "per_page": 100,
                "__offset": offset + 100,
            }
            next_url = f"{route}?{urlencode(next_query)}"
        return GitHubPage(page, next_url)

    def _items(
        self,
        path: str,
        params: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if path == f"{_BASE}/issues" and params and params.get("state") == "all":
            items = deepcopy(self.catalog)
        elif path == f"{_BASE}/issues/comments" and not (params and params.get("since")):
            items = self._repository_comments("issues", "issue_url")
        elif path == f"{_BASE}/pulls/comments" and not (params and params.get("since")):
            items = self._repository_comments("pulls", "pull_request_url")
        else:
            items = deepcopy(self.pages.get(path, []))
            issue_match = re.fullmatch(rf"{re.escape(_BASE)}/issues/\d+/comments", path)
            pull_match = re.fullmatch(rf"{re.escape(_BASE)}/pulls/\d+/comments", path)
            if issue_match:
                items = [item | {"issue_url": path.removesuffix("/comments")} for item in items]
            elif pull_match:
                items = [item | {"pull_request_url": path.removesuffix("/comments")} for item in items]
        if params and params.get("since"):
            since = datetime.fromisoformat(params["since"])
            items = [item for item in items if _time(item["updated_at"]) > since]
        if params and params.get("sort") in {"created", "updated"}:
            field = f"{params['sort']}_at"
            items.sort(
                key=lambda item: (
                    str(item.get(field, "")),
                    int(item.get("number", item.get("id", 0))),
                ),
                reverse=params.get("direction") == "desc",
            )
        return items

    def _repository_comments(
        self,
        parent_kind: str,
        url_field: str,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        prefix = f"{_BASE}/{parent_kind}/"
        for path, comments in self.pages.items():
            if not path.startswith(prefix) or not path.endswith("/comments"):
                continue
            if not path[len(prefix) : -len("/comments")].isdigit():
                continue
            parent = path.removesuffix("/comments")
            result.extend(deepcopy(comment) | {url_field: parent} for comment in comments)
        return result

    async def repository_item_count(self, owner: str, repo: str) -> int | None:
        self._called("count", "/graphql", {"owner": owner, "repo": repo})
        if not self.count_available:
            return None
        return len(self.catalog) if self.reported_count is None else self.reported_count

    async def compare_commits(
        self,
        owner: str,
        repo: str,
        base: str,
        head: str,
    ) -> list[dict[str, Any]]:
        items = deepcopy(self.comparisons[base, head])
        self._called(
            "compare",
            f"/repos/{owner}/{repo}/compare/{base}...{head}",
            None,
            requests=max(1, math.ceil(len(items) / 100)),
        )
        return items

    async def pull_request(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        previous: dict[str, Any] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource:
        value, updated = await self.get_json_cached(
            f"/repos/{owner}/{repo}/pulls/{number}",
            previous=previous,
            cache=cache,
        )
        return GitHubResource(value, "rest", value, updated)

    async def pull_reviews(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource:
        value, updated = await self.paginate_cached(
            f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            previous=previous,
            cache=cache,
        )
        return GitHubResource(value, "rest", value, updated)

    async def pull_commits(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        expected: int,
        base: str,
        head: str,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource:
        if expected > 250:
            value = await self.compare_commits(owner, repo, base, head)
            return GitHubResource(value, "rest", value)
        value, updated = await self.paginate_cached(
            f"/repos/{owner}/{repo}/pulls/{number}/commits",
            previous=previous,
            cache=cache,
        )
        return GitHubResource(value, "rest", value, updated)

    async def pull_review_comments(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource:
        value, updated = await self.paginate_cached(
            f"/repos/{owner}/{repo}/pulls/{number}/comments",
            previous=previous,
            cache=cache,
        )
        return GitHubResource(value, "rest", value, updated)

    async def issue_comments(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource:
        value, updated = await self.paginate_cached(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            previous=previous,
            cache=cache,
        )
        return GitHubResource(value, "rest", value, updated)

    async def reactions(
        self,
        path: str,
        node_id: str | None,
        *,
        previous: list[dict[str, Any]] | None,
        cache: dict[str, Any] | None,
    ) -> GitHubResource:
        value, updated = await self.paginate_cached(
            path,
            previous=previous,
            cache=cache,
        )
        return GitHubResource(value, "rest", value, updated)

    async def closing_issue_references(
        self,
        owner: str,
        repo: str,
        numbers: list[int],
    ) -> dict[int, list[dict[str, Any]]]:
        self._called(
            "closing",
            "/graphql",
            {"owner": owner, "repo": repo, "numbers": numbers},
        )
        return {number: deepcopy(self.closing.get(number, [])) for number in numbers}

    def add_issue(
        self,
        number: int,
        *,
        created_at: datetime = _T0 - timedelta(days=1),
        updated_at: datetime = _T0 - timedelta(hours=1),
        pull: bool = False,
        body: str = "body",
    ) -> dict[str, Any]:
        summary = {
            "id": number * 10,
            "number": number,
            "created_at": _iso(created_at),
            "updated_at": _iso(updated_at),
            "title": f"item {number}",
            "unknown_summary_field": {"preserved": True},
            "body": body,
            "comments": 0,
            "reactions": {"total_count": 0},
            "unknown_detail_field": [1, {"raw": "yes"}],
        }
        if pull:
            summary["pull_request"] = {"url": f"{_BASE}/pulls/{number}"}
        self.catalog.append(summary)
        self.json[f"{_BASE}/issues/{number}"] = summary
        return summary

    def _called(
        self,
        kind: str,
        path: str,
        params: dict[str, Any] | None,
        *,
        requests: int = 1,
    ) -> None:
        self.request_count += requests
        self.calls.append((kind, path, deepcopy(params)))
        if self.on_request:
            self.on_request()


class FakeGitStore:
    def __init__(self) -> None:
        self.prefetches: list[list[int]] = []
        self.captures: list[int] = []

    async def prefetch(self, numbers: list[int], *, heartbeat: Any = None) -> None:
        self.prefetches.append(list(numbers))
        if heartbeat is not None:
            heartbeat()

    async def capture(self, number: int, pull: dict[str, Any]) -> dict[str, Any]:
        self.captures.append(number)
        base = pull.get("base")
        head = pull.get("head")
        base_sha = base.get("sha") if isinstance(base, dict) else "0" * 40
        head_sha = head.get("sha") if isinstance(head, dict) else f"{number:040x}"
        prefix = f"refs/gh-puller/snapshots/pulls/{number}"
        return {
            "base_ref": f"{prefix}/base/{base_sha}",
            "base_sha": base_sha,
            "comparison_kind": "merge_base",
            "comparison_ref": f"{prefix}/comparison/{base_sha}",
            "comparison_sha": base_sha,
            "head_ref": f"{prefix}/head/{head_sha}",
            "head_sha": head_sha,
        }


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _quota_headers(resource: str, remaining: int) -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-reset": str(int((_T0 + timedelta(hours=1)).timestamp())),
        "x-ratelimit-resource": resource,
    }


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _closing_issue(number: int, *, repository: str = "acme/widgets") -> dict[str, Any]:
    return {
        "id": f"issue-{repository}-{number}",
        "number": number,
        "title": f"closing issue {number}",
        "state": "OPEN",
        "url": f"https://github.test/{repository}/issues/{number}",
        "repository": {
            "id": f"repository-{repository}",
            "nameWithOwner": repository,
            "url": f"https://github.test/{repository}",
        },
    }


def _graphql_pull(number: int = 7) -> dict[str, Any]:
    repository = {
        "id": "repository-acme-widgets",
        "name": "widgets",
        "nameWithOwner": "acme/widgets",
        "url": "https://github.test/acme/widgets",
        "isFork": False,
        "owner": {
            "id": "owner-acme",
            "login": "acme",
            "avatarUrl": "https://avatars.test/acme",
            "url": "https://github.test/acme",
        },
    }
    return {
        "id": f"pull-{number}",
        "fullDatabaseId": str(number * 100),
        "number": number,
        "url": f"https://github.test/acme/widgets/pull/{number}",
        "state": "MERGED",
        "locked": False,
        "title": f"pull {number}",
        "body": "body",
        "authorAssociation": "CONTRIBUTOR",
        "createdAt": _iso(_T0 - timedelta(days=2)),
        "updatedAt": _iso(_T0 - timedelta(hours=1)),
        "closedAt": _iso(_T0 - timedelta(hours=2)),
        "mergedAt": _iso(_T0 - timedelta(hours=2)),
        "isDraft": False,
        "merged": True,
        "mergeable": "UNKNOWN",
        "mergeStateStatus": "CLEAN",
        "canBeRebased": True,
        "maintainerCanModify": True,
        "additions": 11,
        "deletions": 3,
        "changedFiles": 2,
        "baseRefName": "main",
        "baseRefOid": "a" * 40,
        "baseRepository": repository,
        "headRefName": "feature",
        "headRefOid": "b" * 40,
        "headRepository": repository,
        "mergeCommit": {"oid": "c" * 40},
        "author": {
            "__typename": "User",
            "id": "user-alice",
            "databaseId": 17,
            "login": "alice",
            "avatarUrl": "https://avatars.test/alice",
            "url": "https://github.test/alice",
            "isSiteAdmin": False,
        },
        "comments": {"totalCount": 4},
        "commits": {"totalCount": 3},
        "futureGraphQLField": {"kept": True},
    }


def _graphql_review(number: int) -> dict[str, Any]:
    return {
        "id": f"review-{number}",
        "fullDatabaseId": str(number),
        "body": f"review {number}",
        "state": "APPROVED",
        "authorAssociation": "MEMBER",
        "submittedAt": _iso(_T0 - timedelta(minutes=number)),
        "createdAt": _iso(_T0 - timedelta(minutes=number + 1)),
        "updatedAt": _iso(_T0 - timedelta(minutes=number)),
        "url": f"https://github.test/acme/widgets/pull/7#pullrequestreview-{number}",
        "commit": {"oid": f"{number:040x}"},
        "author": {
            "__typename": "User",
            "id": f"user-{number}",
            "databaseId": number + 100,
            "login": f"reviewer-{number}",
            "avatarUrl": f"https://avatars.test/{number}",
            "url": f"https://github.test/reviewer-{number}",
            "isSiteAdmin": False,
        },
        "futureGraphQLField": {"kept": number},
    }


def _graphql_commit(number: int) -> dict[str, Any]:
    sha = f"{number:040x}"
    parent = f"{number - 1:040x}"
    user = {
        "id": f"user-{number}",
        "databaseId": number + 100,
        "login": f"committer-{number}",
        "avatarUrl": f"https://avatars.test/{number}",
        "url": f"https://github.test/committer-{number}",
        "isSiteAdmin": False,
    }
    return {
        "id": f"pull-commit-{number}",
        "url": f"https://github.test/acme/widgets/pull/7/commits/{sha}",
        "commit": {
            "id": f"commit-{number}",
            "oid": sha,
            "url": f"https://github.test/acme/widgets/commit/{sha}",
            "message": f"commit {number}",
            "authoredDate": _iso(_T0 - timedelta(minutes=number + 1)),
            "committedDate": _iso(_T0 - timedelta(minutes=number)),
            "additions": number,
            "deletions": 1,
            "changedFilesIfAvailable": 2,
            "author": {
                "name": f"Author {number}",
                "email": f"author{number}@example.test",
                "date": _iso(_T0 - timedelta(minutes=number + 1)),
                "user": user,
            },
            "committer": {
                "name": f"Committer {number}",
                "email": f"committer{number}@example.test",
                "date": _iso(_T0 - timedelta(minutes=number)),
                "user": user,
            },
            "tree": {"oid": "f" * 40},
            "parents": {
                "totalCount": 1,
                "nodes": [{"oid": parent}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
            "futureGraphQLField": {"kept": number},
        },
    }


def _graphql_review_comment(number: int) -> dict[str, Any]:
    return {
        "id": f"review-comment-{number}",
        "fullDatabaseId": str(number),
        "body": f"comment {number}",
        "authorAssociation": "MEMBER",
        "createdAt": _iso(_T0 - timedelta(minutes=number + 1)),
        "updatedAt": _iso(_T0 - timedelta(minutes=number)),
        "url": f"https://github.test/acme/widgets/pull/7#discussion_r{number}",
        "diffHunk": "@@ -1 +1 @@",
        "path": "src/example.py",
        "line": number,
        "originalLine": number - 1,
        "originalStartLine": None,
        "startLine": None,
        "outdated": False,
        "subjectType": "LINE",
        "state": "SUBMITTED",
        "commit": {"oid": f"{number:040x}"},
        "originalCommit": {"oid": f"{number - 1:040x}"},
        "replyTo": None,
        "pullRequestReview": {"fullDatabaseId": "71"},
        "reactions": {"totalCount": 1},
        "author": {
            "__typename": "User",
            "id": f"user-{number}",
            "databaseId": number + 100,
            "login": f"commenter-{number}",
            "avatarUrl": f"https://avatars.test/{number}",
            "url": f"https://github.test/commenter-{number}",
            "isSiteAdmin": False,
        },
        "futureGraphQLField": {"kept": number},
    }


def _graphql_issue_comment(number: int) -> dict[str, Any]:
    return {
        "id": f"issue-comment-{number}",
        "fullDatabaseId": str(number),
        "body": f"comment {number}",
        "bodyHTML": f"<p>comment {number}</p>",
        "bodyText": f"comment {number}",
        "authorAssociation": "CONTRIBUTOR",
        "createdAt": _iso(_T0 - timedelta(minutes=number + 1)),
        "updatedAt": _iso(_T0 - timedelta(minutes=number)),
        "url": f"https://github.test/acme/widgets/issues/7#issuecomment-{number}",
        "reactions": {"totalCount": 1},
        "author": {
            "__typename": "User",
            "id": f"user-{number}",
            "databaseId": number + 100,
            "login": f"commenter-{number}",
            "avatarUrl": f"https://avatars.test/{number}",
            "url": f"https://github.test/commenter-{number}",
            "isSiteAdmin": False,
        },
        "futureGraphQLField": {"kept": number},
    }


def _graphql_reaction(number: int, content: str) -> dict[str, Any]:
    return {
        "id": f"reaction-{number}",
        "databaseId": number,
        "content": content,
        "createdAt": _iso(_T0 - timedelta(minutes=number)),
        "user": {
            "__typename": "User",
            "id": f"user-{number}",
            "databaseId": number + 100,
            "login": f"reactor-{number}",
            "avatarUrl": f"https://avatars.test/{number}",
            "url": f"https://github.test/reactor-{number}",
            "isSiteAdmin": False,
        },
    }


def _config(path: Path, **kwargs: Any) -> GitHubPullConfig:
    return GitHubPullConfig(repository="acme/widgets", destination=path, **kwargs)


def _puller(config: GitHubPullConfig, **kwargs: Any) -> GitHubPuller:
    kwargs.setdefault("git", FakeGitStore())
    return GitHubPuller(config, **kwargs)


async def _versions(path: Path) -> list[ArchivedVersion]:
    return [version async for version in iter_versions(path)]


async def _runs(path: Path) -> list[ArchivedRun]:
    return [run async for run in iter_runs(path)]


async def _current(path: Path) -> dict[int, ArchivedHead]:
    return {head.number: head async for head in iter_heads(path)}


async def _rows(
    path: Path,
    sql: str,
    parameters: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(sql, parameters)
        try:
            return [dict(row) for row in await cursor.fetchall()]
        finally:
            await cursor.close()
    finally:
        await db.close()


def _seed_churn_api(api: FakeAPI, size: int) -> None:
    for number in range(1, size + 1):
        pull = number % 3 == 0
        api.add_issue(number, pull=pull)
        if pull:
            api.json[f"{_BASE}/pulls/{number}"] = {
                "id": number * 10,
                "changed_files": 0,
                "commits": 0,
                "review_comments": 0,
            }
        if number % 4:
            continue
        path = f"{_BASE}/issues/{number}"
        comment = {
            "id": 100_000 + number,
            "body": "seed",
            "created_at": _iso(_T0 - timedelta(hours=2)),
            "updated_at": _iso(_T0 - timedelta(hours=2)),
            "reactions": {"total_count": 0},
        }
        api.pages[f"{path}/comments"] = [comment]
        api.json[path]["comments"] = 1


def _apply_churn_epoch(
    api: FakeAPI,
    *,
    deleted: set[int],
    added: tuple[int, ...],
    comment_operations: list[tuple[int, str, int]],
    changed_at: datetime,
) -> None:
    api.catalog = [item for item in api.catalog if item["number"] not in deleted]
    for number in added:
        pull = number % 3 == 0
        api.add_issue(number, created_at=changed_at, updated_at=changed_at, pull=pull)
        if pull:
            api.json[f"{_BASE}/pulls/{number}"] = {
                "id": number * 10,
                "changed_files": 0,
                "commits": 0,
                "review_comments": 0,
            }

    signals: list[dict[str, Any]] = []
    for number, operation, comment_id in comment_operations:
        path = f"{_BASE}/issues/{number}"
        comments = api.pages.setdefault(f"{path}/comments", [])
        if operation == "add":
            comment = {
                "id": comment_id,
                "body": f"comment {comment_id}",
                "created_at": _iso(changed_at),
                "updated_at": _iso(changed_at),
                "reactions": {"total_count": 0},
            }
            comments.append(comment)
            signals.append(comment | {"issue_url": path})
        else:
            comments[:] = [comment for comment in comments if comment["id"] != comment_id]
            summary = next(item for item in api.catalog if item["number"] == number)
            summary["updated_at"] = _iso(changed_at)
            api.json[path]["updated_at"] = _iso(changed_at)
        if "pull_request" in api.json[path]:
            api.closing[number] = [_closing_issue(comment_id)] if operation == "add" else []
        api.json[path]["comments"] = len(comments)
    api.pages[f"{_BASE}/issues/comments"] = signals


@pytest.mark.asyncio
async def test_default_token_prefers_gh_token(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    api = FakeAPI()

    def make_api(**kwargs: Any) -> FakeAPI:
        captured.update(kwargs)
        return api

    primary = str(id(api))
    monkeypatch.setenv("GH_TOKEN", primary)
    monkeypatch.setenv("GITHUB_TOKEN", f"fallback-{primary}")
    monkeypatch.setattr("gh_puller.github.puller.GitHubAPI", make_api)
    clock = Clock(_T0)

    await _puller(_config(tmp_path / "archive"), now=clock, sleep=clock.sleep).pull(_T0)

    assert captured["token"] == primary


@pytest.mark.asyncio
async def test_cold_pull_preserves_raw_fields_and_publishes_target(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    issue_path = f"{_BASE}/issues/1"
    api.json[issue_path]["comments"] = 1
    api.json[issue_path]["reactions"]["total_count"] = 1
    api.pages[f"{issue_path}/comments"] = [
        {
            "id": 101,
            "body": "comment",
            "updated_at": _iso(_T0 - timedelta(minutes=1)),
            "reactions": {"total_count": 0},
            "future_field": {"kept": 1},
        },
    ]
    api.pages[f"{issue_path}/timeline"] = [{"id": 201, "event": "renamed", "rename": {"from": "a", "to": "b"}}]
    api.pages[f"{issue_path}/events"] = [{"id": 301, "event": "labeled", "label": {"name": "bug"}}]
    api.pages[f"{issue_path}/reactions"] = [{"id": 401, "content": "heart", "user": {"login": "u"}}]
    clock = Clock(_T0)

    result = await _puller(_config(tmp_path / "archive"), api=api, now=clock, sleep=clock.sleep).pull(_T0)

    archive = tmp_path / "archive"
    current = await _current(archive)
    bundle = current[1].bundle
    catalog = current[1].summary
    assert bundle is not None
    assert bundle["schema_version"] == 5
    assert bundle["issue"]["unknown_detail_field"] == [1, {"raw": "yes"}]
    assert bundle["issue_comments"][0]["future_field"] == {"kept": 1}
    assert bundle["timeline"][0]["rename"] == {"from": "a", "to": "b"}
    assert bundle["events"][0]["label"]["name"] == "bug"
    assert bundle["reactions"][0]["content"] == "heart"
    assert bundle["issue_comment_reactions"] == {"101": []}
    assert catalog["unknown_summary_field"] == {"preserved": True}
    assert catalog == bundle["issue"]
    runs = await _runs(archive)
    assert len(runs) == 1
    assert runs[0].target_at == _iso(_T0)
    assert runs[0].completed_at == _iso(_T0)
    assert result.run_id == runs[0].id
    assert result.changed_items == 1
    assert result.catalog_items == 1
    assert runs[0].observed_until == _iso(_T0)
    catalog_call = next(call for call in api.calls if call[0] == "page" and call[1] == f"{_BASE}/issues")
    assert catalog_call[2] == {"state": "all", "sort": "created", "direction": "desc"}
    assert sum(call[0] == "count" for call in api.calls) == 1
    assert not any(call[0] == "json" and call[1] == issue_path for call in api.calls)
    assert api.catalog_accepts == ["application/vnd.github.raw+json"]


@pytest.mark.asyncio
async def test_cold_pull_reports_durable_catalog_and_bundles(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    api.add_issue(2, pull=True)
    api.json[f"{_BASE}/pulls/2"] = {
        "id": 20,
        "changed_files": 0,
        "commits": 0,
        "review_comments": 0,
    }
    events: list[PullProgress] = []

    result = await _puller(
        _config(tmp_path / "archive"),
        api=api,
        now=lambda: _T0,
        observer=events.append,
    ).pull(_T0)

    phases = [event.phase for event in events]
    assert phases[0:2] == ["waiting_lock", "checking"]
    assert {"closing_catalog", "finalizing", "done"} <= set(phases)
    completed = [
        event
        for event in events
        if event.phase == "closing_catalog" and event.bundles_completed == 2
    ][-1]
    assert completed.catalog_seen == completed.catalog_total == 2
    assert completed.catalog_complete
    assert completed.objects_completed == completed.objects_total == 2
    assert (completed.issues_completed, completed.pulls_completed) == (1, 1)
    assert (completed.latest_number, completed.latest_kind) == (2, "pull")
    assert events[-1].phase == "done"
    assert events[-1].catalog_total == result.catalog_items == 2
    assert events[-1].requests == result.requests


@pytest.mark.asyncio
async def test_pull_request_bundle_preserves_discussion_and_git_snapshot(tmp_path: Path) -> None:
    api = FakeAPI()
    git = FakeGitStore()
    api.add_issue(7, pull=True)
    issue_path = f"{_BASE}/issues/7"
    pull_path = f"{_BASE}/pulls/7"
    api.json[pull_path] = {
        "id": 700,
        "base": {"sha": "a" * 40},
        "changed_files": 1,
        "commits": 1,
        "head": {"sha": "b" * 40},
        "mergeable_state": "clean",
        "review_comments": 2,
        "requested_reviewers": [{"login": "alice"}],
        "requested_teams": [],
    }
    api.json[f"{pull_path}/requested_reviewers"] = {"users": [{"login": "alice"}], "teams": []}
    api.pages[f"{pull_path}/reviews"] = [{"id": 71, "state": "APPROVED", "body": "ship it"}]
    api.pages[f"{pull_path}/comments"] = [
        {"id": 72, "body": "nit", "path": "a.py", "reactions": {"total_count": 1}},
    ]
    api.pages[f"{_BASE}/pulls/comments/72/reactions"] = [{"id": 73, "content": "+1"}]
    api.pages[f"{pull_path}/commits"] = [{"sha": "abc", "commit": {"message": "change"}}]
    api.closing[7] = [_closing_issue(11)]

    await _puller(_config(tmp_path / "archive"), api=api, git=git, now=lambda: _T0).pull(_T0)

    bundle = (await _current(tmp_path / "archive"))[7].bundle
    assert bundle is not None
    pull = bundle["pull_request"]
    assert pull["detail"]["mergeable_state"] == "clean"
    assert pull["detail"]["review_comments"] == 2
    assert pull["reviews"][0]["state"] == "APPROVED"
    # GitHub can retain a historical aggregate after a review comment becomes unreadable.
    assert pull["review_comments"][0]["path"] == "a.py"
    assert pull["review_comment_reactions"]["72"][0]["content"] == "+1"
    assert pull["commits"][0]["commit"]["message"] == "change"
    assert pull["git"]["base_sha"] == "a" * 40
    assert pull["git"]["comparison_kind"] == "merge_base"
    assert pull["git"]["head_sha"] == "b" * 40
    assert pull["requested_reviewers"]["users"][0]["login"] == "alice"
    assert pull["closing_issues_references"] == [_closing_issue(11)]
    assert pull["api_sources"] == {
        "commits": {"source": "rest"},
        "detail": {"source": "rest"},
        "review_comments": {"source": "rest"},
        "review_comment_reactions": {"72": {"source": "rest"}},
        "reviews": {"source": "rest"},
    }
    assert {"files", "diff", "patch"}.isdisjoint(pull)
    assert git.prefetches == [[7]]
    assert git.captures == [7]
    assert not any(call[1] == f"{pull_path}/files" for call in api.calls)
    assert (await _current(tmp_path / "archive"))[7].kind == "pull"
    assert not any(call[1] == f"{issue_path}/comments" for call in api.calls)


@pytest.mark.parametrize("commit_count", [250, 251, 413])
@pytest.mark.asyncio
async def test_pull_commit_transport_crosses_the_documented_250_limit(
    tmp_path: Path,
    commit_count: int,
) -> None:
    api = FakeAPI()
    api.add_issue(7, pull=True)
    pull_path = f"{_BASE}/pulls/7"
    commits = [
        {
            "sha": f"{index:040x}",
            "commit": {"message": f"commit {index}"},
            "unknown_commit_field": {"index": index},
        }
        for index in range(commit_count)
    ]
    api.json[pull_path] = {
        "id": 700,
        "base": {"sha": "base-sha"},
        "head": {"sha": "head-sha"},
        "changed_files": 0,
        "commits": commit_count,
        "review_comments": 0,
    }
    if commit_count <= 250:
        api.pages[f"{pull_path}/commits"] = commits
    else:
        api.comparisons["base-sha", "head-sha"] = commits

    await _puller(
        _config(tmp_path / "archive"),
        api=api,
        now=lambda: _T0,
    ).pull(_T0)

    bundle = (await _current(tmp_path / "archive"))[7].bundle
    assert bundle is not None
    assert bundle["pull_request"]["commits"] == commits
    pull_commit_calls = [call for call in api.calls if call[1] == f"{pull_path}/commits"]
    compare_calls = [call for call in api.calls if call[0] == "compare"]
    assert bool(pull_commit_calls) is (commit_count <= 250)
    assert bool(compare_calls) is (commit_count > 250)


@pytest.mark.asyncio
async def test_large_pull_comparison_mismatch_resumes_the_same_task(tmp_path: Path) -> None:
    class ShortComparisonAPI(FakeAPI):
        short = True

        async def compare_commits(
            self,
            owner: str,
            repo: str,
            base: str,
            head: str,
        ) -> list[dict[str, Any]]:
            commits = await super().compare_commits(owner, repo, base, head)
            return commits[:-1] if self.short else commits

    api = ShortComparisonAPI()
    api.add_issue(7, pull=True)
    pull_path = f"{_BASE}/pulls/7"
    commits = [{"sha": f"{index:040x}"} for index in range(251)]
    api.json[pull_path] = {
        "id": 700,
        "base": {"sha": "base-sha"},
        "head": {"sha": "head-sha"},
        "changed_files": 0,
        "commits": 251,
        "review_comments": 0,
    }
    api.comparisons["base-sha", "head-sha"] = commits
    archive = tmp_path / "archive"
    events: list[PullProgress] = []
    puller = _puller(
        _config(archive),
        api=api,
        now=lambda: _T0,
        observer=events.append,
    )

    with pytest.raises(
        IncompleteGitHubDataError,
        match="pull #7 advertised 251 commits, got 250",
    ):
        await puller.pull(_T0)

    assert events[-1].detail == (
        "IncompleteGitHubDataError: pull #7 advertised 251 commits, got 250"
    )
    assert await _rows(
        archive,
        "SELECT number, completed FROM pull_tasks",
    ) == [{"number": 7, "completed": 0}]
    api.short = False

    result = await puller.pull(_T0)

    bundle = (await _current(archive))[7].bundle
    assert bundle is not None
    assert bundle["pull_request"]["commits"] == commits
    assert result.catalog_items == 1
    assert sum(
        call[0] == "page" and call[1] == f"{_BASE}/issues"
        for call in api.calls
    ) == 1
    assert sum(call[0] == "compare" for call in api.calls) == 2


@pytest.mark.asyncio
async def test_closing_issue_queries_batch_selected_pulls_per_catalog_page(tmp_path: Path) -> None:
    api = FakeAPI()
    for number in range(1, 203):
        pull = number % 2 == 0
        api.add_issue(number, pull=pull)
        if pull:
            api.json[f"{_BASE}/pulls/{number}"] = {
                "id": number * 10,
                "changed_files": 0,
                "commits": 0,
                "review_comments": 0,
            }

    result = await _puller(
        _config(tmp_path / "archive", concurrency=32),
        api=api,
        now=lambda: _T0,
    ).pull(_T0)

    calls = [call for call in api.calls if call[0] == "closing"]
    assert [len(call[2]["numbers"]) for call in calls] == [50, 50, 1]
    assert result.requests == 613


@pytest.mark.asyncio
async def test_quiet_pull_does_not_query_closing_issue_references(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(2, pull=True)
    api.json[f"{_BASE}/pulls/2"] = {
        "id": 20,
        "changed_files": 0,
        "commits": 0,
        "review_comments": 0,
    }
    clock = Clock(_T0)
    puller = _puller(_config(tmp_path / "archive"), api=api, now=clock, sleep=clock.sleep)
    await puller.pull(_T0)
    call_start = len(api.calls)
    clock.current += timedelta(hours=1)

    result = await puller.pull(clock.current)

    assert result.requests == 3
    assert not any(call[0] == "closing" for call in api.calls[call_start:])


@pytest.mark.asyncio
async def test_missing_closing_issue_result_aborts_publication(tmp_path: Path) -> None:
    class MissingClosingAPI(FakeAPI):
        async def closing_issue_references(
            self,
            owner: str,
            repo: str,
            numbers: list[int],
        ) -> dict[int, list[dict[str, Any]]]:
            self._called(
                "closing",
                "/graphql",
                {"owner": owner, "repo": repo, "numbers": numbers},
            )
            return {}

    api = MissingClosingAPI()
    api.add_issue(2, pull=True)
    api.json[f"{_BASE}/pulls/2"] = {
        "id": 20,
        "changed_files": 0,
        "commits": 0,
        "review_comments": 0,
    }
    archive = tmp_path / "archive"

    with pytest.raises(IncompleteGitHubDataError, match="has no closing issue references"):
        await _puller(_config(archive), api=api, now=lambda: _T0).pull(_T0)

    assert await _runs(archive) == []
    assert await _versions(archive) == []


@pytest.mark.asyncio
async def test_issue_bundle_preserves_stale_comment_aggregate_and_complete_list(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(8)
    issue_path = f"{_BASE}/issues/8"
    api.json[issue_path]["comments"] = 3
    api.pages[f"{issue_path}/comments"] = [
        {"id": 81, "body": "visible one", "reactions": {"total_count": 0}},
        {"id": 82, "body": "visible two", "reactions": {"total_count": 0}},
    ]

    archive = tmp_path / "archive"
    await _puller(_config(archive), api=api, now=lambda: _T0).pull(_T0)

    bundle = (await _current(archive))[8].bundle
    assert bundle is not None
    assert bundle["issue"]["comments"] == 3
    assert [comment["id"] for comment in bundle["issue_comments"]] == [81, 82]


@pytest.mark.asyncio
async def test_incremental_refreshes_existing_item_and_always_publishes(tmp_path: Path) -> None:
    api = FakeAPI()
    summary = api.add_issue(1)
    archive = tmp_path / "archive"
    clock = Clock(_T0)
    puller = _puller(_config(archive), api=api, now=clock, sleep=clock.sleep)
    first = await puller.pull(_T0)

    updated = _T0 + timedelta(minutes=1)
    summary["updated_at"] = _iso(updated)
    api.catalog[0]["updated_at"] = _iso(updated)
    api.json[f"{_BASE}/issues/1"]["updated_at"] = _iso(updated)
    api.json[f"{_BASE}/issues/1"]["body"] = "edited"
    clock.current = updated
    second = await puller.pull(updated)

    clock.current += timedelta(minutes=1)
    third = await puller.pull(clock.current)

    bundle = (await _current(archive))[1].bundle
    assert bundle is not None
    assert bundle["issue"]["body"] == "edited"
    assert first.changed_items == second.changed_items == 1
    assert third.changed_items == 0
    runs = await _runs(archive)
    assert len(runs) == 3
    assert runs[-1].target_at == _iso(clock.current)
    assert [run.changed_items for run in runs] == [1, 1, 0]


@pytest.mark.asyncio
async def test_bundle_validators_survive_puller_restart_and_reuse_only_paired_payload(
    tmp_path: Path,
) -> None:
    class ConditionalFakeAPI(FakeAPI):
        def __init__(self) -> None:
            super().__init__()
            self.charged_bundle_requests = 0
            self.reused: set[str] = set()

        async def get_json_cached(
            self,
            path: str,
            *,
            previous: Any | None,
            cache: dict[str, Any] | None,
            params: dict[str, Any] | None = None,
            accept: str | None = None,
        ) -> tuple[Any, dict[str, Any] | None]:
            current = await self.get_json(path, params=params, accept=accept)
            token = json.dumps(current, sort_keys=True)
            if previous is not None and cache == {"token": token}:
                self.reused.add(path)
                return deepcopy(previous), cache
            self.charged_bundle_requests += 1
            return current, {"token": token}

        async def paginate_cached(
            self,
            path: str,
            *,
            previous: list[dict[str, Any]] | None,
            cache: dict[str, Any] | None,
            params: dict[str, Any] | None = None,
            page_observer: Any = None,
        ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
            current = await self.paginate(path, params=params, page_observer=page_observer)
            token = json.dumps(current, sort_keys=True)
            if previous is not None and cache == {"token": token}:
                self.reused.add(path)
                return deepcopy(previous), cache
            self.charged_bundle_requests += max(1, math.ceil(len(current) / 100))
            return current, {"token": token}

    first_api = ConditionalFakeAPI()
    first_api.add_issue(1)
    archive = tmp_path / "archive"
    await _puller(_config(archive), api=first_api, now=lambda: _T0).pull(_T0)
    assert len(await _rows(archive, "SELECT bundle_digest FROM bundle_http_cache")) == 1

    second_api = ConditionalFakeAPI()
    second_api.catalog = deepcopy(first_api.catalog)
    second_api.json = deepcopy(first_api.json)
    second_api.pages = deepcopy(first_api.pages)
    changed_at = _T0 + timedelta(hours=1)
    summary = second_api.catalog[0]
    summary["updated_at"] = _iso(changed_at)
    summary["title"] = "changed root"
    second_api.json[f"{_BASE}/issues/1"]["updated_at"] = _iso(changed_at)
    second_api.json[f"{_BASE}/issues/1"]["title"] = "changed root"

    await _puller(_config(archive), api=second_api, now=lambda: changed_at).pull(changed_at)

    assert second_api.reused == {
        f"{_BASE}/issues/1/events",
        f"{_BASE}/issues/1/timeline",
    }
    assert second_api.charged_bundle_requests == 0
    assert len(await _rows(archive, "SELECT bundle_digest FROM bundle_http_cache")) == 2
    bundle = (await _current(archive))[1].bundle
    assert bundle is not None
    assert bundle["issue"]["title"] == "changed root"


@pytest.mark.asyncio
async def test_cold_pull_reuses_catalog_rows_and_skips_proven_empty_endpoints(
    tmp_path: Path,
) -> None:
    api = FakeAPI()
    rng = random.Random(20260902)  # noqa: S311 - The workload must be reproducible.
    for number in range(1, 361):
        pull = number % 5 == 0
        api.add_issue(number, pull=pull)
        issue_path = f"{_BASE}/issues/{number}"
        comments = [
            {
                "id": number * 100 + offset,
                "body": f"issue comment {number}:{offset}",
                "created_at": _iso(_T0 - timedelta(minutes=offset + 1)),
                "updated_at": _iso(_T0 - timedelta(minutes=offset + 1)),
                "reactions": {"total_count": 0},
            }
            for offset in range(rng.randrange(5))
        ]
        api.pages[f"{issue_path}/comments"] = comments
        api.json[issue_path]["comments"] = len(comments)
        api.pages[f"{issue_path}/timeline"] = [{"id": number * 1000, "event": "labeled"}]
        api.pages[f"{issue_path}/events"] = [{"id": number * 1000 + 1, "event": "labeled"}]
        if number % 7 == 0:
            api.json[issue_path]["reactions"]["total_count"] = 1
            api.pages[f"{issue_path}/reactions"] = [{"id": number * 1000 + 2, "content": "+1"}]
        if not pull:
            continue
        pull_path = f"{_BASE}/pulls/{number}"
        users = [{"id": number, "login": f"user-{number}"}] if number % 10 == 0 else []
        review_comments = [
            {
                "id": 1_000_000 + number * 100 + offset,
                "body": f"review comment {number}:{offset}",
                "created_at": _iso(_T0 - timedelta(minutes=offset + 1)),
                "updated_at": _iso(_T0 - timedelta(minutes=offset + 1)),
                "reactions": {"total_count": 0},
            }
            for offset in range(rng.randrange(4))
        ]
        api.json[pull_path] = {
            "id": number * 10,
            "changed_files": 0,
            "commits": 0,
            "review_comments": len(review_comments),
            "requested_reviewers": users,
            "requested_teams": [],
        }
        api.json[f"{pull_path}/requested_reviewers"] = {"users": users, "teams": []}
        api.pages[f"{pull_path}/comments"] = review_comments

    await _puller(
        _config(tmp_path / "archive", concurrency=32),
        api=api,
        now=lambda: _T0,
    ).pull(_T0)

    repository_feeds = {f"{_BASE}/issues/comments", f"{_BASE}/pulls/comments"}
    assert not any(call[1] in repository_feeds for call in api.calls)
    fetched_roots = [
        call
        for call in api.calls
        if call[0] == "json" and re.fullmatch(rf"{re.escape(_BASE)}/issues/\d+", call[1])
    ]
    assert fetched_roots == []
    nonempty_comments = sum(bool(api.pages[f"{_BASE}/issues/{number}/comments"]) for number in range(1, 361))
    comment_calls = sum(
        call[0] == "page" and re.fullmatch(rf"{re.escape(_BASE)}/issues/\d+/comments", call[1]) is not None
        for call in api.calls
    )
    assert comment_calls == nonempty_comments
    assert not any(call[1].endswith("/requested_reviewers") for call in api.calls)


@pytest.mark.asyncio
async def test_requested_team_uses_richer_dedicated_response(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(7, pull=True)
    pull_path = f"{_BASE}/pulls/7"
    simple_team = {"id": 1, "name": "core", "slug": "core"}
    rich_team = simple_team | {"privacy": "closed", "permission": "push"}
    api.json[pull_path] = {
        "id": 70,
        "changed_files": 0,
        "commits": 0,
        "review_comments": 0,
        "requested_reviewers": [],
        "requested_teams": [simple_team],
    }
    api.json[f"{pull_path}/requested_reviewers"] = {"users": [], "teams": [rich_team]}

    await _puller(_config(tmp_path / "archive"), api=api, now=lambda: _T0).pull(_T0)

    bundle = (await _current(tmp_path / "archive"))[7].bundle
    assert bundle is not None
    assert bundle["pull_request"]["requested_reviewers"]["teams"] == [rich_team]
    assert sum(call[1] == f"{pull_path}/requested_reviewers" for call in api.calls) == 1


@pytest.mark.asyncio
async def test_large_random_observable_issue_and_comment_churn(tmp_path: Path) -> None:
    rng = random.Random(20260901)  # noqa: S311 - The workload must be reproducible, not unpredictable.
    api = FakeAPI()
    _seed_churn_api(api, 96)
    clock = Clock(_T0)
    archive = tmp_path / "churn"
    puller = _puller(
        _config(archive, concurrency=32),
        api=api,
        now=clock,
        sleep=clock.sleep,
    )
    await puller.pull(_T0)

    next_number = 97
    next_comment = 1_000_000
    added_total = 0
    deleted_total = 0
    added_pulls = 0
    deleted_pulls = 0
    comment_additions = 0
    comment_deletions = 0
    pull_comment_operations = 0
    observed_numbers = set(range(1, 97))
    for epoch in range(1, 21):
        target = _T0 + timedelta(hours=epoch)
        changed_at = target - timedelta(minutes=1)
        current = sorted(item["number"] for item in api.catalog)
        deleted = set(rng.sample(current, 2)) if epoch % 2 == 0 else set()
        old_survivors = [number for number in current if number not in deleted]
        selected = rng.sample(old_survivors, 12)
        operations: list[tuple[int, str, int]] = []
        for number in selected:
            comments = api.pages.get(f"{_BASE}/issues/{number}/comments", [])
            pull_comment_operations += number % 3 == 0
            if comments and rng.random() < 0.5:
                comment = rng.choice(comments)
                operations.append((number, "delete", int(comment["id"])))
                comment_deletions += 1
            else:
                operations.append((number, "add", next_comment))
                next_comment += 1
                comment_additions += 1
        added = tuple(range(next_number, next_number + 3))
        next_number += len(added)
        added_total += len(added)
        observed_numbers.update(added)
        deleted_total += len(deleted)
        added_pulls += sum(number % 3 == 0 for number in added)
        deleted_pulls += sum(number % 3 == 0 for number in deleted)
        _apply_churn_epoch(
            api,
            deleted=deleted,
            added=added,
            comment_operations=operations,
            changed_at=changed_at,
        )

        clock.current = target
        call_start = len(api.calls)
        await puller.pull(target)
        epoch_calls = api.calls[call_start:]
        root_scans = sum(call[0] == "page" and call[1] == f"{_BASE}/issues" for call in epoch_calls)
        assert root_scans == 1
        fetched_roots = {
            int(call[1].rsplit("/", 1)[1])
            for call in epoch_calls
            if call[0] == "json" and call[1].startswith(f"{_BASE}/issues/")
        }
        expected_roots = {number for number, operation, _ in operations if operation == "add"}
        assert fetched_roots == expected_roots
        heads = await _current(archive)
        present = {number for number, head in heads.items() if head.present}
        assert present == observed_numbers
        for number in {item["number"] for item in api.catalog}:
            bundle = heads[number].bundle
            assert bundle is not None
            expected_comments = api.pages.get(f"{_BASE}/issues/{number}/comments", [])
            assert [(item["id"], item["body"]) for item in bundle["issue_comments"]] == [
                (item["id"], item["body"]) for item in expected_comments
            ]
            if number % 3 == 0:
                assert bundle["pull_request"]["closing_issues_references"] == api.closing.get(
                    number,
                    [],
                )
        assert all(heads[number].present for number in deleted)

    assert (added_total, deleted_total) == (60, 20)
    assert added_pulls > 0
    assert deleted_pulls > 0
    assert comment_additions + comment_deletions == 240
    assert comment_additions > 0
    assert comment_deletions > 0
    assert pull_comment_operations > 0
    assert len(api.catalog) == 136
    assert len(await _current(archive)) == 156
    assert len(await _runs(archive)) == 21


@pytest.mark.asyncio
async def test_silent_parent_deletion_does_not_trigger_full_catalog_scan(
    tmp_path: Path,
) -> None:
    api = FakeAPI()
    for number in range(1, 251):
        api.add_issue(number)
    archive = tmp_path / "archive"
    clock = Clock(_T0)
    puller = _puller(_config(archive), api=api, now=clock, sleep=clock.sleep)
    await puller.pull(_T0)

    api.catalog = [item for item in api.catalog if item["number"] != 125]
    clock.current += timedelta(hours=1)
    call_start = len(api.calls)
    result = await puller.pull(clock.current)

    calls = api.calls[call_start:]
    fetched_roots = [call for call in calls if call[0] == "json" and call[1].startswith(f"{_BASE}/issues/")]
    assert fetched_roots == []
    assert sum(call[0] == "page" and call[1] == f"{_BASE}/issues" for call in calls) == 1
    assert result.requests == 3
    assert result.catalog_items == 250
    assert (await _current(archive))[125].present is True


@pytest.mark.parametrize("catalog_size", [1, 250])
@pytest.mark.asyncio
async def test_quiet_increment_cost_is_three_requests_independent_of_catalog_size(
    tmp_path: Path,
    catalog_size: int,
) -> None:
    api = FakeAPI()
    for number in range(1, catalog_size + 1):
        api.add_issue(number)
    archive = tmp_path / str(catalog_size)
    clock = Clock(_T0)
    puller = _puller(_config(archive), api=api, now=clock, sleep=clock.sleep)
    cold = await puller.pull(_T0)
    baseline = math.ceil(catalog_size / 100) + 5 * catalog_size
    expected = math.ceil(catalog_size / 100) + 1 + 2 * catalog_size
    assert cold.requests == expected
    assert cold.requests < baseline
    assert sum(
        call[0] == "page" and call[1] == f"{_BASE}/issues" for call in api.calls
    ) == math.ceil(catalog_size / 100)
    assert sum(call[0] == "count" for call in api.calls) == 1
    clock.current += timedelta(hours=1)
    call_start = len(api.calls)

    result = await puller.pull(clock.current)

    assert result.requests == 3
    assert result.requests / 5000 == 0.0006
    discovery = api.calls[call_start:]
    assert {call[1] for call in discovery} == {
        f"{_BASE}/issues",
        f"{_BASE}/issues/comments",
        f"{_BASE}/pulls/comments",
    }
    root_call = next(call for call in discovery if call[1] == f"{_BASE}/issues")
    assert root_call[2]["sort"] == "updated"
    assert root_call[2]["since"] == _iso(_T0 - timedelta(seconds=2))


@pytest.mark.asyncio
async def test_same_second_catalog_change_is_not_lost(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1, updated_at=_T0)
    archive = tmp_path / "archive"
    clock = Clock(_T0)
    puller = _puller(_config(archive), api=api, now=clock, sleep=clock.sleep)
    await puller.pull(_T0)
    api.catalog[0]["title"] = "same-second edit"
    api.json[f"{_BASE}/issues/1"]["title"] = "same-second edit"

    result = await puller.pull(_T0 + timedelta(microseconds=1))

    bundle = (await _current(archive))[1].bundle
    assert bundle is not None
    assert result.changed_items == 1
    assert bundle["issue"]["title"] == "same-second edit"


@pytest.mark.asyncio
async def test_repeated_idempotency_key_returns_the_committed_run(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    events: list[PullProgress] = []
    puller = _puller(_config(archive), api=api, now=lambda: _T0, observer=events.append)
    first = await puller.pull(_T0)
    calls = len(api.calls)
    events.clear()

    repeated = await puller.pull(_T0)

    assert repeated == first
    assert len(api.calls) == calls
    runs = await _runs(archive)
    assert [run.target_at for run in runs] == [_iso(_T0)]
    assert [run.changed_items for run in runs] == [1]
    assert len(await _versions(archive)) == 1
    assert [event.phase for event in events] == ["waiting_lock", "checking", "done"]
    assert events[-1].reused is True


@pytest.mark.asyncio
async def test_observer_failure_cannot_change_pull_or_archive(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    calls = 0

    def broken_observer(_: PullProgress) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("observer failed")

    archive = tmp_path / "archive"
    result = await _puller(
        _config(archive),
        api=api,
        now=lambda: _T0,
        observer=broken_observer,
    ).pull(_T0)

    assert calls == 1
    assert result.catalog_items == 1
    assert set(await _current(archive)) == {1}


@pytest.mark.asyncio
async def test_idempotent_hit_does_not_construct_an_api_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = FakeAPI()
    api.add_issue(1)
    constructions = 0

    def make_api(**_: Any) -> FakeAPI:
        nonlocal constructions
        constructions += 1
        return api

    monkeypatch.setattr("gh_puller.github.puller.GitHubAPI", make_api)
    puller = _puller(_config(tmp_path / "archive"), now=lambda: _T0)

    first = await puller.pull(_T0)
    repeated = await puller.pull(_T0)

    assert repeated == first
    assert constructions == 1


@pytest.mark.asyncio
async def test_target_is_the_complete_idempotency_key(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    puller = _puller(_config(archive), api=api, now=lambda: _T0)

    first = await puller.pull(_T0)
    repeated = await puller.pull(_T0)

    assert repeated == first
    assert [(run.id, run.target_at) for run in await _runs(archive)] == [(first.run_id, _iso(_T0))]


@pytest.mark.asyncio
async def test_concurrent_duplicate_pulls_publish_one_run(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    puller = _puller(_config(archive), api=api, now=lambda: _T0)

    left, right = await asyncio.gather(puller.pull(_T0), puller.pull(_T0))

    assert left == right
    assert len(await _runs(archive)) == 1
    assert len(await _versions(archive)) == 1


@pytest.mark.asyncio
async def test_comment_signal_refreshes_parent_when_summary_is_unchanged(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(3)
    archive = tmp_path / "archive"
    clock = Clock(_T0)
    puller = _puller(_config(archive), api=api, now=clock, sleep=clock.sleep)
    await puller.pull(_T0)

    issue_path = f"{_BASE}/issues/3"
    api.pages[f"{_BASE}/issues/comments"] = [
        {
            "id": 31,
            "issue_url": issue_path,
            "created_at": _iso(_T0 + timedelta(seconds=1)),
            "updated_at": _iso(_T0 + timedelta(seconds=1)),
        },
    ]
    api.pages[f"{issue_path}/comments"] = [
        {"id": 31, "body": "late comment", "reactions": {"total_count": 0}},
    ]
    clock.current += timedelta(minutes=1)

    result = await puller.pull(clock.current)

    bundle = (await _current(archive))[3].bundle
    assert bundle is not None
    assert result.changed_items == 1
    assert bundle["issue_comments"][0]["body"] == "late comment"
    signal_call = next(call for call in api.calls if call[1] == f"{_BASE}/issues/comments")
    assert signal_call[2]["since"] == _iso(_T0 - timedelta(seconds=2))


@pytest.mark.asyncio
async def test_future_target_prefetches_then_closes_with_one_target_run(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1, created_at=_T0 - timedelta(days=1))
    target = _T0 + timedelta(minutes=10)
    clock = Clock(_T0)

    def add_late_issue() -> None:
        api.catalog[0]["updated_at"] = _iso(target)
        api.json[f"{_BASE}/issues/1"]["updated_at"] = _iso(target)
        api.json[f"{_BASE}/issues/1"]["body"] = "changed after prefetch"
        api.add_issue(2, created_at=target, updated_at=target)

    clock.on_sleep = add_late_issue
    archive = tmp_path / "archive"
    events: list[PullProgress] = []
    result = await _puller(
        _config(archive),
        api=api,
        now=clock,
        sleep=clock.sleep,
        observer=events.append,
    ).pull(target)

    assert clock.sleeps == [600]
    assert result.target_at == target
    assert result.changed_items == 3
    assert set(await _current(archive)) == {1, 2}
    assert not any(call[0] == "json" and call[1] == f"{_BASE}/issues/1" for call in api.calls)
    issue_versions = [version for version in await _versions(archive) if version.number == 1]
    assert len(issue_versions) == 2
    assert issue_versions[0].bundle is not None
    assert issue_versions[1].bundle is not None
    assert issue_versions[0].bundle["issue"]["body"] == "body"
    assert issue_versions[1].bundle["issue"]["body"] == "changed after prefetch"
    runs = await _runs(archive)
    assert len(runs) == 1
    assert runs[0].target_at == _iso(target)
    phases = [event.phase for event in events]
    assert phases.index("prefetch_catalog") < phases.index("waiting_target")
    assert phases.index("waiting_target") < phases.index("closing_catalog")
    prefetch = next(event for event in events if event.phase == "prefetch_catalog")
    closing = next(event for event in events if event.phase == "closing_catalog")
    assert prefetch.pass_at == _T0
    assert closing.pass_at == target


@pytest.mark.asyncio
async def test_future_wait_serializes_the_single_archive_writer(tmp_path: Path) -> None:
    class PausingClock:
        def __init__(self) -> None:
            self.current = _T0
            self.sleeping = asyncio.Event()
            self.resume = asyncio.Event()

        def __call__(self) -> datetime:
            return self.current

        async def sleep(self, seconds: float) -> None:
            self.sleeping.set()
            await self.resume.wait()
            self.current += timedelta(seconds=seconds)

    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    clock = PausingClock()
    puller = _puller(_config(archive), api=api, now=clock, sleep=clock.sleep)
    future_target = _T0 + timedelta(minutes=10)
    future = asyncio.create_task(puller.pull(future_target))
    await asyncio.wait_for(clock.sleeping.wait(), timeout=1)

    immediate_task = asyncio.create_task(puller.pull(_T0))
    await asyncio.sleep(0)
    assert not immediate_task.done()
    clock.resume.set()
    scheduled = await asyncio.wait_for(future, timeout=1)
    immediate = await asyncio.wait_for(immediate_task, timeout=1)

    assert immediate.target_at == _T0
    assert scheduled.target_at == future_target
    assert [run.target_at for run in await _runs(archive)] == [
        _iso(future_target),
        _iso(_T0),
    ]


@pytest.mark.asyncio
async def test_default_target_is_frozen_before_first_await(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    clock = Clock(_T0)

    def advance_clock() -> None:
        clock.current = _T0 + timedelta(minutes=5)
        api.on_request = None

    api.on_request = advance_clock
    archive = tmp_path / "archive"
    result = await _puller(_config(archive), api=api, now=clock, sleep=clock.sleep).pull()

    assert result.target_at == _T0
    assert result.completed_at == _T0 + timedelta(minutes=5)
    assert result.lag_seconds == 300
    run = (await _runs(archive))[0]
    assert run.target_at == _iso(_T0)
    assert run.completed_at == _iso(result.completed_at)


@pytest.mark.asyncio
async def test_interrupted_cold_pull_resumes_staged_bundles_without_publication(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    api.add_issue(2, pull=True)
    api.json[f"{_BASE}/pulls/2"] = {
        "id": 20,
        "changed_files": 0,
        "commits": 0,
        "review_comments": 0,
    }
    api.add_issue(3)
    api.fail_once.add(f"{_BASE}/issues/3/timeline")
    archive = tmp_path / "archive"
    puller = _puller(_config(archive, concurrency=1), api=api, now=lambda: _T0)

    with pytest.raises(RuntimeError, match="injected failure"):
        await puller.pull(_T0)

    assert await _runs(archive) == []
    assert await _versions(archive) == []
    assert [head async for head in iter_heads(archive)] == []
    staged = await _rows(
        archive,
        """
        SELECT v.number
        FROM resource_versions AS v
        JOIN pull_runs AS r ON r.id = v.run_id
        WHERE r.status = 'pending'
        """,
    )
    assert {row["number"] for row in staged} == {1, 2}
    assert sum(call[1] == f"{_BASE}/issues/1/timeline" for call in api.calls) == 1
    assert sum(call[1] == f"{_BASE}/issues/2/timeline" for call in api.calls) == 1

    events: list[PullProgress] = []
    result = await _puller(
        _config(archive, concurrency=1),
        api=api,
        now=lambda: _T0,
        observer=events.append,
    ).pull(_T0)

    assert result.changed_items == 3
    assert sum(call[1] == f"{_BASE}/issues/1/timeline" for call in api.calls) == 1
    assert sum(call[1] == f"{_BASE}/issues/2/timeline" for call in api.calls) == 1
    assert set(await _current(archive)) == {1, 2, 3}
    assert len(await _runs(archive)) == 1
    restored = next(
        event
        for event in events
        if event.phase == "closing_catalog"
        and event.bundles_completed == 2
        and event.objects_completed == 2
    )
    assert restored.catalog_complete
    assert restored.objects_total == 3
    assert (restored.issues_completed, restored.pulls_completed) == (1, 1)
    assert (restored.latest_number, restored.latest_kind) == (2, "pull")
    assert events[-1].bundles_completed == 3
    assert events[-1].objects_completed == events[-1].objects_total == 3


@pytest.mark.asyncio
async def test_catalog_producer_runs_ahead_of_blocked_bundle_consumers(tmp_path: Path) -> None:
    class BlockingTimelineAPI(FakeAPI):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def paginate(
            self,
            path: str,
            *,
            params: dict[str, Any] | None = None,
            page_observer: Any = None,
        ) -> list[dict[str, Any]]:
            if path.endswith("/timeline"):
                self.started.set()
                await self.release.wait()
            return await super().paginate(path, params=params, page_observer=page_observer)

    api = BlockingTimelineAPI()
    for number in range(1, 251):
        api.add_issue(number)
    archive = tmp_path / "archive"
    pull = asyncio.create_task(
        _puller(
            _config(archive, concurrency=4),
            api=api,
            now=lambda: _T0,
        ).pull(_T0),
    )
    await asyncio.wait_for(api.started.wait(), timeout=1)

    async def catalog_is_durable() -> None:
        while True:
            rows = await _rows(
                archive,
                "SELECT catalog_complete, catalog_items FROM pull_passes",
            )
            if rows == [{"catalog_complete": 1, "catalog_items": 250}]:
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(catalog_is_durable(), timeout=1)
    tasks = await _rows(archive, "SELECT count(*) AS count FROM pull_tasks")
    assert tasks == [{"count": 250}]
    assert api.catalog_accepts == ["application/vnd.github.raw+json"] * 3
    assert not pull.done()

    pull.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pull


@pytest.mark.asyncio
async def test_interrupted_catalog_resumes_at_its_durable_page_cursor(tmp_path: Path) -> None:
    class ThirdPageFailureAPI(FakeAPI):
        def __init__(self) -> None:
            super().__init__()
            self.catalog_paths: list[str] = []
            self.failed_page = False

        async def get_page(
            self,
            path: str,
            *,
            params: dict[str, Any] | None = None,
            accept: str | None = None,
        ) -> GitHubPage:
            self.catalog_paths.append(path)
            page = await super().get_page(path, params=params, accept=accept)
            if "__offset=200" in path and not self.failed_page:
                self.failed_page = True
                raise RuntimeError("interrupt third catalog page")
            return page

    api = ThirdPageFailureAPI()
    for number in range(1, 251):
        api.add_issue(number)
    archive = tmp_path / "archive"
    puller = _puller(_config(archive, concurrency=16), api=api, now=lambda: _T0)

    with pytest.raises(RuntimeError, match="interrupt third catalog page"):
        await puller.pull(_T0)

    state = await _rows(
        archive,
        "SELECT catalog_complete, catalog_items, next_url FROM pull_passes",
    )
    assert state[0]["catalog_complete"] == 0
    assert state[0]["catalog_items"] == 200
    assert "__offset=200" in state[0]["next_url"]
    assert await _rows(
        archive,
        "SELECT count(*) AS count FROM pull_tasks WHERE completed = 1",
    ) == [{"count": 200}]
    previous_calls = len(api.calls)
    previous_paths = len(api.catalog_paths)

    result = await puller.pull(_T0)

    assert api.catalog_paths[previous_paths:] == [state[0]["next_url"]]
    resumed_calls = api.calls[previous_calls:]
    assert not any(
        call[1] == f"{_BASE}/issues/250/timeline" for call in resumed_calls
    )
    assert result.catalog_items == 250
    assert await _rows(archive, "SELECT count(*) AS count FROM pull_passes") == [{"count": 0}]
    assert await _rows(archive, "SELECT count(*) AS count FROM pull_tasks") == [{"count": 0}]


@pytest.mark.asyncio
async def test_expired_durable_cursor_rescans_catalog_but_reuses_completed_bundles(
    tmp_path: Path,
) -> None:
    class ExpiringCursorAPI(FakeAPI):
        def __init__(self) -> None:
            super().__init__()
            self.catalog_paths: list[str] = []
            self.cursor_attempts = 0

        async def get_page(
            self,
            path: str,
            *,
            params: dict[str, Any] | None = None,
            accept: str | None = None,
        ) -> GitHubPage:
            self.catalog_paths.append(path)
            page = await super().get_page(path, params=params, accept=accept)
            if "__offset=100" not in path:
                return page
            self.cursor_attempts += 1
            if self.cursor_attempts == 1:
                raise RuntimeError("interrupt second catalog page")
            if self.cursor_attempts == 2:
                raise GitHubAPIError("expired catalog cursor", status_code=422)
            return page

    api = ExpiringCursorAPI()
    for number in range(1, 102):
        api.add_issue(number)
    archive = tmp_path / "archive"
    puller = _puller(_config(archive, concurrency=16), api=api, now=lambda: _T0)

    with pytest.raises(RuntimeError, match="interrupt second catalog page"):
        await puller.pull(_T0)
    previous_calls = len(api.calls)
    previous_paths = len(api.catalog_paths)

    result = await puller.pull(_T0)

    paths = api.catalog_paths[previous_paths:]
    assert "__offset=100" in paths[0]
    assert paths[1] == f"{_BASE}/issues"
    assert "__offset=100" in paths[2]
    assert not any(
        call[1] == f"{_BASE}/issues/101/timeline"
        for call in api.calls[previous_calls:]
    )
    assert result.catalog_items == 101


@pytest.mark.asyncio
async def test_future_closure_does_not_infer_a_silent_parent_deletion(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    clock = Clock(_T0)
    target = _T0 + timedelta(hours=1)

    async def interrupt_wait(_: float) -> None:
        raise RuntimeError("interrupt before target")

    with pytest.raises(RuntimeError, match="interrupt before target"):
        await _puller(
            _config(archive),
            api=api,
            now=clock,
            sleep=interrupt_wait,
        ).pull(target)

    api.catalog.clear()
    clock.current = target
    events: list[PullProgress] = []
    result = await _puller(
        _config(archive),
        api=api,
        now=clock,
        sleep=clock.sleep,
        observer=events.append,
    ).pull(target)

    restored = next(event for event in events if event.phase == "closing_catalog" and event.bundles_completed == 1)
    assert (restored.latest_number, restored.latest_kind) == (1, "issue")
    assert events[-1].tombstones == 0
    assert result.catalog_items == 1
    assert (await _current(archive))[1].present is True


@pytest.mark.asyncio
async def test_cancellation_aborts_work_and_does_not_publish(tmp_path: Path) -> None:
    class BlockingAPI(FakeAPI):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def paginate(
            self,
            path: str,
            *,
            params: dict[str, Any] | None = None,
            page_observer: Any = None,
        ) -> list[dict[str, Any]]:
            if path == f"{_BASE}/issues/1/timeline":
                self.started.set()
                await self.release.wait()
            return await super().paginate(path, params=params, page_observer=page_observer)

    api = BlockingAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    task = asyncio.create_task(_puller(_config(archive), api=api, now=lambda: _T0).pull(_T0))
    await asyncio.wait_for(api.started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _runs(archive) == []
    pending = await _rows(archive, "SELECT status FROM pull_runs")
    assert pending == [{"status": "pending"}]


@pytest.mark.asyncio
async def test_candidate_executor_keeps_only_a_concurrency_sized_task_window(tmp_path: Path) -> None:
    class BlockingAPI(FakeAPI):
        def __init__(self, concurrency: int) -> None:
            super().__init__()
            self.concurrency = concurrency
            self.started = 0
            self.window_full = asyncio.Event()
            self.release = asyncio.Event()

        async def paginate(
            self,
            path: str,
            *,
            params: dict[str, Any] | None = None,
            page_observer: Any = None,
        ) -> list[dict[str, Any]]:
            if path.endswith("/timeline"):
                self.started += 1
                if self.started == self.concurrency:
                    self.window_full.set()
                await self.release.wait()
            return await super().paginate(path, params=params, page_observer=page_observer)

    concurrency = 3
    api = BlockingAPI(concurrency)
    for number in range(1, 201):
        api.add_issue(number)
    archive = tmp_path / "archive"
    pull = asyncio.create_task(
        _puller(
            _config(archive, concurrency=concurrency),
            api=api,
            now=lambda: _T0,
        ).pull(_T0),
    )
    await asyncio.wait_for(api.window_full.wait(), timeout=1)

    candidate_tasks = [
        task
        for task in asyncio.all_tasks()
        if getattr(task.get_coro(), "__qualname__", "") == "GitHubPuller._fetch_candidates.<locals>.fetch"
    ]
    assert api.started == concurrency
    assert len(candidate_tasks) == concurrency
    pull.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pull


@pytest.mark.asyncio
async def test_slow_oldest_candidate_does_not_block_newer_durable_bundles(tmp_path: Path) -> None:
    class SlowOldestAPI(FakeAPI):
        def __init__(self) -> None:
            super().__init__()
            self.oldest_started = asyncio.Event()
            self.release_oldest = asyncio.Event()

        async def paginate(
            self,
            path: str,
            *,
            params: dict[str, Any] | None = None,
            page_observer: Any = None,
        ) -> list[dict[str, Any]]:
            if path == f"{_BASE}/issues/1/timeline":
                self.oldest_started.set()
                await self.release_oldest.wait()
            return await super().paginate(path, params=params, page_observer=page_observer)

    api = SlowOldestAPI()
    for number in range(1, 4):
        api.add_issue(number)
    durable_newer = asyncio.Event()

    def observe(progress: PullProgress) -> None:
        if progress.phase == "closing_catalog" and progress.bundles_completed == 2:
            durable_newer.set()

    archive = tmp_path / "archive"
    pull = asyncio.create_task(
        _puller(
            _config(archive, concurrency=2),
            api=api,
            now=lambda: _T0,
            observer=observe,
        ).pull(_T0),
    )
    await asyncio.wait_for(api.oldest_started.wait(), timeout=1)
    await asyncio.wait_for(durable_newer.wait(), timeout=1)

    staged = await _rows(archive, "SELECT number FROM resource_versions ORDER BY number")
    assert staged == [{"number": 2}, {"number": 3}]
    assert not pull.done()

    api.release_oldest.set()
    await pull
    assert set(await _current(archive)) == {1, 2, 3}


@pytest.mark.asyncio
async def test_missing_catalog_item_remains_as_last_observed_fact(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    clock = Clock(_T0)
    puller = _puller(_config(archive), api=api, now=clock, sleep=clock.sleep)
    await puller.pull(_T0)
    original = (await _current(archive))[1].bundle
    api.catalog.clear()
    clock.current += timedelta(minutes=1)

    result = await puller.pull(clock.current)

    record = (await _current(archive))[1]
    assert record.present is True
    assert record.missing_since is None
    assert record.bundle == original
    assert result.catalog_items == 1
    assert [head.number async for head in iter_heads(archive, present_only=True)] == [1]


@pytest.mark.asyncio
async def test_selected_parent_absence_stages_a_tombstone(tmp_path: Path) -> None:
    class MissingParentAPI(FakeAPI):
        missing = False

        async def get_json(
            self,
            path: str,
            *,
            params: dict[str, Any] | None = None,
            accept: str | None = None,
        ) -> Any:
            if self.missing and path == f"{_BASE}/issues/1":
                self._called("json", path, params)
                raise GitHubAPIError(
                    "parent missing",
                    status_code=404,
                    url=f"https://api.github.test{path}",
                )
            return await super().get_json(path, params=params, accept=accept)

    api = MissingParentAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    clock = Clock(_T0)
    puller = _puller(_config(archive), api=api, now=clock, sleep=clock.sleep)
    await puller.pull(_T0)
    clock.current += timedelta(hours=1)
    api.pages[f"{_BASE}/issues/comments"] = [
        {
            "id": 1,
            "created_at": _iso(clock.current),
            "updated_at": _iso(clock.current),
            "issue_url": f"{_BASE}/issues/1",
        },
    ]
    api.catalog.clear()
    api.missing = True

    result = await puller.pull(clock.current)

    record = (await _current(archive))[1]
    assert record.present is False
    assert record.missing_since == _iso(clock.current)
    assert result.catalog_items == 0


@pytest.mark.asyncio
async def test_large_pull_never_requests_the_capped_file_collection(tmp_path: Path) -> None:
    api = FakeAPI()
    git = FakeGitStore()
    api.add_issue(9, pull=True)
    pull_path = f"{_BASE}/pulls/9"
    api.json[pull_path] = {
        "id": 900,
        "base": {"sha": "a" * 40},
        "changed_files": 6_856,
        "commits": 0,
        "head": {"sha": "b" * 40},
        "review_comments": 0,
    }
    archive = tmp_path / "archive"

    await _puller(_config(archive), api=api, git=git, now=lambda: _T0).pull(_T0)

    bundle = (await _current(archive))[9].bundle
    assert bundle is not None
    assert bundle["pull_request"]["detail"]["changed_files"] == 6_856
    assert bundle["pull_request"]["git"]["head_sha"] == "b" * 40
    assert not any(call[1] == f"{pull_path}/files" for call in api.calls)


@pytest.mark.asyncio
async def test_failed_git_capture_resumes_the_same_pull_task(tmp_path: Path) -> None:
    class FailOnceGitStore(FakeGitStore):
        failed = False

        async def capture(self, number: int, pull: dict[str, Any]) -> dict[str, Any]:
            if not self.failed:
                self.failed = True
                raise GitStoreError("injected Git failure")
            return await super().capture(number, pull)

    api = FakeAPI()
    git = FailOnceGitStore()
    api.add_issue(9, pull=True)
    pull_path = f"{_BASE}/pulls/9"
    api.json[pull_path] = {
        "id": 900,
        "base": {"sha": "a" * 40},
        "changed_files": 6_856,
        "commits": 0,
        "head": {"sha": "b" * 40},
        "review_comments": 0,
    }
    archive = tmp_path / "archive"
    puller = _puller(_config(archive), api=api, git=git, now=lambda: _T0)

    with pytest.raises(GitStoreError, match="injected Git failure"):
        await puller.pull(_T0)

    assert await _rows(archive, "SELECT number, completed FROM pull_tasks") == [
        {"number": 9, "completed": 0},
    ]

    await puller.pull(_T0)

    assert sum(call[0] == "page" and call[1] == f"{_BASE}/issues" for call in api.calls) == 1
    assert (await _current(archive))[9].bundle is not None


@pytest.mark.asyncio
async def test_naive_target_is_rejected_before_archive_creation(tmp_path: Path) -> None:
    destination = tmp_path / "archive"
    target = datetime(2026, 8, 1, 12)  # noqa: DTZ001  # The rejection fixture must be naive.

    with pytest.raises(ValueError, match="timezone-aware"):
        await _puller(_config(destination), api=FakeAPI(), now=lambda: _T0).pull(target)

    assert not destination.exists()


@pytest.mark.asyncio
async def test_rate_limit_waits_asynchronously() -> None:
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


@pytest.mark.asyncio
async def test_payload_blobs_are_compressed_and_content_addressed(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    clock = Clock(_T0)
    puller = _puller(_config(archive), api=api, now=clock, sleep=clock.sleep)
    await puller.pull(_T0)
    clock.current += timedelta(minutes=1)

    await puller.pull(clock.current)

    blobs = await _rows(
        archive,
        "SELECT digest, codec, raw_size, length(payload) AS stored_size FROM payload_blobs",
    )
    versions = await _rows(archive, "SELECT id FROM resource_versions")
    assert len(blobs) == 2
    assert len(versions) == 1
    assert {row["codec"] for row in blobs} == {"zlib-json-v1"}
    assert all(len(row["digest"]) == 64 for row in blobs)
    assert any(row["stored_size"] < row["raw_size"] for row in blobs)
    assert await _rows(archive, "PRAGMA integrity_check") == [{"integrity_check": "ok"}]


@pytest.mark.asyncio
async def test_v4_archive_migrates_to_v7_without_rebuilding_committed_facts(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    clock = Clock(_T0)
    puller = _puller(_config(archive), api=api, now=clock, sleep=clock.sleep)
    await puller.pull(_T0)
    db = await aiosqlite.connect(archive)
    try:
        await db.execute("DROP TABLE pull_tasks")
        await db.execute("DROP TABLE pull_passes")
        await db.execute(
            "UPDATE archive_meta SET value = '4' WHERE key = 'schema_version'",
        )
        await db.commit()
    finally:
        await db.close()
    clock.current += timedelta(hours=1)

    result = await puller.pull(clock.current)

    assert result.changed_items == 0
    assert (await _current(archive))[1].bundle is not None
    assert await _rows(
        archive,
        "SELECT value FROM archive_meta WHERE key = 'schema_version'",
    ) == [{"value": "7"}]
    tables = await _rows(
        archive,
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'pull_%'",
    )
    assert {row["name"] for row in tables} == {"pull_passes", "pull_runs", "pull_tasks"}


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_version", ["5", "6"])
async def test_prior_migration_requeues_pending_resources_without_resetting_catalog(
    tmp_path: Path,
    schema_version: str,
) -> None:
    api = FakeAPI()
    api.add_issue(1, pull=True)
    api.add_issue(2)
    api.json[f"{_BASE}/pulls/1"] = {
        "id": 10,
        "changed_files": 0,
        "commits": 0,
        "review_comments": 0,
    }
    archive = tmp_path / "archive"
    await _puller(_config(archive), api=api, now=lambda: _T0).pull(_T0)
    db = await aiosqlite.connect(archive)
    try:
        cursor = await db.execute(
            """
            INSERT INTO pull_runs(target_at, started_at, observed_until, status)
            VALUES (?, ?, ?, 'pending')
            """,
            (_iso(_T0 + timedelta(hours=1)), _iso(_T0), _iso(_T0)),
        )
        run_id = int(cursor.lastrowid)
        await db.execute(
            """
            INSERT INTO pull_passes(
                run_id, name, cutoff_at, mode, prepared, catalog_started,
                catalog_complete, next_url, catalog_pages, catalog_items
            ) VALUES (?, 'closing', ?, 'delta', 1, 1, 0, 'cursor-2', 1, 100)
            """,
            (run_id, _iso(_T0 + timedelta(hours=1))),
        )
        await db.execute(
            """
            INSERT INTO pull_tasks(
                run_id, number, github_id, kind, created_at, updated_at,
                summary_digest, catalog_member, completed
            )
            SELECT ?, number, github_id, kind, created_at, updated_at,
                summary_digest, 1, 1
            FROM resource_heads
            """,
            (run_id,),
        )
        await db.execute(
            """
            INSERT INTO resource_versions(
                run_id, observed_at, number, github_id, kind, created_at, updated_at,
                summary_digest, bundle_digest, present, missing_since
            )
            SELECT ?, ?, number, github_id, kind, created_at, updated_at,
                summary_digest, bundle_digest, present, missing_since
            FROM resource_heads
            """,
            (run_id, _iso(_T0)),
        )
        await db.execute(
            "UPDATE archive_meta SET value = ? WHERE key = 'schema_version'",
            (schema_version,),
        )
        await db.commit()
    finally:
        await db.close()

    async with SQLiteArchive(archive, "acme/widgets"):
        pass

    assert await _rows(archive, "SELECT value FROM archive_meta WHERE key = 'schema_version'") == [
        {"value": "7"},
    ]
    assert await _rows(archive, "SELECT number, completed FROM pull_tasks ORDER BY number") == [
        {"number": 1, "completed": 0},
        {"number": 2, "completed": 0},
    ]
    assert await _rows(archive, "SELECT next_url, catalog_items FROM pull_passes") == [
        {"next_url": "cursor-2", "catalog_items": 100},
    ]
    assert await _rows(archive, "SELECT number FROM resource_versions WHERE run_id = ?", (run_id,)) == []


@pytest.mark.asyncio
async def test_incompatible_fact_archive_schema_is_rejected(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    await _puller(_config(archive), api=api, now=lambda: _T0).pull(_T0)

    db = await aiosqlite.connect(archive)
    try:
        await db.execute(
            "UPDATE archive_meta SET value = '3' WHERE key = 'schema_version'",
        )
        await db.commit()
    finally:
        await db.close()

    with pytest.raises(ValueError, match="unsupported GitHub archive schema"):
        await _puller(
            _config(archive),
            api=api,
            now=lambda: _T0 + timedelta(hours=1),
        ).pull(_T0 + timedelta(hours=1))

    assert await _rows(archive, "SELECT value FROM archive_meta WHERE key = 'schema_version'") == [
        {"value": "3"},
    ]


def test_console_progress_uses_throttled_json_for_logs_and_a_tty_bar() -> None:
    progress = PullProgress(
        event_at=_T0,
        phase="closing_bundles",
        target_at=_T0,
        catalog_seen=2,
        catalog_total=2,
        catalog_complete=True,
        objects_completed=1,
        objects_total=2,
        bundles_completed=1,
        issues_completed=1,
        latest_number=1,
        latest_kind="issue",
        requests=7,
        quotas=(
            RateQuota("core", 5000, 4993, _T0 + timedelta(hours=1)),
            RateQuota("graphql", 5000, 4998, _T0 + timedelta(minutes=30)),
        ),
    )
    log = StringIO()
    ticks = iter((0.0, 0.1, 0.2))
    observer = ConsoleProgress(log, interval=10, tty=False, monotonic=lambda: next(ticks))

    observer(progress)
    observer(
        replace(
            progress,
            event_at=_T0 + timedelta(seconds=1),
            objects_completed=2,
            bundles_completed=2,
        ),
    )
    observer(
        replace(
            progress,
            event_at=_T0 + timedelta(seconds=2),
            phase="done",
            objects_completed=2,
            bundles_completed=2,
        ),
    )

    lines = [json.loads(line) for line in log.getvalue().splitlines()]
    assert len(lines) == 2
    assert lines[0]["type"] == "github_pull_progress"
    assert lines[0]["event_at"] == _iso(_T0)
    assert lines[0]["quotas"] == [
        {
            "resource": "core",
            "limit": 5000,
            "remaining": 4993,
            "reset_at": _iso(_T0 + timedelta(hours=1)),
        },
        {
            "resource": "graphql",
            "limit": 5000,
            "remaining": 4998,
            "reset_at": _iso(_T0 + timedelta(minutes=30)),
        },
    ]
    assert lines[1]["phase"] == "done"

    terminal = StringIO()
    tty = ConsoleProgress(terminal, interval=0, tty=True, monotonic=lambda: 0.0)
    tty(progress)
    tty(replace(progress, phase="done", objects_completed=2, bundles_completed=2))
    rendered = terminal.getvalue()
    assert "objects=[##########----------] 1/2" in rendered
    assert "objects=[####################] 2/2" in rendered
    assert "issues=1 pulls=0" in rendered
    assert "latest=issue#1" in rendered
    assert "core=4,993/5,000 graphql=4,998/5,000" in rendered
    assert rendered.endswith("\n")
