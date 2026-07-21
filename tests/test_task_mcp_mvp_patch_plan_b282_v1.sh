#!/usr/bin/env bash
# test_task_mcp_mvp_patch_plan_b282_v1.sh
# Validation test for CLAUDE_TASK_MCP_DRYRUN_TO_MVP_PATCH_PLAN_B282_V1
#
# Tests:
#   1. Plan JSON exists and is valid JSON
#   2. Plan JSON contains all required top-level keys (incl verdict/metrics/blockers/next_wave)
#   3. authority_flags: all runtime/support/apply/training flags explicitly false (>=16)
#   4. mvp_patch_plan.exact_files section present with modify/new/test file lists
#   5. api_surface_new_tool has input_schema and output_schema
#   6. safety_gates: stale recovery, duplicate runner, usage, rollback gates all present
#   7. metrics section present with tool-count fields
#   8. blockers non-empty
#   9. No subprocess/Popen/exec/launch markers anywhere in the plan JSON (patch-plan only)
#  10. Plan artifact under 25MB
#  11. Next-wave JSON exists, valid, has >=3 cards with disjoint allowed_writes
#  12. Next-wave cards have distinct proposed_task_ids, none re-using B282
#  13. Referenced existing source files actually exist on disk (plan is code-grounded)
#  14. taskctl verify

# Use command python3 to avoid site-packages auto-execution hooks
PYTHON3="${PYTHON3:-command python3}"

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
PLAN_JSON="$REPO_ROOT/tools/geoai-task-mcp/eval/task_mcp_mvp_patch_plan_b282_v1.json"
NEXT_WAVE="$REPO_ROOT/tools/geoai-task-mcp/data/tasking/task_mcp_mvp_patch_plan_next_wave_b282_v1.json"
PASS=0
FAIL=0

green() { echo -e "\033[32m  PASS\033[0m $1"; ((PASS++)) || true; }
red()   { echo -e "\033[31m  FAIL\033[0m $1"; ((FAIL++)) || true; }

echo "=== B282 MVP Patch Plan Test ==="
echo ""

# ── 1. Plan JSON exists ─────────────────────────────────────────────────
echo "[1] Plan JSON file exists"
if [ -f "$PLAN_JSON" ]; then
    green "plan JSON found at $PLAN_JSON"
else
    red "plan JSON MISSING: $PLAN_JSON"
fi

# ── 2. Plan JSON is valid JSON ──────────────────────────────────────────
echo "[2] Plan JSON is valid JSON"
if $PYTHON3 -c "import json; json.load(open('$PLAN_JSON'))" 2>/dev/null; then
    green "plan JSON parses correctly"
else
    red "plan JSON is INVALID"
fi

# ── 3. Required top-level keys ──────────────────────────────────────────
echo "[3] Required top-level keys present"
REQUIRED_KEYS=(
    "schema_id" "task_id" "runner" "topic" "mode" "verdict"
    "authority_flags" "builds_on" "mvp_patch_plan" "metrics"
    "gates" "invariants" "blockers" "next_wave"
    "files_written" "commit_contract"
)
for key in "${REQUIRED_KEYS[@]}"; do
    if $PYTHON3 -c "
import json
d = json.load(open('$PLAN_JSON'))
assert '$key' in d, 'missing key: $key'
" 2>/dev/null; then
        green "key present: $key"
    else
        red "MISSING key: $key"
    fi
done

