"""Exercise observed session termination without changing exception or resource ownership."""

import asyncio

import pytest

from gh_puller.agent.base import BaseAgent, RequestFailedError
from gh_puller.agent.events import EventRecorder
from tests.agent._support import capture, settle


class Probe(BaseAgent):
    agent = "lifecycle-probe"

    def __init__(self, *, initialize_error=None, cleanup_error=None):
        super().__init__({})
        self.initialize_error, self.cleanup_error = initialize_error, cleanup_error
        self.exit_contexts = []

    async def _enter(self):
        if self.initialize_error is not None:
            raise self.initialize_error

    async def _exit(self, exc):
        self.exit_contexts.append(exc)
        await asyncio.sleep(0)
        if self.cleanup_error is not None:
            raise self.cleanup_error


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "code"), [
    (RuntimeError("run failed"), "error"),
    (TimeoutError("deadline"), "timeout"),
    (asyncio.CancelledError(), "cancelled"),
    (RequestFailedError("steps exhausted", reason_code="budget_exhausted"), "budget_exhausted"),
    (RequestFailedError("provider timeout", reason_code="timeout"), "timeout"),
])
async def test_run_failures_propagate_with_structured_reasons(tmp_path, error, code):
    events = await capture(tmp_path)
    subject = Probe()
    with pytest.raises(type(error)) as raised:
        async with subject.session():
            raise error
    await settle()
    assert raised.value is error and subject.exit_contexts[0][1] is error
    summary = events[-1]["data"]
    assert (summary["outcome"], summary["reasonCode"], summary["phase"]) == ("failed", code, "run")
    failure = next(event["data"] for event in events if event["type"] == "session/error")
    assert failure["reasonCode"] == code and failure["error"]["type"] == type(error).__name__
    assert subject._event_recorder is None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["initialize", "cleanup"])
async def test_hook_failures_keep_their_stage_and_cleanup_contract(tmp_path, phase):
    events = await capture(tmp_path)
    subject = Probe(**{phase + "_error": ValueError(phase)})
    with pytest.raises(ValueError, match=phase):
        async with subject.session():
            pass
    await settle()
    assert events[-1]["data"]["phase"] == phase
    assert events[-1]["data"]["reason"] == phase
    assert len(subject.exit_contexts) == (0 if phase == "initialize" else 1)
    assert subject._event_recorder is None


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_erase_the_primary_failure(tmp_path):
    events = await capture(tmp_path)
    subject = Probe(cleanup_error=ValueError("cleanup failed"))
    with pytest.raises(ValueError, match="cleanup failed") as raised:
        async with subject.session():
            raise RequestFailedError("budget reached", reason_code="budget_exhausted")
    await settle()
    assert isinstance(raised.value.__context__, RequestFailedError)
    failures = [event["data"] for event in events if event["type"] == "session/error"]
    assert [failure["phase"] for failure in failures] == ["run", "cleanup"]
    assert events[-1]["data"]["reason"] == "budget reached"
    assert events[-1]["data"]["reasonCode"] == "budget_exhausted"
    assert len([event for event in events if event["type"] == "session/end"]) == 1


@pytest.mark.asyncio
async def test_real_task_cancellation_awaits_cleanup_and_remains_cancelled(tmp_path):
    events = await capture(tmp_path)
    subject, entered = Probe(), asyncio.Event()

    async def work():
        async with subject.session():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await settle()
    assert task.cancelled() and len(subject.exit_contexts) == 1
    assert events[-1]["data"]["reasonCode"] == "cancelled"


@pytest.mark.asyncio
async def test_adapter_deadline_is_distinct_from_task_cancellation(tmp_path):
    events = await capture(tmp_path)
    subject = Probe()
    with pytest.raises(TimeoutError):
        async with subject.session(), asyncio.timeout(0.01):
            await asyncio.Event().wait()
    await settle()
    assert events[-1]["data"]["reasonCode"] == "timeout"


@pytest.mark.asyncio
async def test_footer_survives_observer_cleanup_failure(tmp_path, monkeypatch):
    events = await capture(tmp_path)
    subject = Probe()

    async def stop(_self):
        raise RuntimeError("observer cleanup failed")

    monkeypatch.setattr(EventRecorder, "stop_keepwarm", stop)
    with pytest.raises(RuntimeError, match="observer cleanup failed"):
        async with subject.session():
            pass
    await settle()
    assert events[-1]["type"] == "session/end"
    assert events[-1]["data"]["reasonCode"] == "error"
    assert events[-1]["data"]["phase"] == "cleanup"
    assert subject._event_recorder is None


@pytest.mark.asyncio
async def test_success_retains_existing_outcome_and_explicit_completion_reason(tmp_path):
    events = await capture(tmp_path)
    subject = Probe()
    async with subject.session():
        pass
    await settle()
    assert events[-1]["data"]["outcome"] == "completed"
    assert events[-1]["data"]["reasonCode"] == "completed"
    assert "phase" not in events[-1]["data"]
    assert subject.exit_contexts == [(None, None, None)]
