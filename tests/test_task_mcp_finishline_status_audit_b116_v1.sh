#!/usr/bin/env bash
# B116: MVP_ROADMAP finish-line status audit test
# Runs the audit script and validates all acceptance criteria.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AUDIT_SCRIPT="$MCP_ROOT/scripts/audit_task_mcp_finishline_status_b116_v1.py"
EVAL_JSON="$MCP_ROOT/eval/task_mcp_finishline_status_audit_b116_v1.json"
EVAL_JSONL="$MCP_ROOT/eval/task_mcp_finishline_status_audit_rows_b116_v1.jsonl"
NEXT_WAVE="$MCP_ROOT/data/tasking/task_mcp_finishline_status_audit_next_wave_b116_v1.json"

PASS=0
FAIL=0

pass_test() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail_test() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

echo "=== B116 Finish-Line Status Audit Test ==="
echo ""

# ── 1. Run audit script ──
echo "--- Running audit script ---"
cd /home/shrek/AIWorkHub
python3 "$AUDIT_SCRIPT" > /tmp/b116_audit_stdout.txt 2>&1
AUDIT_RC=$?
if [ $AUDIT_RC -eq 0 ]; then
  pass_test "audit_script_exit_zero"
else
  fail_test "audit_script_exit_zero (got $AUDIT_RC)"
fi

# ── 2. Outputs exist ──
echo "--- Checking outputs ---"
if [ -f "$EVAL_JSON" ]; then
  pass_test "eval_json_exists"
else
  fail_test "eval_json_exists"
fi

if [ -f "$EVAL_JSONL" ]; then
  pass_test "eval_jsonl_exists"
else
  fail_test "eval_jsonl_exists"
fi

if [ -f "$NEXT_WAVE" ]; then
  pass_test "next_wave_json_exists"
else
  fail_test "next_wave_json_exists"
fi

# ── 3. Eval JSON is valid JSON ──
if python3 -c "import json; json.load(open('$EVAL_JSON'))" 2>/dev/null; then
  pass_test "eval_json_valid"
else
  fail_test "eval_json_valid"
fi

# ── 4. JSONL has 23 rows (one per checkbox) ──
JSONL_COUNT=$(wc -l < "$EVAL_JSONL")
if [ "$JSONL_COUNT" -eq 23 ]; then
  pass_test "jsonl_23_rows"
else
  fail_test "jsonl_23_rows (got $JSONL_COUNT)"
fi

# ── 5. Every JSONL row is valid JSON ──
INVALID_JSONL=0
while IFS= read -r line; do
  if ! echo "$line" | python3 -c "import json,sys; json.loads(sys.stdin.readline())" 2>/dev/null; then
    INVALID_JSONL=$((INVALID_JSONL+1))
  fi
done < "$EVAL_JSONL"
if [ "$INVALID_JSONL" -eq 0 ]; then
  pass_test "jsonl_all_rows_valid"
else
  fail_test "jsonl_all_rows_valid ($INVALID_JSONL invalid rows)"
fi

