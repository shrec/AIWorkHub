#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_neural_launch_routing_migration_b254_v1.sh
# Harness for the B254 MCP neural launch-routing migration curriculum
# (migrates B108's single-snapshot curriculum to a broader historical
# auto-pickup/review-queue outcome sample).
#
# Verifies:
#   1. Build script runs successfully and produces all outputs
#   2. Curriculum JSONL is valid JSON, non-empty, correct v2 schema,
#      collision-aware, has heldout rows
#   3. New-to-B254 row categories are present: real_active_claim_collision
#      abstain rows AND synthetic_wrong_runner_topic abstain rows
#   4. Every row's safety section states the static allowlist remains the
#      safety floor and is not replaced (static_allowlist_is_safety_floor_
#      not_replaced == True)
#   5. Eval JSON is valid JSON with correct fields, verdict PASS
#   6. Eval rows JSONL matches curriculum JSONL row count
#   7. Next-wave JSON is valid JSON, status == "proposal" (not enqueued;
#      this worker creates no task cards)
#   8. Build script contains no process-launch code (subprocess/os.system/
#      os.exec/os.fork/os.spawn/Popen/shell=True) -- pure data curation only
#   9. Pure-function determinism: adapter_of() is a stable function of
#      runner-name substring on a FIXED fixture (independent of the live,
#      concurrently-mutated task-card store, so this check cannot flake
#      when other topic-bound workers change queue state mid-test)
#
# Isolation: reads/writes only the task's own allowed_writes paths (the same
# curriculum/eval/next-wave artifacts the build script produces); the input
# task-card store (bitnnv2/data/tasking/machine_task_cards_v1.jsonl) is read
# read-only and may be concurrently mutated by other workers -- row COUNTS
# are therefore NOT asserted to an exact value anywhere in this test, only
# lower bounds and structural/schema invariants.
# Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/geoai_mcp_neural_routing_migration_b254_sh.XXXXXX")"
trap 'rm -rf "$TMPDIR"' EXIT

export GEOAI_REPO="$ROOT"

FAILURES=0

pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; FAILURES=$((FAILURES + 1)); }

echo "=== MCP Neural Launch-Routing Migration Curriculum Test B254 v1 ==="
echo "ROOT=$ROOT"

BUILD_SCRIPT="$ROOT/scripts/build_mcp_neural_launch_routing_migration_b254_v1.py"

# ---------------------------------------------------------------------------
# 1. Build script runs
# ---------------------------------------------------------------------------
echo ""
echo "--- 1. Build script ---"
BUILD_OUT="$TMPDIR/build_out.txt"
if PYTHONIOENCODING=utf-8 python3 "$BUILD_SCRIPT" > "$BUILD_OUT" 2>&1; then
    pass "build script ran successfully"
    cat "$BUILD_OUT"
else
    fail "build script failed"
    cat "$BUILD_OUT"
fi

# ---------------------------------------------------------------------------
# 2. Curriculum JSONL validation
# ---------------------------------------------------------------------------
echo ""
echo "--- 2. Curriculum JSONL ---"
CURR="$ROOT/bitnnv2/data/curriculum/mcp_neural_launch_routing_migration_b254_v1.jsonl"
if [ ! -f "$CURR" ]; then
    fail "curriculum JSONL not found: $CURR"
