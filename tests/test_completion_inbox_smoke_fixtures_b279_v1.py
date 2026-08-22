"""B279: executable pytest fixtures for the B278 owner-notification smoke spec.

Source spec (read-only, not modified by this task):
    tools/geoai-task-mcp/eval/task_mcp_owner_notification_smoke_spec_b278_v1.json

Turns B278's 6 fixture-driven smoke scenarios (completion, blocked,
duplicate_runner, stale, usage, tool_error) + 2 edge cases (no_pending_task,
duplicate_runner_cross_reference) into real pytest functions that exercise
``aiworkhub.completion_inbox`` and ``aiworkhub.core`` directly.

Isolation contract (acceptance requirement -- "monkeypatch taskctl subprocess
calls; no live SQLite writes or model CLI launch"):
  * scenario_completion / scenario_blocked / scenario_duplicate_runner /
    scenario_stale / edge_case_no_pending_task / edge_case_duplicate_runner_*
    use ``build_completion_inbox(..., _list_tasks=..., _show_task=...)``
    fixture injection -- this bypasses ``core.list_tasks``/``core.show_task``
    entirely, so ``subprocess.run`` is never invoked (zero live-DB touch).
  * scenario_usage and scenario_tool_error explicitly monkeypatch
    ``aiworkhub.core.subprocess.run`` with ``pytest``'s ``monkeypatch``
    fixture -- the real ``taskctl.py`` binary and the live
    ``task_queue_v1.sqlite`` are never invoked/read/written by this file.
  * No test in this file imports or spawns any model/agent CLI.

This file does NOT edit completion_inbox.py or core.py (read-only,
per task contract). Where the B278 spec's abstract fixture shape does not
match the real code's current field names/behavior, the test documents the
gap explicitly (see NEXT-WAVE items in the companion eval/next_wave JSON)
instead of silently reinterpreting the spec to force a pass.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import completion_inbox, core, task_plan, task_store  # noqa: E402

_NOW = "2026-07-08T10:00:00+00:00"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _entry_by_task_id(entries, task_id):
    return next((e for e in entries if e.get("task_id") == task_id), None)


# ===========================================================================
# scenario_completion (B278 section_1.scenarios.scenario_completion)
# ===========================================================================

def test_scenario_completion():
    fixture_card = {
        "task_id": "FIXTURE_TASK_COMPLETION_01",
        "runner": "fixture_runner_a",
        "claimed_by": "fixture_runner_a",
        "topic": "task_mcp",
        "priority": "high",
        "objective": "fixture objective text",
        "allowed_writes": ["fixture/path/a.json"],
        "updated_at": "2026-07-08T10:00:00+00:00",
        "review_at": "2026-07-08T10:05:00+00:00",
        "validation_status": "PASS",
    }

    def stub_list_tasks(status="pending", topic=None, limit=80):
        if status != "review":
            return {"stdout": "", "returncode": 0}
        return {
            "stdout": f"[review] [task_mcp] [fixture_runner_a] {fixture_card['task_id']}",
            "returncode": 0,
        }

    def stub_show_task(task_id):
        assert task_id == fixture_card["task_id"]
        return {"stdout": json.dumps(fixture_card), "returncode": 0}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp", limit=50, stale_processing_hours=24.0,
        _list_tasks=stub_list_tasks, _show_task=stub_show_task,
    )

    entry = _entry_by_task_id(result["review_queue"], fixture_card["task_id"])
    assert entry is not None
    expected_payload = {
        "task_id": "FIXTURE_TASK_COMPLETION_01",
        "runner": "fixture_runner_a",
        "claimed_by": "fixture_runner_a",
        "topic": "task_mcp",
        "priority": "high",
        "objective": "fixture objective text",
        "allowed_writes": ["fixture/path/a.json"],
        "updated_at": "2026-07-08T10:00:00+00:00",
        "review_at": "2026-07-08T10:05:00+00:00",
        "validation_status": "PASS",
    }
    for key, val in expected_payload.items():
        assert entry[key] == val, key
    # B278 fixture literally says `"runner_task_batch_mismatch": false` (JSON
    # bool); the real code emits Python None (`mismatch or None`), which
    # serializes to JSON null, not false. Documented gap, not re-asserted as
    # `is False` here -- see next_wave item field_shape_mismatches.
    assert entry["runner_task_batch_mismatch"] is None

    template = "Task {task_id} (runner {runner}) is ready for your review; validation {validation_status}."
    rendered = template.format(**entry)
    assert rendered == (
        "Task FIXTURE_TASK_COMPLETION_01 (runner fixture_runner_a) is ready "
        "for your review; validation PASS."
    )


def test_finalization_failures_are_operational_not_ordinary_review_queue():
    cards = {
        "TASK_REVIEW_READY": {
            "task_id": "TASK_REVIEW_READY",
            "runner": "worker_ready",
            "topic": "task_mcp",
            "terminal_substatus": "review_ready",
            "updated_at": "2026-08-09T00:00:00+00:00",
        },
        "TASK_FINALIZE_FAILED": {
            "task_id": "TASK_FINALIZE_FAILED",
            "runner": "worker_failed",
            "topic": "task_mcp",
            "terminal_substatus": "finalize_failed",
            "updated_at": "2026-08-09T00:00:01+00:00",
        },
        "TASK_VALIDATION_SCRATCH_FAILED": {
            "task_id": "TASK_VALIDATION_SCRATCH_FAILED",
            "runner": "worker_scratch",
            "topic": "task_mcp",
            "terminal_substatus": "validation_failed",
            "terminal_review": {
                "substatus": "validation_failed",
                "evidence": {
                    "error": "validation_exec_scratch_unavailable:C:\\Temp:noexec"
                },
            },
            "updated_at": "2026-08-09T00:00:02+00:00",
        },
    }

    def stub_list_tasks(status="pending", topic=None, limit=80):
        if status != "review":
            return {"stdout": "", "returncode": 0}
        return {
            "stdout": "\n".join(
                f"[review] [task_mcp] [{card['runner']}] {task_id}"
                for task_id, card in cards.items()
            ),
            "returncode": 0,
        }

    def stub_show_task(task_id):
        return {"stdout": json.dumps(cards[task_id]), "returncode": 0}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp",
        _list_tasks=stub_list_tasks,
        _show_task=stub_show_task,
    )

    assert [row["task_id"] for row in result["review_queue"]] == [
        "TASK_REVIEW_READY"
    ]
    assert result["review_queue"][0]["quality_reviewer_eligible"] is True
    assert [row["task_id"] for row in result["operational_failures"]] == [
        "TASK_VALIDATION_SCRATCH_FAILED",
        "TASK_FINALIZE_FAILED"
    ]
    assert result["counts"]["review_queue"] == 1
    assert result["counts"]["operational_failures"] == 2


# ===========================================================================
# scenario_blocked -> canonical event_kind "failure" (B278 section_0 mapping)
# ===========================================================================

def test_scenario_blocked():
    fixture_card = {
        "task_id": "FIXTURE_TASK_BLOCKED_02",
        "runner": "fixture_runner_b",
        "topic": "task_mcp",
        "validation_status": "FAIL",
        "validation_error": "shell test exited 1",
        "blocker_reason": "owner_delivery_channel_undecided",
        "updated_at": "2026-07-08T09:00:00+00:00",
    }

    def stub_list_tasks(status="pending", topic=None, limit=80):
        if status != "processing":
            return {"stdout": "", "returncode": 0}
        return {
            "stdout": f"[processing] [task_mcp] [fixture_runner_b] {fixture_card['task_id']}",
            "returncode": 0,
        }

    def stub_show_task(task_id):
        return {"stdout": json.dumps(fixture_card), "returncode": 0}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp", limit=50,
        _list_tasks=stub_list_tasks, _show_task=stub_show_task,
    )

    entry = _entry_by_task_id(result["latest_validation_facts"], fixture_card["task_id"])
    assert entry is not None
    assert entry["runner"] == "fixture_runner_b"
    assert entry["topic"] == "task_mcp"
    assert entry["validation_status"] == "FAIL"
    assert entry["validation_error"] == "shell test exited 1"
    assert entry["blocker_reason"] == "owner_delivery_channel_undecided"
    # Real code sources this from `updated_at` only, key name "last_activity_at"
    # -- spec's envelope calls it "last_recorded_at". Value matches; key does not.
    assert entry["last_activity_at"] == "2026-07-08T09:00:00+00:00"
    assert entry["lifecycle_state"] == "processing"  # extra key not in B278 envelope

    template = "Task {task_id} (runner {runner}) is BLOCKED: {blocker_reason}; last validation {validation_status}."
    rendered = template.format(**entry)
    assert rendered == (
        "Task FIXTURE_TASK_BLOCKED_02 (runner fixture_runner_b) is BLOCKED: "
        "owner_delivery_channel_undecided; last validation FAIL."
    )


# ===========================================================================
# scenario_duplicate_runner (B278 section_1 + section_3 cross-reference edge
# case) -- exercises the REAL `_runner_task_batch_mismatch` pure classifier.
#
# Honest gap: the B278 fixture's literal fields (conflicting_runner_a/b,
# task_id_batch_token as bare ints) do not match how the real classifier
# works -- it reads batch tokens EMBEDDED in the runner/task_id strings
# themselves (`_bNNN` / `_BNNN_`), not separate numeric fields. This test
# constructs a runner/task_id pair that DOES embed tokens (same shape used
# by the B276 smoke test's TASK_MISMATCH_B123_V1 fixture) to exercise the
# real code path, and records the shape gap rather than silently
# reinterpreting the spec's literal fixture into something the code accepts.
# ===========================================================================

def test_scenario_duplicate_runner():
    card = {"task_id": "FIXTURE_TASK_DUP_03_B278", "runner": "fixture_runner_c1_b277"}
    mismatch = completion_inbox._runner_task_batch_mismatch(card)
    assert mismatch != ""
    assert "runner_batch=b277" in mismatch
    assert "task_batch=B278" in mismatch

    # No-mismatch control: same batch token on both sides -> no warning.
    clean_card = {"task_id": "FIXTURE_TASK_DUP_03_B278", "runner": "fixture_runner_c1_b278"}
    assert completion_inbox._runner_task_batch_mismatch(clean_card) == ""

    def stub_list_tasks(status="pending", topic=None, limit=80):
        if status != "processing":
            return {"stdout": "", "returncode": 0}
        return {
            "stdout": f"[processing] [task_mcp] [{card['runner']}] {card['task_id']}",
            "returncode": 0,
        }

    def stub_show_task(task_id):
        return {"stdout": json.dumps({**card, "topic": "task_mcp"}), "returncode": 0}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp", limit=50,
        _list_tasks=stub_list_tasks, _show_task=stub_show_task,
    )
    warning_ids = {e["task_id"] for e in result["runner_mismatch_warnings"]}
    assert warning_ids == {"FIXTURE_TASK_DUP_03_B278"}
    warn_entry = result["runner_mismatch_warnings"][0]
    assert warn_entry["lifecycle_state"] == "processing"
    assert "RUNNER_TASK_BATCH_MISMATCH" in warn_entry["warning"]


def test_edge_case_duplicate_runner_cross_reference():
    """B278 section_3.duplicate_runner_conflict_detail: 'this edge case
    entry only re-confirms the acceptance requirement that duplicate-runner
    is a covered scenario, not a second design.' No new fixture shape is
    introduced here -- this just asserts the same pure classifier used in
    test_scenario_duplicate_runner is deterministic/pure (no I/O, repeatable)."""
    card = {"task_id": "FIXTURE_TASK_DUP_03_B278", "runner": "fixture_runner_c1_b277"}
    first = completion_inbox._runner_task_batch_mismatch(card)
    second = completion_inbox._runner_task_batch_mismatch(dict(card))
    assert first == second != ""


# ===========================================================================
# scenario_stale (B278 section_1.scenarios.scenario_stale)
# ===========================================================================

def test_scenario_stale():
    now = datetime.now(timezone.utc)
    stale_iso = (now - timedelta(hours=54)).isoformat()
    fixture_card = {
        "task_id": "FIXTURE_TASK_STALE_04",
        "runner": "fixture_runner_d",
        "topic": "task_mcp",
        "updated_at": stale_iso,
    }

    def stub_list_tasks(status="pending", topic=None, limit=80):
        if status != "processing":
            return {"stdout": "", "returncode": 0}
        return {
            "stdout": f"[processing] [task_mcp] [fixture_runner_d] {fixture_card['task_id']}",
            "returncode": 0,
        }

    def stub_show_task(task_id):
        return {"stdout": json.dumps(fixture_card), "returncode": 0}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp", limit=50, stale_processing_hours=24.0,
        _list_tasks=stub_list_tasks, _show_task=stub_show_task,
    )

    entry = _entry_by_task_id(result["stale_processing"], fixture_card["task_id"])
    assert entry is not None
    assert entry["runner"] == "fixture_runner_d"
    assert entry["topic"] == "task_mcp"
    assert abs(entry["stale_hours"] - 54.0) < 0.1  # code key: stale_hours, spec: hours_since_activity

    template = "Task {task_id} (runner {runner}) has been claimed for {stale_hours}h with no activity, exceeding the 24.0h threshold."
    rendered = template.format(**entry)
    assert rendered == (
        "Task FIXTURE_TASK_STALE_04 (runner fixture_runner_d) has been "
        "claimed for 54.0h with no activity, exceeding the 24.0h threshold."
    )


# ===========================================================================
# scenario_usage (B278 section_1.scenarios.scenario_usage) -- exercises
# `core.usage_report` against the canonical in-process task engine (B852/
# B863): it reads `task_events` rows directly via `task_store`/sqlite, never
# shells out to `AITools/taskctl.py cmd_usage_report`. This asserts exactly
# one bounded provider call (one canonical sqlite connection) and that
# subprocess.run is never invoked at all.
# ===========================================================================

def test_scenario_usage(tmp_path, monkeypatch):
    root = tmp_path / "repo_usage"
    root.mkdir()
    init = task_store.initialize_repository(root)
    assert init["ok"], init
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))

    def _forbid_subprocess(*args, **kwargs):
        raise AssertionError(f"subprocess.run must not be called; got args={args!r}")

    monkeypatch.setattr(core.subprocess, "run", _forbid_subprocess)

    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "FIXTURE_TASK_USAGE_05", "fixture_runner_e", "task_mcp", "solo", "finished",
                "done", "high", "objective", "{}", _NOW, _NOW,
            ),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (
                "FIXTURE_TASK_USAGE_05", "usage_record", "fixture_runner_e",
                json.dumps({
                    "topic": "task_mcp", "records": 1, "total_tokens": 12800,
                    "input_tokens": 12000, "output_tokens": 800, "cost_usd": 0.21,
                }),
                _NOW,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    connect_calls = []
    real_connect = core._canonical_connect

    def _counting_connect(*args, **kwargs):
        connect_calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(core, "_canonical_connect", _counting_connect)

    # "usage-report" (read aggregate) is distinct from "usage" (write,
    # per-task token recording) -- confirm the write-gate set only guards
    # the mutating subcommand, not this read-only one.
    assert "usage-report" not in core.WRITE_COMMANDS
    assert "usage" in core.WRITE_COMMANDS

    result = core.usage_report(runner="fixture_runner_e", topic="task_mcp")

    assert len(connect_calls) == 1  # exactly one bounded provider (sqlite) call
    assert result["ok"] is True
    assert result["returncode"] == 0
    assert "FIXTURE_TASK_USAGE_05" in result["stdout"]
    assert "12000" in result["stdout"] and "800" in result["stdout"]
    assert "$0.2100" in result["stdout"]

    # Honest gap (unchanged from B278): core.usage_report() returns a
    # human-readable TEXT report, not parseable JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(result["stdout"])


# ===========================================================================
# scenario_tool_error (B278 section_1.scenarios.scenario_tool_error +
# section_2) -- three PASS tests documenting exact current behavior in three
# distinct real code paths, plus one strict-xfail tracking the desired
# (not-yet-implemented) graceful-degrade behavior.
# ===========================================================================

def test_scenario_tool_error_current_bounded_fail_closed(tmp_path, monkeypatch):
    """Current reality (post-B852/B863 canonical in-process task engine):
    core.list_tasks talks to aiworkhub.task_store directly and never shells
    out to subprocess.run at all. A read-source failure there
    (task_store.TaskStoreError, e.g. simulating a locked/unreadable canonical
    sqlite) is caught and returned as a bounded {ok: False, returncode: 1,
    stderr: ...} envelope -- it never raises uncaught. A prior fixture that
    monkeypatched core.subprocess.run to simulate this failure no longer
    exercises any real code path (confirmed by direct code read)."""
    root = tmp_path / "repo_read_failure"
    root.mkdir()
    init = task_store.initialize_repository(root)
    assert init["ok"], init
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))

    def _forbid_subprocess(*args, **kwargs):
        raise AssertionError(f"subprocess.run must not be called; got args={args!r}")

    monkeypatch.setattr(core.subprocess, "run", _forbid_subprocess)

    def _raise(*_a, **_k):
        raise task_store.TaskStoreError("sqlite3.OperationalError: database is locked")

    monkeypatch.setattr(core.task_store, "list_tasks", _raise)

    result = core.list_tasks(status="pending", topic="task_mcp")
    assert result["ok"] is False
    assert result["returncode"] == 1
    assert "database is locked" in result["stderr"]


def test_scenario_tool_error_list_failure_is_visible_as_read_error():
    """A LIST-call outage must not look like a genuinely empty queue.

    ``fetch_errors`` remains SHOW-scoped for backward compatibility, while
    the additive ``read_errors`` facet records each failed status bucket.
    """
    def stub_list_tasks_fail(status="pending", topic=None, limit=80):
        return {"stdout": "", "returncode": 1, "stderr": "sqlite3.OperationalError: database is locked"}

    def stub_show_task_unused(task_id):  # pragma: no cover - must not be reached
        raise AssertionError("show should not be called: list produced zero rows")

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp", limit=10,
        _list_tasks=stub_list_tasks_fail, _show_task=stub_show_task_unused,
    )
    assert result["review_queue"] == []
    assert result["stale_processing"] == []
    assert result["runner_mismatch_warnings"] == []
    assert result["counts"]["fetch_errors"] == 0
    assert result["counts"]["read_errors"] == 4
    assert {entry["scope"] for entry in result["read_errors"]} == {"list"}
    assert {entry["error_kind"] for entry in result["read_errors"]} == {
        "nonzero_returncode"
    }
    assert {
        entry["status"] for entry in result["read_errors"]
    } == {"pending", "processing", "review", "blocked"}
    assert all(
        "database is locked" in entry["error_message"]
        for entry in result["read_errors"]
    )


def test_scenario_tool_error_show_failure_is_recorded():
    """Contrast case: unlike LIST-call failures, a SHOW-call failure for a
    specific task_id (returncode != 0) IS already recorded in fetch_errors.
    This is the one piece of tool_error handling that already exists."""
    def stub_list_tasks_ok(status="pending", topic=None, limit=80):
        if status != "review":
            return {"stdout": "", "returncode": 0}
        return {"stdout": "[review] [task_mcp] [fixture_runner_f] TASK_SHOW_FAIL_01", "returncode": 0}

    def stub_show_task_fail(task_id):
        return {"stdout": "", "returncode": 1, "stderr": "sqlite3.OperationalError: database is locked"}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp", limit=10,
        _list_tasks=stub_list_tasks_ok, _show_task=stub_show_task_fail,
    )
    assert result["counts"]["fetch_errors"] == 1
    assert result["fetch_errors"][0]["task_id"] == "TASK_SHOW_FAIL_01"
    assert "database is locked" in result["fetch_errors"][0]["error"]


def test_scenario_tool_error_show_failure_bounded_fail_closed(tmp_path, monkeypatch):
    """Companion path for core.show_task: the desired graceful-degrade
    contract the old xfail test tracked ("flip to a normal assertion once
    core.py gains a try/except here") is now current reality -- a
    task_store.TaskStoreError on read is returned as a bounded ok=False
    envelope, never raised uncaught."""
    root = tmp_path / "repo_show_read_failure"
    root.mkdir()
    init = task_store.initialize_repository(root)
    assert init["ok"], init
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))

    def _raise(*_a, **_k):
        raise task_store.TaskStoreError("sqlite3.OperationalError: database is locked")

    monkeypatch.setattr(core.task_store, "get_task", _raise)

    result = core.show_task("TASK_SHOW_FAIL_01")  # should not raise
    assert result.get("ok") is False
    assert result.get("returncode") == 1
    assert "database is locked" in result.get("stderr", "")


# ===========================================================================
# edge_case: no_pending_task (B278 section_3.no_pending_task)
# ===========================================================================

def test_edge_case_no_pending_task():
    def stub_list_tasks(status="pending", topic=None, limit=80):
        return {"stdout": "", "returncode": 0}

    def stub_show_task_unused(task_id):  # pragma: no cover
        raise AssertionError("show should not be called: every bucket is empty")

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp", limit=10,
        _list_tasks=stub_list_tasks, _show_task=stub_show_task_unused,
    )
    assert result["review_queue"] == []
    assert result["stale_processing"] == []
    assert result["runner_mismatch_warnings"] == []
    assert result["latest_validation_facts"] == []
    assert result["counts"]["fetch_errors"] == 0
    assert result["counts"]["review_queue"] == 0
    # An empty queue is a successfully-read state, not an error -- no
    # exception, no fetch_errors, ok-shaped dict returned.
    assert isinstance(result, dict) and result.get("readonly") is True


# ===========================================================================
# authority flags shape sanity (touches completion_inbox._authority_flags,
# used by every scenario above via build_completion_inbox's return value)
# ===========================================================================

def test_completion_inbox_authority_flags_never_grant_write_or_launch():
    def stub_list_tasks(status="pending", topic=None, limit=80):
        return {"stdout": "", "returncode": 0}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp", limit=1, _list_tasks=stub_list_tasks, _show_task=lambda t: {"stdout": "", "returncode": 1},
    )
    flags = result["authority_flags"]
    assert flags["process_launch"] is False
    assert flags["agent_launch"] is False
    assert flags["shell_invocation"] is False
    assert flags["queue_write"] is False
    assert flags["audit_write"] is False
    assert flags["subprocess_launch_tripwire_zero"] is True
    mutation = result["mutation"]
    assert mutation["queue_mutated"] is False
    assert mutation["write_gate_bypassed"] is False
    assert mutation["write_command_invoked"] is False
    assert mutation["agent_or_process_launched"] is False


def _inbox_from_cards(cards):
    by_id = {card["task_id"]: card for card in cards}

    def stub_list_tasks(status="pending", topic=None, limit=80):
        matching = [
            card for card in cards
            if str(card.get("status") or "") == status
        ]
        lines = [
            f"[{status}] [{card.get('topic') or 'task_mcp'}] "
            f"[{card.get('runner') or 'fixture'}] {card['task_id']}"
            for card in matching
        ]
        return {"stdout": "\n".join(lines), "returncode": 0}

    def stub_show_task(task_id):
        card = by_id.get(task_id)
        if card is None:
            return {"stdout": f"Task not found: {task_id}", "returncode": 1}
        return {"stdout": json.dumps(card), "returncode": 0}

    return completion_inbox.build_completion_inbox(
        topic="task_mcp",
        limit=50,
        stale_processing_hours=24.0,
        _list_tasks=stub_list_tasks,
        _show_task=stub_show_task,
    )


def test_inbox_excludes_reviewer_retry_when_target_is_terminal():
    result = _inbox_from_cards([
        {
            "task_id": "QR-retry-terminal",
            "runner": "fixture_reviewer",
            "claimed_by": "fixture_reviewer",
            "topic": "quality_review",
            "status": "review",
            "priority": "high",
            "objective": "retry a finished parent",
            "allowed_writes": [],
            "updated_at": "2026-07-08T10:00:00+00:00",
            "quality_review": {
                "target_task_id": "T-done",
                "target_request_id": "req-done",
                "target_status": "finished",
            },
        },
        {
            "task_id": "T-done",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "finished",
            "worker_status": "done",
            "priority": "high",
            "objective": "finished parent discovered by show",
            "allowed_writes": [],
            "updated_at": "2026-07-08T09:00:00+00:00",
        },
        {
            "task_id": "LIVE-REVIEW",
            "runner": "fixture_runner",
            "claimed_by": "fixture_runner",
            "topic": "task_mcp",
            "status": "review",
            "priority": "high",
            "objective": "live review",
            "allowed_writes": [],
            "updated_at": "2026-07-08T11:00:00+00:00",
        },
    ])
    assert _entry_by_task_id(result["review_queue"], "QR-retry-terminal") is None
    assert _entry_by_task_id(result["review_queue"], "LIVE-REVIEW") is not None
    assert result["counts"]["terminal_artifacts_excluded"] == 1
    assert result["counts"]["terminal_artifacts_excluded"] == len(
        result["terminal_artifacts_excluded"]
    )
    assert result["terminal_artifacts_excluded"][0]["target_status"] == "finished"


def test_inbox_excludes_reviewer_without_target_status_via_exact_lookup():
    result = _inbox_from_cards([
        {
            "task_id": "QR-lookup-terminal",
            "runner": "fixture_reviewer",
            "claimed_by": "fixture_reviewer",
            "topic": "quality_review",
            "status": "review",
            "priority": "high",
            "objective": "retry finished parent without recorded status",
            "allowed_writes": [],
            "updated_at": "2026-07-08T10:00:00+00:00",
            "quality_review": {
                "target_task_id": "T-done",
                "target_request_id": "req-done",
            },
        },
        {
            "task_id": "T-done",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "finished",
            "worker_status": "done",
            "priority": "high",
            "objective": "finished parent discovered only by show",
            "allowed_writes": [],
            "updated_at": "2026-07-08T09:00:00+00:00",
        },
        {
            "task_id": "LIVE-REVIEW",
            "runner": "fixture_runner",
            "claimed_by": "fixture_runner",
            "topic": "task_mcp",
            "status": "review",
            "priority": "high",
            "objective": "live review",
            "allowed_writes": [],
            "updated_at": "2026-07-08T11:00:00+00:00",
        },
    ])
    assert _entry_by_task_id(result["review_queue"], "QR-lookup-terminal") is None
    assert _entry_by_task_id(result["review_queue"], "LIVE-REVIEW") is not None
    assert result["counts"]["terminal_artifacts_excluded"] == 1
    assert result["terminal_artifacts_excluded"][0]["target_task_id"] == "T-done"
    assert result["terminal_artifacts_excluded"][0]["target_status"] == "finished"


def test_inbox_retains_reviewer_retry_when_target_is_rework():
    result = _inbox_from_cards([
        {
            "task_id": "T-rework",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "review",
            "worker_status": "cancelled",
            "priority": "high",
            "objective": "cancelled parent without successor",
            "allowed_writes": [],
            "updated_at": "2026-07-08T09:00:00+00:00",
        },
        {
            "task_id": "T-status-rework",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "rework",
            "priority": "high",
            "objective": "genuine rework parent",
            "allowed_writes": [],
            "updated_at": "2026-07-08T09:05:00+00:00",
        },
        {
            "task_id": "QR-retry-rework",
            "runner": "fixture_reviewer",
            "claimed_by": "fixture_reviewer",
            "topic": "quality_review",
            "status": "review",
            "priority": "high",
            "objective": "retry a rework parent",
            "allowed_writes": [],
            "updated_at": "2026-07-08T10:00:00+00:00",
            "quality_review": {
                "target_task_id": "T-rework",
                "target_request_id": "req-rework",
            },
        },
        {
            "task_id": "QR-status-rework",
            "runner": "fixture_reviewer",
            "claimed_by": "fixture_reviewer",
            "topic": "quality_review",
            "status": "review",
            "priority": "high",
            "objective": "retry a genuine rework parent",
            "allowed_writes": [],
            "updated_at": "2026-07-08T10:05:00+00:00",
            "quality_review": {"target_task_id": "T-status-rework"},
        },
    ])
    assert _entry_by_task_id(result["review_queue"], "QR-retry-rework") is not None
    assert _entry_by_task_id(result["review_queue"], "QR-status-rework") is not None
    assert result["counts"]["terminal_artifacts_excluded"] == 0
    assert result["terminal_artifacts_excluded"] == []


def test_inbox_retains_unresolved_target_artifact():
    result = _inbox_from_cards([
        {
            "task_id": "T-status-unresolved",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "unresolved",
            "priority": "medium",
            "objective": "genuine unresolved parent",
            "allowed_writes": [],
            "updated_at": "2026-07-08T09:00:00+00:00",
        },
        {
            "task_id": "IMPL-unresolved",
            "runner": "fixture_impl",
            "claimed_by": "fixture_impl",
            "topic": "task_mcp",
            "status": "pending",
            "priority": "medium",
            "objective": "implementation retry of missing target",
            "allowed_writes": [],
            "updated_at": "2026-07-08T10:00:00+00:00",
            "implementation": {
                "target_task_id": "T-missing",
            },
        },
        {
            "task_id": "QR-status-unresolved",
            "runner": "fixture_reviewer",
            "claimed_by": "fixture_reviewer",
            "topic": "quality_review",
            "status": "review",
            "priority": "high",
            "objective": "retry a genuine unresolved parent",
            "allowed_writes": [],
            "updated_at": "2026-07-08T10:05:00+00:00",
            "quality_review": {"target_task_id": "T-status-unresolved"},
        },
    ])
    assert result["counts"]["terminal_artifacts_excluded"] == 0
    assert result["terminal_artifacts_excluded"] == []
    assert result["counts"]["pending_scanned"] == 1
    assert _entry_by_task_id(result["review_queue"], "QR-status-unresolved") is not None
    assert any(
        error.get("scope") == "show" and error.get("task_id") == "T-missing"
        for error in result["read_errors"]
    )


def test_inbox_keeps_artifact_when_live_target_conflicts_stale_recorded_terminal():
    result = _inbox_from_cards([
        {
            "task_id": "T-live-rework",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "review",
            "worker_status": "cancelled",
            "priority": "high",
            "objective": "live rework parent",
            "allowed_writes": [],
            "updated_at": "2026-07-08T09:00:00+00:00",
        },
        {
            "task_id": "QR-stale-finished",
            "runner": "fixture_reviewer",
            "claimed_by": "fixture_reviewer",
            "topic": "quality_review",
            "status": "review",
            "priority": "high",
            "objective": "stale recorded terminal on live rework",
            "allowed_writes": [],
            "updated_at": "2026-07-08T10:00:00+00:00",
            "quality_review": {
                "target_task_id": "T-live-rework",
                "target_request_id": "req-live-rework",
                "target_status": "finished",
            },
        },
        {
            "task_id": "IMPL-stale-accepted",
            "runner": "fixture_impl",
            "claimed_by": "fixture_impl",
            "topic": "task_mcp",
            "status": "pending",
            "priority": "medium",
            "objective": "missing unresolved target",
            "allowed_writes": [],
            "updated_at": "2026-07-08T10:30:00+00:00",
            "implementation": {
                "target_task_id": "T-missing",
            },
        },
    ])
    assert _entry_by_task_id(result["review_queue"], "QR-stale-finished") is not None
    assert result["counts"]["pending_scanned"] == 1
    assert result["counts"]["terminal_artifacts_excluded"] == 0
    assert result["terminal_artifacts_excluded"] == []
    assert any(
        error.get("scope") == "show" and error.get("task_id") == "T-missing"
        for error in result["read_errors"]
    )


def test_inbox_excludes_accepted_target_without_recorded_status():
    result = _inbox_from_cards([
        {
            "task_id": "QR-accepted",
            "runner": "fixture_reviewer",
            "claimed_by": "fixture_reviewer",
            "topic": "quality_review",
            "status": "review",
            "priority": "high",
            "objective": "retry accepted parent",
            "allowed_writes": [],
            "updated_at": "2026-07-08T10:00:00+00:00",
            "quality_review": {
                "target_task_id": "T-acc",
                "target_request_id": "req-acc",
            },
        },
        {
            "task_id": "T-acc",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "finished",
            "worker_status": "done",
            "accepted_request_id": "req-acc",
            "accepted_at": "2026-07-08T09:00:00+00:00",
            "accepted_by": "owner",
            "accept_evidence": {"acceptance_evidence_record": {"reference": "req-acc"}},
            "priority": "high",
            "objective": "accepted parent",
            "allowed_writes": [],
            "updated_at": "2026-07-08T09:00:00+00:00",
        },
        {
            "task_id": "LIVE-REVIEW",
            "runner": "fixture_runner",
            "claimed_by": "fixture_runner",
            "topic": "task_mcp",
            "status": "review",
            "priority": "high",
            "objective": "live review",
            "allowed_writes": [],
            "updated_at": "2026-07-08T11:00:00+00:00",
        },
    ])
    assert _entry_by_task_id(result["review_queue"], "QR-accepted") is None
    assert _entry_by_task_id(result["review_queue"], "LIVE-REVIEW") is not None
    assert result["counts"]["terminal_artifacts_excluded"] == 1
    assert result["terminal_artifacts_excluded"][0]["target_status"] == "accepted"


def test_inbox_keeps_recorded_accepted_fallback_when_live_target_absent():
    result = _inbox_from_cards([
        {
            "task_id": "QR-recorded-acc",
            "runner": "fixture_reviewer",
            "claimed_by": "fixture_reviewer",
            "topic": "quality_review",
            "status": "review",
            "priority": "high",
            "objective": "retry recorded accepted parent",
            "allowed_writes": [],
            "updated_at": "2026-07-08T10:00:00+00:00",
            "quality_review": {
                "target_task_id": "T-ghost",
                "target_status": "accepted",
            },
        },
        {
            "task_id": "LIVE-REVIEW",
            "runner": "fixture_runner",
            "claimed_by": "fixture_runner",
            "topic": "task_mcp",
            "status": "review",
            "priority": "high",
            "objective": "live review",
            "allowed_writes": [],
            "updated_at": "2026-07-08T11:00:00+00:00",
        },
    ])
    assert _entry_by_task_id(result["review_queue"], "QR-recorded-acc") is not None
    assert _entry_by_task_id(result["review_queue"], "LIVE-REVIEW") is not None
    assert result["counts"]["terminal_artifacts_excluded"] == 0
    assert result["terminal_artifacts_excluded"] == []


def test_inbox_and_dag_agree_on_transitive_superseded_successor():
    cards = [
        {
            "task_id": "QR-chain",
            "runner": "fixture_reviewer",
            "claimed_by": "fixture_reviewer",
            "topic": "quality_review",
            "status": "review",
            "worker_status": "review",
            "priority": "high",
            "objective": "retry mid superseded parent",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T00:00:00Z",
            "launch_request_id": "",
            "updated_at": "2026-07-08T10:00:00+00:00",
            "quality_review": {"target_task_id": "T-mid"},
        },
        {
            "task_id": "T-mid",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "pending",
            "worker_status": "unclaimed",
            "archive_operation": "superseded",
            "superseded_by": "T-hop",
            "priority": "high",
            "objective": "mid hop",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T00:00:00Z",
            "launch_request_id": "",
            "updated_at": "2026-07-08T09:00:00+00:00",
        },
        {
            "task_id": "T-hop",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "pending",
            "archive_operation": "superseded",
            "superseded_by": "T-landed",
            "priority": "high",
            "objective": "second hop",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T01:00:00Z",
            "launch_request_id": "",
            "updated_at": "2026-07-08T09:10:00+00:00",
        },
        {
            "task_id": "T-landed",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "finished",
            "worker_status": "done",
            "priority": "high",
            "objective": "landed successor",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T02:00:00Z",
            "launch_request_id": "",
            "updated_at": "2026-07-08T09:20:00+00:00",
        },
    ]
    inbox = _inbox_from_cards(cards)
    dag = task_plan.build_snapshot(cards)
    assert _entry_by_task_id(inbox["review_queue"], "QR-chain") is None
    assert "QR-chain" not in dag["task_ids"]
    assert inbox["counts"]["terminal_artifacts_excluded"] == 1
    assert dag["terminal_artifacts_excluded_count"] == 1
    assert inbox["terminal_artifacts_excluded"] == dag["terminal_artifacts_excluded"]


def test_inbox_keeps_bare_superseded_and_cancelled_without_successor():
    result = _inbox_from_cards([
        {
            "task_id": "T-bare-sup",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "pending",
            "archive_operation": "superseded",
            "priority": "high",
            "objective": "bare superseded",
            "allowed_writes": [],
            "updated_at": "2026-07-08T09:00:00+00:00",
        },
        {
            "task_id": "QR-bare-sup",
            "runner": "fixture_reviewer",
            "claimed_by": "fixture_reviewer",
            "topic": "quality_review",
            "status": "review",
            "priority": "high",
            "objective": "retry bare superseded",
            "allowed_writes": [],
            "updated_at": "2026-07-08T10:00:00+00:00",
            "quality_review": {"target_task_id": "T-bare-sup"},
        },
        {
            "task_id": "QR-bare-can",
            "runner": "fixture_reviewer",
            "claimed_by": "fixture_reviewer",
            "topic": "quality_review",
            "status": "review",
            "priority": "high",
            "objective": "retry cancelled",
            "allowed_writes": [],
            "updated_at": "2026-07-08T10:05:00+00:00",
            "quality_review": {"target_task_id": "T-bare-can"},
        },
        {
            "task_id": "T-bare-can",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "pending",
            "archive_operation": "cancelled",
            "priority": "high",
            "objective": "bare cancelled",
            "allowed_writes": [],
            "updated_at": "2026-07-08T09:05:00+00:00",
        },
    ])
    assert _entry_by_task_id(result["review_queue"], "QR-bare-sup") is not None
    assert _entry_by_task_id(result["review_queue"], "QR-bare-can") is not None
    assert result["counts"]["terminal_artifacts_excluded"] == 0


def test_inbox_archive_reason_multi_hop_excludes_and_keeps_unresolved():
    cards = [
        {
            "task_id": "QR-reason-chain",
            "runner": "fixture_reviewer",
            "claimed_by": "fixture_reviewer",
            "topic": "quality_review",
            "status": "review",
            "worker_status": "review",
            "priority": "high",
            "objective": "retry archive_reason hop",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T00:00:00Z",
            "launch_request_id": "",
            "updated_at": "2026-07-08T10:00:00+00:00",
            "quality_review": {"target_task_id": "T-reason-mid"},
        },
        {
            "task_id": "QR-unresolved",
            "runner": "fixture_reviewer",
            "claimed_by": "fixture_reviewer",
            "topic": "quality_review",
            "status": "review",
            "worker_status": "review",
            "priority": "high",
            "objective": "retry unresolved rework",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T01:00:00Z",
            "launch_request_id": "",
            "updated_at": "2026-07-08T10:05:00+00:00",
            "quality_review": {
                "target_task_id": "T-rework",
                "target_status": "rework_required",
            },
        },
        {
            "task_id": "T-reason-mid",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "pending",
            "worker_status": "unclaimed",
            "archive_operation": "superseded",
            "archive_reason": "superseded_by:T-reason-hop",
            "priority": "high",
            "objective": "mid hop via archive_reason",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T00:00:00Z",
            "launch_request_id": "",
            "updated_at": "2026-07-08T09:00:00+00:00",
        },
        {
            "task_id": "T-reason-hop",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "pending",
            "archive_operation": "superseded",
            "archive_reason": "superseded_by:T-reason-landed",
            "priority": "high",
            "objective": "second hop via archive_reason",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T01:00:00Z",
            "launch_request_id": "",
            "updated_at": "2026-07-08T09:10:00+00:00",
        },
        {
            "task_id": "T-reason-landed",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "finished",
            "worker_status": "done",
            "priority": "high",
            "objective": "landed successor",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T02:00:00Z",
            "launch_request_id": "",
            "updated_at": "2026-07-08T09:20:00+00:00",
        },
        {
            "task_id": "T-rework",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "pending",
            "worker_status": "unclaimed",
            "priority": "high",
            "objective": "rework required",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T03:00:00Z",
            "launch_request_id": "",
            "updated_at": "2026-07-08T09:30:00+00:00",
        },
    ]
    inbox = _inbox_from_cards(cards)
    dag = task_plan.build_snapshot(cards)
    assert _entry_by_task_id(inbox["review_queue"], "QR-reason-chain") is None
    assert _entry_by_task_id(inbox["review_queue"], "QR-unresolved") is not None
    assert "QR-reason-chain" not in dag["task_ids"]
    assert "QR-unresolved" in dag["task_ids"]
    assert inbox["counts"]["terminal_artifacts_excluded"] == 1
    assert dag["terminal_artifacts_excluded_count"] == 1
    assert inbox["terminal_artifacts_excluded"] == dag["terminal_artifacts_excluded"]


def test_inbox_terminal_enrichment_task_identity_mismatch_discarded():
    review_card = {
        "task_id": "QR-retry-terminal",
        "runner": "fixture_reviewer",
        "claimed_by": "fixture_reviewer",
        "topic": "quality_review",
        "status": "review",
        "priority": "high",
        "objective": "retry terminal target",
        "allowed_writes": [],
        "updated_at": "2026-07-08T10:00:00+00:00",
        "quality_review": {"target_task_id": "T-done"},
    }

    def stub_list_tasks(status="pending", topic=None, limit=80):
        if status != "review":
            return {"stdout": "", "returncode": 0}
        return {
            "stdout": "[review] [quality_review] [fixture_reviewer] QR-retry-terminal",
            "returncode": 0,
        }

    shown: list[str] = []

    def stub_show_task(task_id):
        shown.append(task_id)
        if task_id == "QR-retry-terminal":
            return {"stdout": json.dumps(review_card), "returncode": 0}
        if task_id == "T-done":
            return {
                "stdout": json.dumps({"task_id": "T-OTHER", "status": "finished"}),
                "returncode": 0,
            }
        if task_id == "../bad":
            raise AssertionError("invalid task id must not be shown")
        return {"stdout": f"Task not found: {task_id}", "returncode": 1}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp",
        limit=10,
        _list_tasks=stub_list_tasks,
        _show_task=stub_show_task,
    )
    kinds = {error.get("error_kind") for error in result["read_errors"]}
    assert "task_identity_mismatch" in kinds
    assert "T-done" in shown
    assert "../bad" not in shown
    assert _entry_by_task_id(result["review_queue"], "QR-retry-terminal") is not None
    assert result["counts"]["terminal_artifacts_excluded"] == 0


def test_inbox_show_not_found_json_parse_and_invalid_card_are_read_errors():
    def stub_list_tasks(status="pending", topic=None, limit=80):
        if status != "review":
            return {"stdout": "", "returncode": 0}
        lines = [
            "[review] [task_mcp] [fixture] TASK_NOT_FOUND",
            "[review] [task_mcp] [fixture] TASK_BAD_JSON",
            "[review] [task_mcp] [fixture] TASK_NOT_OBJECT",
        ]
        return {"stdout": "\n".join(lines), "returncode": 0}

    def stub_show_task(task_id):
        if task_id == "TASK_NOT_FOUND":
            return {"stdout": "Task not found: TASK_NOT_FOUND", "returncode": 0}
        if task_id == "TASK_BAD_JSON":
            return {"stdout": "{not-json", "returncode": 0}
        if task_id == "TASK_NOT_OBJECT":
            return {"stdout": '["not", "a", "card"]', "returncode": 0}
        return {"stdout": "", "returncode": 1, "stderr": "unexpected"}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp",
        limit=10,
        _list_tasks=stub_list_tasks,
        _show_task=stub_show_task,
    )
    kinds = {error.get("error_kind") for error in result["read_errors"]}
    assert "not_found" in kinds
    assert "json_parse_error" in kinds
    assert "invalid_card" in kinds
    assert result["counts"]["review_queue"] == 0


def test_inbox_excludes_every_terminal_artifact_beyond_published_200_cap():
    artifacts = [
        {
            "task_id": f"QR-{idx:03d}",
            "runner": "fixture_reviewer",
            "claimed_by": "fixture_reviewer",
            "topic": "quality_review",
            "status": "review",
            "priority": "high",
            "objective": "retry a finished parent",
            "allowed_writes": [],
            "updated_at": "2026-07-08T10:00:00+00:00",
            "quality_review": {
                "target_task_id": "T-done",
                "target_request_id": "req-done",
                "target_status": "finished",
            },
        }
        for idx in range(210)
    ]
    targets = [
        {
            "task_id": "T-done",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "finished",
            "worker_status": "done",
            "priority": "high",
            "objective": "finished parent discovered by show",
            "allowed_writes": [],
            "updated_at": "2026-07-08T09:00:00+00:00",
        }
    ]
    cards = [
        *artifacts,
        *targets,
        {
            "task_id": "T-rework",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "rework",
            "worker_status": "unclaimed",
            "priority": "high",
            "objective": "live rework parent",
            "allowed_writes": [],
            "updated_at": "2026-07-08T09:30:00+00:00",
        },
        {
            "task_id": "LIVE-REVIEW",
            "runner": "fixture_runner",
            "claimed_by": "fixture_runner",
            "topic": "task_mcp",
            "status": "review",
            "priority": "high",
            "objective": "live review",
            "allowed_writes": [],
            "updated_at": "2026-07-08T11:00:00+00:00",
        },
        {
            "task_id": "QR-rework",
            "runner": "fixture_reviewer",
            "claimed_by": "fixture_reviewer",
            "topic": "quality_review",
            "status": "review",
            "priority": "high",
            "objective": "retry a rework parent",
            "allowed_writes": [],
            "updated_at": "2026-07-08T12:00:00+00:00",
            "quality_review": {
                "target_task_id": "T-rework",
                "target_request_id": "req-rework",
                "target_status": "rework",
            },
        },
    ]
    by_id = {card["task_id"]: card for card in cards}

    def stub_list_tasks(status="pending", topic=None, limit=80):
        matching = [card for card in cards if str(card.get("status") or "") == status]
        lines = [
            f"[{status}] [{card.get('topic') or 'task_mcp'}] "
            f"[{card.get('runner') or 'fixture'}] {card['task_id']}"
            for card in matching
        ]
        return {"stdout": "\n".join(lines), "returncode": 0}

    def stub_show_task(task_id):
        card = by_id.get(task_id)
        if card is None:
            return {"stdout": f"Task not found: {task_id}", "returncode": 0}
        return {"stdout": json.dumps(card), "returncode": 0}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp",
        limit=500,
        stale_processing_hours=24.0,
        _list_tasks=stub_list_tasks,
        _show_task=stub_show_task,
    )
    dag = task_plan.build_snapshot(cards)
    assert len(result["terminal_artifacts_excluded"]) == 200
    assert result["counts"]["terminal_artifacts_excluded"] == 210
    assert dag["terminal_artifacts_excluded_count"] == 210
    assert result["counts"]["terminal_artifacts_excluded"] == dag[
        "terminal_artifacts_excluded_count"
    ]
    assert _entry_by_task_id(result["review_queue"], "QR-209") is None
    assert _entry_by_task_id(result["review_queue"], "QR-000") is None
    assert _entry_by_task_id(result["review_queue"], "LIVE-REVIEW") is not None
    assert _entry_by_task_id(result["review_queue"], "QR-rework") is not None
    assert "QR-rework" in dag["task_ids"]


def test_inbox_nonzero_returncode_before_not_found_keeps_full_stderr():
    long_err = "sqlite3.OperationalError: " + ("x" * 220)

    def stub_list_tasks(status="pending", topic=None, limit=80):
        if status != "review":
            return {"stdout": "", "returncode": 0}
        return {
            "stdout": "[review] [task_mcp] [fixture] TASK_SHOW_FAIL_LONG",
            "returncode": 0,
        }

    def stub_show_task(task_id):
        return {
            "stdout": f"Task not found: {task_id}",
            "returncode": 1,
            "stderr": long_err,
        }

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp",
        limit=10,
        _list_tasks=stub_list_tasks,
        _show_task=stub_show_task,
    )
    assert result["fetch_errors"][0]["task_id"] == "TASK_SHOW_FAIL_LONG"
    assert result["fetch_errors"][0]["error"] == long_err
    kinds = {error.get("error_kind") for error in result["read_errors"]}
    assert "nonzero_returncode" in kinds
    assert "not_found" not in kinds
    assert "json_parse_error" not in kinds


def test_inbox_nonzero_garbage_stdout_does_not_add_json_parse_error():
    def stub_list_tasks(status="pending", topic=None, limit=80):
        if status != "review":
            return {"stdout": "", "returncode": 0}
        return {
            "stdout": "[review] [task_mcp] [fixture] TASK_GARBAGE",
            "returncode": 0,
        }

    def stub_show_task(task_id):
        return {"stdout": "{not-json", "returncode": 2, "stderr": "boom-full-stderr"}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp",
        limit=10,
        _list_tasks=stub_list_tasks,
        _show_task=stub_show_task,
    )
    kinds = {error.get("error_kind") for error in result["read_errors"]}
    assert "nonzero_returncode" in kinds
    assert "json_parse_error" not in kinds
    assert result["fetch_errors"][0]["error"] == "boom-full-stderr"


def test_inbox_identity_mismatch_with_recorded_terminal_fails_closed():
    review_card = {
        "task_id": "QR-retry-terminal",
        "runner": "fixture_reviewer",
        "claimed_by": "fixture_reviewer",
        "topic": "quality_review",
        "status": "review",
        "priority": "high",
        "objective": "retry terminal target",
        "allowed_writes": [],
        "updated_at": "2026-07-08T10:00:00+00:00",
        "quality_review": {
            "target_task_id": "T-done",
            "target_status": "finished",
        },
    }

    def stub_list_tasks(status="pending", topic=None, limit=80):
        if status != "review":
            return {"stdout": "", "returncode": 0}
        return {
            "stdout": "[review] [quality_review] [fixture_reviewer] QR-retry-terminal",
            "returncode": 0,
        }

    def stub_show_task(task_id):
        if task_id == "QR-retry-terminal":
            return {"stdout": json.dumps(review_card), "returncode": 0}
        if task_id == "T-done":
            return {
                "stdout": json.dumps({"task_id": "T-OTHER", "status": "finished"}),
                "returncode": 0,
            }
        return {"stdout": f"Task not found: {task_id}", "returncode": 1}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp",
        limit=10,
        _list_tasks=stub_list_tasks,
        _show_task=stub_show_task,
    )
    kinds = {error.get("error_kind") for error in result["read_errors"]}
    assert "task_identity_mismatch" in kinds
    assert _entry_by_task_id(result["review_queue"], "QR-retry-terminal") is not None
    assert result["counts"]["terminal_artifacts_excluded"] == 0


def test_inbox_point_lookup_archive_reason_successor_outside_list():
    review_card = {
        "task_id": "QR-reason-chain",
        "runner": "fixture_reviewer",
        "claimed_by": "fixture_reviewer",
        "topic": "quality_review",
        "status": "review",
        "priority": "high",
        "objective": "retry archive_reason hop",
        "allowed_writes": [],
        "updated_at": "2026-07-08T10:00:00+00:00",
        "quality_review": {"target_task_id": "T-reason-mid"},
    }
    live_review = {
        "task_id": "LIVE-REVIEW",
        "runner": "fixture_runner",
        "claimed_by": "fixture_runner",
        "topic": "task_mcp",
        "status": "review",
        "priority": "high",
        "objective": "live review",
        "allowed_writes": [],
        "updated_at": "2026-07-08T11:00:00+00:00",
    }
    outside = {
        "T-reason-mid": {
            "task_id": "T-reason-mid",
            "status": "pending",
            "archive_operation": "superseded",
            "archive_reason": "superseded_by:T-reason-hop",
        },
        "T-reason-hop": {
            "task_id": "T-reason-hop",
            "status": "pending",
            "archive_operation": "superseded",
            "archive_reason": "superseded_by:T-reason-landed",
        },
        "T-reason-landed": {
            "task_id": "T-reason-landed",
            "status": "finished",
            "worker_status": "done",
        },
    }
    shown: list[str] = []

    def stub_list_tasks(status="pending", topic=None, limit=80):
        if status != "review":
            return {"stdout": "", "returncode": 0}
        return {
            "stdout": (
                "[review] [quality_review] [fixture_reviewer] QR-reason-chain\n"
                "[review] [task_mcp] [fixture_runner] LIVE-REVIEW"
            ),
            "returncode": 0,
        }

    def stub_show_task(task_id):
        shown.append(task_id)
        if task_id == "QR-reason-chain":
            return {"stdout": json.dumps(review_card), "returncode": 0}
        if task_id == "LIVE-REVIEW":
            return {"stdout": json.dumps(live_review), "returncode": 0}
        card = outside.get(task_id)
        if card is None:
            return {"stdout": f"Task not found: {task_id}", "returncode": 1}
        return {"stdout": json.dumps(card), "returncode": 0}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp",
        limit=10,
        _list_tasks=stub_list_tasks,
        _show_task=stub_show_task,
    )
    assert "T-reason-mid" in shown
    assert "T-reason-hop" in shown
    assert "T-reason-landed" in shown
    assert _entry_by_task_id(result["review_queue"], "QR-reason-chain") is None
    assert _entry_by_task_id(result["review_queue"], "LIVE-REVIEW") is not None
    assert result["counts"]["terminal_artifacts_excluded"] == 1
    assert result["terminal_artifacts_excluded"][0]["target_status"] == "superseded"


def test_inbox_listed_show_identity_mismatch_does_not_authorize_exclusion():
    live_review = {
        "task_id": "LIVE-REVIEW",
        "runner": "fixture_runner",
        "claimed_by": "fixture_runner",
        "topic": "task_mcp",
        "status": "review",
        "priority": "high",
        "objective": "live review",
        "allowed_writes": [],
        "updated_at": "2026-07-08T11:00:00+00:00",
    }
    spoofed_terminal = {
        "task_id": "T-acc",
        "status": "finished",
        "worker_status": "done",
        "accepted_request_id": "req-acc",
        "accepted_at": "2026-07-08T09:00:00+00:00",
        "accepted_by": "owner",
        "accept_evidence": {"acceptance_evidence_record": {"reference": "req-acc"}},
    }

    def stub_list_tasks(status="pending", topic=None, limit=80):
        if status != "review":
            return {"stdout": "", "returncode": 0}
        return {
            "stdout": (
                "[review] [task_mcp] [fixture_runner] LIVE-REVIEW\n"
                "[review] [quality_review] [fixture_reviewer] QR-live"
            ),
            "returncode": 0,
        }

    def stub_show_task(task_id):
        if task_id == "LIVE-REVIEW":
            return {"stdout": json.dumps(live_review), "returncode": 0}
        if task_id == "QR-live":
            return {"stdout": json.dumps(spoofed_terminal), "returncode": 0}
        return {"stdout": f"Task not found: {task_id}", "returncode": 1}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp",
        limit=10,
        _list_tasks=stub_list_tasks,
        _show_task=stub_show_task,
    )
    kinds = {error.get("error_kind") for error in result["read_errors"]}
    assert "task_identity_mismatch" in kinds
    assert _entry_by_task_id(result["review_queue"], "LIVE-REVIEW") is not None
    assert _entry_by_task_id(result["review_queue"], "T-acc") is None
    assert _entry_by_task_id(result["review_queue"], "QR-live") is None
    assert result["counts"]["terminal_artifacts_excluded"] == 0
    assert result["terminal_artifacts_excluded"] == []


def test_inbox_over_limit_unique_targets_bounds_show_and_marks_incomplete():
    limit = task_plan.MAX_TERMINAL_ARTIFACT_ROWS
    overflow = limit + 1
    artifacts = [
        {
            "task_id": f"QR-{idx:03d}",
            "runner": "fixture_reviewer",
            "claimed_by": "fixture_reviewer",
            "topic": "quality_review",
            "status": "review",
            "priority": "high",
            "objective": "retry a finished parent",
            "allowed_writes": [],
            "updated_at": "2026-07-08T10:00:00+00:00",
            "quality_review": {
                "target_task_id": f"T-done-{idx:03d}",
                "target_request_id": f"req-done-{idx:03d}",
                "target_status": "finished",
            },
        }
        for idx in range(overflow)
    ]
    targets = {
        f"T-done-{idx:03d}": {
            "task_id": f"T-done-{idx:03d}",
            "runner": "fixture_parent",
            "claimed_by": "fixture_parent",
            "topic": "task_mcp",
            "status": "finished",
            "worker_status": "done",
            "priority": "high",
            "objective": "finished parent discovered by show",
            "allowed_writes": [],
            "updated_at": "2026-07-08T09:00:00+00:00",
        }
        for idx in range(overflow)
    }
    live = {
        "task_id": "LIVE-REVIEW",
        "runner": "fixture_runner",
        "claimed_by": "fixture_runner",
        "topic": "task_mcp",
        "status": "review",
        "priority": "high",
        "objective": "live review",
        "allowed_writes": [],
        "updated_at": "2026-07-08T11:00:00+00:00",
    }
    cards = [*artifacts, live]
    shown: list[str] = []

    def stub_list_tasks(status="pending", topic=None, limit=80):
        matching = [card for card in cards if str(card.get("status") or "") == status]
        lines = [
            f"[{status}] [{card.get('topic') or 'task_mcp'}] "
            f"[{card.get('runner') or 'fixture'}] {card['task_id']}"
            for card in matching
        ]
        return {"stdout": "\n".join(lines), "returncode": 0}

    def stub_show_task(task_id):
        shown.append(task_id)
        card = targets.get(task_id) or next(
            (item for item in cards if item["task_id"] == task_id), None
        )
        if card is None:
            return {"stdout": f"Task not found: {task_id}", "returncode": 0}
        return {"stdout": json.dumps(card), "returncode": 0}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp",
        limit=500,
        stale_processing_hours=24.0,
        _list_tasks=stub_list_tasks,
        _show_task=stub_show_task,
    )
    target_shows = [tid for tid in shown if tid.startswith("T-done-")]
    assert len(target_shows) == limit
    assert result["terminal_projection_incomplete"] is True
    assert result["counts"]["terminal_artifacts_excluded"] == limit
    assert _entry_by_task_id(result["review_queue"], "LIVE-REVIEW") is not None
    overflow_id = f"QR-{limit:03d}"
    assert _entry_by_task_id(result["review_queue"], overflow_id) is not None


def test_inbox_non_superseded_and_invalid_successor_ids_fail_closed():
    live = {
        "task_id": "LIVE-REVIEW",
        "runner": "fixture_runner",
        "claimed_by": "fixture_runner",
        "topic": "task_mcp",
        "status": "review",
        "priority": "high",
        "objective": "live review",
        "allowed_writes": [],
        "updated_at": "2026-07-08T11:00:00+00:00",
    }
    cancelled = {
        "task_id": "T-can",
        "status": "pending",
        "archive_operation": "cancelled",
        "superseded_by": "T-landed",
        "updated_at": "2026-07-08T09:00:00+00:00",
    }
    invalid = {
        "task_id": "T-bad",
        "status": "pending",
        "archive_operation": "superseded",
        "superseded_by": "../etc",
        "updated_at": "2026-07-08T09:10:00+00:00",
    }
    landed = {
        "task_id": "T-landed",
        "status": "finished",
        "worker_status": "done",
        "updated_at": "2026-07-08T08:00:00+00:00",
    }
    qr_can = {
        "task_id": "QR-can",
        "runner": "fixture_reviewer",
        "claimed_by": "fixture_reviewer",
        "topic": "quality_review",
        "status": "review",
        "priority": "high",
        "objective": "retry cancelled",
        "allowed_writes": [],
        "updated_at": "2026-07-08T10:00:00+00:00",
        "quality_review": {"target_task_id": "T-can"},
    }
    qr_bad = {
        "task_id": "QR-bad",
        "runner": "fixture_reviewer",
        "claimed_by": "fixture_reviewer",
        "topic": "quality_review",
        "status": "review",
        "priority": "high",
        "objective": "retry invalid successor",
        "allowed_writes": [],
        "updated_at": "2026-07-08T10:10:00+00:00",
        "quality_review": {"target_task_id": "T-bad"},
    }
    cards = [live, cancelled, invalid, landed, qr_can, qr_bad]
    by_id = {card["task_id"]: card for card in cards}

    def stub_list_tasks(status="pending", topic=None, limit=80):
        matching = [card for card in cards if str(card.get("status") or "") == status]
        lines = [
            f"[{status}] [{card.get('topic') or 'task_mcp'}] "
            f"[{card.get('runner') or 'fixture'}] {card['task_id']}"
            for card in matching
        ]
        return {"stdout": "\n".join(lines), "returncode": 0}

    def stub_show_task(task_id):
        card = by_id.get(task_id)
        if card is None:
            return {"stdout": f"Task not found: {task_id}", "returncode": 0}
        return {"stdout": json.dumps(card), "returncode": 0}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp",
        limit=20,
        _list_tasks=stub_list_tasks,
        _show_task=stub_show_task,
    )
    dag = task_plan.build_snapshot(cards)
    assert result["counts"]["terminal_artifacts_excluded"] == 0
    assert dag["terminal_artifacts_excluded_count"] == 0
    assert _entry_by_task_id(result["review_queue"], "QR-can") is not None
    assert _entry_by_task_id(result["review_queue"], "QR-bad") is not None
    assert "QR-can" in dag["task_ids"]
    assert "QR-bad" in dag["task_ids"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
