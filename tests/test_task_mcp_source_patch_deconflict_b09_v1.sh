#!/usr/bin/env bash
# test_task_mcp_source_patch_deconflict_b09_v1.sh
# Smoke/invariant tests for B09 source-patch deconfliction audit.
# ALL tests are read-only — no source patch, no task status mutation, no env write.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
EVAL_JSON="$REPO_ROOT/tools/geoai-task-mcp/eval/task_mcp_source_patch_deconflict_b09_v1.json"
EVAL_JSONL="$REPO_ROOT/tools/geoai-task-mcp/eval/task_mcp_source_patch_deconflict_rows_b09_v1.jsonl"
NEXT_WAVE="$REPO_ROOT/tools/geoai-task-mcp/data/tasking/task_mcp_source_patch_deconflict_next_wave_b09_v1.json"
MACHINE_CARDS="$REPO_ROOT/bitnnv2/data/tasking/machine_task_cards_v1.jsonl"
CORE_PY="$REPO_ROOT/tools/geoai-task-mcp/src/aiworkhub/core.py"
SERVER_PY="$REPO_ROOT/tools/geoai-task-mcp/src/aiworkhub/server.py"
B08_CONTRACT="$REPO_ROOT/tools/geoai-task-mcp/contracts/task_mcp_supervisor_loop_status_tool_b08_v1.json"

PASS=0
FAIL=0

green() { echo -e "\033[32mPASS\033[0m $*"; }
red()   { echo -e "\033[31mFAIL\033[0m $*"; }