else
    CURR_COUNT=$(python3 -c "
import json
with open('$CURR') as f:
    rows = [json.loads(line) for line in f if line.strip()]
print(len(rows))
")
    if [ "$CURR_COUNT" -ge 50 ]; then
        pass "curriculum JSONL: $CURR_COUNT rows (>= 50)"
    else
        fail "curriculum JSONL too small: $CURR_COUNT rows"
    fi

    python3 -c "
import json, sys
with open('$CURR') as f:
    for i, line in enumerate(f, 1):
        line = line.strip()
        if not line: continue
        r = json.loads(line)
        assert r.get('schema') == 'geoai.mcp_launch_routing_target.v2', f'row {i}: bad schema {r.get(\"schema\")}'
        assert 'curriculum_id' in r, f'row {i}: missing curriculum_id'
        assert 'task_context' in r, f'row {i}: missing task_context'
        assert 'labels' in r, f'row {i}: missing labels'
        assert 'feature_axes' in r, f'row {i}: missing feature_axes'
        fx = r['feature_axes']
        for key in ('runner_prefix', 'adapter_hint', 'historical_status', 'historical_review_outcome', 'is_real_observed'):
            assert key in fx, f'row {i}: feature_axes missing {key}'
        lbl = r['labels']
        for key in ('adapter', 'runner', 'topic', 'abstain', 'abstain_reason'):
            assert key in lbl, f'row {i}: labels missing {key}'
        assert 'wrong_pairing_aware' in r, f'row {i}: missing wrong_pairing_aware'
        assert 'safety' in r, f'row {i}: missing safety'
        s = r['safety']
        assert s.get('deterministic_gates_remain') == True, f'row {i}: deterministic_gates_remain not True'
        assert s.get('no_regex_keyword_authority') == True, f'row {i}: no_regex_keyword_authority not True'
        assert s.get('neural_is_routing_only_not_safety_authority') == True, f'row {i}: safety authority flag'
        assert s.get('static_allowlist_is_safety_floor_not_replaced') == True, f'row {i}: static allowlist floor statement missing/False'
sys.stderr.write('schema OK\\n')
" 2>&1
    if [ $? -eq 0 ]; then
        pass "curriculum JSONL v2 schema valid (feature_axes + safety floor statement present)"
    else
        fail "curriculum JSONL schema invalid"
    fi

    python3 -c "
import json, sys
with open('$CURR') as f:
    rows = [json.loads(line) for line in f if line.strip()]
assert all(r.get('collision_aware') for r in rows), 'not all rows collision_aware'
for r in rows:
    reason = r['labels'].get('abstain_reason') or ''
    if reason.startswith('collision_'):
        assert r['collision_context']['would_collide'] == True, f'{r[\"curriculum_id\"]}: collision abstain without would_collide'
    if r['labels']['abstain']:
        assert reason, f'{r[\"curriculum_id\"]}: abstain row missing abstain_reason'
for r in rows:
    if r['is_positive']:
        assert r['labels']['abstain'] == False, f'{r[\"curriculum_id\"]}: positive row abstains'
heldout = [r for r in rows if r.get('is_heldout')]
assert len(heldout) > 0, 'no heldout rows'
sys.stderr.write('invariants OK\\n')
" 2>&1
    if [ $? -eq 0 ]; then
        pass "curriculum invariants verified (collision-aware, positive!=abstain, heldout exists)"
    else
        fail "curriculum invariants violated"
    fi
fi

# ---------------------------------------------------------------------------
# 3. New B254 row categories present
# ---------------------------------------------------------------------------
echo ""
echo "--- 3. B254-new row categories ---"
if [ -f "$CURR" ]; then
    python3 -c "
import json, sys
with open('$CURR') as f:
    rows = [json.loads(line) for line in f if line.strip()]
collision = [r for r in rows if r['source'] == 'real_active_claim_collision']
wrong_pairing = [r for r in rows if r['source'] == 'synthetic_wrong_runner_topic']
carried = [r for r in rows if r['source'] == 'carried_forward_b108']
assert len(collision) > 0, 'no real_active_claim_collision rows'
assert all(r['labels']['abstain'] for r in collision), 'collision rows must abstain'
assert len(wrong_pairing) > 0, 'no synthetic_wrong_runner_topic rows'
assert all(r['labels']['abstain'] and r['labels']['abstain_reason'] == 'wrong_runner_topic_mismatch' for r in wrong_pairing), 'wrong-pairing rows must abstain with correct reason'
assert all(r['wrong_pairing_aware'] for r in wrong_pairing), 'wrong-pairing rows must set wrong_pairing_aware'
assert len(carried) == 29, f'expected 29 carried-forward B108 rows, got {len(carried)}'
sys.stderr.write(f'collision={len(collision)} wrong_pairing={len(wrong_pairing)} carried={len(carried)}\\n')
" 2>&1
    if [ $? -eq 0 ]; then
        pass "collision-aware abstain AND wrong-runner/topic rows both present; B108 carried forward intact"
    else
        fail "B254-new row categories missing or malformed"
    fi
else
    fail "curriculum JSONL not found for category check"
fi

# ---------------------------------------------------------------------------
# 4. Eval JSON validation
# ---------------------------------------------------------------------------
echo ""
echo "--- 4. Eval JSON ---"
EVAL="$MCPROOT/eval/mcp_neural_launch_routing_migration_b254_v1.json"
if [ ! -f "$EVAL" ]; then
    fail "eval JSON not found: $EVAL"
else
    python3 -c "
import json, sys
with open('$EVAL') as f:
    d = json.load(f)
assert d.get('eval_id') == 'mcp_neural_launch_routing_migration_b254_v1'
assert d.get('verdict') == 'PASS'
assert 'counts' in d
assert d['counts']['total_rows'] >= 50
assert d['counts']['heldout'] > 0
assert d['counts']['real_active_claim_collision_abstain'] > 0
assert d['counts']['synthetic_wrong_runner_topic_abstain'] > 0
assert d['counts']['carried_forward_b108'] == 29
assert 'safety_invariants' in d
assert d['safety_invariants']['no_regex_keyword_launch_authority'] == True
assert d['safety_invariants']['no_process_launch_code'] == True
assert d['safety_invariants']['static_allowlist_is_safety_floor_not_replaced'] == True
assert 'gates' in d
assert d['gates']['all_rows_have_safety_section'] == True
assert d['gates']['positive_rows_not_abstain'] == True
assert d['gates']['heldout_set_exists'] == True
assert d['gates']['collision_aware_abstain_rows_present'] == True
assert d['gates']['wrong_runner_topic_rows_present'] == True
assert d['authority_flags']['process_launch_authority'] == False
assert d['authority_flags']['regex_launch_authority'] == False
assert d['authority_flags']['keyword_router_authority'] == False
assert d.get('commit_contract') == 'NO_COMMIT'
sys.stderr.write('eval OK\\n')
" 2>&1
    if [ $? -eq 0 ]; then
        pass "eval JSON valid (verdict PASS, safety floor + no-authority flags asserted)"
    else
        fail "eval JSON invalid"
    fi
fi

# ---------------------------------------------------------------------------
# 5. Eval rows JSONL matches curriculum count
# ---------------------------------------------------------------------------
echo ""
echo "--- 5. Eval rows JSONL ---"
EVAL_ROWS="$MCPROOT/eval/mcp_neural_launch_routing_migration_rows_b254_v1.jsonl"
if [ ! -f "$EVAL_ROWS" ]; then
    fail "eval rows JSONL not found: $EVAL_ROWS"
else
    EVAL_ROWS_COUNT=$(python3 -c "
import json
with open('$EVAL_ROWS') as f:
    print(sum(1 for line in f if line.strip()))
")
    if [ "$EVAL_ROWS_COUNT" = "$CURR_COUNT" ]; then
        pass "eval rows matches curriculum: $EVAL_ROWS_COUNT rows"
    else
        fail "eval rows count mismatch: eval=$EVAL_ROWS_COUNT curriculum=$CURR_COUNT"
    fi
fi

# ---------------------------------------------------------------------------
# 6. Next-wave JSON validation
# ---------------------------------------------------------------------------
echo ""
echo "--- 6. Next-wave JSON ---"
NW="$MCPROOT/data/tasking/mcp_neural_launch_routing_migration_next_wave_b254_v1.json"
if [ ! -f "$NW" ]; then
    fail "next-wave JSON not found: $NW"
else
    python3 -c "
import json, sys
with open('$NW') as f:
    d = json.load(f)
assert d.get('next_wave_id') == 'mcp_neural_launch_routing_migration_next_wave_b254_v1'
assert d.get('parent_task') == 'CLAUDE_TASK_MCP_NEURAL_LAUNCH_ROUTING_MIGRATION_B254_V1'
assert d.get('status') == 'proposal', 'next-wave must stay a proposal, not enqueued'
assert 'follow_up_tasks' in d
assert len(d['follow_up_tasks']) >= 1
sys.stderr.write('next_wave OK\\n')
" 2>&1
    if [ $? -eq 0 ]; then
        pass "next-wave JSON valid (status=proposal, not enqueued)"
    else
        fail "next-wave JSON invalid"
    fi
fi

# ---------------------------------------------------------------------------
# 7. No process-launch code in build script
# ---------------------------------------------------------------------------
echo ""
echo "--- 7. No process-launch code ---"
if python3 -c "
import ast
with open('$BUILD_SCRIPT') as f:
    ast.parse(f.read())
" 2>/dev/null; then
    pass "build script parses cleanly"
else
    fail "build script parse failed"
fi
if grep -q 'subprocess\|os\.system\|os\.popen\|os\.exec\|os\.fork\|os\.spawn\|Popen(\|shell=True' "$BUILD_SCRIPT" 2>/dev/null; then
    fail "build script contains process-launch code"
else
    pass "build script has no process-launch code (pure JSON/JSONL I/O only)"
fi

# ---------------------------------------------------------------------------
# 8. Pure-function determinism (fixed fixture, independent of live queue)
# ---------------------------------------------------------------------------
echo ""
echo "--- 8. adapter_of() determinism (fixed fixture) ---"
if PYTHONIOENCODING=utf-8 python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('b254_build', '$BUILD_SCRIPT')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

cases = [
    ('claude_task_mcp_neural_launch_routing_migration_b254', 'claude_cli'),
    ('deepseek_lxa_e2e_shadow_fixture_b137', 'deepseek_manual'),
    ('codex_review_worker', 'codex_cli'),
    ('totally_unknown_runner_xyz', 'unknown'),
]
for runner, expected in cases:
    got = mod.adapter_of(runner)
    assert got == expected, f'adapter_of({runner!r})={got!r} expected {expected!r}'

# call twice -> must be identical (pure function, no hidden state/IO)
for runner, expected in cases:
    assert mod.adapter_of(runner) == mod.adapter_of(runner)

import sys
sys.stderr.write('adapter_of determinism OK\\n')
" 2>&1; then
    pass "adapter_of() is a deterministic pure function on a fixed fixture"
else
    fail "adapter_of() determinism check failed"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Test Summary ==="
if [ "$FAILURES" -eq 0 ]; then
    echo "RESULT: PASS (0 failures)"
    exit 0
else
    echo "RESULT: FAIL ($FAILURES failures)"
    exit 1
fi
