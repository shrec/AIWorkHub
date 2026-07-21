#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# test_task_mcp_supervisor_loop_status_wiring_b11_v1.sh
#
# B11 retry of the stale-claimed B10 wiring task: exercises the REAL
# core.supervisor_loop_status() + the registered @mcp.tool()
# aiworkhub_supervisor_loop_status against the live queue and in-process
# fixtures, covering all 6 B08-contract scenarios:
#   live real task, not_found, runner_topic_mismatch, fixture missing_artifact,
#   fixture collision, fixture stale_task, fixture failed_validation.
# (not_found is an extra beyond the 6 -- it is required by the output schema's
#  "state" enum and step 1 of the derivation algorithm.)
#
# Also proves NO mutation occurs anywhere in this run: sha256-hashes the
# live queue JSONL, its manifest, and the write-gate audit log BEFORE the
# first scenario and AFTER the last, and asserts byte-identical.
#
# Isolation: all "fixture" scenarios monkeypatch core.show_task /
# core.collision_guard IN-PROCESS with synthetic in-memory dicts (never
# written to the live queue/taskdb), restored in a try/finally in every
# fixture test. The "live" scenarios (live_real_task, not_found,
# runner_topic_mismatch, server_tool_wiring) call the real read-only
# taskctl show/collision-guard subcommands against the live queue --
# never a write command (done/review/start/auto-pickup/export-jsonl/usage).
# Parallel-safe: no fixed shared temp path, no writes to the real queue.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

export PYTHONPATH="$MCPROOT/src"
export AIWORKHUB_REPO="$ROOT"
export AIWORKHUB_ALLOW_WRITES="${AIWORKHUB_ALLOW_WRITES:-0}"

echo "=== Task MCP Supervisor Loop STATUS WIRING Test B11 v1 (retry of stale B10) ==="
echo "ROOT=$ROOT"
echo "PYTHONPATH=$PYTHONPATH"
echo "AIWORKHUB_ALLOW_WRITES=$AIWORKHUB_ALLOW_WRITES"
echo ""

if [ "$AIWORKHUB_ALLOW_WRITES" != "0" ]; then
    echo "FATAL: AIWORKHUB_ALLOW_WRITES must be 0 for this test, got '$AIWORKHUB_ALLOW_WRITES'"
    exit 2
fi

python3 - <<'PYEOF'
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from aiworkhub import core

FAILURES: list[str] = []
PASSES: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        PASSES.append(label)
        print(f"  PASS - {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL - {label}")


REPO_ROOT = Path(os.environ["AIWORKHUB_REPO"]).resolve()
SELF_TASK_ID = "CLAUDE_TASK_MCP_SUPERVISOR_LOOP_STATUS_WIRING_RETRY_B11_V1"

# The relevant on-disk state a mutation could touch: the live task queue
# JSONL + its manifest, and the write-gate audit log.
STATE_FILES = [
    REPO_ROOT / "bitnnv2/data/tasking/machine_task_cards_v1.jsonl",
    REPO_ROOT / "bitnnv2/data/tasking/machine_task_cards_manifest_v1.json",
    REPO_ROOT / "tools/geoai-task-mcp/logs/audit.jsonl",
]