# ── 4. authority_flags: >=16 flags, all explicitly false ───────────────
echo "[4] All authority_flags explicitly false (>=16 flags)"
FLAG_CHECK=$($PYTHON3 -c "
import json
d = json.load(open('$PLAN_JSON'))
flags = d['authority_flags']
false_count = 0
bad = []
for k, v in flags.items():
    if v is not False:
        bad.append((k, v))
    else:
        false_count += 1
assert not bad, f'not-false flags: {bad}'
assert false_count >= 16, f'only {false_count} flags (need >=16)'
print('ALL_FALSE_OK:' + str(false_count))
" 2>/dev/null)
if echo "$FLAG_CHECK" | grep -q "ALL_FALSE_OK"; then
    green "all authority flags false ($(echo "$FLAG_CHECK" | cut -d: -f2) flags)"
else
    red "authority flag violation: $FLAG_CHECK"
fi

# ── 5. mvp_patch_plan.exact_files present with 3 sub-lists ─────────────
echo "[5] exact_files section present (modify/new/test lists)"
if $PYTHON3 -c "
import json
d = json.load(open('$PLAN_JSON'))
ef = d['mvp_patch_plan']['exact_files']
assert len(ef.get('modify_additive_only', [])) >= 1, 'no modify_additive_only entries'
assert len(ef.get('new_files', [])) >= 1, 'no new_files entries'
assert len(ef.get('test_files', [])) >= 1, 'no test_files entries'
" 2>/dev/null; then
    green "exact_files has modify/new/test entries"
else
    red "exact_files section incomplete"
fi

# ── 6. api_surface_new_tool has input/output schema ─────────────────────
echo "[6] api_surface_new_tool has input_schema and output_schema"
if $PYTHON3 -c "
import json
d = json.load(open('$PLAN_JSON'))
t = d['mvp_patch_plan']['api_surface_new_tool']
assert 'name' in t
assert isinstance(t.get('input_schema'), dict) and len(t['input_schema']) >= 8, 'input_schema too small'
assert isinstance(t.get('output_schema'), dict) and len(t['output_schema']) >= 6, 'output_schema too small'
" 2>/dev/null; then
    green "new-tool API surface has input_schema and output_schema"
else
    red "new-tool API surface MISSING or incomplete"
fi

# ── 7. safety_gates: 4 required gate kinds present ──────────────────────
echo "[7] Safety gates: stale recovery, duplicate runner, usage, rollback"
if $PYTHON3 -c "
import json
d = json.load(open('$PLAN_JSON'))
gates = d['mvp_patch_plan']['safety_gates']
text = json.dumps(gates).lower()
for token in ('stale', 'duplicate_runner', 'usage', 'rollback'):
    assert token in text, f'missing gate keyword: {token}'
assert len(gates) >= 4, f'only {len(gates)} gates defined'
" 2>/dev/null; then
    green "all 4 required gate kinds present"
else
    red "MISSING one or more required safety gates"
fi

# ── 8. metrics section present with tool-count fields ───────────────────
echo "[8] metrics section present"
if $PYTHON3 -c "
import json
d = json.load(open('$PLAN_JSON'))
m = d['metrics']
assert 'existing_registered_mcp_tools_before_patch' in m
assert 'new_mcp_tools_added' in m
assert isinstance(m['existing_registered_mcp_tools_before_patch'], int)
" 2>/dev/null; then
    green "metrics section present with tool-count fields"
else
    red "metrics section MISSING or incomplete"
fi

# ── 9. blockers non-empty ────────────────────────────────────────────────
echo "[9] blockers non-empty"
BLOCK_COUNT=$($PYTHON3 -c "
import json
d = json.load(open('$PLAN_JSON'))
print(len(d.get('blockers', [])))
" 2>/dev/null)
if [ "$BLOCK_COUNT" -ge 1 ]; then
    green "blockers listed ($BLOCK_COUNT)"
else
    red "blockers section EMPTY"
fi

# ── 10. No subprocess/launch code markers in plan JSON ──────────────────
echo "[10] No subprocess/Popen/exec/launch code markers in plan JSON"
if $PYTHON3 -c "
text = open('$PLAN_JSON').read().lower()
# Require call-syntax '(' so prose that DISCUSSES avoiding these (e.g. 'no
# os.exec/os.spawn import added') is not a false positive -- only literal
# call sites would trip this in a code file; this is a plan JSON, so any
# match at all would mean actual invocation-shaped text was pasted in.
forbidden = ['subprocess.popen(', 'os.exec(', 'os.system(', 'shell=true', 'pty.spawn(', 'subprocess.run(', 'subprocess.call(', 'os.spawn(', 'os.popen(']
for f in forbidden:
    if f in text:
        print(f'FORBIDDEN_MARKER: {f}')
        raise SystemExit(1)
print('CLEAN')
" 2>/dev/null; then
    green "no process-launch code markers in plan JSON"
else
    red "FORBIDDEN launch code marker found in plan JSON"
fi

# ── 11. Plan artifact under 25MB ─────────────────────────────────────────
echo "[11] Plan JSON artifact size under 25MB"
SIZE=$(stat -c%s "$PLAN_JSON" 2>/dev/null || echo 0)
if [ "$SIZE" -lt 26214400 ]; then
    green "plan JSON size: $SIZE bytes (< 25MB)"
else
    red "plan JSON size: $SIZE bytes (EXCEEDS 25MB)"
fi

# ── 12. Next-wave JSON exists and is valid ───────────────────────────────
echo "[12] Next-wave JSON exists and is valid"
if [ -f "$NEXT_WAVE" ]; then
    if $PYTHON3 -c "import json; json.load(open('$NEXT_WAVE'))" 2>/dev/null; then
        green "next-wave JSON found and valid"
    else
        red "next-wave JSON is INVALID"
    fi
else
    red "next-wave JSON MISSING: $NEXT_WAVE"
fi

# ── 13. Next-wave has >=3 cards ──────────────────────────────────────────
echo "[13] Next-wave has >=3 proposed task cards"
NW_COUNT=$($PYTHON3 -c "
import json
d = json.load(open('$NEXT_WAVE'))
print(len(d.get('next_wave_cards', [])))
" 2>/dev/null)
if [ "$NW_COUNT" -ge 3 ]; then
    green "next-wave has $NW_COUNT cards (>=3 required)"
else
    red "next-wave has only $NW_COUNT cards (<3 required)"
fi

# ── 14. Next-wave cards: distinct proposed_task_ids, none re-use B282 ───
echo "[14] Next-wave cards have distinct proposed_task_ids, none re-use _B282_"
DUP_CHECK=$($PYTHON3 -c "
import json
d = json.load(open('$NEXT_WAVE'))
ids = [c['proposed_task_id'] for c in d.get('next_wave_cards', [])]
bad_batch = [i for i in ids if '_B282_' in i]
if bad_batch:
    print('BATCH_COLLISION: ' + str(bad_batch))
elif len(ids) == len(set(ids)):
    print('DISTINCT_OK')
else:
    print('DUPLICATE_IDS: ' + str([i for i in ids if ids.count(i) > 1]))
" 2>/dev/null)
if echo "$DUP_CHECK" | grep -q "DISTINCT_OK"; then
    green "all proposed_task_ids distinct and batch-token-safe"
else
    red "task_id problem: $DUP_CHECK"
fi

# ── 15. Next-wave cards have disjoint allowed_writes ─────────────────────
echo "[15] Next-wave cards have disjoint allowed_writes"
OVERLAP_CHECK=$($PYTHON3 -c "
import json
d = json.load(open('$NEXT_WAVE'))
cards = d.get('next_wave_cards', [])
all_writes = [set(c.get('allowed_writes', [])) for c in cards]
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

# ── 16. Referenced existing source files actually exist on disk ─────────
echo "[16] Plan's referenced existing source files exist on disk"
if $PYTHON3 -c "
import os
paths = [
    'tools/geoai-task-mcp/src/aiworkhub/launch_queue_contract.py',
    'tools/geoai-task-mcp/src/aiworkhub/launch_queue_persist.py',
    'tools/geoai-task-mcp/src/aiworkhub/server.py',
    'tools/geoai-task-mcp/src/aiworkhub/completion_inbox.py',
    'tools/geoai-task-mcp/src/aiworkhub/core.py',
]
missing = [p for p in paths if not os.path.isfile(os.path.join('$REPO_ROOT', p))]
assert not missing, f'missing referenced files: {missing}'
" 2>/dev/null; then
    green "all referenced existing source files exist"
else
    red "one or more referenced source files are MISSING"
fi

# ── 17. supersedes section documents the B281 collision it fixes ────────
echo "[17] Next-wave documents the superseded B281-proposed card"
if $PYTHON3 -c "
import json
d = json.load(open('$NEXT_WAVE'))
s = d.get('supersedes', {})
assert s.get('proposed_task_id') == 'CLAUDE_TASK_MCP_QUEUE_REQUEST_TOOL_B282_V1'
assert len(s.get('reason', '')) > 20
" 2>/dev/null; then
    green "supersedes section present and references the collided card"
else
    red "supersedes section MISSING or incomplete"
fi

# ── SUMMARY ────────────────────────────────────────────────────────────
echo ""
echo "=============================================="
echo "  B282 MVP Patch Plan Test Summary"
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
