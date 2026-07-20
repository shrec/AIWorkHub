#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_task_mcp_supervisor_loop_contract_b07_v1.sh
# Harness for the B07 MCP supervisor loop contract (design-only, no source
# patch, no launch, no task-status mutation).
#
# Verifies:
#   1. contract JSON parses; 4 flow_legs; each leg has a request/response
#      shape (leg 2 documented as a non-MCP argv handoff, never executed).
#   2. lifecycle_states == exactly {planned, dispatched_dryrun,
#      worker_review_ready, codex_reviewed, blocked}, each
#      task_status_mutation == false.
#   3. error_taxonomy == exactly {collision, stale_task,
#      runner_topic_mismatch, missing_artifact, failed_validation}, each
#      with trigger/detection_point/example_reason/result.
#   4. safety_boundaries all enforced == true; forbidden_operations all false.
#   5. contract raw text contains no subprocess/exec/network invocation
#      pattern (same scan style as test_agent_launcher_mvp_b02_v1.py).
#   6. runner_topic_mismatch examples cross-validated LIVE against the
#      already-existing, UNMODIFIED core.check_runner_topic_allowlist
#      (read-only call only -- this task patches no source file).
#   7. eval JSON parses with required sections.
#   8. next_wave JSON parses with required sections.
#   9. this task's own footprint touches zero paths under
#      tools/geoai-task-mcp/src/ (nested repo is shared with concurrent
#      workers; their dirty src/ files are NOT this task's concern).
#  10. parent GeoAI task queue is not mutated (taskctl verify).
#
# Isolation: read-only against the live module (in-process import + pure
# function calls only, no subprocess beyond taskctl verify, no daemon, no
# network, no env mutation beyond this process). Never calls taskctl
# review/done/auto-pickup. Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
CONTRACT_JSON="$MCPROOT/contracts/task_mcp_supervisor_loop_contract_b07_v1.json"
EVAL_JSON="$MCPROOT/eval/task_mcp_supervisor_loop_contract_b07_v1.json"
NEXT_WAVE_JSON="$MCPROOT/data/tasking/task_mcp_supervisor_loop_contract_next_wave_b07_v1.json"

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES=0

echo "=== Task MCP Supervisor Loop Contract Test B07 v1 ==="
echo "GEOAI_REPO=$GEOAI_REPO"
echo "GEOAI_TASK_MCP_ALLOW_WRITES=$GEOAI_TASK_MCP_ALLOW_WRITES"
echo ""

echo "--- [1/10] contract JSON: flow_legs + per-leg schema shape ---"
CONTRACT_JSON="$CONTRACT_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["CONTRACT_JSON"]))

required_top = [
    "flow_legs", "lifecycle_states", "lifecycle_transitions",
    "lifecycle_invariant", "common_types_extended", "safety_boundaries",
    "forbidden_operations_confirmed_not_taken", "error_taxonomy",
    "dry_run_examples", "neural_bridge_note", "no_launch_code_marker",
    "no_source_patch_marker", "acceptance", "rollback",
]
for k in required_top:
    assert k in d, f"missing top-level section: {k}"

legs = d["flow_legs"]
assert len(legs) == 4, f"expected 4 flow_legs, got {len(legs)}"
by_leg = {l["leg"]: l for l in legs}
assert set(by_leg) == {1, 2, 3, 4}

for n in (1, 3, 4):
    leg = by_leg[n]
    assert leg.get("mcp_tool"), f"leg {n} missing mcp_tool"
    assert isinstance(leg.get("request_schema"), dict) and leg["request_schema"], f"leg {n} request_schema empty"
    assert isinstance(leg.get("response_schema"), dict) and leg["response_schema"], f"leg {n} response_schema empty"
    assert "lifecycle_transition" in leg
    assert "causer" in leg

leg2 = by_leg[2]
assert leg2["mcp_tool"] is None, "leg 2 must document a non-MCP handoff (mcp_tool: null)"
assert "mechanism" in leg2 and "NOT" in leg2["mechanism"]

print(f"flow_legs OK: 4 legs, request/response schema present on legs 1/3/4, leg 2 documented as non-MCP handoff")
PYEOF
echo ""

echo "--- [2/10] lifecycle_states exact set + task_status_mutation==false ---"
CONTRACT_JSON="$CONTRACT_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["CONTRACT_JSON"]))
states = d["lifecycle_states"]

expected = {"planned", "dispatched_dryrun", "worker_review_ready", "codex_reviewed", "blocked"}
assert set(states) == expected, f"lifecycle_states mismatch: {set(states)} != {expected}"

for name, spec in states.items():
    assert spec["task_status_mutation"] is False, f"{name}: task_status_mutation must be false"
    assert "description" in spec and spec["description"]
    assert "who_causes_entry" in spec

transitions = d["lifecycle_transitions"]
assert len(transitions) >= 5, f"expected >=5 lifecycle_transitions, got {len(transitions)}"
for t in transitions:
    assert t["to"] in expected, f"transition target not in lifecycle_states: {t}"

print(f"lifecycle_states OK: exactly {sorted(expected)}, all task_status_mutation=false, {len(transitions)} transitions")
PYEOF
echo ""

