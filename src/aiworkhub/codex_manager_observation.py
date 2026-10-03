"""Passive, content-free Codex metadata; no RPC or context-fill inference.

Wire shapes qualified from the mux-pinned Codex 0.159.0-alpha.12.1
generate-json-schema export: v2 ThreadTokenUsageUpdatedNotification and
ThreadStatusChangedNotification. Keep only the active owned thread snapshot:
512 owned threads must not multiply the existing 64-KiB descriptor budget.
"""
from __future__ import annotations

import copy
import math
from typing import Any

COUNTERS = ("cachedInputTokens", "inputTokens", "outputTokens",
            "reasoningOutputTokens", "totalTokens")
FLAGS = ("waitingOnApproval", "waitingOnUserInput")
MAX_COUNTER = (1 << 63) - 1


def _counter(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_COUNTER


def _timestamp(value: Any) -> bool:
    if type(value) is int:
        return 0 < value <= MAX_COUNTER
    return type(value) is float and math.isfinite(value) and value > 0


def _usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for section in ("last", "total"):
        data = value.get(section)
        if not isinstance(data, dict) or not all(_counter(data.get(k)) for k in COUNTERS):
            return None
        result[section] = {k: data[k] for k in COUNTERS}
        if "cacheWriteInputTokens" in data:
            if not _counter(data["cacheWriteInputTokens"]):
                return None
            result[section]["cacheWriteInputTokens"] = data["cacheWriteInputTokens"]
    capacity = value.get("modelContextWindow")
    if capacity is not None and (not _counter(capacity) or capacity == 0):
        return None
    result["modelContextWindow"] = capacity
    return result


def _status(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    kind = value.get("type")
    if kind not in ("idle", "active", "notLoaded", "systemError"):
        return None
    result = {"type": kind}
    if kind == "active":
        flags = value.get("activeFlags")
        if (not isinstance(flags, list) or len(flags) > len(FLAGS)
                or any(f not in FLAGS for f in flags) or len(set(flags)) != len(flags)):
            return None
        result["activeFlags"] = list(flags)
    return result


def observe(message: Any, active_thread: str, previous: dict[str, Any], now: float) -> dict[str, Any]:
    """Coalesce one already-owned active thread, without storing arbitrary input."""
    if (not isinstance(active_thread, str) or not 0 < len(active_thread) <= 128
            or not isinstance(message, dict) or "id" in message or not _timestamp(now)):
        return previous
    params = message.get("params")
    if not isinstance(params, dict) or params.get("threadId") != active_thread:
        return previous
    method = message.get("method")
    if method == "thread/tokenUsage/updated":
        parsed = _usage(params.get("tokenUsage"))
        turn = params.get("turnId")
        if parsed is None or not isinstance(turn, str) or not turn or len(turn) > 128:
            return previous
        update = {"usage": parsed, "usage_observed_at": now, "turn_id": turn}
    elif method == "thread/status/changed":
        parsed = _status(params.get("status"))
        if parsed is None:
            return previous
        update = {"status": parsed, "status_observed_at": now}
    else:
        return previous
    result = dict(previous) if previous.get("thread_id") == active_thread else {}
    return {**result, "thread_id": active_thread, **update}


def hydrate(value: Any, active_thread: str, generation: str) -> dict[str, Any]:
    """Revalidate descriptor metadata independently of its routing authority."""
    if (not isinstance(value, dict) or value.get("thread_id") != active_thread
            or value.get("generation_id") != generation or not active_thread or not generation):
        return {}
    result: dict[str, Any] = {"thread_id": active_thread, "generation_id": generation}
    usage = _usage(value.get("usage"))
    turn = value.get("turn_id")
    if (usage is not None and _timestamp(value.get("usage_observed_at"))
            and isinstance(turn, str) and 0 < len(turn) <= 128):
        result.update(usage=usage, usage_observed_at=value["usage_observed_at"], turn_id=turn)
    status = _status(value.get("status"))
    if status is not None and _timestamp(value.get("status_observed_at")):
        result.update(status=status, status_observed_at=value["status_observed_at"])
    return result


def project(value: dict[str, Any], now: float, lease: float) -> dict[str, Any]:
    """Event freshness is independent of descriptor heartbeat freshness."""
    result: dict[str, Any] = {
        "source": "codex_app_server_notifications", "usage_observed": False,
        "status_observed": False, "usage_reason": "unobserved", "status_reason": "unobserved",
        "usage_labels": {"last": "last_provider_usage", "total": "cumulative_provider_usage"},
    }
    if not _timestamp(now) or not _timestamp(lease):
        return result
    for key in ("usage", "status"):
        observed_at = value.get(key + "_observed_at")
        if key not in value or not _timestamp(observed_at):
            continue
        if not 0 <= now - observed_at <= lease:
            result[key + "_reason"] = "stale_observation"
            continue
        result[key + "_observed"] = True
        result[key + "_reason"] = "observed"
        result[key + "_observed_at"] = observed_at
        result[key] = copy.deepcopy(value[key])
    return result
