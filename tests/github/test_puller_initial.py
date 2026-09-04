"""Test initial GitHub pulls and bundle construction."""

from __future__ import annotations

import json
import math
import random
import re
from copy import deepcopy
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from gh_puller.github import (
    GitStoreError,
    IncompleteGitHubDataError,
    PullProgress,
)
from gh_puller.github.git_store import TransientGitStoreError

if TYPE_CHECKING:
    from pathlib import Path

from tests.github._puller_support import (
    _BASE,
    _T0,
    Clock,
    FakeAPI,
    FakeGitStore,
    _closing_issue,
    _config,
    _current,
    _iso,
    _puller,
    _rows,
    _runs,
    _versions,
)


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
    assert bundle["schema_version"] == 7
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
    api.pages[f"{pull_path}/commits"] = [{"sha": "c" * 40, "commit": {"message": "change"}}]
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
    assert await _rows(
        tmp_path / "archive",
        """
        SELECT number, base_sha, head_sha, comparison_kind, history_preserved
        FROM current_pull_git
        """,
    ) == [
        {
            "number": 7,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "comparison_kind": "merge_base",
            "history_preserved": None,
        },
    ]
    assert await _rows(
        tmp_path / "archive",
        "SELECT number, ordinal, sha FROM current_pull_commits",
    ) == [{"number": 7, "ordinal": 0, "sha": "c" * 40}]
    assert not any(call[1] == f"{issue_path}/comments" for call in api.calls)


@pytest.mark.asyncio
async def test_rest_and_graphql_atomic_operations_publish_equal_stable_facts(
    tmp_path: Path,
) -> None:
    def api_fixture(source: str) -> FakeAPI:
        api = FakeAPI(source)
        api.add_issue(7, pull=True)
        issue_path = f"{_BASE}/issues/7"
        pull_path = f"{_BASE}/pulls/7"
        api.json[issue_path]["comments"] = 1
        api.json[issue_path]["node_id"] = "parent-7"
        api.pages[f"{issue_path}/comments"] = [
            {
                "id": 70,
                "node_id": "issue-comment-70",
                "body": "discussion",
                "reactions": {"total_count": 1},
            },
        ]
        api.pages[f"{_BASE}/issues/comments/70/reactions"] = [
            {"id": 701, "content": "heart"},
        ]
        api.json[pull_path] = {
            "id": 700,
            "base": {"sha": "a" * 40},
            "changed_files": 1,
            "commits": 1,
            "head": {"sha": "b" * 40},
            "review_comments": 1,
            "requested_reviewers": [],
            "requested_teams": [],
        }
        api.pages[f"{pull_path}/reviews"] = [
            {"id": 71, "state": "APPROVED", "body": "ship it"},
        ]
        api.pages[f"{pull_path}/comments"] = [
            {
                "id": 72,
                "node_id": "review-comment-72",
                "body": "nit",
                "path": "a.py",
                "reactions": {"total_count": 1},
            },
        ]
        api.pages[f"{_BASE}/pulls/comments/72/reactions"] = [
            {"id": 73, "content": "+1"},
        ]
        api.pages[f"{pull_path}/commits"] = [
            {"sha": "c" * 40, "commit": {"message": "change"}},
        ]
        return api

    bundles = []
    for source in ("rest", "graphql"):
        destination = tmp_path / f"{source}.sqlite3"
        await _puller(
            _config(destination),
            api=api_fixture(source),
            git=FakeGitStore(),
            now=lambda: _T0,
        ).pull(_T0)
        bundle = (await _current(destination))[7].bundle
        assert bundle is not None
        bundles.append(bundle)

    rest_bundle, graphql_bundle = map(deepcopy, bundles)
    rest_sources = rest_bundle.pop("api_sources")
    graphql_sources = graphql_bundle.pop("api_sources")
    rest_pull_sources = rest_bundle["pull_request"].pop("api_sources")
    graphql_pull_sources = graphql_bundle["pull_request"].pop("api_sources")

    assert graphql_bundle == rest_bundle
    assert set(graphql_sources) == set(rest_sources) == {
        "issue_comments",
        "issue_comment_reactions",
    }
    assert set(graphql_pull_sources) == set(rest_pull_sources) == {
        "commits",
        "detail",
        "review_comments",
        "review_comment_reactions",
        "reviews",
    }
    def leaves(value: dict[str, Any]) -> list[dict[str, Any]]:
        if "source" in value:
            return [value]
        return [leaf for child in value.values() for leaf in leaves(child)]

    assert {item["source"] for item in leaves(rest_sources)} == {"rest"}
    assert {item["source"] for item in leaves(rest_pull_sources)} == {"rest"}
    assert {item["source"] for item in leaves(graphql_sources)} == {"graphql"}
    assert {item["source"] for item in leaves(graphql_pull_sources)} == {"graphql"}
    assert all("raw" in item for item in leaves(graphql_sources))
    assert all("raw" in item for item in leaves(graphql_pull_sources))


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
        "base": {"sha": "a" * 40},
        "head": {"sha": "b" * 40},
        "changed_files": 0,
        "commits": commit_count,
        "review_comments": 0,
    }
    if commit_count <= 250:
        api.pages[f"{pull_path}/commits"] = commits
    else:
        api.comparisons["a" * 40, "b" * 40] = commits

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
        "base": {"sha": "a" * 40},
        "head": {"sha": "b" * 40},
        "changed_files": 0,
        "commits": 251,
        "review_comments": 0,
    }
    api.comparisons["a" * 40, "b" * 40] = commits
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
async def test_git_prefetch_batches_do_not_split_closing_reference_queries(
    tmp_path: Path,
) -> None:
    api = FakeAPI()
    git = FakeGitStore()
    for number in range(1, 24):
        api.add_issue(number, pull=True)
        api.json[f"{_BASE}/pulls/{number}"] = {
            "id": number * 10,
            "changed_files": 0,
            "commits": 0,
            "review_comments": 0,
        }

    await _puller(
        _config(tmp_path / "archive", concurrency=16),
        api=api,
        git=git,
        now=lambda: _T0,
    ).pull(_T0)

    assert git.prefetches == [list(range(1, 9)), list(range(9, 17)), list(range(17, 24))]
    calls = [call for call in api.calls if call[0] == "closing"]
    assert [call[2]["numbers"] for call in calls] == [list(range(1, 24))]


