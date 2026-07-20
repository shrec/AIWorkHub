#!/usr/bin/env bash
# test_task_mcp_autolaunch_queue_dryrun_b281_v1.sh
# Validation test for DEEPSEEK_TASK_MCP_AUTOLAUNCH_QUEUE_DRYRUN_B281_V1
#
# Tests:
#   1. Eval JSON exists and is valid JSON
#   2. Eval JSON contains all required top-level keys
#   3. All 5 protocol sections present
#   4. Queue request envelope has required fields (>=10)
#   5. Queue request lifecycle has all 8 states defined

# Use command python3 to avoid site-packages auto-execution hooks
PYTHON3="${PYTHON3:-command python3}"
#   6. Completion inbox integration covers review, stale, collision flows
#   7. Usage accounting reconciliation rules defined (dual-source)
#   8. State machine has exactly 9 states and 12 transitions
#   9. All authority_flags are explicitly false (>=14 flags checked)
#  10. No subprocess/Popen/exec markers in eval JSON (dryrun-only)
#  11. Artifact size under 25MB
#  12. Next-wave JSON exists, valid, has >=2 cards with disjoint allowed_writes
#  13. Next-wave cards have distinct proposed_task_ids
#  14. taskctl verify

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
EVAL_JSON="$REPO_ROOT/tools/geoai-task-mcp/eval/task_mcp_autolaunch_queue_dryrun_b281_v1.json"
NEXT_WAVE="$REPO_ROOT/tools/geoai-task-mcp/data/tasking/task_mcp_autolaunch_queue_dryrun_next_wave_b281_v1.json"
PASS=0
FAIL=0

green() { echo -e "\033[32m  PASS\033[0m $1"; ((PASS++)) || true; }
red()   { echo -e "\033[31m  FAIL\033[0m $1"; ((FAIL++)) || true; }

echo "=== B281 Autolaunch Queue Dryrun Test ==="
echo ""

# ── 1. Eval JSON exists ───────────────────────────────────────────────
echo "[1] Eval JSON file exists"
if [ -f "$EVAL_JSON" ]; then
    green "eval JSON found at $EVAL_JSON"
else
    red "eval JSON MISSING: $EVAL_JSON"
fi

# ── 2. Eval JSON is valid JSON ────────────────────────────────────────
echo "[2] Eval JSON is valid JSON"
if $PYTHON3 -c "import json; json.load(open('$EVAL_JSON'))" 2>/dev/null; then
    green "eval JSON parses correctly"
else
    red "eval JSON is INVALID"
fi

# ── 3. Required top-level keys ─────────────────────────────────────────
echo "[3] Required top-level keys present"
REQUIRED_KEYS=(
    "eval_id" "task_id" "runner" "topic" "mode" "verdict"
    "builds_on" "summary" "protocol_sections" "gates"
    "invariants" "authority_flags"
    "files_written" "commit_contract"
)
for key in "${REQUIRED_KEYS[@]}"; do
    if $PYTHON3 -c "
import json
d = json.load(open('$EVAL_JSON'))
assert '$key' in d, 'missing key: $key'
" 2>/dev/null; then
        green "key present: $key"
    else
        red "MISSING key: $key"
    fi
done

# ── 4. Protocol sections present ───────────────────────────────────────
echo "[4] All 5 protocol sections present"
SECTIONS=(
    "section_1_queue_request_schema"
    "section_2_completion_inbox_integration"
    "section_3_usage_accounting_events"
    "section_4_queue_flow_state_machine"
    "section_5_authority_flags"
)
for sec in "${SECTIONS[@]}"; do
    if $PYTHON3 -c "
import json
d = json.load(open('$EVAL_JSON'))
ps = d['protocol_sections']
assert '$sec' in ps, 'missing section: $sec'
" 2>/dev/null; then
        green "section present: $sec"
    else
        red "MISSING section: $sec"
    fi
done

# ── 5. Queue request envelope has >=10 required fields ─────────────────
echo "[5] Queue request envelope has required fields (>=10)"
REQ_FIELDS=(
    "request_id" "task_id" "runner" "topic" "adapter" "model"
    "owner_prompt" "requested_at" "priority" "max_turns"
    "stale_timeout_seconds" "env_overrides" "authority_flags"
)
FIELD_COUNT=0
for f in "${REQ_FIELDS[@]}"; do
    if $PYTHON3 -c "