check() {
    local desc="$1"; shift
    if "$@"; then
        green "$desc"
        PASS=$((PASS + 1))
    else
        red "$desc"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== B09 Source Patch Deconfliction Tests ==="
echo ""

# ------------------------------------------------------------------
# 1. Artifact existence
# ------------------------------------------------------------------
check "eval JSON exists"               test -f "$EVAL_JSON"
check "eval JSONL exists"              test -f "$EVAL_JSONL"
check "next_wave JSON exists"          test -f "$NEXT_WAVE"
check "test script exists"             test -f "$SCRIPT_DIR/test_task_mcp_source_patch_deconflict_b09_v1.sh"

# ------------------------------------------------------------------
# 2. JSON validity
# ------------------------------------------------------------------
check "eval JSON parses"               python3 -c "import json; json.load(open('$EVAL_JSON'))" 2>/dev/null
check "next_wave JSON parses"          python3 -c "import json; json.load(open('$NEXT_WAVE'))" 2>/dev/null

# ------------------------------------------------------------------
# 3. eval JSON required top-level sections
# ------------------------------------------------------------------
check "eval: inventory_summary"        python3 -c "import json; d=json.load(open('$EVAL_JSON')); assert 'inventory_summary' in d"
check "eval: write_lane_claimants_classification" python3 -c "import json; d=json.load(open('$EVAL_JSON')); assert 'write_lane_claimants_classification' in d"
check "eval: deconflict_verdict"       python3 -c "import json; d=json.load(open('$EVAL_JSON')); assert 'deconflict_verdict' in d"
check "eval: ordered_safe_plan_for_b08_wiring" python3 -c "import json; d=json.load(open('$EVAL_JSON')); assert 'ordered_safe_plan_for_b08_wiring' in d"
check "eval: acceptance"               python3 -c "import json; d=json.load(open('$EVAL_JSON')); assert 'acceptance' in d"
check "eval: forbidden_operations_confirmed_not_taken" python3 -c "import json; d=json.load(open('$EVAL_JSON')); assert 'forbidden_operations_confirmed_not_taken' in d"

# ------------------------------------------------------------------
# 4. eval JSON verdict invariants
# ------------------------------------------------------------------
check "eval: verdict is SAFE_TO_PROCEED" python3 -c "import json; d=json.load(open('$EVAL_JSON')); assert d['verdict'] == 'SAFE_TO_PROCEED_NO_ACTIVE_WRITE_LANE_CONFLICT'"
check "eval: all 9 write-lane claimants ALREADY_DONE" python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
claimants = d['write_lane_claimants_classification']
assert len(claimants) == 9, f'expected 9, got {len(claimants)}'
for c in claimants:
    assert c['classification'] == 'ALREADY_DONE', f'{c[\"task_id\"]} not ALREADY_DONE: {c[\"classification\"]}'
"
check "eval: inventory says zero active write claimants" python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
inv = d['inventory_summary']
assert inv['active_or_blocked_tasks_with_core_server_write_claim'] == 0
assert inv['all_write_lane_claimants_already_done'] == True
"
check "eval: forbidden ops all false" python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
for k,v in d['forbidden_operations_confirmed_not_taken'].items():
    assert v == False, f'{k} is {v}, expected False'
"

# ------------------------------------------------------------------
# 5. JSONL row count and shape
# ------------------------------------------------------------------
check "JSONL: at least 12 rows"        test "$(wc -l < "$EVAL_JSONL")" -ge 12
check "JSONL: each row is valid JSON"  python3 -c "
import json
with open('$EVAL_JSONL') as f:
    for i, line in enumerate(f, 1):
        line = line.strip()
        if not line: continue
        try:
            json.loads(line)
        except json.JSONDecodeError as e:
            raise SystemExit(f'Line {i}: {e}')
"
check "JSONL: contains verdict row"    python3 -c "
with open('$EVAL_JSONL') as f:
    for line in f:
        if 'b09_verdict_001' in line:
            break
    else:
        raise SystemExit('verdict row not found')
"
check "JSONL: verdict row says SAFE_TO_PROCEED" python3 -c "
with open('$EVAL_JSONL') as f:
    for line in f:
        if 'b09_verdict_001' in line and 'SAFE_TO_PROCEED' in line:
            break
    else:
        raise SystemExit('SAFE_TO_PROCEED not in verdict row')
"

# ------------------------------------------------------------------
# 6. Next wave JSON invariants
# ------------------------------------------------------------------
check "next_wave: deconflict_result.verdict is SAFE_TO_PROCEED" python3 -c "
import json
d = json.load(open('$NEXT_WAVE'))
assert d['deconflict_result']['verdict'] == 'SAFE_TO_PROCEED'
assert d['deconflict_result']['b08_wiring_blocked'] == False
"

# ------------------------------------------------------------------
# 7. LIVE cross-validation: B08 wiring NOT yet in source
# ------------------------------------------------------------------
check "LIVE: supervisor_loop_status NOT in core.py" python3 -c "
with open('$CORE_PY') as f:
    assert 'supervisor_loop_status' not in f.read(), 'B08 wiring already in core.py!'
"
check "LIVE: aiworkhub_supervisor_loop_status NOT in server.py" python3 -c "
with open('$SERVER_PY') as f:
    assert 'aiworkhub_supervisor_loop_status' not in f.read(), 'B08 wiring already in server.py!'
"

# ------------------------------------------------------------------
# 8. LIVE cross-validation: no active task_mcp card claims core.py/server.py write lane
# ------------------------------------------------------------------
check "LIVE: zero active core.py/server.py write claimants" python3 -c "
import json
with open('$MACHINE_CARDS') as f:
    cards = [json.loads(line) for line in f if line.strip()]

active_states = {'processing', 'in_progress', 'pending', 'review', 'blocked', 'blocked_review_failed'}
violations = []
for c in cards:
    st = str(c.get('status','') or '').strip().lower()
    ws = str(c.get('worker_status','') or '').strip().lower()
    if st not in active_states and ws not in {'claimed', 'in_progress'}:
        continue
    aw = c.get('allowed_writes', [])
    for p in aw:
        if 'core.py' in p or 'server.py' in p:
            violations.append(f'{c[\"task_id\"]} claims {p} (status={st}, ws={ws})')

if violations:
    for v in violations:
        print(f'VIOLATION: {v}')
    raise SystemExit(f'{len(violations)} active write-lane violations found')
print('OK: zero active write-lane claimants')
"

# ------------------------------------------------------------------
# 9. B08 contract cross-reference
# ------------------------------------------------------------------
check "B08 contract JSON parses"       python3 -c "import json; json.load(open('$B08_CONTRACT'))" 2>/dev/null
check "B08: future_source_patch_paths names core.py + server.py" python3 -c "
import json
d = json.load(open('$B08_CONTRACT'))
fsp = d.get('future_source_patch_paths', {})
patches = fsp.get('patches', [])
paths = [p['path'] for p in patches]
assert 'tools/geoai-task-mcp/src/aiworkhub/core.py' in paths, f'core.py not in {paths}'
assert 'tools/geoai-task-mcp/src/aiworkhub/server.py' in paths, f'server.py not in {paths}'
"

# ------------------------------------------------------------------
# 10. This task does NOT patch source (self-check)
# ------------------------------------------------------------------
check "SELF: B09 allowed_writes has zero src/ paths" python3 -c "
import json
with open('$MACHINE_CARDS') as f:
    for line in f:
        line = line.strip()
        if not line: continue
        c = json.loads(line)
        if c.get('task_id') == 'DEEPSEEK_TASK_MCP_SOURCE_PATCH_DECONFLICT_B09_V1':
            aw = c.get('allowed_writes', [])
            src_writes = [p for p in aw if '/src/' in p]
            if src_writes:
                raise SystemExit(f'B09 allowed_writes contains src/ paths: {src_writes}')
            print('OK: B09 allowed_writes has zero src/ paths')
            break
"

# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
echo ""
echo "=== Results: $PASS PASS, $FAIL FAIL ==="
if [ "$FAIL" -gt 0 ]; then
    echo "Some tests FAILED."
    exit 1
else
    echo "All tests PASSED."
    exit 0
fi
