#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_runner_topic_allowlist_dryrun_b05_v1.sh
# Harness for the B05 runner/topic allowlist dry-run policy artifacts.
#
# Verifies:
#   1. eval JSON parses and required sections are present, all
#      forbidden_operations_confirmed_not_taken are false
#   2. blocked_examples_measured are cross-validated LIVE against the real
#      core.check_runner_topic_allowlist (read-only import, no file text
#      read, no subprocess launch, no daemon)
#   3. write_gate_status_measured / launch_status_measured match the live
#      module (writes_allowed()/launch_enabled()/LAUNCH_IMPLEMENTED all False)
#   4. next_wave proposal JSON parses, is explicitly not_applied_in_this_task,
#      keeps process_launch=false / write_gate off, and documents the
#      proposed code change + required tests (proposal only, not code)
#   5. the actual current_allowlist_snapshot in the eval JSON has NO entry
#      for topic=='task_mcp' (i.e. the patch really is unapplied -- this
#      guards against silently drifting into "already patched")
#   6. no source .py file under tools/geoai-task-mcp/src changed as part of
#      this task (git diff --stat is empty for that dir, best-effort check)
#   7. parent task queue is not mutated (taskctl verify)
#
# Isolation: read-only; no writes to any shared repo path; no env var
# mutation beyond this process; never calls taskctl review/done.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
EVAL_JSON="$MCPROOT/eval/runner_topic_allowlist_dryrun_b05_v1.json"
NEXT_WAVE_JSON="$MCPROOT/data/tasking/runner_topic_allowlist_dryrun_next_wave_b05_v1.json"

export PYTHONPATH="$MCPROOT/src"
export AIWORKHUB_REPO="$ROOT"
export AIWORKHUB_ALLOW_WRITES=0

echo "=== Runner/Topic Allowlist DryRun Test B05 v1 ==="
echo "AIWORKHUB_REPO=$AIWORKHUB_REPO"
echo "AIWORKHUB_ALLOW_WRITES=$AIWORKHUB_ALLOW_WRITES"
echo ""

echo "--- [1/7] eval JSON structure ---"
EVAL_JSON="$EVAL_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["EVAL_JSON"]))

required_top = [
    "measurement_method", "forbidden_operations_confirmed_not_taken",
    "current_allowlist_snapshot_measured", "blocked_examples_measured",
    "write_gate_status_measured", "launch_status_measured",
    "finding_confirmed", "proposal_pointer", "neural_bridge_note", "rollback",
]
for k in required_top:
    assert k in d, f"missing top-level section: {k}"

forbidden = d["forbidden_operations_confirmed_not_taken"]
assert forbidden["source_code_patch"] is False, "source_code_patch must be confirmed false"
for k, v in forbidden.items():
    assert v is False, f"forbidden op {k} not confirmed false: {v}"

examples = d["blocked_examples_measured"]
assert len(examples) >= 3, f"expected >=3 blocked/control examples, got {len(examples)}"

print(f"eval JSON structure: OK ({len(examples)} measured examples, "
      f"{len(forbidden)} forbidden-ops all false)")
PYEOF
echo ""

echo "--- [2/7] cross-validate measured examples against REAL core code ---"
EVAL_JSON="$EVAL_JSON" python3 - <<'PYEOF'
import json, os, sys
sys.path.insert(0, os.path.join(os.environ["AIWORKHUB_REPO"], "tools/geoai-task-mcp/src"))
from aiworkhub import core

d = json.load(open(os.environ["EVAL_JSON"]))
examples = d["blocked_examples_measured"]

checked = 0
for ex in examples:
    runner, topic, action = ex["runner"], ex["topic"], ex["action"]
    real = core.check_runner_topic_allowlist(runner, topic, action)
    assert real["allowed"] == ex["result"]["allowed"], (
        f"{runner}/{topic}/{action}: allowed mismatch real={real} recorded={ex['result']}")
    assert real["reason"] == ex["result"]["reason"], (
        f"{runner}/{topic}/{action}: reason mismatch real={real} recorded={ex['result']}")
    checked += 1

assert checked == len(examples), "not all examples were checked"

# The two headline task_mcp examples must STILL be blocked (proves the
# patch was never applied and this is genuinely a dry-run artifact).
still_blocked = core.check_runner_topic_allowlist(
    "claude_task_mcp_allowlist_dryrun_b05", "task_mcp", "review")
