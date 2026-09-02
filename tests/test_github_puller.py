"""GitHub 拉取器的水位、原始事实完整性、恢复与 SQLite 发布测试。"""

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
    iter_heads,
    iter_runs,
    iter_versions,
)
from gh_puller.github.client import _CATALOG_HINT
from gh_puller.github.puller import _certified_merge

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
        return deepcopy(self.json[path])

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
        if page_observer is not None:
            if items:
                for offset in range(0, len(items), 100):
                    page_observer(min(100, len(items) - offset))
            else:
                page_observer(0)
        return items

    async def collection_size(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> int | None:
        items = self._items(path, params)
        self._called("size", path, params)
        return len(items)

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

    async def repository_catalog(
        self,
        owner: str,
        repo: str,
        *,
        page_observer: Any = None,
    ) -> tuple[list[dict[str, Any]], int] | None:
        if not self.count_available:
            return None
        issues = [item for item in self.catalog if "pull_request" not in item]
        pulls = [item for item in self.catalog if "pull_request" in item]
        issues.sort(key=lambda item: (_time(item["created_at"]), item["number"]), reverse=True)
        pulls.sort(key=lambda item: (_time(item["created_at"]), item["number"]), reverse=True)
        pages = max(1, math.ceil(len(issues) / 100), math.ceil(len(pulls) / 100))
        self._called("catalog", "/graphql/catalog", {"owner": owner, "repo": repo}, requests=pages)
        if page_observer is not None:
            for page in range(pages):
                start = page * 100
                page_observer(len(issues[start : start + 100]) + len(pulls[start : start + 100]))
        hints = []
        for item in issues + pulls:
            hint = {
                _CATALOG_HINT: True,
                "number": item["number"],
                "created_at": item["created_at"],
                "updated_at": item["updated_at"],
            }
            if "pull_request" in item:
                hint["pull_request"] = {}
            hints.append(hint)
        return hints, len(hints)

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
        }
        if pull:
            summary["pull_request"] = {"url": f"{_BASE}/pulls/{number}"}
        self.catalog.append(summary)
        detail = deepcopy(summary) | {
            "body": body,
            "comments": 0,
            "reactions": {"total_count": 0},
            "unknown_detail_field": [1, {"raw": "yes"}],
        }
        self.json[f"{_BASE}/issues/{number}"] = detail
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


def _seed_differential_api(api: FakeAPI) -> None:
    api.add_issue(1)
    api.add_issue(2, pull=True)
    api.add_issue(4)
    pull_path = f"{_BASE}/pulls/2"
    api.json[pull_path] = {
        "id": 20,
        "changed_files": 0,
        "commits": 0,
        "review_comments": 0,
    }


def _mutate_active_interval(api: FakeAPI, changed_at: datetime) -> None:
    issue = next(item for item in api.catalog if item["number"] == 1)
    issue["updated_at"] = _iso(changed_at)
    issue["title"] = "root edit"
    api.json[f"{_BASE}/issues/1"]["updated_at"] = _iso(changed_at)
    api.json[f"{_BASE}/issues/1"]["title"] = "root edit"
    api.add_issue(3, created_at=changed_at, updated_at=changed_at)

    issue_path = f"{_BASE}/issues/4"
    api.pages[f"{_BASE}/issues/comments"] = [
        {
            "id": 41,
            "issue_url": issue_path,
            "created_at": _iso(changed_at),
            "updated_at": _iso(changed_at),
        },
    ]
    api.json[issue_path]["comments"] = 1
    api.pages[f"{issue_path}/comments"] = [
        {
            "id": 41,
            "body": "child-only edit",
            "created_at": _iso(changed_at),
            "updated_at": _iso(changed_at),
            "reactions": {"total_count": 0},
        },
    ]

    pull_path = f"{_BASE}/pulls/2"
    api.pages[f"{_BASE}/pulls/comments"] = [
        {
            "id": 21,
            "pull_request_url": pull_path,
            "created_at": _iso(changed_at),
            "updated_at": _iso(changed_at),
        },
    ]
    api.json[pull_path]["review_comments"] = 1
    api.pages[f"{pull_path}/comments"] = [
        {
            "id": 21,
            "body": "review child-only edit",
            "created_at": _iso(changed_at),
            "updated_at": _iso(changed_at),
            "reactions": {"total_count": 0},
        },
    ]