@pytest.mark.asyncio
async def test_transient_git_ref_is_isolated_deferred_and_eventually_staged(
    tmp_path: Path,
) -> None:
    class IsolatingGitStore(FakeGitStore):
        def __init__(self) -> None:
            super().__init__()
            self.single_failures = 0

        async def prefetch(
            self,
            pulls: dict[int, dict[str, Any]],
            *,
            heartbeat: Any = None,
            retry: Any = None,
            retry_transient: bool = True,
        ) -> None:
            del retry
            assert not retry_transient
            numbers = list(pulls)
            self.prefetches.append(numbers)
            if heartbeat is not None:
                heartbeat()
            if 6 not in numbers:
                return
            if len(numbers) > 1 or self.single_failures == 0:
                self.single_failures += len(numbers) == 1
                raise TransientGitStoreError("transient pull ref failure")

    api = FakeAPI()
    git = IsolatingGitStore()
    for number in range(1, 13):
        api.add_issue(number, pull=True)
        api.json[f"{_BASE}/pulls/{number}"] = {
            "id": number * 10,
            "changed_files": 0,
            "commits": 0,
            "review_comments": 0,
        }
    clock = Clock(_T0)
    progress: list[PullProgress] = []

    result = await _puller(
        _config(tmp_path / "archive", concurrency=8),
        api=api,
        git=git,
        now=clock,
        sleep=clock.sleep,
        observer=progress.append,
    ).pull(_T0)

    assert result.catalog_items == 12
    assert sorted(git.captures) == list(range(1, 13))
    assert git.captures.count(6) == 1
    assert clock.sleeps == [1]
    assert any(event.detail == "git_ref_retry=pull#6" for event in progress)
    assert git.prefetches == [
        list(range(1, 9)),
        list(range(1, 5)),
        list(range(5, 9)),
        [5, 6],
        [5],
        [6],
        [7, 8],
        list(range(9, 13)),
        [6],
    ]


@pytest.mark.asyncio
async def test_completed_git_batches_survive_a_later_permanent_failure(
    tmp_path: Path,
) -> None:
    class FailingGitStore(FakeGitStore):
        async def prefetch(
            self,
            pulls: dict[int, dict[str, Any]],
            *,
            heartbeat: Any = None,
            retry: Any = None,
            retry_transient: bool = True,
        ) -> None:
            del heartbeat, retry, retry_transient
            numbers = list(pulls)
            self.prefetches.append(list(numbers))
            if 9 in numbers:
                raise GitStoreError("permanent pull ref failure")

    def api_fixture() -> FakeAPI:
        api = FakeAPI()
        for number in range(1, 12):
            api.add_issue(number, pull=True)
            api.json[f"{_BASE}/pulls/{number}"] = {
                "id": number * 10,
                "changed_files": 0,
                "commits": 0,
                "review_comments": 0,
            }
        return api

    archive = tmp_path / "archive"
    api = api_fixture()
    failing = FailingGitStore()
    with pytest.raises(GitStoreError, match="permanent pull ref failure"):
        await _puller(
            _config(archive, concurrency=8),
            api=api,
            git=failing,
            now=lambda: _T0,
        ).pull(_T0)

    assert failing.prefetches == [list(range(1, 9)), [9, 10, 11]]
    assert await _rows(
        archive,
        "SELECT count(*) AS count FROM pull_tasks WHERE completed = 1",
    ) == [{"count": 8}]

    resumed_git = FakeGitStore()
    await _puller(
        _config(archive, concurrency=8),
        api=api,
        git=resumed_git,
        now=lambda: _T0,
    ).pull(_T0)
    reference = tmp_path / "reference"
    await _puller(
        _config(reference, concurrency=8),
        api=api_fixture(),
        git=FakeGitStore(),
        now=lambda: _T0,
    ).pull(_T0)

    assert resumed_git.prefetches == [[9, 10, 11]]
    actual = await _current(archive)
    expected = await _current(reference)
    assert {number: head.bundle for number, head in actual.items()} == {
        number: head.bundle for number, head in expected.items()
    }


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
