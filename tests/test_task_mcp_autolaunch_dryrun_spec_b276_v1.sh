#!/usr/bin/env bash
# test_task_mcp_autolaunch_dryrun_spec_b276_v1.sh
# Validation test for DEEPSEEK_TASK_MCP_AUTOLAUNCH_DRYRUN_SPEC_B276_V1
#
# Tests:
#   1. Eval JSON exists and is valid JSON
#   2. Eval JSON contains all required top-level keys
#   3. All protocol sections present (command_templates, safety_gates, log_usage, completion_path)
#   4. All authority_flags are explicitly false
#   5. No launch-implemented code markers (no subprocess, no Popen, no exec)
#   6. Artifact size under 25MB
#   7. Next-wave JSON exists and is valid JSON
#   8. Next-wave contains at least 2 follow-up task cards
#   9. Next-wave task cards have distinct allowed_writes (no overlap)
#  10. Gate invariants hold: dryrun_only=true, launch_implemented=false

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
EVAL_JSON="$REPO_ROOT/tools/geoai-task-mcp/eval/task_mcp_autolaunch_dryrun_spec_b276_v1.json"
NEXT_WAVE="$REPO_ROOT/tools/geoai-task-mcp/data/tasking/task_mcp_autolaunch_dryrun_spec_next_wave_b276_v1.json"
PASS=0
FAIL=0

green() { echo -e "\033[32m  PASS\033[0m $1"; ((PASS++)) || true; }
red()   { echo -e "\033[31m  FAIL\033[0m $1"; ((FAIL++)) || true; }

echo "=== B276 Autolaunch Dryrun Spec Test ==="
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
if python3 -c "import json; json.load(open('$EVAL_JSON'))" 2>/dev/null; then
    green "eval JSON parses correctly"
else
    red "eval JSON is INVALID"
fi

# ── 3. Required top-level keys ─────────────────────────────────────────
echo "[3] Required top-level keys present"
REQUIRED_KEYS=(
    "eval_id" "task_id" "runner" "topic" "mode" "verdict"
    "protocol_sections" "gates" "invariants" "authority_flags"
    "files_written" "commit_contract"
)
for key in "${REQUIRED_KEYS[@]}"; do
    if python3 -c "
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
echo "[4] All protocol sections present"
SECTIONS=(
    "section_1_command_templates"
    "section_2_safety_gates"
    "section_3_log_usage_accounting"
    "section_4_completion_return_path"
    "section_5_authority_flags"
)
for sec in "${SECTIONS[@]}"; do
    if python3 -c "
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

# ── 5. Command templates for all 3 adapters ────────────────────────────
echo "[5] Command templates for all 3 adapters"
ADAPTERS=("claude_cli" "codex_cli" "deepseek_manual")
for adp in "${ADAPTERS[@]}"; do
    if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
tmpl = d['protocol_sections']['section_1_command_templates']['adapters']
assert '$adp' in tmpl, 'missing adapter: $adp'
a = tmpl['$adp']
assert 'template' in a, 'missing template for $adp'
assert len(a['template']) > 20, 'template too short for $adp'
" 2>/dev/null; then
        green "adapter template present: $adp"
    else
        red "MISSING or EMPTY template: $adp"
    fi
done

# ── 6. Safety gates: all 4 gates defined ───────────────────────────────
echo "[6] Four safety gates defined (G1-G4)"
GATE_IDS=("G1_env_dual_gate" "G2_taskctl_guard" "G3_allowed_writes_enforcement" "G4_launch_implemented_flag")
for gid in "${GATE_IDS[@]}"; do
    if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
gates = d['protocol_sections']['section_2_safety_gates']['gates']
found = any(g['gate_id'] == '$gid' for g in gates)
assert found, 'gate not found: $gid'
" 2>/dev/null; then
        green "gate defined: $gid"
    else
        red "MISSING gate: $gid"
    fi
done