async def _managed_snapshot(path: Path) -> list[tuple[Any, ...]]:
    return [
        (
            version.target_at,
            version.observed_at,
            version.number,
            version.github_id,
            version.kind,
            version.created_at,
            version.updated_at,
            version.present,
            version.missing_since,
            version.summary,
            version.bundle,
        )
        for version in await _versions(path)
    ]


def _seed_churn_api(api: FakeAPI, size: int) -> None:
    for number in range(1, size + 1):
        api.add_issue(number)
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
        api.add_issue(number, created_at=changed_at, updated_at=changed_at)

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
        api.json[path]["comments"] = len(comments)
    api.pages[f"{_BASE}/issues/comments"] = signals


def test_cardinality_certificate_equals_exhaustive_catalog_for_all_small_transitions() -> None:
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

                certified = _certified_merge(previous, delta, len(current), cutoff)
                if survivors == set(previous_ids):
                    expected = sorted(current, key=lambda item: (item["created_at"], item["number"]))
                    assert certified == expected
                else:
                    assert certified is None


def test_cardinality_certificate_excludes_items_created_after_target() -> None:
    previous = [_summary(1, _T0 - timedelta(days=1), _T0)]
    cutoff = _T0 + timedelta(hours=1)
    visible = _summary(2, cutoff, cutoff, title="visible")
    future = _summary(3, cutoff + timedelta(seconds=1), cutoff + timedelta(seconds=1), title="future")

    assert _certified_merge(previous, [visible, future], 3, cutoff) == [previous[0], visible]
    assert _certified_merge(previous, [visible, future], 2, cutoff) is None


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

        certified = _certified_merge(previous, delta, len(current), cutoff)
        if deleted:
            assert certified is None
            rejected += 1
        else:
            assert certified == current
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
    assert bundle["issue"]["unknown_detail_field"] == [1, {"raw": "yes"}]
    assert bundle["issue_comments"][0]["future_field"] == {"kept": 1}
    assert bundle["timeline"][0]["rename"] == {"from": "a", "to": "b"}
    assert bundle["events"][0]["label"]["name"] == "bug"
    assert bundle["reactions"][0]["content"] == "heart"
    assert bundle["issue_comment_reactions"] == {"101": []}
    assert catalog["unknown_summary_field"] == {"preserved": True}
    assert _CATALOG_HINT not in catalog
    runs = await _runs(archive)
    assert len(runs) == 1
    assert runs[0].target_at == _iso(_T0)
    assert runs[0].completed_at == _iso(_T0)
    assert result.run_id == runs[0].id
    assert result.changed_items == 1
    assert result.catalog_items == 1
    assert runs[0].observed_until == _iso(_T0)
    catalog_call = next(call for call in api.calls if call[0] == "catalog")
    assert catalog_call[2] == {"owner": "acme", "repo": "widgets"}


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
    }
    api.json[f"{pull_path}/requested_reviewers"] = {"users": [{"login": "alice"}], "teams": []}
    api.pages[f"{pull_path}/reviews"] = [{"id": 71, "state": "APPROVED", "body": "ship it"}]
    api.pages[f"{pull_path}/comments"] = [
        {"id": 72, "body": "nit", "path": "a.py", "reactions": {"total_count": 1}},
    ]
    api.pages[f"{_BASE}/pulls/comments/72/reactions"] = [{"id": 73, "content": "+1"}]
    api.pages[f"{pull_path}/commits"] = [{"sha": "abc", "commit": {"message": "change"}}]
    api.pages[f"{pull_path}/files"] = [{"sha": "abc", "filename": "a.py", "patch": "@@"}]
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
    assert pull["diff"].startswith("diff --git")
    assert pull["patch"].startswith("From abc")
    assert (await _current(tmp_path / "archive"))[7].kind == "pull"
    assert any(call[1] == f"{issue_path}/comments" for call in api.calls)


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
    second_api.json[f"{_BASE}/issues/1"]["updated_at"] = _iso(changed_at)
    second_api.json[f"{_BASE}/issues/1"]["title"] = "changed root"

    await GitHubPuller(_config(archive), api=second_api, now=lambda: changed_at).pull(changed_at)

    assert second_api.reused == {
        f"{_BASE}/issues/1/comments",
        f"{_BASE}/issues/1/events",
        f"{_BASE}/issues/1/timeline",
    }
    assert second_api.charged_bundle_requests == 1
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
async def test_certified_puller_stays_byte_equal_to_exhaustive_oracle(tmp_path: Path) -> None:
    certified_api = FakeAPI()
    exhaustive_api = FakeAPI()
    _seed_differential_api(certified_api)
    _seed_differential_api(exhaustive_api)
    certified_clock = Clock(_T0)
    exhaustive_clock = Clock(_T0)
    certified_path = tmp_path / "certified"
    exhaustive_path = tmp_path / "exhaustive"
    certified = GitHubPuller(
        _config(certified_path, catalog_mode="certified"),
        api=certified_api,
        now=certified_clock,
        sleep=certified_clock.sleep,
    )
    exhaustive = GitHubPuller(
        _config(
            exhaustive_path,
            catalog_mode="exhaustive",
            bundle_mode="exhaustive",
        ),
        api=exhaustive_api,
        now=exhaustive_clock,
        sleep=exhaustive_clock.sleep,
    )

    await certified.pull(_T0)
    await exhaustive.pull(_T0)
    assert await _managed_snapshot(certified_path) == await _managed_snapshot(exhaustive_path)

    active_target = _T0 + timedelta(hours=1)
    changed_at = active_target - timedelta(minutes=1)
    for api in (certified_api, exhaustive_api):
        _mutate_active_interval(api, changed_at)
    certified_clock.current = active_target
    exhaustive_clock.current = active_target
    await certified.pull(active_target)
    await exhaustive.pull(active_target)
    assert await _managed_snapshot(certified_path) == await _managed_snapshot(exhaustive_path)

    quiet_target = active_target + timedelta(hours=1)
    certified_clock.current = quiet_target
    exhaustive_clock.current = quiet_target
    quiet = await certified.pull(quiet_target)
    await exhaustive.pull(quiet_target)
    assert quiet.requests == 4
    assert await _managed_snapshot(certified_path) == await _managed_snapshot(exhaustive_path)

    replacement_target = quiet_target + timedelta(hours=1)
    replacement_at = replacement_target - timedelta(minutes=1)
    catalog_call_start = len(certified_api.calls)
    for api in (certified_api, exhaustive_api):
        api.catalog = [item for item in api.catalog if item["number"] != 1]
        api.add_issue(5, created_at=replacement_at, updated_at=replacement_at)
    certified_clock.current = replacement_target
    exhaustive_clock.current = replacement_target
    await certified.pull(replacement_target)
    await exhaustive.pull(replacement_target)
    catalog_calls = certified_api.calls[catalog_call_start:]
    root_calls = [call for call in catalog_calls if call[0] == "page" and call[1] == f"{_BASE}/issues"]
    full_calls = [call for call in catalog_calls if call[0] == "catalog"]
    fetched_roots = [call[1] for call in catalog_calls if call[0] == "json" and call[1].startswith(f"{_BASE}/issues/")]
    assert (len(root_calls), len(full_calls)) == (1, 2)
    assert fetched_roots == [f"{_BASE}/issues/5"]
    assert (await _current(certified_path))[1].present is False
    assert await _managed_snapshot(certified_path) == await _managed_snapshot(exhaustive_path)

    same_second_target = replacement_target + timedelta(microseconds=1)
    for api in (certified_api, exhaustive_api):
        item = next(item for item in api.catalog if item["number"] == 3)
        item["title"] = "same-second replacement"
        item["updated_at"] = _iso(replacement_target)
        api.json[f"{_BASE}/issues/3"]["title"] = "same-second replacement"
        api.json[f"{_BASE}/issues/3"]["updated_at"] = _iso(replacement_target)
    certified_clock.current = same_second_target
    exhaustive_clock.current = same_second_target
    await certified.pull(same_second_target)
    await exhaustive.pull(same_second_target)
    assert await _managed_snapshot(certified_path) == await _managed_snapshot(exhaustive_path)