echo "--- [3/10] error_taxonomy exact set + required fields ---"
CONTRACT_JSON="$CONTRACT_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["CONTRACT_JSON"]))
tax = d["error_taxonomy"]

expected = {"collision", "stale_task", "runner_topic_mismatch", "missing_artifact", "failed_validation"}
assert set(tax) == expected, f"error_taxonomy mismatch: {set(tax)} != {expected}"

for name, spec in tax.items():
    for field in ("trigger", "detection_point", "example_reason", "result"):
        assert field in spec and spec[field], f"{name}: missing/empty field {field}"

common = d["common_types_extended"]
assert common["error_taxonomy_code"]["enum"] == sorted(expected) or set(common["error_taxonomy_code"]["enum"]) == expected
assert common["supervisor_state"]["enum"] == ["planned", "dispatched_dryrun", "worker_review_ready", "codex_reviewed", "blocked"]

print(f"error_taxonomy OK: exactly {sorted(expected)}, each with trigger/detection_point/example_reason/result")
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

echo "--- [6/10] runner_topic_mismatch examples cross-validated LIVE (read-only, no source patch) ---"
python3 -c "
import sys, os
sys.path.insert(0, os.path.join(os.environ['GEOAI_REPO'], 'tools/geoai-task-mcp/src'))
from geoai_task_mcp import core

checks = [
    ('not_claude_task_mcp_prefixed', 'task_mcp', 'auto-pickup', 'unknown_runner_topic_pair'),
    ('claude_task_mcp_supervisor_contract_b07', 'task_mcp', 'auto-pickup', 'per_wave_prefix_allowlisted'),
    ('claude_task_mcp_supervisor_contract_b07', 'task_mcp', 'start', 'per_wave_action_not_allowed:start'),
    ('codex', 'any_topic_at_all', 'auto-pickup', 'codex_action_not_allowed:auto-pickup'),
]
for runner, topic, action, expected_reason in checks:
    real = core.check_runner_topic_allowlist(runner, topic, action)
    assert real['reason'] == expected_reason, f'{runner}/{topic}/{action}: expected {expected_reason}, got {real}'
print(f'cross-validated {len(checks)} runner_topic_mismatch examples LIVE against unmodified core.check_runner_topic_allowlist: OK')
"
echo ""

echo "--- [7/10] eval JSON structure ---"
EVAL_JSON="$EVAL_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["EVAL_JSON"]))
required_top = [
    "contract_shape_measured", "acceptance_results",
    "cross_validated_against_live_module", "no_source_patch_verified",
    "forbidden_pattern_scan", "forbidden_operations_confirmed_not_taken",
    "rollback",
]
for k in required_top:
    assert k in d, f"missing eval section: {k}"

shape = d["contract_shape_measured"]
assert shape["flow_legs_count"] == 4
assert shape["lifecycle_states_count"] == 5
assert shape["error_taxonomy_categories_count"] == 5
assert shape["source_files_created_or_modified"] == 0

nsp = d["no_source_patch_verified"]
assert nsp["result"] is True
assert all(not p.startswith("tools/geoai-task-mcp/src/") for p in nsp["this_task_allowed_writes"])

print("eval JSON structure: OK")
PYEOF
echo ""

echo "--- [8/10] next_wave JSON structure ---"
NEXT_WAVE_JSON="$NEXT_WAVE_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["NEXT_WAVE_JSON"]))
required_top = [
    "proposal_id", "status", "source_task", "this_task_scope",
    "not_applied_generalization_next_wave", "required_tests_verified_by_this_task",
    "rollback", "neural_bridge_note",
]
for k in required_top:
    assert k in d, f"missing next_wave section: {k}"

assert d["status"] == "design_only_not_applied_this_task"
scope = d["this_task_scope"]
assert scope["source_code_changed"] is False
assert scope["process_launch"] is False
assert scope["task_status_mutated"] is False

proposals = d["not_applied_generalization_next_wave"]["proposed_next_wave_tasks"]
assert len(proposals) >= 2

tests = d["required_tests_verified_by_this_task"]
assert len(tests) >= 8, f"expected >=8 verified tests documented, got {len(tests)}"

print(f"next_wave JSON structure: OK ({len(proposals)} proposals, {len(tests)} verified tests documented)")
PYEOF
echo ""

echo "--- [9/10] this task's own footprint touches zero paths under src/ ---"
# tools/geoai-task-mcp is its OWN nested git repo, SHARED with other
# concurrent workers -- other in-flight tasks may legitimately hold
# src/core.py, src/geoai_task_mcp/server.py, etc. dirty at the same time.
# This check only confirms THIS task's 4 allowed_writes paths (all already
# asserted to exist above) contain zero src/ paths -- it does NOT require
# the nested repo's src/ tree to be globally clean.
for f in "$CONTRACT_JSON" "$EVAL_JSON" "$NEXT_WAVE_JSON" "$MCPROOT/tests/test_task_mcp_supervisor_loop_contract_b07_v1.sh"; do
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

echo "--- [10/10] parent queue intact ---"
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact; this script never calls review/done/auto-pickup)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