# ── 7. Log/usage schema has 15 fields ──────────────────────────────────
echo "[7] Log/usage schema has required fields"
LOG_FIELDS=(
    "launch_id" "task_id" "runner" "topic" "adapter" "model"
    "launch_requested_at" "launch_completed_at" "exit_code"
    "gates_passed" "gates_failed" "usage_input_tokens"
    "usage_output_tokens" "cost_usd" "error_message" "stale_detected_at"
)
FIELD_COUNT=$(python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
fields = d['protocol_sections']['section_3_log_usage_accounting']['fields']
print(len(fields))
" 2>/dev/null)
if [ "$FIELD_COUNT" -ge 15 ]; then
    green "log schema has $FIELD_COUNT fields (>=15 required)"
else
    red "log schema has only $FIELD_COUNT fields (<15 required)"
fi

# ── 8. Completion path has 6 steps ─────────────────────────────────────
echo "[8] Completion return path has 6 steps"
STEP_COUNT=$(python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
steps = d['protocol_sections']['section_4_completion_return_path']['steps']
print(len(steps))
" 2>/dev/null)
if [ "$STEP_COUNT" -eq 6 ]; then
    green "completion path has $STEP_COUNT steps"
else
    red "completion path has $STEP_COUNT steps (expected 6)"
fi

# ── 9. All authority flags explicitly false ────────────────────────────
echo "[9] All authority_flags are explicitly false"
FLAG_COUNT=$(python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
# Check protocol_sections flags
ps_flags = d['protocol_sections']['section_5_authority_flags']['flags']
# Check top-level gates
gates = d['gates']
# Check invariants
inv = d['invariants']

all_false = True
for k, v in ps_flags.items():
    if v is not False:
        print(f'FLAG_NOT_FALSE: {k}={v}')
        all_false = False

# Also verify key dryrun invariants
assert inv.get('dryrun_only') == True, 'dryrun_only must be true'
assert inv.get('launch_implemented') == False, 'launch_implemented must be false'
assert inv.get('workflow_switch') == False, 'workflow_switch must be false'
assert inv.get('parent_queue_mutation') == False, 'parent_queue_mutation must be false'

# Verify gates
assert gates.get('dryrun_only_no_cli_launch_code') == True, 'dryrun_only gate must be true'
assert gates.get('launch_implemented_false') == True, 'launch_implemented_false must be true'
assert gates.get('all_authority_flags_false') == True, 'all_authority_flags_false must be true'

if all_false:
    print('ALL_FALSE_OK')
" 2>/dev/null)
if echo "$FLAG_COUNT" | grep -q "ALL_FALSE_OK"; then
    green "all authority flags false, dryrun invariants hold"
else
    red "authority flag violation or invariant broken: $FLAG_COUNT"
fi

# ── 10. No subprocess/launch code in eval JSON ─────────────────────────
echo "[10] No subprocess/Popen/exec markers in eval JSON (dryrun-only)"
if python3 -c "
import json
text = open('$EVAL_JSON').read().lower()
forbidden = ['subprocess.popen', 'os.exec', 'os.system(', 'shell=true', 'pty.spawn']
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

# ── 11. Artifact size under 25MB ───────────────────────────────────────
echo "[11] Eval JSON under 25MB"
SIZE=$(stat -c%s "$EVAL_JSON" 2>/dev/null || echo 0)
if [ "$SIZE" -lt 26214400 ]; then
    green "eval JSON size: $SIZE bytes (< 25MB)"
else
    red "eval JSON size: $SIZE bytes (>= 25MB LIMIT)"
fi

# ── 12. Next-wave JSON exists and valid ────────────────────────────────
echo "[12] Next-wave JSON exists and is valid"
if [ -f "$NEXT_WAVE" ]; then
    if python3 -c "import json; json.load(open('$NEXT_WAVE'))" 2>/dev/null; then
        green "next-wave JSON found and valid"
    else
        red "next-wave JSON is INVALID"
    fi
else
    red "next-wave JSON MISSING: $NEXT_WAVE"
fi

# ── 13. Next-wave has at least 2 follow-up task cards ──────────────────
echo "[13] Next-wave has >=2 follow-up task cards"
CARD_COUNT=$(python3 -c "
import json
d = json.load(open('$NEXT_WAVE'))
cards = d.get('next_wave_cards', [])
print(len(cards))
" 2>/dev/null)
if [ "$CARD_COUNT" -ge 2 ]; then
    green "next-wave has $CARD_COUNT follow-up cards (>=2 required)"
else
    red "next-wave has only $CARD_COUNT follow-up cards (<2 required)"
fi

# ── 14. Next-wave task cards have distinct allowed_writes ──────────────
echo "[14] Next-wave cards have non-overlapping allowed_writes (disjoint)"
if python3 -c "
import json
d = json.load(open('$NEXT_WAVE'))
cards = d.get('next_wave_cards', [])
all_paths = []
for c in cards:
    aw = c.get('allowed_writes', [])
    all_paths.extend(aw)
# Check for duplicates
seen = set()
dups = []
for p in all_paths:
    if p in seen:
        dups.append(p)
    seen.add(p)
if dups:
    print(f'OVERLAP: {dups}')
    raise SystemExit(1)
print(f'DISJOINT_OK: {len(all_paths)} paths, 0 overlaps')
" 2>/dev/null; then
    green "next-wave allowed_writes are disjoint across cards"
else
    red "next-wave allowed_writes OVERLAP detected"
fi

# ── 15. taskctl verify ─────────────────────────────────────────────────
echo "[15] taskctl verify"
if python3 "$REPO_ROOT/AITools/taskctl.py" verify 2>/dev/null; then
    green "taskctl verify PASS"
else
    red "taskctl verify FAIL"
fi

# ── Summary ────────────────────────────────────────────────────────────
echo ""
echo "=== RESULTS: $PASS PASS, $FAIL FAIL ==="
if [ "$FAIL" -gt 0 ]; then
    echo "VERDICT: FAIL"
    exit 1
else
    echo "VERDICT: PASS"
    exit 0
fi