def _hash_file(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _snapshot() -> dict[str, str]:
    return {str(p): _hash_file(p) for p in STATE_FILES}


def _fake_show_task(card: dict[str, Any]):
    def _inner(task_id: str) -> dict[str, Any]:
        return {
            "ok": True,
            "returncode": 0,
            "command": ["python3", "taskctl.py", "show", task_id],
            "stdout": json.dumps(card, indent=2, ensure_ascii=False),
            "stderr": "",
        }
    return _inner


def _fake_collision_guard_clean(print_json: bool = True) -> dict[str, Any]:
    report = {
        "schema_id": "geoai.tasking_parallel_group_collision_guard.v1",
        "collision_free": True,
        "collision_count": 0,
        "file_collisions": [],
    }
    return {
        "ok": True,
        "returncode": 0,
        "command": ["python3", "taskctl.py", "collision-guard", "--print"],
        "stdout": "OK collision_free=true -- fixture (0 active)\n\n" + json.dumps(report, indent=2),
        "stderr": "",
    }


def _fake_collision_guard_conflict(task_id: str, other_task_id: str, file_path: str):
    def _inner(print_json: bool = True) -> dict[str, Any]:
        report = {
            "schema_id": "geoai.tasking_parallel_group_collision_guard.v1",
            "collision_free": False,
            "collision_count": 1,
            "file_collisions": [
                {
                    "file": file_path,
                    "conflicting_tasks": sorted([task_id, other_task_id]),
                    "owners": [],
                }
            ],
        }
        return {
            "ok": False,
            "returncode": 1,
            "command": ["python3", "taskctl.py", "collision-guard", "--print"],
            "stdout": "FAIL collision_free=false -- fixture (1 collision)\n\n" + json.dumps(report, indent=2),
            "stderr": "",
        }
    return _inner


# ---------------------------------------------------------------------------
# Scenario 0: module wiring -- function/mapping/server-tool present, existing
# tools not disturbed.
# ---------------------------------------------------------------------------
def test_module_wiring() -> None:
    print("[test_module_wiring] core.supervisor_loop_status + LIFECYCLE_TO_SUPERVISOR_STATE + server tool")
    check(callable(getattr(core, "supervisor_loop_status", None)), "core.supervisor_loop_status exists and is callable")

    mapping = getattr(core, "LIFECYCLE_TO_SUPERVISOR_STATE", None)
    check(mapping == {
        "pending": "planned",
        "processing": "dispatched_dryrun",
        "review": "worker_review_ready",
        "finished": "codex_reviewed",
        "blocked": "blocked",
    }, "LIFECYCLE_TO_SUPERVISOR_STATE matches B08 contract exactly")

    try:
        from aiworkhub import server
    except Exception as exc:  # pragma: no cover - mcp SDK missing in minimal env
        check(False, f"import server (mcp SDK): {exc}")
        return

    tool = getattr(server, "aiworkhub_supervisor_loop_status", None)
    check(tool is not None, "server exposes aiworkhub_supervisor_loop_status")

    server_text = (REPO_ROOT / "tools/geoai-task-mcp/src/aiworkhub/server.py").read_text(encoding="utf-8")
    def_count = server_text.count("def aiworkhub_supervisor_loop_status(")
    check(def_count == 1, f"exactly one aiworkhub_supervisor_loop_status def in server.py (found {def_count})")
    tool_decorator_count = server_text.count("@mcp.tool()")
    check(tool_decorator_count >= 16, f"~15+ existing tools preserved plus the new one ({tool_decorator_count} @mcp.tool() decorators total)")

    # existing tools spot-checked still present (not disturbed)
    for existing in (
        "aiworkhub_task_health", "aiworkhub_task_list", "aiworkhub_task_show",
        "aiworkhub_task_auto_pickup", "aiworkhub_task_mark_review", "aiworkhub_task_mark_done",
        "aiworkhub_task_collision_guard", "aiworkhub_task_codex_handoff",
        "aiworkhub_task_codex_handoff_markdown",
    ):
        check(getattr(server, existing, None) is not None, f"existing tool preserved: {existing}")


# ---------------------------------------------------------------------------
# Scenario 1: live real task (clean_task) -- real queue, this task's own id.
# ---------------------------------------------------------------------------
def test_live_real_task() -> None:
    print("[test_live_real_task] live queue, own task_id, no fixtures")
    result = core.supervisor_loop_status(SELF_TASK_ID)

    check(result["task_id"] == SELF_TASK_ID, "task_id echoed")
    check(result["process_state"] == "no_process_local_only_mvp", "process_state constant")
    check(result["supervisor_request_id"] is None, "supervisor_request_id defaults to None")
    check(isinstance(result["last_updated"], str) and result["last_updated"], "last_updated is a non-empty ISO string")
    check(result["taskctl_status"] is not None, "taskctl_status populated for a found task")
    check(result["state"] in ("planned", "blocked"), "state is planned or blocked for a real live task")

    if result["error"] is None:
        check(result["state"] == "planned", "state=planned when error=None")
        card = json.loads(core.show_task(SELF_TASK_ID)["stdout"])
        lifecycle = core._lifecycle_state(card)
        expected = core.LIFECYCLE_TO_SUPERVISOR_STATE[lifecycle]
        check(result["supervisor_state"] == expected, f"supervisor_state == mapping[{lifecycle}] == {expected}")
    else:
        check(result["state"] == "blocked", "state=blocked when error present")
        check(result["supervisor_state"] == "blocked", "supervisor_state=blocked when error present")
        check(
            result["error"]["code"] in {"collision", "stale_task", "runner_topic_mismatch", "missing_artifact", "failed_validation"},
            "error code is one of the 5 B07 taxonomy codes",
        )


# ---------------------------------------------------------------------------
# Scenario 2: not_found -- step 1 on-miss detection (stdout string check, not
# returncode).
# ---------------------------------------------------------------------------
def test_not_found() -> None:
    print("[test_not_found] nonexistent task_id")
    fake_id = "AIWORKHUB_SUPERVISOR_LOOP_STATUS_DOES_NOT_EXIST_B11_TEST"
    result = core.supervisor_loop_status(fake_id)
    check(result["state"] == "not_found", "state=not_found")
    check(result["taskctl_status"] is None, "taskctl_status is None")
    check(result["supervisor_state"] is None, "supervisor_state is None")
    check(result["error"] is None, "error is None")


# ---------------------------------------------------------------------------
# Scenario 3: runner_topic_mismatch -- direct helper cross-validation (same 4
# examples as B07/B08) + tool integration (step 2, action hardcoded 'review').
# ---------------------------------------------------------------------------
def test_runner_topic_mismatch() -> None:
    print("[test_runner_topic_mismatch] direct helper cross-validation + tool integration")
    checks = [
        ("not_claude_task_mcp_prefixed", "task_mcp", "auto-pickup", "unknown_runner_topic_pair"),
        ("claude_task_mcp_supervisor_status_b08", "task_mcp", "auto-pickup", "per_wave_prefix_allowlisted"),
        ("claude_task_mcp_supervisor_status_b08", "task_mcp", "start", "per_wave_action_not_allowed:start"),
        ("codex", "any_topic_at_all", "auto-pickup", "codex_action_not_allowed:auto-pickup"),
    ]
    for runner, topic, action, expected_reason in checks:
        real = core.check_runner_topic_allowlist(runner, topic, action)
        check(real["reason"] == expected_reason, f"check_runner_topic_allowlist({runner!r},{topic!r},{action!r}) == {expected_reason!r}")

    mismatched = core.supervisor_loop_status(SELF_TASK_ID, runner="not_claude_task_mcp_prefixed", topic="task_mcp")
    check(mismatched["state"] == "blocked", "tool: mismatched runner/topic -> state=blocked")
    err = mismatched.get("error") or {}
    check(err.get("code") == "runner_topic_mismatch", "tool: error.code=runner_topic_mismatch")
    check("unknown_runner_topic_pair" in err.get("reason", ""), "tool: reason carries unknown_runner_topic_pair")
    check(err.get("detected_at_leg") == 1, "tool: detected_at_leg=1")
    check(mismatched["supervisor_state"] == "blocked", "tool: supervisor_state=blocked")

    allowed = core.supervisor_loop_status(SELF_TASK_ID, runner="claude_task_mcp_supervisor_status_b08", topic="task_mcp")
    allowed_error = allowed.get("error")
    check(
        allowed_error is None or allowed_error.get("code") != "runner_topic_mismatch",
        "tool: per-wave-allowlisted runner/topic does not itself trip runner_topic_mismatch",
    )


# ---------------------------------------------------------------------------
# Scenario 4: fixture missing_artifact -- pure filesystem existence check,
# step 3, via a monkeypatched synthetic card.
# ---------------------------------------------------------------------------
def test_fixture_missing_artifact() -> None:
    print("[test_fixture_missing_artifact] synthetic card, nonexistent read_first path")
    fixture_path = "tools/geoai-task-mcp/fixtures/does_not_exist_missing_artifact_b11.json"
    check(not (REPO_ROOT / fixture_path).exists(), "fixture path confirmed absent on disk")

    task_id = "SYNTH_FIXTURE_TASK_MISSING_ARTIFACT_B11"
    card = {
        "task_id": task_id, "status": "pending", "worker_status": "unclaimed",
        "read_first": [fixture_path], "allowed_writes": [],
    }
    original_show_task = core.show_task
    core.show_task = _fake_show_task(card)
    try:
        result = core.supervisor_loop_status(task_id)
    finally:
        core.show_task = original_show_task

    check(result["state"] == "blocked", "missing_artifact -> state=blocked")
    err = result.get("error") or {}
    check(err.get("code") == "missing_artifact", "error.code=missing_artifact")
    check(err.get("reason") == f"missing_artifact:{fixture_path}", "reason matches missing_artifact:<path>")
    check(err.get("detected_at_leg") == 1, "detected_at_leg=1")
    check(result["supervisor_state"] == "blocked", "supervisor_state=blocked")


# ---------------------------------------------------------------------------
# Scenario 5: fixture collision -- unmodified scan_collisions cross-validation
# + full tool integration (step 4) via monkeypatched show_task/collision_guard.
# ---------------------------------------------------------------------------
def test_fixture_collision() -> None:
    print("[test_fixture_collision] scan_collisions cross-validation + tool integration")
    sys.path.insert(0, str(REPO_ROOT))
    from scripts.build_tasking_parallel_group_collision_guard_v1 import scan_collisions

    synthetic_a = {
        "task_id": "SYNTH_FIXTURE_TASK_A_B11", "status": "pending", "worker_status": "unclaimed",
        "allowed_writes": ["tools/geoai-task-mcp/fixtures/does_not_exist_shared_b11.json"],
    }
    synthetic_b = {
        "task_id": "SYNTH_FIXTURE_TASK_B_B11", "status": "pending", "worker_status": "unclaimed",
        "allowed_writes": ["tools/geoai-task-mcp/fixtures/does_not_exist_shared_b11.json"],
    }
    scan_result = scan_collisions([synthetic_a, synthetic_b])
    check(scan_result["collision_free"] is False, "scan_collisions: synthetic overlap detected")
    conflict_ids: set[str] = set()
    for fc in scan_result["file_collisions"]:
        conflict_ids.update(fc["conflicting_tasks"])
    check(
        {"SYNTH_FIXTURE_TASK_A_B11", "SYNTH_FIXTURE_TASK_B_B11"} <= conflict_ids,
        "scan_collisions: both synthetic task_ids present in conflicting_tasks",
    )

    task_id = "SYNTH_FIXTURE_TASK_COLLISION_B11"
    file_path = "tools/geoai-task-mcp/fixtures/shared_b11.json"
    card = {"task_id": task_id, "status": "pending", "worker_status": "unclaimed", "read_first": [], "allowed_writes": []}
    original_show_task = core.show_task
    original_collision_guard = core.collision_guard
    core.show_task = _fake_show_task(card)
    core.collision_guard = _fake_collision_guard_conflict(task_id, "OTHER_TASK_B11", file_path)
    try:
        result = core.supervisor_loop_status(task_id)
    finally:
        core.show_task = original_show_task
        core.collision_guard = original_collision_guard

    check(result["state"] == "blocked", "tool: collision -> state=blocked")
    err = result.get("error") or {}
    check(err.get("code") == "collision", "tool: error.code=collision")
    check(err.get("reason") == f"collision:{file_path}:{task_id}", "tool: reason matches collision:<file>:<task_id>")
    check(err.get("detected_at_leg") == 1, "tool: detected_at_leg=1")
    check(result["supervisor_state"] == "blocked", "tool: supervisor_state=blocked")


# ---------------------------------------------------------------------------
# Scenario 6: fixture stale_task -- step 5, worker_status flips
# unclaimed -> claimed between caller's previous_snapshot and the re-read.
# ---------------------------------------------------------------------------
def test_fixture_stale_task() -> None:
    print("[test_fixture_stale_task] worker_status flips unclaimed -> claimed")
    task_id = "SYNTH_FIXTURE_TASK_STALE_B11"
    card = {
        "task_id": task_id, "status": "processing", "worker_status": "claimed",
        "read_first": [], "allowed_writes": [],
    }
    previous_snapshot = {"status": "pending", "worker_status": "unclaimed"}
    cur_snapshot = {"status": card["status"], "worker_status": card["worker_status"]}

    original_show_task = core.show_task
    original_collision_guard = core.collision_guard
    core.show_task = _fake_show_task(card)
    core.collision_guard = _fake_collision_guard_clean
    try:
        result = core.supervisor_loop_status(task_id, previous_snapshot=previous_snapshot)
    finally:
        core.show_task = original_show_task
        core.collision_guard = original_collision_guard

    check(result["state"] == "blocked", "stale_task -> state=blocked")
    err = result.get("error") or {}
    check(err.get("code") == "stale_task", "error.code=stale_task")
    expected_reason = f"stale_task:worker_status_changed:{task_id}:{previous_snapshot}->{cur_snapshot}"
    check(err.get("reason") == expected_reason, "reason matches stale_task:worker_status_changed:<id>:<before>-><after>")
    check(err.get("detected_at_leg") == 4, "detected_at_leg=4")
    check(result["supervisor_state"] == "blocked", "supervisor_state=blocked")


# ---------------------------------------------------------------------------
# Scenario 7: fixture failed_validation -- step 6, relay-only caller-reported
# verdict; never independently derived.
# ---------------------------------------------------------------------------
def test_fixture_failed_validation() -> None:
    print("[test_fixture_failed_validation] caller-reported verdict=failed relayed (never independently derived)")
    task_id = "SYNTH_FIXTURE_TASK_FAILED_VALIDATION_B11"
    card = {
        "task_id": task_id, "status": "pending", "worker_status": "unclaimed",
        "read_first": [], "allowed_writes": [],
    }
    original_show_task = core.show_task
    original_collision_guard = core.collision_guard
    core.show_task = _fake_show_task(card)
    core.collision_guard = _fake_collision_guard_clean
    try:
        result = core.supervisor_loop_status(task_id, reported_validation_verdict="failed")
        check(result["state"] == "blocked", "failed_validation -> state=blocked")
        err = result.get("error") or {}
        check(err.get("code") == "failed_validation", "error.code=failed_validation")
        check(err.get("reason") == f"failed_validation:reported_by_caller:{task_id}", "reason matches failed_validation:reported_by_caller:<id>")
        check(err.get("detected_at_leg") == 4, "detected_at_leg=4")
        check(result["supervisor_state"] == "blocked", "supervisor_state=blocked")

        for verdict in (None, "passed"):
            clean = core.supervisor_loop_status(task_id, reported_validation_verdict=verdict)
            clean_err = clean.get("error")
            check(
                clean_err is None or clean_err.get("code") != "failed_validation",
                f"verdict={verdict!r} does not trigger failed_validation",
            )
            check(clean["state"] == "planned", f"verdict={verdict!r} -> clean card falls through to state=planned")
    finally:
        core.show_task = original_show_task
        core.collision_guard = original_collision_guard


# ---------------------------------------------------------------------------
# Scenario 8: registered MCP server tool wiring -- calls the real decorated
# function, live, read-only; proves it matches the B08 output_schema exactly
# and leaves the live queue/manifest/audit-log byte-identical.
# ---------------------------------------------------------------------------
def test_server_tool_wiring() -> None:
    print("[test_server_tool_wiring] registered MCP tool reads live queue read-only")
    try:
        from aiworkhub import server
    except Exception as exc:  # pragma: no cover - mcp SDK missing in minimal env
        check(False, f"import server (mcp SDK): {exc}")
        return

    tool = getattr(server, "aiworkhub_supervisor_loop_status", None)
    check(tool is not None, "server exposes aiworkhub_supervisor_loop_status")
    if tool is None:
        return

    before = _snapshot()
    try:
        result = tool(task_id=SELF_TASK_ID)
    except TypeError as exc:  # pragma: no cover - decorator wrapped it
        check(False, f"tool not directly callable: {exc}")
        return
    after = _snapshot()

    check(isinstance(result, dict), "server tool returns a dict")
    check(result.get("task_id") == SELF_TASK_ID, "server tool echoes task_id")
    check(result.get("process_state") == "no_process_local_only_mvp", "server tool process_state constant")
    expected_keys = {
        "task_id", "supervisor_request_id", "state", "taskctl_status",
        "process_state", "last_updated", "supervisor_state", "error",
    }
    check(set(result.keys()) == expected_keys, f"server tool output has exactly the B08 output_schema keys (got {sorted(result.keys())})")
    check(before == after, "server tool call left queue/manifest/audit-log byte-identical")


def main() -> int:
    print("=== B11 Supervisor Loop STATUS Wiring: 6 B08 scenarios + no-mutation proof ===")
    before_all = _snapshot()

    test_module_wiring()
    print("")
    test_live_real_task()
    print("")
    test_not_found()
    print("")
    test_runner_topic_mismatch()
    print("")
    test_fixture_missing_artifact()
    print("")
    test_fixture_collision()
    print("")
    test_fixture_stale_task()
    print("")
    test_fixture_failed_validation()
    print("")
    test_server_tool_wiring()
    print("")

    after_all = _snapshot()
    check(
        before_all == after_all,
        "FULL-SESSION: live queue JSONL + manifest + audit log byte-identical before vs after all scenarios",
    )

    print("")
    print(f"PASS={len(PASSES)} FAIL={len(FAILURES)}")
    if FAILURES:
        print("FAILURES:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL PASS")
    return 0


sys.exit(main())
PYEOF
RC=$?

echo ""
if [ $RC -ne 0 ]; then
    echo "=== test_task_mcp_supervisor_loop_status_wiring_b11_v1.sh: FAIL (exit=$RC) ==="
    exit $RC
fi

echo "--- taskctl verify (parent queue intact; this script never calls review/done/auto-pickup/start) ---"
python3 "$ROOT/AITools/taskctl.py" verify
echo ""
echo "=== test_task_mcp_supervisor_loop_status_wiring_b11_v1.sh: PASS ==="
exit 0
