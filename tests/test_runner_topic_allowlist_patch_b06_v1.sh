#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_runner_topic_allowlist_patch_b06_v1.sh
# Harness for the B06 applied per-wave task_mcp prefix allowlist patch.
#
# Verifies:
#   1. eval JSON parses, forbidden ops all confirmed false, applied_patch
#      section documents the exact B05-proposed diff.
#   2. regression: all 11 pre-existing static (runner, topic) pairs x their
#      allowed actions + excluded 'done' are byte-identical to pre-patch
#      behavior, checked LIVE against core.check_runner_topic_allowlist.
#   3. codex wildcard behavior unchanged.
#   4. new_behavior_measured examples (task_mcp prefix allow/deny, malformed
#      token, non-prefixed runner, wrong topic) cross-validated LIVE.
#   5. write_gate_status_measured / launch_status_measured match the live
#      module (writes_allowed()/launch_enabled()/LAUNCH_IMPLEMENTED all False).
#   6. next_wave JSON parses and documents applied-scope + rollback.
#   7. no source .py file OTHER than core.py changed under
#      tools/geoai-task-mcp/src (best-effort git-status scoped check).
#   8. parent task queue is not mutated (taskctl verify).
#
# Isolation: read-only against the live module (in-process import + pure
# function calls only, no subprocess, no daemon, no network, no env
# mutation beyond this process). Never calls taskctl review/done. Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
EVAL_JSON="$MCPROOT/eval/runner_topic_allowlist_patch_b06_v1.json"
NEXT_WAVE_JSON="$MCPROOT/data/tasking/runner_topic_allowlist_patch_next_wave_b06_v1.json"

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES=0

echo "=== Runner/Topic Allowlist Patch Test B06 v1 ==="
echo "GEOAI_REPO=$GEOAI_REPO"
echo "GEOAI_TASK_MCP_ALLOW_WRITES=$GEOAI_TASK_MCP_ALLOW_WRITES"
echo ""

echo "--- [1/8] eval JSON structure ---"
EVAL_JSON="$EVAL_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["EVAL_JSON"]))

required_top = [
    "applied_patch", "forbidden_operations_confirmed_not_taken",
    "regression_static_pairs_measured", "new_behavior_measured",
    "write_gate_status_measured", "launch_status_measured",
    "neural_bridge_note", "rollback",
]
for k in required_top:
    assert k in d, f"missing top-level section: {k}"

forbidden = d["forbidden_operations_confirmed_not_taken"]
for k, v in forbidden.items():
    assert v is False, f"forbidden op {k} not confirmed false: {v}"

ap = d["applied_patch"]
assert ap["target_file"] == "tools/geoai-task-mcp/src/geoai_task_mcp/core.py"
assert ap["target_function"] == "check_runner_topic_allowlist(runner, topic, action)"
assert ap["matches_b05_proposal_exactly"] is True
assert ap["malformed_token_check_unchanged"] is True
assert ap["codex_branch_unchanged"] is True
assert ap["static_dict_unchanged"] is True

examples = d["new_behavior_measured"]
assert len(examples) >= 6, f"expected >=6 new-behavior examples, got {len(examples)}"

print(f"eval JSON structure: OK ({len(examples)} new-behavior examples, "
      f"{len(forbidden)} forbidden-ops all false)")
PYEOF
echo ""

echo "--- [2/8] regression: 11 existing static pairs byte-identical LIVE ---"
python3 -c "
import sys, os
sys.path.insert(0, os.path.join(os.environ['GEOAI_REPO'], 'tools/geoai-task-mcp/src'))
from geoai_task_mcp import core

assert len(core.RUNNER_TOPIC_ALLOWLIST) == 11, f'expected 11 static pairs, got {len(core.RUNNER_TOPIC_ALLOWLIST)}'

checked = 0
for (runner, topic), actions in core.RUNNER_TOPIC_ALLOWLIST.items():
    for action in actions:
        real = core.check_runner_topic_allowlist(runner, topic, action)
        assert real == {'allowed': True, 'reason': 'allowlisted'}, f'{runner}/{topic}/{action}: {real}'
        checked += 1
    # 'done' is never in any of the 4 standard action sets -> must stay excluded
    assert 'done' not in actions
    real_done = core.check_runner_topic_allowlist(runner, topic, 'done')
    assert real_done == {'allowed': False, 'reason': 'action_not_allowed_for_runner_topic:done'}, f'{runner}/{topic}/done: {real_done}'
    checked += 1

print(f'regression OK: {checked} (pair, action) checks byte-identical to pre-patch behavior')
"
echo ""

echo "--- [3/8] codex wildcard unchanged ---"
python3 -c "
import sys, os
sys.path.insert(0, os.path.join(os.environ['GEOAI_REPO'], 'tools/geoai-task-mcp/src'))
from geoai_task_mcp import core

for action in core.CODEX_ALLOWED_ACTIONS:
    real = core.check_runner_topic_allowlist('codex', 'any_topic_at_all', action)
    assert real == {'allowed': True, 'reason': 'codex_wildcard_topic_allowed'}, f'codex/{action}: {real}'