assert still_blocked == {"allowed": False, "reason": "unknown_runner_topic_pair"}, (
    f"expected this task's own runner/topic to remain blocked pre-patch, got {still_blocked}")

print(f"cross-validated {checked} measured examples against live core.py: OK "
      f"(task_mcp still unpatched: {still_blocked})")
PYEOF
echo ""

echo "--- [3/7] write gate + launch status match live module ---"
python3 -c "
import sys, os
sys.path.insert(0, os.path.join(os.environ['AIWORKHUB_REPO'], 'tools/geoai-task-mcp/src'))
from aiworkhub import core, cli_adapter_dryrun as cad
assert core.writes_allowed() is False, 'core.writes_allowed() must be False'
assert cad.writes_allowed() is False, 'cli_adapter_dryrun.writes_allowed() must be False'
assert cad.launch_enabled() is False, 'launch_enabled() must be False'
assert cad.LAUNCH_IMPLEMENTED is False, 'LAUNCH_IMPLEMENTED must be False'
print('write_gate off + launch disabled: OK')
"
echo ""

echo "--- [4/7] next_wave proposal JSON structure ---"
NEXT_WAVE_JSON="$NEXT_WAVE_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["NEXT_WAVE_JSON"]))

required_top = [
    "proposal_id", "status", "not_applied_in_this_task", "process_launch",
    "write_gate_default", "target_file", "target_function",
    "current_behavior_measured", "proposed_change",
    "required_tests_for_the_real_patch", "rollback", "neural_bridge_note",
]
for k in required_top:
    assert k in d, f"missing next_wave section: {k}"

assert d["status"] == "proposed_not_applied", f"status must be proposed_not_applied, got {d['status']}"
assert d["not_applied_in_this_task"] is True, "not_applied_in_this_task must be True"
assert d["process_launch"] is False, "process_launch must stay False in the proposal"
assert "off" in d["write_gate_default"].lower(), "write_gate_default must document off-by-default"

pc = d["proposed_change"]
for f in ("description", "new_module_constant", "check_runner_topic_allowlist_diff_sketch",
          "action_scope_rationale", "malformed_token_check_unchanged", "backward_compatibility"):
    assert f in pc, f"proposed_change missing field: {f}"
assert "task_mcp" in pc["new_module_constant"], "proposal must scope the new constant to task_mcp"

tests = d["required_tests_for_the_real_patch"]
assert len(tests) >= 5, f"expected >=5 required tests documented, got {len(tests)}"

print(f"next_wave proposal structure: OK ({len(tests)} required tests documented, "
      f"target={d['target_file']}::{d['target_function']})")
PYEOF
echo ""

echo "--- [5/7] task_mcp truly absent from the live/recorded allowlist (patch unapplied) ---"
python3 -c "
import json, os, sys
sys.path.insert(0, os.path.join(os.environ['AIWORKHUB_REPO'], 'tools/geoai-task-mcp/src'))
from aiworkhub import core

live_topics = {topic for (_runner, topic) in core.RUNNER_TOPIC_ALLOWLIST.keys()}
assert 'task_mcp' not in live_topics, (
    f'task_mcp unexpectedly present in live RUNNER_TOPIC_ALLOWLIST topics: {live_topics} '
    '-- this test/proposal is stale, the source may already have been patched')

eval_json = json.load(open('$EVAL_JSON'))
snap_topics = {p['topic'] for p in eval_json['current_allowlist_snapshot_measured']['static_pairs']}
assert 'task_mcp' not in snap_topics, 'recorded snapshot must not list task_mcp as already allowlisted'
print('confirmed: task_mcp topic absent from live allowlist AND recorded snapshot (proposal genuinely unapplied)')
"
echo ""

echo "--- [6/7] no MCP source .py file touched by this task ---"
cd "$ROOT"
CHANGED_SRC="$(git status --porcelain -- tools/geoai-task-mcp/src 2>/dev/null || true)"
if [ -n "$CHANGED_SRC" ]; then
    echo "FAIL: unexpected changes under tools/geoai-task-mcp/src:"
    echo "$CHANGED_SRC"
    exit 1
fi
echo "tools/geoai-task-mcp/src: clean (no source_code_patch performed) OK"
echo ""

echo "--- [7/7] parent queue intact ---"
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact; this script never calls review/done)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
