from __future__ import annotations

import concurrent.futures
import json
import math
import os
import sys
from collections.abc import Callable, Mapping
from typing import Any

from . import platform_io, runtime_generation

SCHEMA_ID = "aiworkhub.doctor_diagnose.v1"
STATE_OK = "ok"
STATE_PROBLEM = "problem"
STATE_UNAVAILABLE = "unavailable"
# A probe still running at the deadline only proves it is slow, not that it is down.
STATE_SLOW = "slow"
VERDICT_DEGRADED = "degraded"
SUMMARY_MAX_CHARS = 200
REPLY_MAX_BYTES = 8 * 1024
NESTED_PROBE_KEYS = ("reconciler", "learning_coverage")
DETAIL_KEYS = ("error", "last_error", "reason")

Probe = Callable[[], dict[str, Any]]


def _bounded(text: Any) -> str:
    return str(text)[:SUMMARY_MAX_CHARS]


def _detail(payload: Mapping[str, Any]) -> str:
    for key in DETAIL_KEYS:
        value = payload.get(key)
        if value not in (None, ""):
            return _bounded(value)
    return "ok=false"


def _source_graph_generation_problem(result: Mapping[str, Any]) -> str | None:
    """Name a never-built or stale Source Graph index (NF-2026-02081).

    Only a payload carrying the canonical-generation projection
    (``readable_generation``) is judged. An index that has never produced a
    readable generation, or whose generation is older than the stale threshold,
    is a problem even when the writer process itself answers ``ok``.
    """
    if "readable_generation" not in result:
        return None
    if result.get("readable_generation") is not True:
        detail = result.get("generation_read_error") or result.get("status") or "unknown"
        return _bounded(f"source_graph_never_built: no readable generation ({detail})")
    age = result.get("index_age_seconds")
    if isinstance(age, bool) or not isinstance(age, (int, float)):
        return None
    threshold = result.get("stale_after_seconds")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or threshold <= 0:
        # An unregistered daemon reports no threshold; judge it against the
        # daemon's own default instead of skipping the check.
        from .source_graph_daemon import (
            DEFAULT_REFRESH_INTERVAL_SECONDS,
            DEFAULT_STALE_MULTIPLIER,
            MIN_STALE_AFTER_SECONDS,
        )

        threshold = max(
            MIN_STALE_AFTER_SECONDS,
            DEFAULT_REFRESH_INTERVAL_SECONDS * DEFAULT_STALE_MULTIPLIER,
        )
    if float(age) > float(threshold):
        return _bounded(
            f"source_graph_stale: generation age {float(age):.0f}s exceeds {float(threshold):.0f}s"
        )
    return None


def _problem_summary(result: Mapping[str, Any]) -> str | None:
    """Return the excerpt for a failing probe result, or None when it is healthy."""
    if result.get("ok") is False:
        detail = _detail(result)
        if detail != "ok=false":
            return detail
        generation = _source_graph_generation_problem(result)
        return generation if generation is not None else detail
    for key in NESTED_PROBE_KEYS:
        nested = result.get(key)
        if isinstance(nested, Mapping) and nested.get("ok") is False:
            return _bounded(f"{key}: {_detail(nested)}")
    return _source_graph_generation_problem(result)


def _entry(name: str, state: str, summary: str) -> dict[str, Any]:
    return {"name": name, "state": state, "summary": summary, "next_tool": name}


def _classify(name: str, result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return _entry(name, STATE_UNAVAILABLE, _bounded(f"non_dict:{type(result).__name__}"))
    problem = _problem_summary(result)
    if problem is not None:
        return _entry(name, STATE_PROBLEM, problem)
    return _entry(name, STATE_OK, "")


def _verdict(entries: list[dict[str, Any]]) -> str:
    states = {entry["state"] for entry in entries}
    if STATE_PROBLEM in states:
        return STATE_PROBLEM
    if STATE_UNAVAILABLE in states:
        return STATE_UNAVAILABLE
    if STATE_SLOW in states:
        return VERDICT_DEGRADED
    return STATE_OK


def _encoded_size(payload: Mapping[str, Any]) -> int:
    return len(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def diagnose(
    probes: Mapping[str, Probe], *, timeout_seconds: float = 10.0
) -> dict[str, Any]:
    """Run every probe concurrently and fold the results into one verdict."""
    if not probes:
        raise ValueError("doctor_diagnose_requires_at_least_one_probe")
    # Probes are IO-bound; keep one core free for the MCP server itself.
    workers = min(len(probes), max(1, (os.cpu_count() or 2) - 1))
    # One timeout per wave of workers; a probe still queued at the deadline is cancelled.
    deadline = timeout_seconds * math.ceil(len(probes) / workers)
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="aiworkhub-doctor"
    )
    try:
        futures = {name: pool.submit(probe) for name, probe in probes.items()}
        concurrent.futures.wait(futures.values(), timeout=deadline)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    entries: list[dict[str, Any]] = []
    for name, future in futures.items():
        if future.cancelled():
            # Still queued behind a hung probe at the deadline: it never ran.
            entries.append(_entry(name, STATE_UNAVAILABLE, "not_started"))
            continue
        if not future.done():
            # Still running at the deadline: slow, not down (NF-2026-01967).
            slow = _entry(name, STATE_SLOW, "timeout")
            slow["timeout_seconds"] = timeout_seconds
            entries.append(slow)
            continue
        error = future.exception()
        if error is not None:
            entries.append(_entry(name, STATE_UNAVAILABLE, type(error).__name__))
            continue
        entries.append(_classify(name, future.result()))
    reply: dict[str, Any] = {
        "schema_id": SCHEMA_ID,
        "verdict": _verdict(entries),
        "problems": [entry for entry in entries if entry["state"] != STATE_OK],
        "probes": entries,
    }
    if _encoded_size(reply) > REPLY_MAX_BYTES:
        for entry in entries:
            entry["summary"] = ""
        reply["summaries_dropped"] = True
    return reply


def _or_none(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except Exception:
        return None


def server_process() -> dict[str, Any]:
    """Identify and size the process answering this call; never raises."""

    return {
        "pid": os.getpid(),
        **platform_io.process_memory_bytes(),
        "python_allocated_blocks": sys.getallocatedblocks(),
        "runtime_generation": _or_none(runtime_generation.own_generation),
        "superseded": _or_none(runtime_generation.superseded),
    }