# ── 6. Acceptance: every MVP_ROADMAP checkbox mapped ──
EVAL_PHASES=$(python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
bp = d['by_phase']
total = sum(bp[p]['total'] for p in bp)
print(total)
")
if [ "$EVAL_PHASES" -eq 23 ]; then
  pass_test "all_23_checkboxes_mapped"
else
  fail_test "all_23_checkboxes_mapped (got $EVAL_PHASES)"
fi

# ── 7. Acceptance: shortest safe sequence provided ──
SEQ_LEN=$(python3 -c "import json; print(len(json.load(open('$EVAL_JSON'))['next_wave_sequence']))")
if [ "$SEQ_LEN" -gt 0 ]; then
  pass_test "shortest_safe_sequence_provided ($SEQ_LEN steps)"
else
  fail_test "shortest_safe_sequence_provided"
fi

# ── 8. Acceptance: dirty/untracked reported ──
DIRTY_COUNT=$(python3 -c "import json; print(json.load(open('$EVAL_JSON'))['git_state']['total_changed'])")
UNT_COUNT=$(python3 -c "import json; print(json.load(open('$EVAL_JSON'))['git_state']['untracked_count'])")
echo "  INFO: dirty=$DIRTY_COUNT untracked=$UNT_COUNT"
pass_test "dirty_untracked_reported"

# ── 9. Acceptance: workflow_switch=false ──
WF=$(python3 -c "import json; print(json.load(open('$EVAL_JSON'))['authority_flags']['workflow_switch'])")
if [ "$WF" = "False" ]; then
  pass_test "workflow_switch_false"
else
  fail_test "workflow_switch_false (got $WF)"
fi

# ── 10. Acceptance: launch_enabled=false ──
LE=$(python3 -c "import json; print(json.load(open('$EVAL_JSON'))['authority_flags']['launch_enabled'])")
if [ "$LE" = "False" ]; then
  pass_test "launch_enabled_false"
else
  fail_test "launch_enabled_false (got $LE)"
fi

# ── 11. Next-wave JSON valid ──
if python3 -c "import json; json.load(open('$NEXT_WAVE'))" 2>/dev/null; then
  pass_test "next_wave_json_valid"
else
  fail_test "next_wave_json_valid"
fi

# ── 12. Phase 3 gaps include p3_full_task_wave ──
HAS_WAVE_GAP=$(python3 -c "
import json
with open('$EVAL_JSONL') as f:
    for line in f:
        r = json.loads(line)
        if r['checkbox'] == 'p3_full_task_wave' and r['status'] == 'NOT_STARTED':
            print('yes')
            break
")
if [ "$HAS_WAVE_GAP" = "yes" ]; then
  pass_test "p3_full_task_wave_is_gap"
else
  fail_test "p3_full_task_wave_is_gap"
fi

# ── 13. Phase 0 client smoke is GAP ──
HAS_SMOKE_GAP=$(python3 -c "
import json
with open('$EVAL_JSONL') as f:
    for line in f:
        r = json.loads(line)
        if r['checkbox'] == 'p0_mcp_client_smoke' and r['status'] == 'GAP':
            print('yes')
            break
")
if [ "$HAS_SMOKE_GAP" = "yes" ]; then
  pass_test "p0_mcp_client_smoke_is_gap"
else
  fail_test "p0_mcp_client_smoke_is_gap"
fi

# ── 14. Write-gated tools counted as DONE ──
WRITE_GATED=$(python3 -c "
import json
with open('$EVAL_JSONL') as f:
    for line in f:
        r = json.loads(line)
        if r['checkbox'] == 'p0_write_gated_tools' and r['status'] == 'DONE':
            print('yes')
            break
")
if [ "$WRITE_GATED" = "yes" ]; then
  pass_test "p0_write_gated_tools_is_done"
else
  fail_test "p0_write_gated_tools_is_done"
fi

# ── 15. Launch disabled confirmed ──
LAUNCH_DISABLED=$(python3 -c "
import json
with open('$EVAL_JSONL') as f:
    for line in f:
        r = json.loads(line)
        if r['checkbox'] == 'p2_launch_disabled' and r['status'] == 'DONE':
            print('yes')
            break
")
if [ "$LAUNCH_DISABLED" = "yes" ]; then
  pass_test "p2_launch_disabled_is_done"
else
  fail_test "p2_launch_disabled_is_done"
fi

# ── 16. No blocked checkboxes with zero evidence ──
BLOCKED_NO_EVIDENCE=$(python3 -c "
import json
with open('$EVAL_JSONL') as f:
    for line in f:
        r = json.loads(line)
        refs = r.get('evidence_refs', [])
        if r['status'] == 'BLOCKED' and len(refs) == 0:
            print('FAIL')
            break
    else:
        print('PASS')
")
if [ "$BLOCKED_NO_EVIDENCE" = "PASS" ]; then
  pass_test "no_blocked_without_evidence"
else
  fail_test "no_blocked_without_evidence"
fi

echo ""
echo "=== Results: $PASS PASS, $FAIL FAIL ==="

if [ "$FAIL" -gt 0 ]; then
  echo "AUDIT FAILED"
  exit 1
else
  echo "AUDIT PASSED"
  exit 0
fi