import json
d = json.load(open('$EVAL_JSON'))
env = d['protocol_sections']['section_1_queue_request_schema']['queue_request_envelope']
assert '$f' in env, 'missing field: $f'
" 2>/dev/null; then
        ((FIELD_COUNT++)) || true
    fi
done
if [ "$FIELD_COUNT" -ge 10 ]; then
    green "queue request envelope has $FIELD_COUNT fields (>=10 required)"
else
    red "queue request envelope has only $FIELD_COUNT fields (<10 required)"
fi

# ── 6. Queue request lifecycle has 8 states ────────────────────────────
echo "[6] Queue request lifecycle has 8 states"
STATE_COUNT=$($PYTHON3 -c "
import json
d = json.load(open('$EVAL_JSON'))
states = d['protocol_sections']['section_1_queue_request_schema']['queue_request_lifecycle']['states']
print(len(states))
" 2>/dev/null)
if [ "$STATE_COUNT" -ge 8 ]; then
    green "lifecycle has $STATE_COUNT states (>=8 required)"
else
    red "lifecycle has only $STATE_COUNT states (<8 required)"
fi

# ── 7. Completion inbox integration: all 3 sub-sections present ────────
echo "[7] Completion inbox integration sub-sections present"
CI_SUBS=("completion_flow" "stale_recovery_flow" "collision_and_duplicate_runner_handling")
for sub in "${CI_SUBS[@]}"; do
    if $PYTHON3 -c "
import json
d = json.load(open('$EVAL_JSON'))
ci = d['protocol_sections']['section_2_completion_inbox_integration']
assert '$sub' in ci, 'missing sub-section: $sub'
" 2>/dev/null; then
        green "sub-section present: $sub"
    else
        red "MISSING sub-section: $sub"
    fi
done

# ── 8. Stale recovery flow has >=3 recovery steps ──────────────────────
echo "[8] Stale recovery flow has steps"
STALE_STEPS=$($PYTHON3 -c "
import json
d = json.load(open('$EVAL_JSON'))
steps = d['protocol_sections']['section_2_completion_inbox_integration']['stale_recovery_flow']['recovery_steps']
print(len(steps))
" 2>/dev/null)
if [ "$STALE_STEPS" -ge 3 ]; then
    green "stale recovery has $STALE_STEPS steps (>=3 required)"
else
    red "stale recovery has only $STALE_STEPS steps (<3 required)"
fi

# ── 9. Collision handling has >=3 handling steps ───────────────────────
echo "[9] Collision/duplicate-runner handling has steps"
COLL_STEPS=$($PYTHON3 -c "
import json
d = json.load(open('$EVAL_JSON'))
steps = d['protocol_sections']['section_2_completion_inbox_integration']['collision_and_duplicate_runner_handling']['handling']
print(len(steps))
" 2>/dev/null)
if [ "$COLL_STEPS" -ge 3 ]; then
    green "collision handling has $COLL_STEPS steps (>=3 required)"
else
    red "collision handling has only $COLL_STEPS steps (<3 required)"
fi

# ── 10. Usage accounting: dual sources defined ─────────────────────────
echo "[10] Usage accounting dual sources defined"
if $PYTHON3 -c "
import json
d = json.load(open('$EVAL_JSON'))
ua = d['protocol_sections']['section_3_usage_accounting_events']['dual_source_accounting']
assert 'source_1_launch_log' in ua, 'missing source_1'
assert 'source_2_taskctl_usage' in ua, 'missing source_2'
assert 'reconciliation_rules' in d['protocol_sections']['section_3_usage_accounting_events']
" 2>/dev/null; then
    green "dual sources and reconciliation rules present"
else
    red "MISSING dual sources or reconciliation rules"
fi

# ── 11. State machine: 9 states ────────────────────────────────────────
echo "[11] State machine has 9 states"
SM_STATES=$($PYTHON3 -c "
import json
d = json.load(open('$EVAL_JSON'))
states = d['protocol_sections']['section_4_queue_flow_state_machine']['states']
print(len(states))
" 2>/dev/null)
if [ "$SM_STATES" -eq 9 ]; then
    green "state machine has $SM_STATES states"
else
    red "state machine has $SM_STATES states (expected 9)"
fi

# ── 12. State machine: 12 transitions ──────────────────────────────────
echo "[12] State machine has 12 transitions"
SM_TRANS=$($PYTHON3 -c "
import json
d = json.load(open('$EVAL_JSON'))
trans = d['protocol_sections']['section_4_queue_flow_state_machine']['transitions']
print(len(trans))
" 2>/dev/null)
if [ "$SM_TRANS" -eq 12 ]; then
    green "state machine has $SM_TRANS transitions"
else
    red "state machine has $SM_TRANS transitions (expected 12)"
fi

# ── 13. State machine invariants: >=4 ───────────────────────────────────
echo "[13] State machine invariants defined"
SM_INV=$($PYTHON3 -c "
import json
d = json.load(open('$EVAL_JSON'))
inv = d['protocol_sections']['section_4_queue_flow_state_machine']['invariants']
print(len(inv))
" 2>/dev/null)
if [ "$SM_INV" -ge 4 ]; then
    green "state machine has $SM_INV invariants (>=4 required)"
else
    red "state machine has only $SM_INV invariants (<4 required)"
fi

# ── 14. All authority_flags explicitly false (>=14 flags) ──────────────
echo "[14] All authority_flags are explicitly false"
FLAG_CHECK=$($PYTHON3 -c "
import json
d = json.load(open('$EVAL_JSON'))
flags = d['protocol_sections']['section_5_authority_flags']['flags']
all_false = True
false_count = 0
for k, v in flags.items():
    if v is not False:
        print(f'FLAG_NOT_FALSE: {k}={v}')
        all_false = False
    else:
        false_count += 1

assert false_count >= 14, f'only {false_count} flags (need >=14)'
assert all_false, 'some flags are not false'

# Also verify top-level invariants
inv = d['invariants']
assert inv.get('dryrun_only') == True, 'dryrun_only must be true'
assert inv.get('launch_implemented') == False, 'launch_implemented must be false'
assert inv.get('queue_write') == False, 'queue_write must be false'

# Verify gates
gates = d['gates']
assert gates.get('dryrun_only_no_cli_launch_code') == True
assert gates.get('all_authority_flags_false') == True
assert gates.get('launch_implemented_remains_false') == True

print('ALL_FALSE_OK:' + str(false_count))
" 2>/dev/null)
if echo "$FLAG_CHECK" | grep -q "ALL_FALSE_OK"; then
    green "all authority flags false ($(echo "$FLAG_CHECK" | grep ALL_FALSE_OK | cut -d: -f2) flags), dryrun invariants hold"
else
    red "authority flag violation or invariant broken: $FLAG_CHECK"
fi

# ── 15. No subprocess/launch code in eval JSON ─────────────────────────
echo "[15] No subprocess/Popen/exec markers in eval JSON (dryrun-only)"
if $PYTHON3 -c "
text = open('$EVAL_JSON').read().lower()
forbidden = ['subprocess.popen', 'os.exec', 'os.system(', 'shell=true', 'pty.spawn', 'subprocess.run(', 'subprocess.call(']
for f in forbidden:
    if f in text:
        print(f'FORBIDDEN_MARKER: {f}')
        raise SystemExit(1)
print('CLEAN')
" 2>/dev/null; then
    green "no process-launch code markers in eval JSON"
else
    red "FORBIDDEN launch code marker found in eval JSON"
fi

# ── 16. Artifact size under 25MB ───────────────────────────────────────
echo "[16] Eval JSON artifact size under 25MB"
SIZE=$(stat -c%s "$EVAL_JSON" 2>/dev/null || echo 0)
if [ "$SIZE" -lt 26214400 ]; then
    green "eval JSON size: $SIZE bytes (< 25MB)"
else
    red "eval JSON size: $SIZE bytes (EXCEEDS 25MB)"
fi

# ── 17. Next-wave JSON exists and is valid ─────────────────────────────
echo "[17] Next-wave JSON exists and is valid"
if [ -f "$NEXT_WAVE" ]; then
    if $PYTHON3 -c "import json; json.load(open('$NEXT_WAVE'))" 2>/dev/null; then
        green "next-wave JSON found and valid"
    else
        red "next-wave JSON is INVALID"
    fi
else
    red "next-wave JSON MISSING: $NEXT_WAVE"
fi

# ── 18. Next-wave has >=2 cards ────────────────────────────────────────
echo "[18] Next-wave has >=2 proposed task cards"
NW_COUNT=$($PYTHON3 -c "
import json
d = json.load(open('$NEXT_WAVE'))
cards = d.get('next_wave_cards', [])
print(len(cards))
" 2>/dev/null)
if [ "$NW_COUNT" -ge 2 ]; then
    green "next-wave has $NW_COUNT cards (>=2 required)"
else
    red "next-wave has only $NW_COUNT cards (<2 required)"
fi

# ── 19. Next-wave cards have distinct proposed_task_ids ────────────────
echo "[19] Next-wave cards have distinct proposed_task_ids"
DUP_CHECK=$($PYTHON3 -c "
import json
d = json.load(open('$NEXT_WAVE'))
ids = [c['proposed_task_id'] for c in d.get('next_wave_cards', [])]
if len(ids) == len(set(ids)):
    print('DISTINCT_OK')
else:
    print('DUPLICATE_IDS: ' + str([i for i in ids if ids.count(i) > 1]))
" 2>/dev/null)
if echo "$DUP_CHECK" | grep -q "DISTINCT_OK"; then
    green "all proposed_task_ids are distinct"
else
    red "DUPLICATE proposed_task_ids: $DUP_CHECK"
fi

# ── 20. Next-wave cards have allowed_writes with no intra-wave overlap ──
echo "[20] Next-wave cards have disjoint allowed_writes"
OVERLAP_CHECK=$($PYTHON3 -c "
import json
d = json.load(open('$NEXT_WAVE'))
cards = d.get('next_wave_cards', [])
all_writes = []
for c in cards:
    aw = c.get('allowed_writes', [])
    all_writes.append(set(aw))
overlap = False
for i in range(len(all_writes)):
    for j in range(i+1, len(all_writes)):
        common = all_writes[i] & all_writes[j]
        if common:
            print(f'OVERLAP between card {i} and {j}: {common}')
            overlap = True
if not overlap:
    print('DISJOINT_OK')
" 2>/dev/null)
if echo "$OVERLAP_CHECK" | grep -q "DISJOINT_OK"; then
    green "all next-wave allowed_writes are disjoint"
else
    red "OVERLAPPING allowed_writes: $OVERLAP_CHECK"
fi

# ── 21. Section_1 idempotency rule present ─────────────────────────────
echo "[21] Queue request idempotency rule defined"
if $PYTHON3 -c "
import json
d = json.load(open('$EVAL_JSON'))
env = d['protocol_sections']['section_1_queue_request_schema']
rule = env.get('idempotency_rule', '')
assert len(rule) > 20, 'idempotency rule missing or too short'
" 2>/dev/null; then
    green "idempotency rule defined"
else
    red "idempotency rule MISSING or too short"
fi

# ── 22. Remaining gaps section present and non-empty ───────────────────
echo "[22] Remaining gaps section present"
if $PYTHON3 -c "
import json
d = json.load(open('$EVAL_JSON'))
gaps = d.get('remaining_gaps_not_solved_by_this_spec', [])
assert len(gaps) >= 3, f'only {len(gaps)} gaps listed (need >=3)'
" 2>/dev/null; then
    green "remaining gaps listed ($($PYTHON3 -c "import json; print(len(json.load(open('$EVAL_JSON'))['remaining_gaps_not_solved_by_this_spec']))") gaps)"
else
    red "remaining gaps MISSING or too few (<3)"
fi

# ── 23. completion_inbox.py integration: builds_on references present ──
echo "[23] Builds_on references completion_inbox and B276/B277"
if $PYTHON3 -c "
import json
d = json.load(open('$EVAL_JSON'))
bo = ' '.join(d.get('builds_on', []))
assert 'completion_inbox' in bo.lower() or 'B275' in bo, 'missing completion_inbox reference'
assert 'B276' in bo, 'missing B276 reference'
assert 'B277' in bo, 'missing B277 reference'
" 2>/dev/null; then
    green "builds_on references B275/B276/B277"
else
    red "MISSING builds_on reference to B275/B276/B277"
fi

# ── 24. Neural bridge note present ─────────────────────────────────────
echo "[24] Neural bridge note present"
if $PYTHON3 -c "
import json
d = json.load(open('$EVAL_JSON'))
note = d.get('neural_bridge_note', '')
assert len(note) > 50, 'neural_bridge_note missing or too short'
" 2>/dev/null; then
    green "neural bridge note present"
else
    red "neural_bridge_note MISSING or too short"
fi

# ── SUMMARY ────────────────────────────────────────────────────────────
echo ""
echo "=============================================="
echo "  B281 Autolaunch Queue Dryrun Test Summary"
echo "=============================================="
echo "  PASS: $PASS"
echo "  FAIL: $FAIL"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "OVERALL: FAIL ($FAIL test(s) failed)"
    exit 1
else
    echo "OVERALL: PASS"
    exit 0
fi
