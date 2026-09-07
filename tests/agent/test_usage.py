"""Verify lossless adapter usage records and conservative normalized token counters."""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from gh_puller.agent.events import EventRecorder, normalize_usage
from tests.agent._support import capture, settle


@pytest.mark.parametrize(("value", "expected"), [
    (None, None),
    ({}, None),
    ({"prompt_tokens": None, "completion_tokens": None}, None),
    ({"prompt_tokens": 0, "completion_tokens": 0}, {"input": 0, "output": 0}),
    ({"prompt_tokens": 12}, {"input": 12}),
    ({"input": 0, "prompt_tokens": 12}, {"input": 0}),
    ({"prompt_tokens": -1, "completion_tokens": True}, None),
    ({"prompt_tokens": "12", "completion_tokens": 1.5}, None),
    ({"prompt_tokens_details": None, "completion_tokens_details": None}, None),
    ({"prompt_tokens_details": {"cached_tokens": 0},
      "completion_tokens_details": {"reasoning_tokens": 0}}, {"cacheRead": 0, "reasoning": 0}),
    ({"input_tokens_details": {"cached_tokens": 7},
      "output_tokens_details": {"reasoning_tokens": 4}}, {"cacheRead": 7, "reasoning": 4}),
    ({"cacheRead": 0, "prompt_tokens_details": {"cached_tokens": 7},
      "reasoning": 1, "completion_tokens_details": {"reasoning_tokens": 4}}, {"cacheRead": 0, "reasoning": 1}),
    (SimpleNamespace(input_tokens=8, completion_tokens_details=SimpleNamespace(reasoning_tokens=2)),
     {"input": 8, "reasoning": 2}),
])
def test_normalization_preserves_zero_and_unknown(value, expected):
    assert normalize_usage(value) == expected


@pytest.mark.asyncio
async def test_model_records_preserve_raw_details_without_aliasing_or_credentials(tmp_path):
    events = await capture(tmp_path)
    recorder = EventRecorder("usage")
    usage = {"prompt_tokens": 100, "completion_tokens": 9,
             "prompt_tokens_details": {"cached_tokens": 80, "audio_tokens": 3},
             "completion_tokens_details": {"reasoning_tokens": 6}, "api_key": "fixture-secret"}
    request = recorder.model_request()
    recorder.model_response([], request_id=request, usage=usage)
    usage["prompt_tokens_details"]["cached_tokens"] = 999
    await settle()
    response = next(event["data"] for event in events if event["type"] == "model/response")
    assert response["usage"] == {"input": 100, "output": 9, "cacheRead": 80, "reasoning": 6}
    assert response["rawUsage"]["prompt_tokens_details"] == {"cached_tokens": 80, "audio_tokens": 3}
    assert response["rawUsage"]["api_key"] == "<redacted>"


@dataclass
class CounterRecord:
    prompt_tokens: int
    completion_tokens: int


class ModelCounterRecord:
    def model_dump(self, *, mode):
        assert mode == "json"
        return {"prompt_tokens": 4, "completion_tokens": 0}

    prompt_tokens = 4
    completion_tokens = 0


@pytest.mark.asyncio
@pytest.mark.parametrize("record", [CounterRecord(4, 0), ModelCounterRecord(),
                                    SimpleNamespace(prompt_tokens=4, completion_tokens=0)])
async def test_sdk_usage_record_shapes_are_serialized(tmp_path, record):
    events = await capture(tmp_path)
    recorder = EventRecorder("sdk-usage")
    recorder.model_response([], request_id="one", usage=record)
    await settle()
    response = events[-1]["data"]
    assert response["usage"] == {"input": 4, "output": 0}
    assert response["rawUsage"] == {"prompt_tokens": 4, "completion_tokens": 0}


@pytest.mark.asyncio
async def test_backend_summary_replaces_instead_of_double_counting_responses(tmp_path):
    events = await capture(tmp_path)
    recorder = EventRecorder("summary")
    recorder.start()
    for identifier in ("one", "two"):
        recorder.model_request(request_id=identifier)
        recorder.model_response([], request_id=identifier, usage={"input": 10, "output": 3})
    recorder.result_meta(SimpleNamespace(usage={"prompt_tokens": 20, "completion_tokens": 6}))
    recorder.finish(True)
    await settle()
    summary = events[-1]["data"]
    assert summary["usage"] == {"input": 20, "output": 6}
    assert summary["rawUsage"] == {"prompt_tokens": 20, "completion_tokens": 6}


@pytest.mark.asyncio
async def test_missing_summary_and_new_requests_clear_stale_raw_usage(tmp_path):
    events = await capture(tmp_path)
    recorder = EventRecorder("missing")
    recorder.result_meta(SimpleNamespace(usage={"input": 9}))
    recorder.result_meta(SimpleNamespace(usage=None))
    assert recorder.result_usage is None and recorder.result_raw_usage is None
    recorder.result_meta(SimpleNamespace(usage={"input": 0, "output": 0}))
    recorder.model_request()
    recorder.finish(False)
    await settle()
    assert "usage" not in events[-1]["data"] and "rawUsage" not in events[-1]["data"]


@pytest.mark.asyncio
async def test_zero_summary_is_observed_instead_of_omitted(tmp_path):
    events = await capture(tmp_path)
    recorder = EventRecorder("zero")
    recorder.result_meta(SimpleNamespace(usage={"input": 0, "output": 0}))
    recorder.finish(True)
    await settle()
    assert events[-1]["data"]["usage"] == {"input": 0, "output": 0}
