"""Schemas exported from mux-pinned Codex 0.159.0-alpha.12.1; no RPC fixtures."""
import copy

import pytest

from aiworkhub import codex_manager_observation as observation


def usage_event(thread="owned"):
    counters = {key: 0 for key in observation.COUNTERS}
    return {"method": "thread/tokenUsage/updated", "params": {
        "threadId": thread, "turnId": "turn", "tokenUsage": {
            "last": dict(counters), "total": dict(counters), "modelContextWindow": None}}}


def test_qualified_usage_zero_null_privacy_and_timestamp():
    event = usage_event()
    event["params"]["tokenUsage"]["secret"] = "never retain"
    event["params"]["tokenUsage"]["last"]["prompt"] = "never retain"
    before = copy.deepcopy(event)
    snapshot = observation.observe(event, "owned", {}, 100)
    data = observation.hydrate({**snapshot, "generation_id": "g"}, "owned", "g")
    result = observation.project(data, 101, 90)
    assert result["usage_observed"] is True
    assert result["usage"]["last"]["totalTokens"] == 0
    assert result["usage"]["modelContextWindow"] is None
    assert "cacheWriteInputTokens" not in result["usage"]["last"]
    assert "never retain" not in str(data)
    assert result["usage_observed_at"] == 100
    assert observation.project(data, 191, 90)["usage_reason"] == "stale_observation"
    assert "usage" not in observation.project(data, 191, 90)
    assert event == before
    assert observation.hydrate({**snapshot, "generation_id": "old"}, "owned", "new") == {}


@pytest.mark.parametrize("value", [True, -1, 1.5, "0", None, 1 << 63, float("nan"), float("inf")])
def test_malformed_counters_do_not_replace_or_grow(value):
    event = usage_event()
    event["params"]["tokenUsage"]["last"]["inputTokens"] = value
    previous = {"thread_id": "owned"}
    assert observation.observe(event, "owned", previous, 100) is previous


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), 1 << 10000])
def test_invalid_timestamps_fail_closed(value):
    assert observation.observe(usage_event(), "owned", {}, value) == {}
    snapshot = observation.observe(usage_event(), "owned", {}, 100)
    assert observation.project(snapshot, value, 90)["usage_observed"] is False
    assert observation.project(snapshot, 100, value)["usage_observed"] is False
    snapshot["usage_observed_at"] = value
    assert "usage" not in observation.hydrate({**snapshot, "generation_id": "g"}, "owned", "g")


def test_foreign_oversized_and_unknown_notifications_never_grow():
    previous = {}
    for index in range(1000):
        assert observation.observe(usage_event(str(index)), "owned", previous, 100) is previous
    event = usage_event()
    event["params"]["turnId"] = "x" * 129
    assert observation.observe(event, "owned", previous, 100) is previous
    assert observation.observe({"method": "unknown"}, "owned", previous, 100) is previous
    assert observation.project({}, 100, 90)["status_observed"] is False


@pytest.mark.parametrize("kind,flags", [("idle", None), ("notLoaded", None),
                                        ("systemError", None), ("active", [])])
def test_qualified_status_and_independent_usage(kind, flags):
    status = {"type": kind}
    if flags is not None:
        status["activeFlags"] = flags
    event = {"method": "thread/status/changed", "params": {"threadId": "owned", "status": status}}
    snapshot = observation.observe(event, "owned", {}, 100)
    assert observation.project(snapshot, 100, 90)["status"] == status
    assert observation.project(snapshot, 100, 90)["usage_observed"] is False
    snapshot = observation.observe(usage_event(), "owned", snapshot, 102)
    assert snapshot["status_observed_at"] == 100
    event["params"]["status"] = {"type": "active", "activeFlags": ["secret"]}
    assert observation.observe(event, "owned", snapshot, 103) is snapshot


@pytest.mark.parametrize("flags", [None, True, "waitingOnApproval",
                                   ["waitingOnApproval", "waitingOnApproval"], [{}], ["secret"]])
def test_malformed_status_flags_fail_closed(flags):
    event = {"method": "thread/status/changed", "params": {
        "threadId": "owned", "status": {"type": "active", "activeFlags": flags}}}
    previous = {}
    assert observation.observe(event, "owned", previous, 100) is previous


@pytest.mark.parametrize("capacity", [True, 0, -1, 1 << 63, float("nan"), float("inf"), "100"])
def test_malformed_capacity_fail_closed(capacity):
    event = usage_event()
    event["params"]["tokenUsage"]["modelContextWindow"] = capacity
    previous = {}
    assert observation.observe(event, "owned", previous, 100) is previous


def test_missing_optional_capacity_is_unknown_not_zero():
    event = usage_event()
    del event["params"]["tokenUsage"]["modelContextWindow"]
    snapshot = observation.observe(event, "owned", {}, 100)
    assert snapshot["usage"]["modelContextWindow"] is None
    assert "cacheWriteInputTokens" not in snapshot["usage"]["last"]
