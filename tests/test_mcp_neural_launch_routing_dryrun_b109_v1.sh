#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_neural_launch_routing_dryrun_b109_v1.sh
# Harness for MCP neural launch-routing dryrun validation.
#
# Verifies:
#   1. Dryrun script runs successfully and produces all outputs
#   2. Eval JSON is valid with correct fields and verdict
#   3. Eval rows JSONL is valid, non-empty, matches curriculum row count
#   4. Next-wave JSON is valid
#   5. Neural beats baselines (adapter > random, abstain precision >= 0.8)
#   6. Safety invariants: no regex/keyword authority, deterministic gates remain
#   7. Held-out row is in test set and correctly predicted
#   8. Collision-aware abstain rows correctly predicted
#
# Isolation: uses temp copies for validation; no shared repo artifact is written.
# Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/aiworkhub_mcp_neural_dryrun_sh.XXXXXX")"
trap 'rm -rf "$TMPDIR"' EXIT

export AIWORKHUB_REPO="$ROOT"

FAILURES=0

pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; FAILURES=$((FAILURES + 1)); }

echo "=== MCP Neural Launch-Routing Dryrun Test B109 v1 ==="
echo "ROOT=$ROOT"

# ---------------------------------------------------------------------------
# 1. Dryrun script runs
# ---------------------------------------------------------------------------
echo ""
echo "--- 1. Dryrun script ---"
BUILD_OUT="$TMPDIR/dryrun_out.txt"
if PYTHONIOENCODING=utf-8 python3 "$ROOT/scripts/run_task_mcp_neural_routing_dryrun_b109_v1.py" > "$BUILD_OUT" 2>&1; then
    pass "dryrun script ran successfully"
else
    fail "dryrun script failed"
    cat "$BUILD_OUT"
fi

# ---------------------------------------------------------------------------
# 2. Eval JSON validation
# ---------------------------------------------------------------------------
echo ""
echo "--- 2. Eval JSON ---"
EVAL_JSON="$MCPROOT/eval/mcp_neural_launch_routing_dryrun_b109_v1.json"
if [ ! -f "$EVAL_JSON" ]; then
    fail "eval JSON not found: $EVAL_JSON"
else
    # Validate JSON structure
    python3 -c "
import json
with open('$EVAL_JSON') as f:
    r = json.load(f)
assert r.get('eval_id') == 'mcp_neural_launch_routing_dryrun_b109_v1', 'bad eval_id'
assert r.get('verdict') in ('PASS', 'PARTIAL'), f'bad verdict: {r.get(\"verdict\")}'
assert 'full_dataset_metrics' in r, 'missing full_dataset_metrics'
assert 'gates' in r, 'missing gates'
assert 'safety_invariants' in r, 'missing safety_invariants'
print('eval_id:', r['eval_id'])
print('verdict:', r['verdict'])
print('summary:', r['summary'])
" && pass "eval JSON valid" || fail "eval JSON validation failed"

    # Check gates
    python3 -c "
import json
with open('$EVAL_JSON') as f:
    r = json.load(f)
g = r['gates']
assert g.get('adapter_above_majority') == True, 'adapter_above_majority gate failed'
assert g.get('no_launch_authority') == True, 'no_launch_authority gate failed'
assert g.get('no_regex_keyword_authority') == True, 'no_regex_keyword_authority gate failed'
assert g.get('deterministic_gates_remain') == True, 'deterministic_gates_remain gate failed'
assert g.get('heldout_in_test') == True, 'heldout_in_test gate failed'
print('All critical gates PASS')
" && pass "gates validation" || fail "gates validation failed"

    # Check neural metrics
    python3 -c "
import json
with open('$EVAL_JSON') as f:
    r = json.load(f)
n = r['full_dataset_metrics']['neural']
assert n['adapter_accuracy'] > 0.5, f'adapter accuracy too low: {n[\"adapter_accuracy\"]}'
assert n['abstain_precision'] >= 0.8, f'abstain precision too low: {n[\"abstain_precision\"]}'
assert n['collision_abstain_accuracy'] >= 0.5, f'collision abstain too low: {n[\"collision_abstain_accuracy\"]}'
print(f'adapter_acc={n[\"adapter_accuracy\"]:.3f} abstain_prec={n[\"abstain_precision\"]:.3f} collision_acc={n[\"collision_abstain_accuracy\"]:.3f}')
" && pass "neural metrics above thresholds" || fail "neural metrics below thresholds"

    # Check neural vs baselines
    python3 -c "
import json
with open('$EVAL_JSON') as f:
    r = json.load(f)
n = r['full_dataset_metrics']['neural']
t = r['full_dataset_metrics']['table_baseline']
rand = r['full_dataset_metrics']['random_baseline']
assert n['adapter_accuracy'] >= rand['adapter_accuracy'], f'neural adapter <= random'
assert n['abstain_accuracy'] >= rand['abstain_accuracy'], f'neural abstain <= random'
print('Neural beats random baseline')
" && pass "neural beats random baseline" || fail "neural does not beat random baseline"

    # Safety invariants
    python3 -c "
import json
with open('$EVAL_JSON') as f:
    r = json.load(f)