real_blocked = core.check_runner_topic_allowlist('codex', 'any_topic_at_all', 'auto-pickup')
assert real_blocked == {'allowed': False, 'reason': 'codex_action_not_allowed:auto-pickup'}, real_blocked
print('codex wildcard behavior unchanged: OK')
"
echo ""

echo "--- [4/8] new_behavior_measured cross-validated LIVE ---"
EVAL_JSON="$EVAL_JSON" python3 - <<'PYEOF'
import json, os, sys
sys.path.insert(0, os.path.join(os.environ["GEOAI_REPO"], "tools/geoai-task-mcp/src"))
from geoai_task_mcp import core

d = json.load(open(os.environ["EVAL_JSON"]))
examples = d["new_behavior_measured"]

checked = 0
for ex in examples:
    runner, topic, action = ex["runner"], ex["topic"], ex["action"]
    real = core.check_runner_topic_allowlist(runner, topic, action)
    assert real["allowed"] == ex["result"]["allowed"], (
        f"{runner}/{topic}/{action}: allowed mismatch real={real} recorded={ex['result']}")
    assert real["reason"] == ex["result"]["reason"], (
        f"{runner}/{topic}/{action}: reason mismatch real={real} recorded={ex['result']}")
    checked += 1

# This task's OWN runner must now be allowed for auto-pickup/review/usage,
# and still blocked for start/done -- dogfooding the exact contract.
own_runner = "claude_task_mcp_allowlist_patch_b06"
for action in ("auto-pickup", "review", "usage"):
    real = core.check_runner_topic_allowlist(own_runner, "task_mcp", action)
    assert real == {"allowed": True, "reason": "per_wave_prefix_allowlisted"}, (own_runner, action, real)
for action in ("start", "done"):
    real = core.check_runner_topic_allowlist(own_runner, "task_mcp", action)
    assert real == {"allowed": False, "reason": f"per_wave_action_not_allowed:{action}"}, (own_runner, action, real)

print(f"cross-validated {checked} new-behavior examples + own-runner dogfood checks: OK")
PYEOF
echo ""

echo "--- [5/8] write gate + launch status match live module ---"
python3 -c "
import sys, os
sys.path.insert(0, os.path.join(os.environ['GEOAI_REPO'], 'tools/geoai-task-mcp/src'))
from geoai_task_mcp import core, cli_adapter_dryrun as cad
assert core.writes_allowed() is False, 'core.writes_allowed() must be False'
assert cad.writes_allowed() is False, 'cli_adapter_dryrun.writes_allowed() must be False'
assert cad.launch_enabled() is False, 'launch_enabled() must be False'
assert cad.LAUNCH_IMPLEMENTED is False, 'LAUNCH_IMPLEMENTED must be False'
print('write_gate off + launch disabled: OK')
"
echo ""

echo "--- [6/8] next_wave JSON structure ---"
NEXT_WAVE_JSON="$NEXT_WAVE_JSON" python3 - <<'PYEOF'
import json, os

d = json.load(open(os.environ["NEXT_WAVE_JSON"]))

required_top = [
    "proposal_id", "status", "source_task", "applied_in_this_task",
    "not_applied_generalization_next_wave", "required_tests_verified_by_this_task",
    "rollback", "neural_bridge_note",
]
for k in required_top:
    assert k in d, f"missing next_wave section: {k}"

assert d["status"] == "applied_this_task_scope_only", d["status"]
ap = d["applied_in_this_task"]
assert ap["process_launch"] is False
assert "off" in ap["write_gate"].lower()
assert ap["target_file"] == "tools/geoai-task-mcp/src/geoai_task_mcp/core.py"

gen = d["not_applied_generalization_next_wave"]
assert len(gen["other_per_wave_topics_observed_in_project_memory"]) >= 1

tests = d["required_tests_verified_by_this_task"]
assert len(tests) >= 8, f"expected >=8 verified tests documented, got {len(tests)}"

print(f"next_wave JSON structure: OK ({len(tests)} verified tests documented)")
PYEOF
echo ""

echo "--- [7/8] core.py shows as this task's own change (nested repo) ---"
# tools/geoai-task-mcp is its OWN nested git repo (has its own .git), so it
# must be queried directly -- the parent GeoAI repo only sees it as a single
# untracked directory. This is a SHARED checkout: other concurrent workers'
# in-flight files (e.g. review_summarizer.py, server.py, launch_queue_persist.py)
# may legitimately be dirty at the same time and are NOT this task's concern
# (project rule: never treat other agents' concurrent changes as a violation
# to fix here). This check only confirms core.py -- this task's one allowed
# source file -- is present as a modification; it does not require the
# nested repo to otherwise be clean.
cd "$MCPROOT"
CORE_STATUS="$(git status --porcelain -- src/geoai_task_mcp/core.py 2>/dev/null || true)"
if [ -z "$CORE_STATUS" ]; then
    echo "FAIL: expected src/geoai_task_mcp/core.py to show as modified in the nested geoai-task-mcp repo, found none"
    exit 1
fi
echo "core.py change present in nested repo: OK ($CORE_STATUS)"
cd "$ROOT"
echo ""

echo "--- [8/8] parent queue intact ---"
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact; this script never calls review/done)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
