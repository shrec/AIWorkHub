"""B279: executable pytest fixtures for the B278 owner-notification smoke spec.

Source spec (read-only, not modified by this task):
    tools/geoai-task-mcp/eval/task_mcp_owner_notification_smoke_spec_b278_v1.json

Turns B278's 6 fixture-driven smoke scenarios (completion, blocked,
duplicate_runner, stale, usage, tool_error) + 2 edge cases (no_pending_task,
duplicate_runner_cross_reference) into real pytest functions that exercise
``geoai_task_mcp.completion_inbox`` and ``geoai_task_mcp.core`` directly.

Isolation contract (acceptance requirement -- "monkeypatch taskctl subprocess
calls; no live SQLite writes or model CLI launch"):
  * scenario_completion / scenario_blocked / scenario_duplicate_runner /
    scenario_stale / edge_case_no_pending_task / edge_case_duplicate_runner_*
    use ``build_completion_inbox(..., _list_tasks=..., _show_task=...)``
    fixture injection -- this bypasses ``core.list_tasks``/``core.show_task``
    entirely, so ``subprocess.run`` is never invoked (zero live-DB touch).
  * scenario_usage and scenario_tool_error explicitly monkeypatch
    ``geoai_task_mcp.core.subprocess.run`` with ``pytest``'s ``monkeypatch``
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
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from geoai_task_mcp import completion_inbox, core  # noqa: E402


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
# `core.usage_report` with subprocess.run monkeypatched to a canned
# `cmd_usage_report` text-format response (AITools/taskctl.py:861-943).
# ===========================================================================

def test_scenario_usage(monkeypatch):
    canned_stdout = (
        "=== Task Usage Report ===\n"
        "FIXTURE_TASK_USAGE_05 | runner=fixture_runner_e | topic=task_mcp | "
        "records=1 | tokens=12800 | in=12000 out=800 | cost=$0.2100\n\n"
        "TOTAL | tasks=1 | records=1 | tokens=12800 | in=12000 out=800 | cost=$0.2100\n"
        "BY_RUNNER\n"
        "  fixture_runner_e: records=1 tokens=12800 cost=$0.2100\n"
        "BY_MODEL\n"
        "  claude-sonnet-5: records=1 tokens=12800 cost=$0.2100\n"
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=canned_stdout, stderr="")

    monkeypatch.setattr(core.subprocess, "run", fake_run)

    # "usage-report" (read aggregate) is distinct from "usage" (write,
    # per-task token recording) -- confirm the write-gate set only guards
    # the mutating subcommand, not this read-only one.
    assert "usage-report" not in core.WRITE_COMMANDS
    assert "usage" in core.WRITE_COMMANDS

    result = core.usage_report(runner="fixture_runner_e", topic="task_mcp")

    assert len(calls) == 1  # exactly one subprocess call, no live DB touch (mocked)
    assert "usage-report" in calls[0]
    assert "--runner" in calls[0] and "fixture_runner_e" in calls[0]
    assert "--topic" in calls[0] and "task_mcp" in calls[0]

    assert result["returncode"] == 0
    assert "FIXTURE_TASK_USAGE_05" in result["stdout"]
    assert "12000" in result["stdout"] and "800" in result["stdout"]
    assert "$0.2100" in result["stdout"]

    # Honest gap: B278's expected_payload for "usage" is structured JSON
    # (usage_input_tokens/usage_output_tokens/cost_usd). core.usage_report()
    # returns cmd_usage_report's human-readable TEXT, not parseable JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(result["stdout"])


# ===========================================================================
# scenario_tool_error (B278 section_1.scenarios.scenario_tool_error +
# section_2) -- three PASS tests documenting exact current behavior in three
# distinct real code paths, plus one strict-xfail tracking the desired
# (not-yet-implemented) graceful-degrade behavior.
# ===========================================================================

def test_scenario_tool_error_current_raises_uncaught(monkeypatch):
    """Current reality: if the taskctl subprocess.run() call itself raises
    (simulating e.g. an OS-level failure while sqlite is locked), the
    exception propagates fully uncaught through run_taskctl -> core.list_tasks.
    No try/except exists at this layer (confirmed by direct code read)."""
    def _raise(*_a, **_k):
        raise OSError("simulated: sqlite3.OperationalError: database is locked")

    monkeypatch.setattr(core.subprocess, "run", _raise)
    with pytest.raises(OSError, match="database is locked"):
        core.list_tasks(status="pending", topic="task_mcp")


def test_scenario_tool_error_list_failure_silently_swallowed():
    """Subtler current gap: when the LIST call returns (does not raise) with
    a non-zero returncode and empty stdout, `_fetch_full_cards` never checks
    the LIST result's returncode -- it just parses stdout ("" -> zero rows).
    Result: a read-source outage on the LIST call is INDISTINGUISHABLE from
    a genuinely empty queue. fetch_errors stays 0."""
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
    assert result["counts"]["fetch_errors"] == 0  # <- the gap: this stays 0, not >0


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


@pytest.mark.xfail(
    reason=(
        "B278 known gap, not yet implemented: neither run_taskctl nor "
        "core.list_tasks catch a subprocess-level read-source failure and "
        "degrade to a bounded {ok: False, error_kind: ...} result. Today it "
        "raises uncaught (see test_scenario_tool_error_current_raises_uncaught). "
        "This test encodes the DESIRED future contract; flip to a normal "
        "(non-xfail) assertion once core.py gains a try/except here."
    ),
    strict=True,
)
def test_scenario_tool_error_graceful_degrade_desired_future_contract(monkeypatch):
    def _raise(*_a, **_k):
        raise OSError("simulated: sqlite3.OperationalError: database is locked")

    monkeypatch.setattr(core.subprocess, "run", _raise)
    result = core.list_tasks(status="pending", topic="task_mcp")  # should not raise
    assert result.get("ok") is False
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
