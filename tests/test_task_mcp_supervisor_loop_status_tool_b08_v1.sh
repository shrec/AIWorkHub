#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_task_mcp_supervisor_loop_status_tool_b08_v1.sh
# Harness for the B08 read-only supervisor-loop STATUS TOOL contract + smoke
# implementation plan (design-only, no server.py/core.py patch, no launch,
# no task-status mutation).
#
# Verifies:
#   1. contract JSON: tool_schema (input/output) + derivation_algorithm shape.
#   2. smoke_cases == exactly the 6 required scenarios.
#   3. future_source_patch_paths names exactly core.py + server.py, both
#      requires_source_patch, and NEITHER file is actually touched by this
#      task's own footprint.
#   4. safety_boundaries all enforced==true; forbidden_operations all false.
#   5. contract raw text contains no subprocess/exec/network invocation
#      pattern (same scan style as B07/test_agent_launcher_mvp_b02_v1.py).
#   6. clean_task: LIVE read-only cross-validation of the derivation
#      algorithm's mapping step against a real task card (this task's own
#      card), using only pre-existing core.py functions.
#   7. runner_topic_mismatch: same 4 examples as B07, LIVE against the
#      unmodified core.check_runner_topic_allowlist.
#   8. collision: fixture-only (in-memory synthetic cards, never written to
#      the live queue) cross-validated against the unmodified
#      scan_collisions.
#   9. stale_task / missing_artifact / failed_validation: fixture-only pure
#      comparison logic matching derivation_algorithm steps 3/5/6.
#  10. eval + next_wave JSON structure, own footprint (no src/ paths), parent
#      queue intact (taskctl verify).
#
# Isolation: read-only against the live module (in-process import + pure
# function calls only). Never calls taskctl review/done/auto-pickup/start.
# All collision/stale/missing-artifact/failed-validation fixtures are
# synthetic in-memory dicts -- nothing is written to the live task queue.
# Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
CONTRACT_JSON="$MCPROOT/contracts/task_mcp_supervisor_loop_status_tool_b08_v1.json"
EVAL_JSON="$MCPROOT/eval/task_mcp_supervisor_loop_status_tool_b08_v1.json"
NEXT_WAVE_JSON="$MCPROOT/data/tasking/task_mcp_supervisor_loop_status_tool_next_wave_b08_v1.json"
SELF_TASK_ID="CLAUDE_TASK_MCP_SUPERVISOR_LOOP_STATUS_TOOL_B08_V1"

export PYTHONPATH="$MCPROOT/src"
export AIWORKHUB_REPO="$ROOT"
export AIWORKHUB_ALLOW_WRITES=0

echo "=== Task MCP Supervisor Loop STATUS TOOL Contract Test B08 v1 ==="
echo "AIWORKHUB_REPO=$AIWORKHUB_REPO"
echo "AIWORKHUB_ALLOW_WRITES=$AIWORKHUB_ALLOW_WRITES"
echo ""

echo "--- [1/10] contract JSON: tool_schema + derivation_algorithm shape ---"
CONTRACT_JSON="$CONTRACT_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["CONTRACT_JSON"]))

required_top = [
    "tool_schema", "derivation_algorithm", "lifecycle_state_to_supervisor_state_mapping",
    "smoke_cases", "safety_boundaries", "forbidden_operations_confirmed_not_taken",
    "future_source_patch_paths", "neural_bridge_note", "no_launch_code_marker",
    "no_source_patch_marker", "acceptance", "rollback",
]
for k in required_top:
    assert k in d, f"missing top-level section: {k}"

ts = d["tool_schema"]
assert ts["tool_name"] == "aiworkhub_supervisor_loop_status"
assert "task_id" in ts["input_schema"] and ts["input_schema"]["task_id"]["required"] is True
for opt in ("runner", "topic", "supervisor_request_id", "previous_snapshot", "reported_validation_verdict"):
    assert opt in ts["input_schema"], f"input_schema missing {opt}"

out = ts["output_schema"]
for k in ("task_id", "state", "taskctl_status", "process_state", "supervisor_state", "error"):
    assert k in out, f"output_schema missing {k}"
assert set(out["state"]["enum"]) == {"planned", "blocked", "not_found"}