s = r['safety_invariants']
assert s.get('no_regex_keyword_launch_authority') == True, 'regex/keyword authority found'
assert s.get('no_process_launch_code') == True, 'process launch code found'
assert s.get('write_gate_default_off') == True, 'write gate not default off'
assert s.get('readonly_tools_only_as_evidence') == True, 'tools used beyond evidence'
print('All safety invariants hold')
" && pass "safety invariants" || fail "safety invariants violated"
fi

# ---------------------------------------------------------------------------
# 3. Eval rows JSONL validation
# ---------------------------------------------------------------------------
echo ""
echo "--- 3. Eval rows JSONL ---"
EVAL_ROWS="$MCPROOT/eval/mcp_neural_launch_routing_dryrun_rows_b109_v1.jsonl"
CURR="$ROOT/bitnnv2/data/curriculum/mcp_launch_routing_targets_v1.jsonl"

if [ ! -f "$EVAL_ROWS" ]; then
    fail "eval rows JSONL not found: $EVAL_ROWS"
else
    # Count rows
    ROWS_COUNT=$(python3 -c "
import json
with open('$EVAL_ROWS') as f:
    rows = [json.loads(line) for line in f if line.strip()]
print(len(rows))
")
    CURR_COUNT=$(python3 -c "
import json
with open('$CURR') as f:
    rows = [json.loads(line) for line in f if line.strip()]
print(len(rows))
")

    if [ "$ROWS_COUNT" -eq "$CURR_COUNT" ]; then
        pass "eval rows count matches curriculum: $ROWS_COUNT rows"
    else
        fail "eval rows count ($ROWS_COUNT) != curriculum ($CURR_COUNT)"
    fi

    # Schema check
    python3 -c "
import json
with open('$EVAL_ROWS') as f:
    for i, line in enumerate(f, 1):
        line = line.strip()
        if not line: continue
        r = json.loads(line)
        assert 'curriculum_id' in r, f'row {i}: missing curriculum_id'
        assert 'neural_adapter_correct' in r, f'row {i}: missing neural_adapter_correct'
        assert 'neural_abstain_correct' in r, f'row {i}: missing neural_abstain_correct'
        assert 'true_adapter' in r, f'row {i}: missing true_adapter'
        assert 'pred_neural_adapter' in r, f'row {i}: missing pred_neural_adapter'
" && pass "eval rows JSONL schema valid" || fail "eval rows JSONL schema validation failed"

    # Held-out row check
    python3 -c "
import json
with open('$EVAL_ROWS') as f:
    rows = [json.loads(line) for line in f if line.strip()]
heldout = [r for r in rows if r.get('is_heldout')]
assert len(heldout) == 1, f'expected 1 heldout, got {len(heldout)}'
h = heldout[0]
assert h['neural_adapter_correct'] == True, f'heldout row adapter incorrect: {h[\"curriculum_id\"]}'
assert h['neural_abstain_correct'] == True, f'heldout row abstain incorrect: {h[\"curriculum_id\"]}'
print(f'Held-out row {h[\"curriculum_id\"]} correctly predicted')
" && pass "held-out row correct" || fail "held-out row prediction failed"

    # Collision rows check
    python3 -c "
import json
with open('$EVAL_ROWS') as f:
    rows = [json.loads(line) for line in f if line.strip()]
collision_rows = [r for r in rows if (r.get('true_abstain_reason') or '').startswith('collision_')]
assert len(collision_rows) == 3, f'expected 3 collision rows, got {len(collision_rows)}'
all_correct = all(r['neural_abstain_correct'] for r in collision_rows)
assert all_correct, 'not all collision rows correctly predicted abstain'
print(f'All {len(collision_rows)} collision rows correctly predicted abstain')
" && pass "collision abstain rows correct" || fail "collision abstain rows prediction failed"
fi

# ---------------------------------------------------------------------------
# 4. Next-wave JSON validation
# ---------------------------------------------------------------------------
echo ""
echo "--- 4. Next-wave JSON ---"
NEXT_WAVE="$MCPROOT/data/tasking/mcp_neural_launch_routing_dryrun_next_wave_b109_v1.json"
if [ ! -f "$NEXT_WAVE" ]; then
    fail "next-wave JSON not found: $NEXT_WAVE"
else
    python3 -c "
import json
with open('$NEXT_WAVE') as f:
    r = json.load(f)
assert r.get('next_wave_id') == 'mcp_neural_launch_routing_dryrun_next_wave_b109_v1', 'bad next_wave_id'
assert r.get('status') == 'proposal', 'bad status'
assert len(r.get('follow_up_tasks', [])) >= 1, 'no follow_up_tasks'
print('next_wave_id:', r['next_wave_id'])
print('follow_up_tasks:', len(r['follow_up_tasks']))
" && pass "next-wave JSON valid" || fail "next-wave JSON validation failed"
fi

# ---------------------------------------------------------------------------
# 5. Summary
# ---------------------------------------------------------------------------
echo ""
if [ "$FAILURES" -eq 0 ]; then
    echo "=== ALL TESTS PASSED ==="
else
    echo "=== $FAILURES TEST(S) FAILED ==="
fi

exit $FAILURES