@pytest.mark.asyncio
async def test_pooled_bundle_transport_matches_per_parent_oracle_and_saves_requests(
    tmp_path: Path,
) -> None:
    optimized_api = FakeAPI()
    oracle_api = FakeAPI()
    for api in (optimized_api, oracle_api):
        rng = random.Random(20260902)  # noqa: S311 - The workload must be reproducible.
        for number in range(1, 361):
            pull = number % 5 == 0
            api.add_issue(number, pull=pull)
            issue_path = f"{_BASE}/issues/{number}"
            issue_comments = []
            for offset in range(rng.randrange(5)):
                comment_id = number * 100 + offset
                issue_comments.append(
                    {
                        "id": comment_id,
                        "body": f"issue comment {comment_id}",
                        "issue_url": issue_path,
                        "created_at": _iso(_T0 - timedelta(minutes=offset + 1)),
                        "updated_at": _iso(_T0 - timedelta(minutes=offset + 1)),
                        "reactions": {"total_count": 0},
                    },
                )
            api.pages[f"{issue_path}/comments"] = issue_comments
            api.json[issue_path]["comments"] = len(issue_comments)
            if not pull:
                continue
            pull_path = f"{_BASE}/pulls/{number}"
            api.json[pull_path] = {
                "id": number * 10,
                "changed_files": 0,
                "commits": 0,
                "review_comments": 0,
            }
            review_comments = []
            for offset in range(rng.randrange(4)):
                comment_id = 1_000_000 + number * 100 + offset
                review_comments.append(
                    {
                        "id": comment_id,
                        "body": f"review comment {comment_id}",
                        "pull_request_url": pull_path,
                        "created_at": _iso(_T0 - timedelta(minutes=offset + 1)),
                        "updated_at": _iso(_T0 - timedelta(minutes=offset + 1)),
                        "reactions": {"total_count": 0},
                    },
                )
            api.pages[f"{pull_path}/comments"] = review_comments
            api.json[pull_path]["review_comments"] = len(review_comments)

    optimized_path = tmp_path / "optimized"
    oracle_path = tmp_path / "oracle"
    optimized = await GitHubPuller(
        _config(optimized_path, bundle_mode="optimized", concurrency=32),
        api=optimized_api,
        now=lambda: _T0,
    ).pull(_T0)
    oracle = await GitHubPuller(
        _config(oracle_path, bundle_mode="exhaustive", concurrency=32),
        api=oracle_api,
        now=lambda: _T0,
    ).pull(_T0)

    assert await _managed_snapshot(optimized_path) == await _managed_snapshot(oracle_path)
    assert optimized.requests < oracle.requests * 0.7
    assert sum(call[0] == "page" and call[1] == f"{_BASE}/issues/comments" for call in optimized_api.calls) == 2
    assert sum(call[0] == "page" and call[1] == f"{_BASE}/pulls/comments" for call in optimized_api.calls) == 2
    assert not any(
        call[0] == "page" and re.fullmatch(rf"{re.escape(_BASE)}/issues/\d+/comments", call[1])
        for call in optimized_api.calls
    )
    assert any(
        call[0] == "page" and re.fullmatch(rf"{re.escape(_BASE)}/issues/\d+/comments", call[1])
        for call in oracle_api.calls
    )