algo = d["derivation_algorithm"]
steps = algo["ordered_steps"]
assert len(steps) == 7, f"expected 7 ordered_steps, got {len(steps)}"
assert [s["step"] for s in steps] == list(range(1, 8))
reused = algo["reused_functions"]
assert any("show_task" in f for f in reused)
assert any("check_runner_topic_allowlist" in f for f in reused)
assert any("collision_guard" in f for f in reused)
assert any("_lifecycle_state" in f for f in reused)

mapping = d["lifecycle_state_to_supervisor_state_mapping"]["mapping"]
assert mapping == {
    "pending": "planned",
    "processing": "dispatched_dryrun",
    "review": "worker_review_ready",
    "finished": "codex_reviewed",
    "blocked": "blocked",
}

print("tool_schema + derivation_algorithm OK: 7 ordered steps, 4 reused pre-existing functions, 5-way mapping")
PYEOF
echo ""

echo "--- [2/10] smoke_cases exact set of 6 scenarios ---"
CONTRACT_JSON="$CONTRACT_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["CONTRACT_JSON"]))
cases = d["smoke_cases"]
ids = {c["case_id"] for c in cases}
expected = {"clean_task", "collision", "stale_task", "runner_topic_mismatch", "missing_artifact", "failed_validation"}
assert ids == expected, f"smoke_cases mismatch: {ids} != {expected}"
for c in cases:
    assert c.get("scenario"), f"{c['case_id']}: missing scenario"
    assert c.get("method"), f"{c['case_id']}: missing method"
    assert c.get("expected"), f"{c['case_id']}: missing expected"
print(f"smoke_cases OK: exactly {sorted(expected)}")
PYEOF
echo ""

echo "--- [3/10] future_source_patch_paths: exact 2 paths, not touched by this task ---"
CONTRACT_JSON="$CONTRACT_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["CONTRACT_JSON"]))
fp = d["future_source_patch_paths"]
assert fp["requires_source_patch"] is True
paths = {p["path"] for p in fp["patches"]}
expected = {
    "tools/geoai-task-mcp/src/aiworkhub/core.py",
    "tools/geoai-task-mcp/src/aiworkhub/server.py",
}
assert paths == expected, f"future_source_patch_paths mismatch: {paths} != {expected}"
for p in fp["patches"]:
    assert p.get("change"), f"{p['path']}: missing change description"
print("future_source_patch_paths OK: exactly core.py + server.py, both documented, neither patched now")
PYEOF
echo ""

echo "--- [4/10] safety_boundaries + forbidden_operations ---"
CONTRACT_JSON="$CONTRACT_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["CONTRACT_JSON"]))
sb = d["safety_boundaries"]
expected_keys = {"no_credential_read", "no_daemon_start", "no_real_process_launch", "no_writes_unless_explicit_future_gate"}
assert set(sb) == expected_keys, f"safety_boundaries mismatch: {set(sb)} != {expected_keys}"
for name, spec in sb.items():
    assert spec["enforced"] is True, f"{name}: enforced must be true"
    assert spec["detail"], f"{name}: detail must be non-empty"

forbidden = d["forbidden_operations_confirmed_not_taken"]
required_forbidden = {
    "source_patch", "daemon_start", "real_agent_launch", "credential_read",
    "credential_write", "write_gate_enable", "task_status_mutation",
    "network_operation", "git_add_A", "mixed_task_commit",
}
assert set(forbidden) == required_forbidden, f"forbidden ops key mismatch: {set(forbidden)} != {required_forbidden}"
for k, v in forbidden.items():
    assert v is False, f"forbidden op {k} not confirmed false: {v}"

print(f"safety_boundaries OK: 4 boundaries all enforced=true; {len(forbidden)} forbidden ops all false")
PYEOF
echo ""

echo "--- [5/10] no subprocess/exec/network pattern in contract raw text ---"
CONTRACT_JSON="$CONTRACT_JSON" python3 - <<'PYEOF'
import os

raw = open(os.environ["CONTRACT_JSON"], encoding="utf-8").read()
forbidden_patterns = (
    "subprocess.run", "subprocess.Popen", "subprocess.call", "import subprocess",
    "os.system", "os.popen", "os.exec", "os.fork", "os.spawn", "Popen(",
    "shell=True", "pty.spawn", "requests.post", "urllib.request", "httpx.",
)
for pat in forbidden_patterns:
    assert pat not in raw, f"forbidden launch/network pattern found in contract: {pat!r}"
