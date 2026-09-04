"""Provide deterministic GitHub API, Git, clock, and archive test doubles."""

from __future__ import annotations

import math
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit

import aiosqlite

from gh_puller.github import (
    ArchivedHead,
    ArchivedRun,
    ArchivedVersion,
    GitHubPullConfig,
    GitHubPuller,
    iter_heads,
    iter_runs,
    iter_versions,
)
from gh_puller.github.client import GitHubPage, GitHubResource

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
    def __init__(self, source: str = "rest") -> None:
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
        self.source = source

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
        return self._resource(value, updated)

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
        return self._resource(value, updated)

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
            return self._resource(value)
        value, updated = await self.paginate_cached(
            f"/repos/{owner}/{repo}/pulls/{number}/commits",
            previous=previous,
            cache=cache,
        )
        return self._resource(value, updated)

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
        return self._resource(value, updated)

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
        return self._resource(value, updated)

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
        return self._resource(value, updated)

    def _resource(
        self,
        value: Any,
        cache: dict[str, Any] | None = None,
    ) -> GitHubResource:
        raw = value if self.source == "rest" else {"native": deepcopy(value)}
        return GitHubResource(value, self.source, raw, cache)

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
        self.syncs = 0

    async def sync_upstream(
        self,
        *,
        heartbeat: Any = None,
        retry: Any = None,
    ) -> None:
        del retry
        self.syncs += 1
        if heartbeat is not None:
            heartbeat()

    async def prefetch(
        self,
        pulls: dict[int, dict[str, Any]],
        *,
        heartbeat: Any = None,
        retry: Any = None,
        retry_transient: bool = True,
    ) -> None:
        del retry_transient
        self.prefetches.append(list(pulls))
        if heartbeat is not None:
            heartbeat()

    async def capture(
        self,
        number: int,
        pull: dict[str, Any],
        *,
        heartbeat: Any = None,
        retry: Any = None,
    ) -> dict[str, Any]:
        self.captures.append(number)
        base = pull.get("base")
        head = pull.get("head")
        base_sha = base.get("sha") if isinstance(base, dict) else "0" * 40
        head_sha = head.get("sha") if isinstance(head, dict) else f"{number:040x}"
        prefix = f"refs/github-archive/pulls/{number}"
        return {
            "base_ref": f"{prefix}/bases/{base_sha}",
            "base_sha": base_sha,
            "comparison_kind": "merge_base",
            "comparison_ref": f"{prefix}/comparisons/{base_sha}",
            "comparison_sha": base_sha,
            "head_ref": f"{prefix}/heads/{head_sha}",
            "head_sha": head_sha,
            "history_preserved": None,
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