@pytest.mark.asyncio
async def test_failed_pooled_feed_falls_back_to_equivalent_per_parent_reads(
    tmp_path: Path,
) -> None:
    class FailingPooledFeedAPI(FakeAPI):
        async def paginate_cached(
            self,
            path: str,
            *,
            previous: list[dict[str, Any]] | None,
            cache: dict[str, Any] | None,
            params: dict[str, Any] | None = None,
            page_observer: Any = None,
        ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
            if path == f"{_BASE}/issues/comments" and not (params and params.get("since")):
                self._called("page", path, params, requests=3)
                if page_observer is not None:
                    page_observer(100)
                    page_observer(100)
                raise GitHubAPIError("persistent repository feed failure")
            return await super().paginate_cached(
                path,
                previous=previous,
                cache=cache,
                params=params,
                page_observer=page_observer,
            )

    optimized_api = FailingPooledFeedAPI()
    oracle_api = FakeAPI()
    for api in (optimized_api, oracle_api):
        for number in range(1, 11):
            api.add_issue(number)
            issue_path = f"{_BASE}/issues/{number}"
            comments = [
                {
                    "id": number * 100 + offset,
                    "body": f"comment {number}:{offset}",
                    "created_at": _iso(_T0 - timedelta(seconds=offset)),
                    "updated_at": _iso(_T0 - timedelta(seconds=offset)),
                    "reactions": {"total_count": 0},
                }
                for offset in range(25)
            ]
            api.pages[f"{issue_path}/comments"] = comments
            api.json[issue_path]["comments"] = len(comments)

    events: list[PullProgress] = []
    optimized_path = tmp_path / "optimized"
    oracle_path = tmp_path / "oracle"
    await GitHubPuller(
        _config(optimized_path, bundle_mode="optimized"),
        api=optimized_api,
        now=lambda: _T0,
        observer=events.append,
    ).pull(_T0)
    await GitHubPuller(
        _config(oracle_path, bundle_mode="exhaustive"),
        api=oracle_api,
        now=lambda: _T0,
    ).pull(_T0)

    assert await _managed_snapshot(optimized_path) == await _managed_snapshot(oracle_path)
    assert any(
        event.phase == "closing_feeds"
        and event.feed_name == "issues/comments"
        and event.feed_scan == 1
        and event.feed_pages_seen == 2
        for event in events
    )
    assert any(event.detail == "issues_comments_per_parent_fallback" for event in events)
    assert sum(
        call[0] == "page"
        and re.fullmatch(rf"{re.escape(_BASE)}/issues/\d+/comments", call[1]) is not None
        for call in optimized_api.calls
    ) == 10
    assert events[-1].phase == "done"


@pytest.mark.asyncio
async def test_pooled_feed_repeats_until_concurrent_mutation_is_stable(tmp_path: Path) -> None:
    class MutatingFeedAPI(FakeAPI):
        def __init__(self) -> None:
            super().__init__()
            self.feed_scans = 0

        async def paginate(
            self,
            path: str,
            *,
            params: dict[str, Any] | None = None,
            page_observer: Any = None,
        ) -> list[dict[str, Any]]:
            items = await super().paginate(path, params=params, page_observer=page_observer)
            if path != f"{_BASE}/issues/comments" or (params and params.get("since")):
                return items
            self.feed_scans += 1
            if self.feed_scans == 1:
                issue_path = f"{_BASE}/issues/1"
                late = {
                    "id": 102,
                    "body": "arrived between scans",
                    "created_at": _iso(_T0 - timedelta(seconds=1)),
                    "updated_at": _iso(_T0 - timedelta(seconds=1)),
                    "reactions": {"total_count": 0},
                }
                self.pages[f"{issue_path}/comments"].append(late)
                self.json[issue_path]["comments"] = 2
            return items

    api = MutatingFeedAPI()
    for number in range(1, 11):
        api.add_issue(number)
    issue_path = f"{_BASE}/issues/1"
    api.pages[f"{issue_path}/comments"] = [
        {
            "id": 101,
            "body": "before scan",
            "created_at": _iso(_T0 - timedelta(minutes=1)),
            "updated_at": _iso(_T0 - timedelta(minutes=1)),
            "reactions": {"total_count": 0},
        },
    ]
    api.json[issue_path]["comments"] = 1

    await GitHubPuller(_config(tmp_path / "archive"), api=api, now=lambda: _T0).pull(_T0)

    bundle = (await _current(tmp_path / "archive"))[1].bundle
    assert bundle is not None
    assert [comment["id"] for comment in bundle["issue_comments"]] == [101, 102]
    assert api.feed_scans == 3


@pytest.mark.asyncio
async def test_pooled_feed_closes_target_prefix_during_continuous_future_appends(
    tmp_path: Path,
) -> None:
    class AppendingFeedAPI(FakeAPI):
        def __init__(self) -> None:
            super().__init__()
            self.feed_scans = 0

        async def paginate(
            self,
            path: str,
            *,
            params: dict[str, Any] | None = None,
            page_observer: Any = None,
        ) -> list[dict[str, Any]]:
            items = await super().paginate(path, params=params, page_observer=page_observer)
            if path != f"{_BASE}/issues/comments" or (params and params.get("since")):
                return items
            self.feed_scans += 1
            issue_path = f"{_BASE}/issues/1"
            comment_id = 101 + self.feed_scans
            self.pages[f"{issue_path}/comments"].append(
                {
                    "id": comment_id,
                    "body": "after target",
                    "created_at": _iso(_T0 + timedelta(seconds=self.feed_scans)),
                    "updated_at": _iso(_T0 + timedelta(seconds=self.feed_scans)),
                    "reactions": {"total_count": 0},
                },
            )
            self.json[issue_path]["comments"] += 1
            return items

    api = AppendingFeedAPI()
    for number in range(1, 11):
        api.add_issue(number)
    issue_path = f"{_BASE}/issues/1"
    api.pages[f"{issue_path}/comments"] = [
        {
            "id": 101,
            "body": "at target",
            "created_at": _iso(_T0),
            "updated_at": _iso(_T0),
            "reactions": {"total_count": 0},
        },
    ]
    api.json[issue_path]["comments"] = 1

    await GitHubPuller(_config(tmp_path / "archive"), api=api, now=lambda: _T0).pull(_T0)

    bundle = (await _current(tmp_path / "archive"))[1].bundle
    assert bundle is not None
    assert [comment["id"] for comment in bundle["issue_comments"]] == [101, 102]
    assert api.feed_scans == 2


@pytest.mark.asyncio
async def test_large_random_issue_and_comment_churn_matches_exhaustive_oracle(tmp_path: Path) -> None:
    rng = random.Random(20260901)  # noqa: S311 - The workload must be reproducible, not unpredictable.
    certified_api = FakeAPI()
    exhaustive_api = FakeAPI()
    _seed_churn_api(certified_api, 96)
    _seed_churn_api(exhaustive_api, 96)
    certified_clock = Clock(_T0)
    exhaustive_clock = Clock(_T0)
    certified_path = tmp_path / "certified-churn"
    exhaustive_path = tmp_path / "exhaustive-churn"
    certified = GitHubPuller(
        _config(certified_path, catalog_mode="certified", concurrency=32),
        api=certified_api,
        now=certified_clock,
        sleep=certified_clock.sleep,
    )
    exhaustive = GitHubPuller(
        _config(
            exhaustive_path,
            catalog_mode="exhaustive",
            bundle_mode="exhaustive",
            concurrency=32,
        ),
        api=exhaustive_api,
        now=exhaustive_clock,
        sleep=exhaustive_clock.sleep,
    )
    await certified.pull(_T0)
    await exhaustive.pull(_T0)
    assert await _managed_snapshot(certified_path) == await _managed_snapshot(exhaustive_path)

    next_number = 97
    next_comment = 1_000_000
    added_total = 0
    deleted_total = 0
    comment_additions = 0
    comment_deletions = 0
    fast_epochs = 0
    fallback_epochs = 0
    for epoch in range(1, 21):
        target = _T0 + timedelta(hours=epoch)
        changed_at = target - timedelta(minutes=1)
        current = sorted(item["number"] for item in certified_api.catalog)
        deleted = set(rng.sample(current, 2)) if epoch % 2 == 0 else set()
        old_survivors = [number for number in current if number not in deleted]
        selected = rng.sample(old_survivors, 12)
        operations: list[tuple[int, str, int]] = []
        for number in selected:
            comments = certified_api.pages.get(f"{_BASE}/issues/{number}/comments", [])
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
        for api in (certified_api, exhaustive_api):
            _apply_churn_epoch(
                api,
                deleted=deleted,
                added=added,
                comment_operations=operations,
                changed_at=changed_at,
            )

        certified_clock.current = target
        exhaustive_clock.current = target
        call_start = len(certified_api.calls)
        await certified.pull(target)
        await exhaustive.pull(target)
        epoch_calls = certified_api.calls[call_start:]
        root_scans = sum(call[0] == "page" and call[1] == f"{_BASE}/issues" for call in epoch_calls)
        full_scans = sum(call[0] == "catalog" for call in epoch_calls)
        assert root_scans == 1
        if deleted:
            assert full_scans == 2
            fallback_epochs += 1
        else:
            assert full_scans == 0
            fast_epochs += 1
        fetched_roots = {
            int(call[1].rsplit("/", 1)[1])
            for call in epoch_calls
            if call[0] == "json" and call[1].startswith(f"{_BASE}/issues/")
        }
        assert fetched_roots == set(selected) | set(added)
        assert await _managed_snapshot(certified_path) == await _managed_snapshot(exhaustive_path)

    assert (added_total, deleted_total) == (60, 20)
    assert comment_additions + comment_deletions == 240
    assert comment_additions > 0
    assert comment_deletions > 0
    assert (fast_epochs, fallback_epochs) == (10, 10)
    assert len(certified_api.catalog) == 136
    assert len(await _runs(certified_path)) == 21
    assert len(await _runs(exhaustive_path)) == 21


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
    assert sum(call[0] == "catalog" for call in calls) == 2
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
    expected = (
        math.ceil(catalog_size / 100) + 4 * catalog_size
        if catalog_size == 1
        else math.ceil(catalog_size / 100) + 3 * catalog_size + 3
    )
    assert cold.requests == expected
    assert cold.requests < baseline
    assert sum(call[0] == "catalog" for call in api.calls) == 1
    assert sum(call[0] == "count" for call in api.calls) == 0
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
    api.json[issue_path]["comments"] = 1
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
    assert sum(call[1] == f"{_BASE}/issues/1" for call in api.calls) == 2
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
    api.fail_once.add(f"{_BASE}/issues/3")
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
    assert sum(call[1] == f"{_BASE}/issues/1" for call in api.calls) == 1
    assert sum(call[1] == f"{_BASE}/issues/2" for call in api.calls) == 1

    events: list[PullProgress] = []
    result = await GitHubPuller(
        _config(archive, concurrency=1),
        api=api,
        now=lambda: _T0,
        observer=events.append,
    ).pull(_T0)

    assert result.changed_items == 3
    assert sum(call[1] == f"{_BASE}/issues/1" for call in api.calls) == 1
    assert sum(call[1] == f"{_BASE}/issues/2" for call in api.calls) == 1
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

        async def get_json(
            self,
            path: str,
            *,
            params: dict[str, Any] | None = None,
            accept: str | None = None,
        ) -> Any:
            if path == f"{_BASE}/issues/1":
                self.started.set()
                await self.release.wait()
            return await super().get_json(path, params=params, accept=accept)

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

        async def get_json(
            self,
            path: str,
            *,
            params: dict[str, Any] | None = None,
            accept: str | None = None,
        ) -> Any:
            if path.startswith(f"{_BASE}/issues/"):
                self.started += 1
                if self.started == self.concurrency:
                    self.window_full.set()
                await self.release.wait()
            return await super().get_json(path, params=params, accept=accept)

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
async def test_rate_limit_waits_asynchronously_and_does_not_consume_retry_budget() -> None:
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
        transient_retries=0,
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
    assert waits[0].quota_limit == 5000
    assert waits[0].quota_remaining == 0
    assert waits[0].quota_resource == "core"
    assert waits[0].quota_reset_at == _T0 + timedelta(seconds=2)


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
async def test_graphql_catalog_paginates_both_connections_with_one_query_stream() -> None:
    seen: list[httpx.Request] = []
    responses = [
        {
            "data": {
                "repository": {
                    "issues": {
                        "totalCount": 2,
                        "pageInfo": {"hasNextPage": True, "endCursor": "issue-1"},
                        "nodes": [
                            {
                                "number": 3,
                                "createdAt": "2026-08-01T11:00:00Z",
                                "updatedAt": "2026-08-01T11:30:00Z",
                            },
                        ],
                    },
                    "pullRequests": {
                        "totalCount": 1,
                        "pageInfo": {"hasNextPage": False, "endCursor": "pull-1"},
                        "nodes": [
                            {
                                "number": 2,
                                "createdAt": "2026-08-01T10:00:00Z",
                                "updatedAt": "2026-08-01T10:30:00Z",
                            },
                        ],
                    },
                },
            },
        },
        {
            "data": {
                "repository": {
                    "issues": {
                        "totalCount": 2,
                        "pageInfo": {"hasNextPage": False, "endCursor": "issue-2"},
                        "nodes": [
                            {
                                "number": 1,
                                "createdAt": "2026-08-01T09:00:00Z",
                                "updatedAt": "2026-08-01T09:30:00Z",
                            },
                        ],
                    },
                },
            },
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=responses.pop(0), request=request)

    page_sizes: list[int] = []
    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(token=str(id(client)), client=client, graphql_url="/graphql")
    try:
        result = await api.repository_catalog(
            "acme",
            "widgets",
            page_observer=page_sizes.append,
        )
    finally:
        await client.aclose()

    assert result is not None
    catalog, count = result
    assert ([item["number"] for item in catalog], count) == ([3, 1, 2], 3)
    assert catalog[0] == {
        _CATALOG_HINT: True,
        "number": 3,
        "created_at": "2026-08-01T11:00:00Z",
        "updated_at": "2026-08-01T11:30:00Z",
    }
    assert catalog[-1]["pull_request"] == {}
    assert page_sizes == [2, 1]
    assert api.request_count == 2
    bodies = [json.loads(request.content) for request in seen]
    assert bodies[0]["variables"] == {
        "owner": "acme",
        "repo": "widgets",
        "issuesAfter": None,
        "pullsAfter": None,
        "scanIssues": True,
        "scanPulls": True,
    }
    assert bodies[1]["variables"] == {
        "owner": "acme",
        "repo": "widgets",
        "issuesAfter": "issue-1",
        "pullsAfter": "pull-1",
        "scanIssues": True,
        "scanPulls": False,
    }
    assert bodies[0]["query"].count("orderBy: {field: CREATED_AT, direction: DESC}") == 2


@pytest.mark.asyncio
async def test_graphql_catalog_rejects_a_truncated_connection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "issues": {
                            "totalCount": 2,
                            "pageInfo": {"hasNextPage": False, "endCursor": "issue-1"},
                            "nodes": [
                                {
                                    "number": 1,
                                    "createdAt": "2026-08-01T09:00:00Z",
                                    "updatedAt": "2026-08-01T09:30:00Z",
                                },
                            ],
                        },
                        "pullRequests": {
                            "totalCount": 0,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        },
                    },
                },
            },
            request=request,
        )

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(token=str(id(client)), client=client, graphql_url="/graphql")
    try:
        with pytest.raises(GitHubAPIError, match="catalog changed while paginating"):
            await api.repository_catalog("acme", "widgets")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_anonymous_repository_discovery_uses_no_graphql_quota() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected request: {request.url}")

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client, graphql_url="/graphql")
    try:
        assert await api.repository_item_count("acme", "widgets") is None
        assert await api.repository_catalog("acme", "widgets") is None
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
    assert waits[0].quota_remaining == 0


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
async def test_collection_size_uses_one_item_last_page_certificate() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        link = (
            '<https://api.github.test/items?sort=created&per_page=1&page=2>; rel="next", '
            '<https://api.github.test/items?sort=created&per_page=1&page=237>; rel="last"'
        )
        return httpx.Response(200, headers={"link": link}, json=[{"id": 1}], request=request)

    client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    api = GitHubAPI(client=client)
    try:
        size = await api.collection_size("/items", params={"sort": "created"})
    finally:
        await client.aclose()

    assert size == 237
    assert len(seen) == 1
    assert "per_page=1" in seen[0]


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
async def test_existing_fact_archive_adds_discardable_http_cache_on_open(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_issue(1)
    archive = tmp_path / "archive"
    clock = Clock(_T0)
    puller = GitHubPuller(_config(archive), api=api, now=clock, sleep=clock.sleep)
    await puller.pull(_T0)

    db = await aiosqlite.connect(archive)
    try:
        await db.execute("DROP TABLE bundle_http_cache")
        await db.execute(
            "UPDATE archive_meta SET value = '2' WHERE key = 'schema_version'",
        )
        await db.commit()
    finally:
        await db.close()
    clock.current += timedelta(hours=1)
    await puller.pull(clock.current)

    meta = await _rows(
        archive,
        "SELECT value FROM archive_meta WHERE key = 'schema_version'",
    )
    tables = await _rows(
        archive,
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'bundle_http_cache'",
    )
    assert meta == [{"value": "3"}]
    assert tables == [{"name": "bundle_http_cache"}]


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
        quota_limit=5000,
        quota_remaining=4993,
        quota_reset_at=_T0 + timedelta(hours=1),
        quota_resource="core",
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
    assert lines[0]["quota_reset_at"] == _iso(_T0 + timedelta(hours=1))
    assert lines[1]["phase"] == "done"

    terminal = StringIO()
    tty = ConsoleProgress(terminal, interval=0, tty=True, monotonic=lambda: 0.0)
    tty(progress)
    tty(replace(progress, phase="done", bundles_completed=2))
    rendered = terminal.getvalue()
    assert "[##########----------] 1/2" in rendered
    assert "issues=1 prs=0" in rendered
    assert "latest=issue#1" in rendered
    assert rendered.endswith("\n")