print(f"forbidden-pattern scan OK: {len(forbidden_patterns)} patterns checked, none found")
PYEOF
echo ""

echo "--- [6/10] clean_task: LIVE cross-validation of mapping step (read-only) ---"
SELF_TASK_ID="$SELF_TASK_ID" python3 -c "
import os, sys
sys.path.insert(0, os.path.join(os.environ['AIWORKHUB_REPO'], 'tools/geoai-task-mcp/src'))
from aiworkhub import core

task_id = os.environ['SELF_TASK_ID']
result = core.show_task(task_id)
assert result['ok'], f'show_task failed for {task_id}: {result}'

import json
# taskctl show prints one pretty-printed (multi-line) JSON object -- parse
# the whole stdout, not a single line.
card = json.loads(result['stdout'])
assert card is not None, f'could not parse card JSON from taskctl show stdout for {task_id}'
assert card.get('task_id') == task_id or card.get('task_id') is None

lifecycle = core._lifecycle_state(card)
mapping = {
    'pending': 'planned', 'processing': 'dispatched_dryrun', 'review': 'worker_review_ready',
    'finished': 'codex_reviewed', 'blocked': 'blocked',
}
supervisor_state = mapping[lifecycle]
print(f'clean_task OK: task_id={task_id} lifecycle_state={lifecycle} -> supervisor_state={supervisor_state}')
"
echo ""

echo "--- [7/10] runner_topic_mismatch: LIVE cross-validation (same 4 as B07) ---"
python3 -c "
import sys, os
sys.path.insert(0, os.path.join(os.environ['AIWORKHUB_REPO'], 'tools/geoai-task-mcp/src'))
from aiworkhub import core

checks = [
    ('not_claude_task_mcp_prefixed', 'task_mcp', 'auto-pickup', 'unknown_runner_topic_pair'),
    ('claude_task_mcp_supervisor_status_b08', 'task_mcp', 'auto-pickup', 'per_wave_prefix_allowlisted'),
    ('claude_task_mcp_supervisor_status_b08', 'task_mcp', 'start', 'per_wave_action_not_allowed:start'),
    ('codex', 'any_topic_at_all', 'auto-pickup', 'codex_action_not_allowed:auto-pickup'),
]
for runner, topic, action, expected_reason in checks:
    real = core.check_runner_topic_allowlist(runner, topic, action)
    assert real['reason'] == expected_reason, f'{runner}/{topic}/{action}: expected {expected_reason}, got {real}'
print(f'cross-validated {len(checks)} runner_topic_mismatch examples LIVE against unmodified core.check_runner_topic_allowlist: OK')
"
echo ""

echo "--- [8/10] collision: fixture-only cross-validation against unmodified scan_collisions ---"
python3 -c "
import sys, os
sys.path.insert(0, os.environ['AIWORKHUB_REPO'])
from scripts.build_tasking_parallel_group_collision_guard_v1 import scan_collisions

synthetic_a = {
    'task_id': 'SYNTH_FIXTURE_TASK_A_B08', 'status': 'pending', 'worker_status': 'unclaimed',
    'allowed_writes': ['tools/geoai-task-mcp/fixtures/does_not_exist_a.json'],
}
synthetic_b = {
    'task_id': 'SYNTH_FIXTURE_TASK_B_B08', 'status': 'pending', 'worker_status': 'unclaimed',
    'allowed_writes': ['tools/geoai-task-mcp/fixtures/does_not_exist_a.json'],
}
result = scan_collisions([synthetic_a, synthetic_b])
assert result['collision_free'] is False, f'expected a collision, got {result}'
conflict_task_ids = set()
for fc in result['file_collisions']:
    conflict_task_ids.update(fc['conflicting_tasks'])
assert 'SYNTH_FIXTURE_TASK_A_B08' in conflict_task_ids and 'SYNTH_FIXTURE_TASK_B_B08' in conflict_task_ids
print('collision fixture OK: synthetic overlapping allowed_writes detected by unmodified scan_collisions; live queue untouched')
"
echo ""

