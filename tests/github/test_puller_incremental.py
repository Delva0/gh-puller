"""Test incremental GitHub pull recovery, concurrency, and idempotency."""

from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from gh_puller.github import (
    GitHubAPIError,
    GitStoreError,
    PullProgress,
    iter_heads,
)

if TYPE_CHECKING:
    from pathlib import Path

    from gh_puller.github.client import GitHubPage

from tests.github._puller_support import (
    _BASE,
    _T0,
    Clock,
    FakeAPI,
    FakeGitStore,
    _apply_churn_epoch,
    _config,
    _current,
    _iso,
    _puller,
    _rows,
    _runs,
    _seed_churn_api,
    _versions,
)


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
    git = FakeGitStore()
    puller = _puller(
        _config(archive),
        api=api,
        git=git,
        now=lambda: _T0,
        observer=events.append,
    )
    first = await puller.pull(_T0)
    calls = len(api.calls)
    events.clear()

    repeated = await puller.pull(_T0)

    assert repeated == first
    assert len(api.calls) == calls
    assert git.syncs == 1
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
async def test_git_retry_is_observable_and_restores_the_work_phase(tmp_path: Path) -> None:
    class RetryingGitStore(FakeGitStore):
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
            retry(4)
            heartbeat()

    api = FakeAPI()
    api.add_issue(1, pull=True)
    api.json[f"{_BASE}/pulls/1"] = {
        "id": 10,
        "changed_files": 0,
        "commits": 0,
        "review_comments": 0,
    }
    events: list[PullProgress] = []

    await _puller(
        _config(tmp_path / "archive"),
        api=api,
        git=RetryingGitStore(),
        now=lambda: _T0,
        observer=events.append,
    ).pull(_T0)

    retry_index = next(
        index for index, event in enumerate(events) if event.detail == "git_transient_retry"
    )
    retry = events[retry_index]
    resumed = events[retry_index + 1]
    assert (retry.phase, retry.wait_seconds) == ("retry_wait", 4)
    assert (resumed.phase, resumed.wait_seconds, resumed.detail) == (
        "syncing_git",
        None,
        "pull_refs=1",
    )
    assert events[-1].phase == "done"


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

        async def capture(
            self,
            number: int,
            pull: dict[str, Any],
            *,
            heartbeat: Any = None,
            retry: Any = None,
        ) -> dict[str, Any]:
            if not self.failed:
                self.failed = True
                raise GitStoreError("injected Git failure")
            return await super().capture(
                number,
                pull,
                heartbeat=heartbeat,
                retry=retry,
            )

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
