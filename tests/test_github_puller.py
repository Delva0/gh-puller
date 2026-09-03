"""GitHub 拉取器的观测水位、原始响应保留、恢复与 SQLite 发布测试。"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import random
import re
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import TYPE_CHECKING, Any

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
    IncompleteGitHubDataError,
    PullProgress,
    RateQuota,
    iter_heads,
    iter_runs,
    iter_versions,
)
from gh_puller.github.puller import _certified_head_merge
from gh_puller.github.store import StoredHead, json_digest

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
        self.text: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.request_count = 0
        self.fail_once: set[str] = set()
        self.failed: set[str] = set()
        self.on_request: Any = None
        self.reported_count: int | None = None
        self.count_available = True
        self.closing: dict[int, list[dict[str, Any]]] = {}

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

    async def get_text(self, path: str, *, accept: str) -> str:
        self._called("text", path, None)
        return self.text.get((path, accept), f"{accept}:{path}")

    async def get_text_cached(
        self,
        path: str,
        *,
        accept: str,
        previous: str | None,
        cache: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any] | None]:
        return await self.get_text(path, accept=accept), None

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


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


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


def _config(path: Path, **kwargs: Any) -> GitHubPullConfig:
    return GitHubPullConfig(repository="acme/widgets", destination=path, **kwargs)


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


def _subsets(values: tuple[int, ...]) -> Any:
    return itertools.chain.from_iterable(itertools.combinations(values, size) for size in range(len(values) + 1))


def _summary(number: int, created_at: datetime, updated_at: datetime, *, title: str = "old") -> dict[str, Any]:
    return {
        "id": number * 10,
        "number": number,
        "created_at": _iso(created_at),
        "updated_at": _iso(updated_at),
        "title": title,
    }


def _stored_heads(items: list[dict[str, Any]]) -> dict[int, StoredHead]:
    return {
        item["number"]: StoredHead(
            number=item["number"],
            github_id=item["id"],
            kind="pull" if "pull_request" in item else "issue",
            created_at=item["created_at"],
            updated_at=item["updated_at"],
            summary_digest=json_digest(item),
            bundle_digest="bundle",
            present=True,
            missing_since=None,
        )
        for item in items
    }


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


def test_cardinality_certificate_accepts_exactly_deletion_free_small_transitions() -> None:
    previous_ids = (1, 2, 3)
    addition_ids = (4, 5)
    previous = [_summary(number, _T0 - timedelta(days=number), _T0) for number in previous_ids]
    cutoff = _T0 + timedelta(hours=1)
    changed_at = cutoff - timedelta(minutes=1)

    for survivors_tuple in _subsets(previous_ids):
        survivors = set(survivors_tuple)
        for additions_tuple in _subsets(addition_ids):
            additions = set(additions_tuple)
            for updates_tuple in _subsets(tuple(sorted(survivors))):
                updates = set(updates_tuple)
                current: list[dict[str, Any]] = []
                delta: list[dict[str, Any]] = []
                for item in previous:
                    if item["number"] not in survivors:
                        continue
                    candidate = deepcopy(item)
                    if item["number"] in updates:
                        candidate["updated_at"] = _iso(changed_at)
                        candidate["title"] = "updated"
                        delta.append(candidate)
                    current.append(candidate)
                for number in additions:
                    candidate = _summary(number, changed_at, changed_at, title="new")
                    current.append(candidate)
                    delta.append(candidate)

                certified = _certified_head_merge(_stored_heads(previous), delta, len(current), cutoff)
                if survivors == set(previous_ids):
                    assert certified == (
                        {item["number"] for item in current},
                        {item["number"]: item for item in delta},
                    )
                else:
                    assert certified is None


def test_cardinality_certificate_excludes_items_created_after_target() -> None:
    previous = [_summary(1, _T0 - timedelta(days=1), _T0)]
    cutoff = _T0 + timedelta(hours=1)
    visible = _summary(2, cutoff, cutoff, title="visible")
    future = _summary(3, cutoff + timedelta(seconds=1), cutoff + timedelta(seconds=1), title="future")

    heads = _stored_heads(previous)
    assert _certified_head_merge(heads, [visible, future], 3, cutoff) == ({1, 2}, {2: visible})
    assert _certified_head_merge(heads, [visible, future], 2, cutoff) is None


def test_cardinality_certificate_matches_large_random_catalog_churn() -> None:
    rng = random.Random(20260901)  # noqa: S311 - The workload must be reproducible, not unpredictable.
    previous = [_summary(number, _T0 - timedelta(days=1), _T0 - timedelta(hours=1)) for number in range(1, 5001)]
    next_number = 5001
    accepted = 0
    rejected = 0

    for epoch in range(1, 81):
        cutoff = _T0 + timedelta(hours=epoch)
        changed_at = cutoff - timedelta(minutes=1)
        old = {item["number"]: item for item in previous}
        deleted = set(rng.sample(sorted(old), 5)) if epoch % 2 == 0 else set()
        survivors = set(old) - deleted
        updated = set(rng.sample(sorted(survivors), 25))
        additions = tuple(range(next_number, next_number + 5))
        next_number += len(additions)
        current: list[dict[str, Any]] = []
        delta: list[dict[str, Any]] = []
        for number in sorted(survivors):
            item = deepcopy(old[number])
            if number in updated:
                item["updated_at"] = _iso(changed_at)
                item["title"] = f"epoch {epoch}"
                delta.append(item)
            current.append(item)
        for number in additions:
            item = _summary(number, changed_at, changed_at, title=f"epoch {epoch}")
            current.append(item)
            delta.append(item)

        certified = _certified_head_merge(_stored_heads(previous), delta, len(current), cutoff)
        if deleted:
            assert certified is None
            rejected += 1
        else:
            assert certified == (
                {item["number"] for item in current},
                {item["number"]: item for item in delta},
            )
            accepted += 1
        previous = current

    assert len(previous) == 5200
    assert (accepted, rejected) == (40, 40)


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

    await GitHubPuller(_config(tmp_path / "archive"), now=clock, sleep=clock.sleep).pull(_T0)

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

    result = await GitHubPuller(_config(tmp_path / "archive"), api=api, now=clock, sleep=clock.sleep).pull(_T0)

    archive = tmp_path / "archive"
    current = await _current(archive)
    bundle = current[1].bundle
    catalog = current[1].summary
    assert bundle is not None
    assert bundle["schema_version"] == 2
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


@pytest.mark.asyncio
async def test_cold_pull_reports_certified_catalog_and_durable_bundles(tmp_path: Path) -> None:
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

    result = await GitHubPuller(
        _config(tmp_path / "archive"),
        api=api,
        now=lambda: _T0,
        observer=events.append,
    ).pull(_T0)

    phases = [event.phase for event in events]
    assert phases[0:2] == ["waiting_lock", "checking"]
    assert {"closing_catalog", "closing_bundles", "finalizing", "done"} <= set(phases)
    completed = [event for event in events if event.phase == "closing_bundles" and event.bundles_completed == 2][-1]
    assert completed.catalog_seen == completed.catalog_total == 2
    assert completed.bundles_total == 2
    assert (completed.issues_completed, completed.pulls_completed) == (1, 1)
    assert (completed.latest_number, completed.latest_kind) == (2, "pull")
    assert events[-1].phase == "done"
    assert events[-1].catalog_total == result.catalog_items == 2
    assert events[-1].requests == result.requests


@pytest.mark.asyncio
async def test_pull_request_bundle_preserves_all_discussion_and_diff_data(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(7, pull=True)
    issue_path = f"{_BASE}/issues/7"
    pull_path = f"{_BASE}/pulls/7"
    api.json[pull_path] = {
        "id": 700,
        "changed_files": 1,
        "commits": 1,
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
    api.pages[f"{pull_path}/files"] = [{"sha": "abc", "filename": "a.py", "patch": "@@"}]
    api.closing[7] = [_closing_issue(11)]
    api.text[(pull_path, "application/vnd.github.diff")] = "diff --git a/a.py b/a.py\n"
    api.text[(pull_path, "application/vnd.github.patch")] = "From abc Mon Sep 17 00:00:00 2001\n"

    await GitHubPuller(_config(tmp_path / "archive"), api=api, now=lambda: _T0).pull(_T0)

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
    assert pull["files"][0]["patch"] == "@@"
    assert pull["requested_reviewers"]["users"][0]["login"] == "alice"
    assert pull["closing_issues_references"] == [_closing_issue(11)]
    assert pull["diff"].startswith("diff --git")
    assert pull["patch"].startswith("From abc")
    assert (await _current(tmp_path / "archive"))[7].kind == "pull"
    assert not any(call[1] == f"{issue_path}/comments" for call in api.calls)


@pytest.mark.asyncio
async def test_closing_issue_queries_pack_one_hundred_selected_pulls(tmp_path: Path) -> None:
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

    result = await GitHubPuller(
        _config(tmp_path / "archive", concurrency=32),
        api=api,
        now=lambda: _T0,
    ).pull(_T0)

    calls = [call for call in api.calls if call[0] == "closing"]
    assert [len(call[2]["numbers"]) for call in calls] == [100, 1]
    assert result.requests == 814


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
    puller = GitHubPuller(_config(tmp_path / "archive"), api=api, now=clock, sleep=clock.sleep)
    await puller.pull(_T0)
    call_start = len(api.calls)
    clock.current += timedelta(hours=1)

    result = await puller.pull(clock.current)

    assert result.requests == 4
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
        await GitHubPuller(_config(archive), api=api, now=lambda: _T0).pull(_T0)

    assert await _runs(archive) == []
    assert await _versions(archive) == []


@pytest.mark.asyncio
async def test_pull_patch_falls_back_to_lossless_commit_parts(tmp_path: Path) -> None:
    pull_path = f"{_BASE}/pulls/8"

    class PatchFailureAPI(FakeAPI):
        async def get_text(self, path: str, *, accept: str) -> str:
            if accept == "application/vnd.github.patch" and path in {
                pull_path,
                f"{_BASE}/commits/def",
            }:
                self._called("text", path, None)
                raise GitHubAPIError(f"persistent patch failure: {path}")
            return await super().get_text(path, accept=accept)

    api = PatchFailureAPI()
    api.add_issue(8, pull=True)
    api.json[pull_path] = {
        "id": 80,
        "changed_files": 0,
        "commits": 2,
        "review_comments": 0,
    }
    api.pages[f"{pull_path}/commits"] = [{"sha": "abc"}, {"sha": "def"}]

    await GitHubPuller(_config(tmp_path / "archive"), api=api, now=lambda: _T0).pull(_T0)

    bundle = (await _current(tmp_path / "archive"))[8].bundle
    assert bundle is not None
    pull = bundle["pull_request"]
    assert pull["patch"] is None
    assert pull["patch_fallback"] == {
        "error": f"persistent patch failure: {pull_path}",
        "commits": [
            {
                "sha": "abc",
                "patch": f"application/vnd.github.patch:{_BASE}/commits/abc",
            },
            {
                "sha": "def",
                "patch": None,
                "patch_error": f"persistent patch failure: {_BASE}/commits/def",
                "diff": f"application/vnd.github.diff:{_BASE}/commits/def",
            },
        ],
    }


@pytest.mark.asyncio
async def test_oversized_pull_media_falls_back_to_cached_commit_parts(tmp_path: Path) -> None:
    pull_path = f"{_BASE}/pulls/9"

    class MediaFailureAPI(FakeAPI):
        async def get_text(self, path: str, *, accept: str) -> str:
            if path == pull_path or (path == f"{_BASE}/commits/def" and accept == "application/vnd.github.diff"):
                self._called("text", path, None)
                raise GitHubAPIError(f"unavailable media: {accept}:{path}")
            return await super().get_text(path, accept=accept)

    api = MediaFailureAPI()
    api.add_issue(9, pull=True)
    api.json[pull_path] = {
        "id": 90,
        "changed_files": 0,
        "commits": 2,
        "review_comments": 0,
    }
    api.pages[f"{pull_path}/commits"] = [{"sha": "abc"}, {"sha": "def"}]

    await GitHubPuller(_config(tmp_path / "archive"), api=api, now=lambda: _T0).pull(_T0)

    bundle = (await _current(tmp_path / "archive"))[9].bundle
    assert bundle is not None
    pull = bundle["pull_request"]
    assert pull["diff"] is None
    assert pull["patch"] is None
    assert pull["diff_fallback"] == {
        "error": f"unavailable media: application/vnd.github.diff:{pull_path}",
        "commits": [
            {
                "sha": "abc",
                "diff": f"application/vnd.github.diff:{_BASE}/commits/abc",
            },
            {
                "sha": "def",
                "diff": None,
                "diff_error": (f"unavailable media: application/vnd.github.diff:{_BASE}/commits/def"),
                "patch": f"application/vnd.github.patch:{_BASE}/commits/def",
            },
        ],
    }
    assert pull["patch_fallback"] == {
        "error": f"unavailable media: application/vnd.github.patch:{pull_path}",
        "commits": [
            {
                "sha": "abc",
                "patch": f"application/vnd.github.patch:{_BASE}/commits/abc",
            },
            {
                "sha": "def",
                "patch": f"application/vnd.github.patch:{_BASE}/commits/def",
            },
        ],
    }
    commit_media = [call for call in api.calls if call[0] == "text" and "/commits/" in call[1]]
    assert len(commit_media) == 4


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
    await GitHubPuller(_config(archive), api=api, now=lambda: _T0).pull(_T0)

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
    puller = GitHubPuller(_config(archive), api=api, now=clock, sleep=clock.sleep)
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
    await GitHubPuller(_config(archive), api=first_api, now=lambda: _T0).pull(_T0)
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

    await GitHubPuller(_config(archive), api=second_api, now=lambda: changed_at).pull(changed_at)

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
async def test_pull_media_validators_survive_puller_restart(tmp_path: Path) -> None:
    class ConditionalTextAPI(FakeAPI):
        def __init__(self) -> None:
            super().__init__()
            self.reused_media: set[str] = set()

        async def get_text_cached(
            self,
            path: str,
            *,
            accept: str,
            previous: str | None,
            cache: dict[str, Any] | None,
        ) -> tuple[str, dict[str, Any] | None]:
            current = await self.get_text(path, accept=accept)
            token = json.dumps([accept, current])
            if previous is not None and cache == {"token": token}:
                self.reused_media.add(accept)
                return previous, cache
            return current, {"token": token}

    first_api = ConditionalTextAPI()
    first_api.add_issue(1, pull=True)
    pull_path = f"{_BASE}/pulls/1"
    first_api.json[pull_path] = {
        "id": 10,
        "changed_files": 0,
        "commits": 0,
        "review_comments": 0,
    }
    archive = tmp_path / "archive"
    await GitHubPuller(_config(archive), api=first_api, now=lambda: _T0).pull(_T0)

    second_api = ConditionalTextAPI()
    second_api.catalog = deepcopy(first_api.catalog)
    second_api.json = deepcopy(first_api.json)
    second_api.pages = deepcopy(first_api.pages)
    changed_at = _T0 + timedelta(hours=1)
    second_api.catalog[0]["updated_at"] = _iso(changed_at)
    second_api.json[f"{_BASE}/issues/1"]["updated_at"] = _iso(changed_at)

    await GitHubPuller(_config(archive), api=second_api, now=lambda: changed_at).pull(changed_at)

    assert second_api.reused_media == {
        "application/vnd.github.diff",
        "application/vnd.github.patch",
    }


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

    await GitHubPuller(
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

    await GitHubPuller(_config(tmp_path / "archive"), api=api, now=lambda: _T0).pull(_T0)

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
    puller = GitHubPuller(
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
    fast_epochs = 0
    fallback_epochs = 0
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
        if deleted:
            assert root_scans == 3
            fallback_epochs += 1
        else:
            assert root_scans == 1
            fast_epochs += 1
        fetched_roots = {
            int(call[1].rsplit("/", 1)[1])
            for call in epoch_calls
            if call[0] == "json" and call[1].startswith(f"{_BASE}/issues/")
        }
        expected_roots = (
            set()
            if deleted
            else {number for number, operation, _ in operations if operation == "add"}
        )
        assert fetched_roots == expected_roots
        heads = await _current(archive)
        present = {number for number, head in heads.items() if head.present}
        expected_present = {item["number"] for item in api.catalog}
        assert present == expected_present
        for number in expected_present:
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
        assert all(not heads[number].present for number in deleted)

    assert (added_total, deleted_total) == (60, 20)
    assert added_pulls > 0
    assert deleted_pulls > 0
    assert comment_additions + comment_deletions == 240
    assert comment_additions > 0
    assert comment_deletions > 0
    assert pull_comment_operations > 0
    assert (fast_epochs, fallback_epochs) == (10, 10)
    assert len(api.catalog) == 136
    assert len(await _runs(archive)) == 21


@pytest.mark.asyncio
async def test_parent_deletion_fallback_reads_catalog_without_survivor_bundles(
    tmp_path: Path,
) -> None:
    api = FakeAPI()
    for number in range(1, 251):
        api.add_issue(number)
    archive = tmp_path / "archive"
    clock = Clock(_T0)
    puller = GitHubPuller(_config(archive), api=api, now=clock, sleep=clock.sleep)
    await puller.pull(_T0)

    api.catalog = [item for item in api.catalog if item["number"] != 125]
    clock.current += timedelta(hours=1)
    call_start = len(api.calls)
    result = await puller.pull(clock.current)

    calls = api.calls[call_start:]
    fetched_roots = [call for call in calls if call[0] == "json" and call[1].startswith(f"{_BASE}/issues/")]
    assert fetched_roots == []
    assert sum(call[0] == "page" and call[1] == f"{_BASE}/issues" for call in calls) == 3
    assert result.requests == 10
    assert result.catalog_items == 249
    assert (await _current(archive))[125].present is False


@pytest.mark.parametrize("catalog_size", [1, 250])
@pytest.mark.asyncio
async def test_quiet_increment_cost_is_four_requests_independent_of_catalog_size(
    tmp_path: Path,
    catalog_size: int,
) -> None:
    api = FakeAPI()
    for number in range(1, catalog_size + 1):
        api.add_issue(number)
    archive = tmp_path / str(catalog_size)
    clock = Clock(_T0)
    puller = GitHubPuller(_config(archive), api=api, now=clock, sleep=clock.sleep)
    cold = await puller.pull(_T0)
    baseline = math.ceil(catalog_size / 100) + 5 * catalog_size
    expected = math.ceil(catalog_size / 100) + 1 + 2 * catalog_size
    assert cold.requests == expected
    assert cold.requests < baseline
    assert sum(call[0] == "page" and call[1] == f"{_BASE}/issues" for call in api.calls) == 1
    assert sum(call[0] == "count" for call in api.calls) == 1
    clock.current += timedelta(hours=1)
    call_start = len(api.calls)

    result = await puller.pull(clock.current)

    assert result.requests == 4
    assert result.requests / 5000 == 0.0008
    discovery = api.calls[call_start:]
    assert {call[1] for call in discovery} == {
        f"{_BASE}/issues",
        f"{_BASE}/issues/comments",
        f"{_BASE}/pulls/comments",
        "/graphql",
    }
    root_call = next(call for call in discovery if call[1] == f"{_BASE}/issues")
    assert root_call[2]["sort"] == "created"
    assert root_call[2]["since"] == _iso(_T0 - timedelta(seconds=2))


@pytest.mark.asyncio
async def test_same_second_catalog_change_is_not_lost(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1, updated_at=_T0)
    archive = tmp_path / "archive"
    clock = Clock(_T0)
    puller = GitHubPuller(_config(archive), api=api, now=clock, sleep=clock.sleep)
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
    puller = GitHubPuller(_config(archive), api=api, now=lambda: _T0, observer=events.append)
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
    result = await GitHubPuller(
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
    puller = GitHubPuller(_config(tmp_path / "archive"), now=lambda: _T0)

    first = await puller.pull(_T0)
    repeated = await puller.pull(_T0)

    assert repeated == first
    assert constructions == 1


@pytest.mark.asyncio
async def test_target_is_the_complete_idempotency_key(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    puller = GitHubPuller(_config(archive), api=api, now=lambda: _T0)

    first = await puller.pull(_T0)
    repeated = await puller.pull(_T0)

    assert repeated == first
    assert [(run.id, run.target_at) for run in await _runs(archive)] == [(first.run_id, _iso(_T0))]


@pytest.mark.asyncio
async def test_concurrent_duplicate_pulls_publish_one_run(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    puller = GitHubPuller(_config(archive), api=api, now=lambda: _T0)

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
    puller = GitHubPuller(_config(archive), api=api, now=clock, sleep=clock.sleep)
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
    result = await GitHubPuller(
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
    assert phases.index("prefetch_catalog") < phases.index("prefetch_bundles")
    assert phases.index("prefetch_bundles") < phases.index("waiting_target")
    assert phases.index("waiting_target") < phases.index("closing_catalog")
    assert phases.index("closing_catalog") < phases.index("closing_bundles")
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
    puller = GitHubPuller(_config(archive), api=api, now=clock, sleep=clock.sleep)
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
    result = await GitHubPuller(_config(archive), api=api, now=clock, sleep=clock.sleep).pull()

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
    puller = GitHubPuller(_config(archive, concurrency=1), api=api, now=lambda: _T0)

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
    result = await GitHubPuller(
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
    restored = next(event for event in events if event.phase == "closing_catalog" and event.bundles_completed == 2)
    planned = next(event for event in events if event.phase == "closing_bundles")
    assert restored.bundles_total is None
    assert (restored.issues_completed, restored.pulls_completed) == (1, 1)
    assert (restored.latest_number, restored.latest_kind) == (2, "pull")
    assert (planned.bundles_completed, planned.bundles_total) == (2, 3)
    assert events[-1].bundles_completed == events[-1].bundles_total == 3


@pytest.mark.asyncio
async def test_resumed_plan_reclassifies_a_staged_parent_deleted_before_target(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    clock = Clock(_T0)
    target = _T0 + timedelta(hours=1)

    async def interrupt_wait(_: float) -> None:
        raise RuntimeError("interrupt before target")

    with pytest.raises(RuntimeError, match="interrupt before target"):
        await GitHubPuller(
            _config(archive),
            api=api,
            now=clock,
            sleep=interrupt_wait,
        ).pull(target)

    api.catalog.clear()
    clock.current = target
    events: list[PullProgress] = []
    result = await GitHubPuller(
        _config(archive),
        api=api,
        now=clock,
        sleep=clock.sleep,
        observer=events.append,
    ).pull(target)

    restored = next(event for event in events if event.phase == "closing_catalog" and event.bundles_completed == 1)
    planned = next(event for event in events if event.phase == "closing_bundles")
    assert (restored.latest_number, restored.latest_kind) == (1, "issue")
    assert (planned.bundles_completed, planned.bundles_total) == (0, 0)
    assert (planned.issues_completed, planned.pulls_completed) == (0, 0)
    assert (planned.latest_number, planned.latest_kind) == (None, None)
    assert events[-1].tombstones == 1
    assert result.catalog_items == 0
    assert (await _current(archive))[1].present is False


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
    task = asyncio.create_task(GitHubPuller(_config(archive), api=api, now=lambda: _T0).pull(_T0))
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
        GitHubPuller(
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
        if progress.phase == "closing_bundles" and progress.bundles_completed == 2:
            durable_newer.set()

    archive = tmp_path / "archive"
    pull = asyncio.create_task(
        GitHubPuller(
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
async def test_missing_catalog_item_becomes_tombstone_without_losing_bundle(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    clock = Clock(_T0)
    puller = GitHubPuller(_config(archive), api=api, now=clock, sleep=clock.sleep)
    await puller.pull(_T0)
    original = (await _current(archive))[1].bundle
    api.catalog.clear()
    clock.current += timedelta(minutes=1)

    result = await puller.pull(clock.current)

    record = (await _current(archive))[1]
    assert record.present is False
    assert record.missing_since == _iso(clock.current)
    assert record.bundle == original
    assert result.catalog_items == 0
    assert [head async for head in iter_heads(archive, present_only=True)] == []


@pytest.mark.asyncio
async def test_detectable_pull_file_truncation_aborts_without_publication(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(9, pull=True)
    pull_path = f"{_BASE}/pulls/9"
    api.json[pull_path] = {"id": 900, "changed_files": 2, "commits": 0}
    api.pages[f"{pull_path}/files"] = [{"filename": "only-one.py"}]
    archive = tmp_path / "archive"

    with pytest.raises(IncompleteGitHubDataError, match="advertised 2 files, got 1"):
        await GitHubPuller(_config(archive), api=api, now=lambda: _T0).pull(_T0)

    assert await _runs(archive) == []
    assert await _versions(archive) == []


@pytest.mark.asyncio
async def test_naive_target_is_rejected_before_archive_creation(tmp_path: Path) -> None:
    destination = tmp_path / "archive"
    target = datetime(2026, 8, 1, 12)  # noqa: DTZ001  # The rejection fixture must be naive.

    with pytest.raises(ValueError, match="timezone-aware"):
        await GitHubPuller(_config(destination), api=FakeAPI(), now=lambda: _T0).pull(target)

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
        ("secondary_rate_limit", 3),
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
async def test_conditional_text_reuses_exact_paired_response_on_304() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                200,
                headers={"etag": '"diff-1"'},
                text="diff --git a/a b/a\n",
                request=request,
            )
        assert request.headers["accept"] == "application/vnd.github.diff"
        assert request.headers["if-none-match"] == '"diff-1"'
        return httpx.Response(304, headers={"etag": '"diff-1"'}, request=request)

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client)
    try:
        first, cache = await api.get_text_cached(
            "/pull",
            accept="application/vnd.github.diff",
            previous=None,
            cache=None,
        )
        second, updated = await api.get_text_cached(
            "/pull",
            accept="application/vnd.github.diff",
            previous=first,
            cache=cache,
        )
    finally:
        await client.aclose()

    assert second == first
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
async def test_conditional_multi_page_collection_reuses_every_certified_page() -> None:
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
    puller = GitHubPuller(_config(archive), api=api, now=clock, sleep=clock.sleep)
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
async def test_incompatible_fact_archive_schema_is_rejected(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    await GitHubPuller(_config(archive), api=api, now=lambda: _T0).pull(_T0)

    db = await aiosqlite.connect(archive)
    try:
        await db.execute(
            "UPDATE archive_meta SET value = '3' WHERE key = 'schema_version'",
        )
        await db.commit()
    finally:
        await db.close()

    with pytest.raises(ValueError, match="unsupported GitHub archive schema"):
        await GitHubPuller(
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
        bundles_completed=1,
        bundles_total=2,
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
    observer(replace(progress, event_at=_T0 + timedelta(seconds=1), bundles_completed=2))
    observer(replace(progress, event_at=_T0 + timedelta(seconds=2), phase="done"))

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
    tty(replace(progress, phase="done", bundles_completed=2))
    rendered = terminal.getvalue()
    assert "[##########----------] 1/2" in rendered
    assert "issues=1 pulls=0" in rendered
    assert "latest=issue#1" in rendered
    assert "core=4,993/5,000 graphql=4,998/5,000" in rendered
    assert rendered.endswith("\n")