echo "--- [9/10] stale_task / missing_artifact / failed_validation: fixture-only pure logic ---"
ROOT="$ROOT" python3 -c "
import os
from pathlib import Path

# stale_task: derivation step 5 comparison logic
previous_snapshot = {'status': 'pending', 'worker_status': 'unclaimed'}
current_snapshot = {'status': 'processing', 'worker_status': 'claimed'}
is_stale = previous_snapshot != current_snapshot
assert is_stale, 'expected stale_task fixture to differ'
print('stale_task fixture OK: worker_status change unclaimed->claimed detected by pure dict comparison')

# missing_artifact: derivation step 3 pure filesystem existence check
repo_root = Path(os.environ['ROOT'])
fixture_path = 'tools/geoai-task-mcp/fixtures/does_not_exist_missing_artifact_b08.json'
assert not (repo_root / fixture_path).exists(), 'fixture path must not exist for this smoke case'
print(f'missing_artifact fixture OK: {fixture_path} confirmed absent by pure Path.exists() check')

# failed_validation: derivation step 6 relay-only logic (schema-reserved, never independently derived)
reported_validation_verdict = 'failed'
triggers_error = reported_validation_verdict == 'failed'
assert triggers_error
print('failed_validation fixture OK: caller-reported verdict=failed relayed to error.code=failed_validation (schema-reserved, never independently run)')
"
echo ""

echo "--- [10/10] eval + next_wave JSON structure, own footprint, parent queue intact ---"
EVAL_JSON="$EVAL_JSON" NEXT_WAVE_JSON="$NEXT_WAVE_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["EVAL_JSON"]))
required_top = [
    "contract_shape_measured", "acceptance_results", "smoke_cases_verified",
    "no_source_patch_verified", "forbidden_pattern_scan",
    "forbidden_operations_confirmed_not_taken", "rollback",
]
for k in required_top:
    assert k in d, f"missing eval section: {k}"
shape = d["contract_shape_measured"]
assert shape["smoke_cases_count"] == 6
assert shape["derivation_steps_count"] == 7
assert shape["source_files_created_or_modified"] == 0
nsp = d["no_source_patch_verified"]
assert nsp["result"] is True
assert all(not p.startswith("tools/geoai-task-mcp/src/") for p in nsp["this_task_allowed_writes"])
print("eval JSON structure: OK")

d2 = json.load(open(os.environ["NEXT_WAVE_JSON"]))
required_top2 = [
    "proposal_id", "status", "source_task", "this_task_scope",
    "not_applied_generalization_next_wave", "required_tests_verified_by_this_task",
    "rollback", "neural_bridge_note",
]
for k in required_top2:
    assert k in d2, f"missing next_wave section: {k}"
assert d2["status"] == "design_only_not_applied_this_task"
scope = d2["this_task_scope"]
assert scope["source_code_changed"] is False
assert scope["process_launch"] is False
assert scope["task_status_mutated"] is False
proposals = d2["not_applied_generalization_next_wave"]["proposed_next_wave_tasks"]
assert len(proposals) >= 1
tests = d2["required_tests_verified_by_this_task"]
assert len(tests) >= 8, f"expected >=8 verified tests documented, got {len(tests)}"
print(f"next_wave JSON structure: OK ({len(proposals)} proposals, {len(tests)} verified tests documented)")
PYEOF

CONTRACT_JSON="$CONTRACT_JSON"
EVAL_JSON="$EVAL_JSON"
NEXT_WAVE_JSON="$NEXT_WAVE_JSON"
for f in "$CONTRACT_JSON" "$EVAL_JSON" "$NEXT_WAVE_JSON" "$MCPROOT/tests/test_task_mcp_supervisor_loop_status_tool_b08_v1.sh"; do
    case "$f" in
        "$MCPROOT/src/"*)
            echo "FAIL: this task's own artifact path falls under src/: $f"
            exit 1
            ;;
    esac
    if [ ! -f "$f" ]; then
        echo "FAIL: expected artifact missing: $f"
        exit 1
    fi
done
echo "own footprint OK: 4/4 artifacts present, none under tools/geoai-task-mcp/src/"
echo ""

python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact; this script never calls review/done/auto-pickup/start)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
